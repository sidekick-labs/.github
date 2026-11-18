#!/usr/bin/env python3
"""Outcome ledger for the CI agents of one repository (EVAL-010).

WHAT IT IS
----------
Convention rule 8 (rarebit-sre `.claude/conventions/llm-evals.md`): a CI agent that
writes machine-readable output gets a scheduled count of its verdicts against what
actually happened. This script is that count for the sidekick-labs CI agents:

  * `claude-review`               review / Claude Code Review: PASS or BLOCKING;
  * `post-deploy-triage`          post-deploy triage, success path:
                                  `<verdict>/<confidence>`;
  * `post-deploy-failure-triage`  post-deploy triage, failure path:
                                  `<category>/<confidence>`;
  * `autofix`                     the draft `auto-fix/<label>-<sha>` PRs, keyed by
                                  `<label>`.

It reads ONLY the repository it runs in, with that repository's own GITHUB_TOKEN
(contents, pull requests and actions: read), makes NO model call, and writes one
JSON document in the shared shape (schema 1):

    {"schema": 1, "repo": "<org>/<repo>", "generated_at": "<ISO>", "window_days": 30,
     "agents": [{"agent": "...", "verdicts": n, "by_verdict": {...},
                 "outcomes": {"<verdict>": {"<outcome>": n}}, "unknown": n,
                 "notes": "..."}]}

THE ONE RULE THAT MATTERS
-------------------------
A verdict whose outcome cannot be read counts as `unknown`, never as a good outcome.
That covers: a PR still open; a "good-looking" outcome (merged and not reverted,
deployed and nothing followed) younger than `--settle-days`, because the absence of
a revert after an hour is not evidence; a review verdict the check's own conclusion
contradicts; and a triage verdict with no deploy SHA or PR to follow. Bad outcomes
(a revert, a merge over BLOCKING) are definite the moment they happen and are never
held back. For every agent `sum(outcomes) + unknown == verdicts`; the self-test
asserts it.

SELF-TEST (check-positive-controls)
-----------------------------------
`--self-test` runs `compute` over a planted world with KNOWN wrong verdicts (a PASS
whose PR was reverted, a `clean/high` triage whose deploy was reverted, a BLOCKING
merged anyway, a BLOCKING a human marked false-positive, fresh and open verdicts,
a verdict its own check contradicts) and fails unless every planted error lands in
the bucket it belongs in and no unknown is folded into a good one. The composite
runs it before every real run, so a ledger whose classifier has gone blind
publishes nothing.

Usage:
  ledger.py --self-test
  ledger.py compute --repo ORG/REPO [--window-days 30] [--settle-days 7]
                    [--pace 1.0] [--out FILE] [--facts-out FILE] [--facts-in FILE]
  ledger.py publish --repo ORG/REPO --file FILE [--branch outcome-ledger]
"""
import argparse
import datetime as dt
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "claude-review"))
import derive_verdict  # noqa: E402  (the review composite's own verdict parser)
import review_ack  # noqa: E402       (the review composite's own ack rule)

SCHEMA = 1
REVIEW_AUTHOR = "claude[bot]"
REVIEW_WORKFLOW = "claude-code-review.yml"
DEPLOY_WORKFLOWS = ("Deploy to Staging", "Deploy to Production")
TRIAGE_ARTIFACT = "triage-verdict"
SUCCESS_MARKER = "<!-- post-deploy-triage-hint -->"
FAILURE_MARKER = "<!-- post-deploy-fail-triage-hint -->"
AUTOFIX_PREFIX = "auto-fix/"
FOLLOW_DAYS = 14          # how long after a triage verdict a touch of a suspect file counts
MAX_SUSPECT_FILES = 10
FORBIDDEN_BACKOFF = 900   # seconds to back off on a 403/429 (the token is shared)

TRIAGE_VERDICTS = ("clean", "minor concern", "likely problem")
TRIAGE_CATEGORIES = ("code regression", "environment / infra", "flake / transient")
CONFIDENCES = ("low", "medium", "high")
RUN_LINK = re.compile(r"/actions/runs/(\d+)")


# --------------------------------------------------------------------------- time
def parse_ts(value):
    if not value:
        return None
    try:
        t = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def iso(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def settled(at, now, settle_days):
    return at is not None and now - at >= dt.timedelta(days=settle_days)


# --------------------------------------------------------------------------- reverts
REVERTS_SHA = re.compile(r"This reverts commit ([0-9a-f]{7,40})", re.I)
REVERTS_PR = re.compile(r"\bReverts [\w.-]+/[\w.-]+#(\d+)", re.I)
REVERT_TITLE = re.compile(r'^Revert "(.+)"', re.I)
TRAILING_PR = re.compile(r"\s*\(#(\d+)\)\s*$")


class Reverts:
    """Everything on the default branch that undid something, with its time.

    Read from commit messages (`git revert` bodies, GitHub's `Revert "<title>"`
    squash titles, `Reverts org/repo#N` bodies) and from merged revert PRs."""

    def __init__(self, commits, pulls):
        self.shas = {}      # sha (prefix) -> earliest revert time
        self.prs = {}       # PR number -> earliest revert time
        self.titles = {}    # reverted PR title -> earliest revert time
        for c in commits:
            self._scan(c.get("message") or "", parse_ts(c.get("date")))
        for p in pulls:
            if p.get("merged_at") and REVERT_TITLE.match(p.get("title") or ""):
                self._scan((p.get("title") or "") + "\n" + (p.get("body") or ""),
                           parse_ts(p.get("merged_at")))

    @staticmethod
    def _keep(d, key, t):
        if t is not None and (key not in d or t < d[key]):
            d[key] = t

    def _scan(self, message, t):
        for m in REVERTS_SHA.finditer(message):
            self._keep(self.shas, m.group(1).lower(), t)
        for m in REVERTS_PR.finditer(message):
            self._keep(self.prs, int(m.group(1)), t)
        first = message.split("\n", 1)[0].strip()
        m = REVERT_TITLE.match(TRAILING_PR.sub("", first))
        if m:
            inner = m.group(1)
            n = TRAILING_PR.search(inner)
            if n:
                self._keep(self.prs, int(n.group(1)), t)
            self._keep(self.titles, TRAILING_PR.sub("", inner).strip(), t)

    def when(self, pr=None, sha=None, title=None, after=None):
        """Earliest revert of this PR / commit at or after `after`, or None."""
        hits = []
        if pr is not None and pr in self.prs:
            hits.append(self.prs[pr])
        if sha:
            sha = sha.lower()
            hits += [t for k, t in self.shas.items() if sha.startswith(k) or k.startswith(sha)]
        if title and title.strip() in self.titles:
            hits.append(self.titles[title.strip()])
        hits = [t for t in hits if after is None or t >= after]
        return min(hits) if hits else None


# --------------------------------------------------------------------------- tally
class Tally:
    def __init__(self, agent, notes):
        self.agent, self.notes = agent, notes
        self.by_verdict, self.outcomes, self.unknown = {}, {}, 0
        self.unknown_why = {}

    def add(self, verdict, outcome=None, unknown_why=None):
        self.by_verdict[verdict] = self.by_verdict.get(verdict, 0) + 1
        if outcome is None:
            self.unknown += 1
            why = unknown_why or "unreadable"
            self.unknown_why[why] = self.unknown_why.get(why, 0) + 1
            return
        bucket = self.outcomes.setdefault(verdict, {})
        bucket[outcome] = bucket.get(outcome, 0) + 1

    def render(self):
        notes = self.notes
        if self.unknown_why:
            notes += " Unknown here: " + ", ".join(
                f"{n} {why}" for why, n in sorted(self.unknown_why.items())) + "."
        return {"agent": self.agent, "verdicts": sum(self.by_verdict.values()),
                "by_verdict": dict(sorted(self.by_verdict.items())),
                "outcomes": {k: dict(sorted(v.items())) for k, v in sorted(self.outcomes.items())},
                "unknown": self.unknown, "notes": notes}


# --------------------------------------------------------------------------- compute
def compute(facts, now, window_days=30, settle_days=7):
    """The whole classification, pure over already-fetched facts."""
    start = now - dt.timedelta(days=window_days)
    pulls = {p["number"]: p for p in facts["pulls"]}
    reverts = Reverts(facts["commits"], facts["pulls"])
    comments = {int(k): v for k, v in facts["comments"].items()}
    permissions = facts.get("permissions", {})

    def pr_reverted(p, after):
        return reverts.when(pr=p["number"], sha=p.get("merge_commit_sha"),
                            title=p.get("title"), after=after)

    agents = []

    # --- claude-review --------------------------------------------------------
    review = Tally("claude-review",
                   "One verdict per review run (one PR head SHA), re-derived from the "
                   "reviewer's comment with the composite's own parser (derive_verdict.py) and "
                   "cross-checked against the run's conclusion. BLOCKING: acked_false_positive / "
                   "acked_fixed (a valid /review-ack from a user with write access), later_pass "
                   "(the next push reviewed PASS), later_blocking, merged_anyway (merged with this "
                   "BLOCKING as the last verdict and no ack), closed_unmerged. PASS: merged (last "
                   f"verdict, merged, not reverted after {settle_days} days), reverted (merged "
                   "then reverted), superseded (a later push was reviewed again), "
                   "closed_unmerged.")
    # A run's `pull_requests` is EMPTY once its PR is merged and its branch is gone
    # (measured: on sidekick-inference only the dependabot runs carried one). The
    # reviewer's own tracking comment links its run, so that link is the mapping.
    # Runs of other workflows (the interactive @claude lane) are not in this index
    # and are never mapped.
    runs = [dict(r) for r in facts["review_runs"]]
    by_id = {r["run_id"]: r for r in runs}
    for n, cs in comments.items():
        for c in cs:
            if c.get("login") != REVIEW_AUTHOR:
                continue
            for run_id in RUN_LINK.findall(c.get("body") or ""):
                if run_id in by_id and by_id[run_id].get("pr") is None:
                    by_id[run_id]["pr"] = n
    no_verdict = 0
    per_pr = {}
    for run in runs:
        at = parse_ts(run.get("created_at"))
        if at is None or at < start or run.get("pr") not in pulls:
            continue
        if run.get("conclusion") not in ("success", "failure"):
            continue  # cancelled / skipped runs were never acted on
        pr_comments = comments.get(run["pr"], [])
        verdict = derive_verdict.derive(pr_comments, REVIEW_AUTHOR, run["run_id"], since=None)
        if verdict["verdict"] not in ("PASS", "BLOCKING"):
            no_verdict += 1
            continue
        ack = None
        if verdict["verdict"] == "BLOCKING":
            ack = review_ack.evaluate(pr_comments, REVIEW_AUTHOR, run["run_id"], permissions)
        per_pr.setdefault(run["pr"], []).append(
            {"at": at, "verdict": verdict["verdict"], "conclusion": run["conclusion"],
             "ack": ack if ack and ack["acknowledged"] else None})
    for number, runs in per_pr.items():
        p = pulls[number]
        merged_at = parse_ts(p.get("merged_at"))
        runs.sort(key=lambda r: r["at"])
        if merged_at:
            runs = [r for r in runs if r["at"] <= merged_at]
        for i, r in enumerate(runs):
            v, later = r["verdict"], runs[i + 1:]
            expected = "failure" if v == "BLOCKING" and not r["ack"] else "success"
            if r["conclusion"] != expected:
                review.add(v, unknown_why="verdict contradicts the check conclusion")
                continue
            if v == "BLOCKING" and r["ack"]:
                fp = any(f["verdict"] == "false-positive" for f in r["ack"]["findings"])
                review.add(v, "acked_false_positive" if fp else "acked_fixed")
            elif later:
                if v == "BLOCKING":
                    review.add(v, "later_pass" if later[0]["verdict"] == "PASS" else "later_blocking")
                else:
                    review.add(v, "superseded")
            elif p.get("state") == "open":
                review.add(v, unknown_why="PR still open")
            elif not merged_at:
                review.add(v, "closed_unmerged")
            elif v == "BLOCKING":
                review.add(v, "merged_anyway")
            elif pr_reverted(p, merged_at):
                review.add(v, "reverted")
            elif settled(merged_at, now, settle_days):
                review.add(v, "merged")
            else:
                review.add(v, unknown_why=f"merged less than {settle_days} days ago")
    if no_verdict:
        review.notes += (f" {no_verdict} review run(s) in the window produced no PASS/BLOCKING "
                         "verdict (errored, zero-turn or skipped) and are not counted.")
    agents.append(review)

    # --- post-deploy triage (both paths) --------------------------------------
    success = Tally("post-deploy-triage",
                    "Success-path triage, keyed <verdict>/<confidence>. reverted: the deployed "
                    "SHA or its PR was reverted after the verdict; suspect_files_touched_later: a "
                    f"default-branch commit touched a suspect file within {FOLLOW_DAYS} days; "
                    f"no_follow_up: neither, after {settle_days} days. A clean verdict wants "
                    "no_follow_up; a likely problem wants one of the other two.")
    failure = Tally("post-deploy-failure-triage",
                    "Failure-path triage, keyed <category>/<confidence>. reverted and "
                    "suspect_files_touched_later as for the success path; "
                    "same_sha_redeployed_ok: the same SHA later deployed successfully "
                    "(points to flake/infra); no_follow_up: none of these after "
                    f"{settle_days} days.")
    sources = {"artifact": 0, "comment": 0}
    for rec in facts["triage"]:
        at = parse_ts(rec.get("at"))
        if at is None or at < start:
            continue
        tally = failure if rec["path"] == "failure" else success
        key = f"{rec['verdict']}/{rec.get('confidence') or 'unstated'}"
        src = rec.get("source", "comment")
        sources[src] = sources.get(src, 0) + 1
        p = pulls.get(rec.get("pr"))
        sha = rec.get("sha") or (p or {}).get("merge_commit_sha")
        if not sha and not p:
            tally.add(key, unknown_why="no deploy SHA or PR to follow")
            continue
        reverted = reverts.when(pr=rec.get("pr"), sha=sha, title=(p or {}).get("title"), after=at)
        touched = []
        for t in rec.get("later_touches", []):
            when = parse_ts(t.get("date"))
            if when and when > at and when - at <= dt.timedelta(days=FOLLOW_DAYS):
                touched.append(t)
        if reverted:
            tally.add(key, "reverted")
        elif touched:
            tally.add(key, "suspect_files_touched_later")
        elif rec["path"] == "failure" and rec.get("redeployed_ok"):
            tally.add(key, "same_sha_redeployed_ok")
        elif settled(at, now, settle_days):
            tally.add(key, "no_follow_up")
        else:
            tally.add(key, unknown_why=f"verdict less than {settle_days} days old")
    src_note = (f" Verdicts read from {sources['artifact']} triage-verdict artifact(s) and "
                f"{sources['comment']} PR comment(s); a PR comment is overwritten in place, so "
                "comment-sourced history keeps only the latest verdict per PR and path.")
    success.notes += src_note
    failure.notes += src_note
    agents += [success, failure]

    # --- autofix ----------------------------------------------------------------
    autofix = Tally("autofix",
                    "Draft auto-fix PRs opened by post-deploy triage, keyed by branch label. "
                    f"merged (and not reverted after {settle_days} days), reverted, "
                    "closed_unmerged.")
    for p in facts["pulls"]:
        ref = p.get("head_ref") or ""
        created = parse_ts(p.get("created_at"))
        if not ref.startswith(AUTOFIX_PREFIX) or created is None or created < start:
            continue
        label = re.sub(r"-[0-9a-f]{7}$", "", ref[len(AUTOFIX_PREFIX):]) or "unlabelled"
        merged_at = parse_ts(p.get("merged_at"))
        if p.get("state") == "open":
            autofix.add(label, unknown_why="PR still open")
        elif not merged_at:
            autofix.add(label, "closed_unmerged")
        elif pr_reverted(p, merged_at):
            autofix.add(label, "reverted")
        elif settled(merged_at, now, settle_days):
            autofix.add(label, "merged")
        else:
            autofix.add(label, unknown_why=f"merged less than {settle_days} days ago")
    agents.append(autofix)

    return {"schema": SCHEMA, "repo": facts["repo"], "generated_at": iso(now),
            "window_days": window_days, "agents": [a.render() for a in agents]}


# --------------------------------------------------------------------------- GitHub
class Api:
    """Minimal REST client. Paced (the token may be shared), backs off
    FORBIDDEN_BACKOFF seconds on a 403/429, and reads the reset time from the
    response headers rather than calling /rate_limit."""

    def __init__(self, repo, token, pace=1.0, verbose=False):
        self.repo, self.token, self.pace, self.verbose = repo, token, pace, verbose
        self.calls, self._last = 0, 0.0

    def _wait(self):
        delay = self.pace - (time.monotonic() - self._last)
        if delay > 0:
            time.sleep(delay)
        self._last = time.monotonic()

    def _request(self, url, accept="application/vnd.github+json", data=None, method=None):
        for attempt in range(3):
            self._wait()
            self.calls += 1
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Accept", accept)
            req.add_header("X-GitHub-Api-Version", "2022-11-28")
            req.add_header("User-Agent", "sidekick-outcome-ledger")
            # Unredirected: an artifact download redirects to blob storage, which
            # must not receive the GitHub token.
            req.add_unredirected_header("Authorization", f"Bearer {self.token}")
            if data is not None:
                req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    remaining = resp.headers.get("X-RateLimit-Remaining")
                    if remaining is not None and int(remaining) < 100:
                        reset = int(resp.headers.get("X-RateLimit-Reset", "0"))
                        wait = min(max(reset - time.time(), 1), FORBIDDEN_BACKOFF)
                        print(f"rate limit low ({remaining}); sleeping {int(wait)}s", file=sys.stderr)
                        time.sleep(wait)
                    return resp.read(), resp.headers
            except urllib.error.HTTPError as e:
                limited = e.code == 429 or (e.code == 403 and (
                    e.headers.get("Retry-After") or e.headers.get("X-RateLimit-Remaining") == "0"))
                if e.code in (502, 503, 504) and attempt < 2:
                    time.sleep(30)
                    continue
                if limited and attempt < 2:
                    reset = int(e.headers.get("X-RateLimit-Reset") or 0)
                    wait = max(FORBIDDEN_BACKOFF, reset - time.time())
                    print(f"{e.code} rate limited; backing off {int(wait)}s", file=sys.stderr)
                    time.sleep(wait)
                    continue
                raise
        raise RuntimeError("unreachable")

    def url(self, path, params=None):
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        return f"https://api.github.com/{path.lstrip('/')}{q}"

    def get(self, path, params=None):
        if self.verbose:
            print(f"  GET {path} {params or ''}", file=sys.stderr)
        body, _ = self._request(self.url(path, params))
        return json.loads(body)

    def paged(self, path, params=None, key=None, stop=None, max_pages=50):
        """Yield items page by page; `stop(item)` True ends the walk (sorted lists)."""
        params = dict(params or {})
        params.setdefault("per_page", 100)
        for page in range(1, max_pages + 1):
            params["page"] = page
            data = self.get(path, params)
            items = data.get(key, []) if key else data
            for item in items:
                if stop and stop(item):
                    return
                yield item
            if len(items) < params["per_page"]:
                return

    def raw(self, path):
        body, _ = self._request(self.url(path))
        return body

    def send(self, method, path, payload):
        body, _ = self._request(self.url(path), data=json.dumps(payload).encode(), method=method)
        return json.loads(body) if body else {}


SUSPECT_LINE = re.compile(r"^\*\*Suspect lines:\*\*\s*(.*)$", re.M)
PATHISH = re.compile(r"`?([A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+\.[A-Za-z0-9]+)(?::\d+(?:-\d+)?)?`?")


def parse_triage_comment(body, marker):
    """A triage hint comment -> (verdict, confidence, suspect_files, sha) or None."""
    if marker == SUCCESS_MARKER:
        m = re.search(r"^\*\*Verdict:\*\*\s*(clean|minor concern|likely problem)\b", body, re.M | re.I)
    else:
        m = re.search(r"^\*\*Category:\*\*\s*(code regression|environment / infra|flake / transient)\b",
                      body, re.M | re.I)
    if not m:
        return None
    c = re.search(r"^\*\*Confidence:\*\*\s*(low|medium|high)\b", body, re.M | re.I)
    s = SUSPECT_LINE.search(body)
    files = sorted({x.group(1) for x in PATHISH.finditer(s.group(1))}) if s else []
    sha = re.search(r"Failed SHA:\s*`([0-9a-f]{7,40})`", body)
    return (m.group(1).lower(), c.group(1).lower() if c else None, files[:MAX_SUSPECT_FILES],
            sha.group(1) if sha else None)


def validate_triage_json(doc, path):
    """triage.json is model output: accept only the allow-listed values."""
    if not isinstance(doc, dict):
        return None
    key = "category" if path == "failure" else "verdict"
    allowed = TRIAGE_CATEGORIES if path == "failure" else TRIAGE_VERDICTS
    verdict = str(doc.get(key, "")).strip().lower()
    confidence = str(doc.get("confidence", "")).strip().lower()
    if verdict not in allowed:
        return None
    files = doc.get("suspect_files") if isinstance(doc.get("suspect_files"), list) else []
    files = [f for f in files if isinstance(f, str) and re.fullmatch(r"[\w./\-]+", f)
             and ".." not in f.split("/")][:MAX_SUSPECT_FILES]
    return verdict, (confidence if confidence in CONFIDENCES else None), files


def collect_review_runs(api, start, now, numbers):
    """Every review run created in [start, now].

    The runs API returns at most 1000 results per query (sidekick-harness runs
    more than that in 30 days), so the window is walked in date slices and any
    slice that reaches the cap is split until it does not."""
    runs, seen = [], set()

    def walk(lo, hi):
        q = {"created": f"{lo.isoformat()}..{hi.isoformat()}", "per_page": 1}
        total = api.get(f"repos/{api.repo}/actions/workflows/{REVIEW_WORKFLOW}/runs", q)["total_count"]
        if total >= 1000 and hi > lo:
            mid = lo + (hi - lo) // 2
            walk(lo, mid)
            walk(mid + dt.timedelta(days=1), hi)
            return
        if total >= 1000:
            raise RuntimeError(f"more than 1000 review runs on {lo}; cannot list them all")
        if not total:
            return
        for r in api.paged(f"repos/{api.repo}/actions/workflows/{REVIEW_WORKFLOW}/runs",
                           {"created": f"{lo.isoformat()}..{hi.isoformat()}"}, key="workflow_runs"):
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            prs = [x["number"] for x in r.get("pull_requests") or [] if x["number"] in numbers]
            runs.append({"run_id": str(r["id"]), "pr": prs[0] if prs else None,
                         "created_at": r["created_at"], "conclusion": r.get("conclusion"),
                         "head_sha": r["head_sha"]})

    walk(start.date(), now.date())
    return runs


def collect(api, now, window_days=30, log=print):
    """Gather every fact `compute` needs. Read-only."""
    start = now - dt.timedelta(days=window_days)
    repo = api.repo
    default = api.get(f"repos/{repo}")["default_branch"]

    log("pull requests updated in the window…")
    pulls = []
    for p in api.paged(f"repos/{repo}/pulls",
                       {"state": "all", "sort": "updated", "direction": "desc"},
                       stop=lambda p: (parse_ts(p["updated_at"]) or now) < start):
        pulls.append({"number": p["number"], "title": p["title"], "state": p["state"],
                      "created_at": p["created_at"], "merged_at": p.get("merged_at"),
                      "merge_commit_sha": p.get("merge_commit_sha"),
                      "head_ref": p["head"]["ref"], "body": p.get("body") or ""})
    numbers = {p["number"] for p in pulls}

    log("default-branch commits since the window opened…")
    commits = [{"sha": c["sha"], "message": c["commit"]["message"],
                "date": c["commit"]["committer"]["date"]}
               for c in api.paged(f"repos/{repo}/commits", {"sha": default, "since": iso(start)})]

    log("review runs…")
    runs = collect_review_runs(api, start, now, numbers)

    log(f"comments on {len(numbers)} PR(s)…")
    comments = {}
    for n in sorted(numbers):
        comments[n] = [{"id": c["id"], "login": c["user"]["login"], "user_type": c["user"]["type"],
                        "created_at": c["created_at"], "updated_at": c["updated_at"],
                        "html_url": c["html_url"], "body": c.get("body") or ""}
                       for c in api.paged(f"repos/{repo}/issues/{n}/comments")]

    permissions = {}
    for cs in comments.values():
        for c in cs:
            login = c["login"]
            if c["user_type"] == "User" and review_ack.is_ack(c["body"]) and login not in permissions:
                try:
                    permissions[login] = api.get(
                        f"repos/{repo}/collaborators/{login}/permission")["permission"]
                except urllib.error.HTTPError:
                    permissions[login] = None  # unreadable -> not authorized

    log("triage verdicts (artifacts first, then PR comments)…")
    triage, seen = [], set()
    try:
        artifacts = list(api.paged(f"repos/{repo}/actions/artifacts", {"name": TRIAGE_ARTIFACT},
                                   key="artifacts"))
    except urllib.error.HTTPError:
        artifacts = []
    for a in artifacts:
        if a.get("expired") or (parse_ts(a.get("created_at")) or start) < start:
            continue
        try:
            z = zipfile.ZipFile(io.BytesIO(api.raw(f"repos/{repo}/actions/artifacts/{a['id']}/zip")))
            ctx = json.loads(z.read("context.json"))
            doc = json.loads(z.read("triage.json"))
        except (urllib.error.HTTPError, KeyError, ValueError, zipfile.BadZipFile):
            continue
        path = "failure" if ctx.get("path") == "failure" else "success"
        ok = validate_triage_json(doc, path)
        if not ok:
            continue
        pr = int(ctx["pr"]) if str(ctx.get("pr") or "").isdigit() else None
        sha = ctx.get("sha") if re.fullmatch(r"[0-9a-f]{40}", str(ctx.get("sha") or "")) else None
        triage.append({"source": "artifact", "path": path, "pr": pr, "sha": sha,
                       "at": ctx.get("generated_at") or a["created_at"],
                       "verdict": ok[0], "confidence": ok[1], "suspect_files": ok[2]})
        seen.add((pr, path))
    for n, cs in comments.items():
        for marker, path in ((SUCCESS_MARKER, "success"), (FAILURE_MARKER, "failure")):
            if (n, path) in seen:
                continue
            hits = [c for c in cs if marker in c["body"] and c["user_type"] == "Bot"]
            if not hits:
                continue
            c = hits[-1]
            parsed = parse_triage_comment(c["body"], marker)
            if not parsed:
                continue
            triage.append({"source": "comment", "path": path, "pr": n, "sha": parsed[3],
                           "at": c["updated_at"], "verdict": parsed[0],
                           "confidence": parsed[1], "suspect_files": parsed[2]})

    log(f"follow-ups for {len(triage)} triage verdict(s)…")
    for rec in triage:
        at = parse_ts(rec["at"])
        rec["later_touches"] = []
        for f in rec["suspect_files"]:
            for c in api.paged(f"repos/{repo}/commits",
                               {"sha": default, "path": f, "since": iso(at)}, max_pages=2):
                rec["later_touches"].append({"sha": c["sha"],
                                             "date": c["commit"]["committer"]["date"]})
        rec["redeployed_ok"] = None
        if rec["path"] == "failure" and rec.get("sha"):
            ok = api.get(f"repos/{repo}/actions/runs",
                         {"head_sha": rec["sha"], "status": "success", "per_page": 50})
            rec["redeployed_ok"] = any(
                r["name"] in DEPLOY_WORKFLOWS and (parse_ts(r["created_at"]) or at) > at
                for r in ok.get("workflow_runs", []))

    return {"repo": repo, "pulls": pulls, "commits": commits, "review_runs": runs,
            "comments": comments, "permissions": permissions, "triage": triage}


def publish(api, ledger, branch="outcome-ledger"):
    """Commit ledger/<date>.json and ledger/latest.json to `branch` (created as an
    orphan if absent). Git data API, so only those two files are ever written."""
    repo = api.repo
    text = json.dumps(ledger, indent=2) + "\n"
    day = ledger["generated_at"][:10]
    try:
        parent = api.get(f"repos/{repo}/git/ref/heads/{branch}")["object"]["sha"]
        base_tree = api.get(f"repos/{repo}/git/commits/{parent}")["tree"]["sha"]
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        parent, base_tree = None, None
    tree = {"tree": [{"path": f"ledger/{day}.json", "mode": "100644", "type": "blob", "content": text},
                     {"path": "ledger/latest.json", "mode": "100644", "type": "blob", "content": text}]}
    if base_tree:
        tree["base_tree"] = base_tree
    new_tree = api.send("POST", f"repos/{repo}/git/trees", tree)["sha"]
    commit = api.send("POST", f"repos/{repo}/git/commits",
                      {"message": f"ledger: {day}", "tree": new_tree,
                       "parents": [parent] if parent else []})["sha"]
    if parent:
        api.send("PATCH", f"repos/{repo}/git/refs/heads/{branch}", {"sha": commit, "force": False})
    else:
        api.send("POST", f"repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit})
    return commit


# --------------------------------------------------------------------------- self-test
def _lane(run_id, body):
    return (f"**Claude finished @a's task in 1m** —— [View job](https://github.com/o/r/actions/"
            f"runs/{run_id})\n\n---\n{body}")


def planted_world(now):
    """A repository where every verdict's truth is known; `expected` says which
    bucket each planted verdict must land in."""
    def d(days, hours=0):
        return iso(now - dt.timedelta(days=days, hours=hours))

    blocking = "**BLOCKING — nil dereference in Foo#bar**\n\nDetails."
    passing = "No blocking findings.\n\n**ADVISORY — naming**"

    def c(i, login, at, body, user_type="Bot"):
        return {"id": i, "login": login, "user_type": user_type, "created_at": at,
                "updated_at": at, "html_url": f"https://github.com/o/r/pull/x#issuecomment-{i}",
                "body": body}

    def pr(n, title, created, merged, sha, ref, state="closed", body=""):
        return {"number": n, "title": title, "state": state, "created_at": d(created),
                "merged_at": d(merged) if merged is not None else None,
                "merge_commit_sha": sha, "head_ref": ref, "body": body}

    pulls = [
        pr(1, "Add Foo", 21, 20, "a" * 40, "feat/foo"),        # PASS, merged, then REVERTED
        pr(2, "Add Bar", 19, 18, "b" * 40, "feat/bar"),        # BLOCKING merged anyway
        pr(3, "Add Baz", 17, 16, "c" * 40, "feat/baz"),        # BLOCKING acked false-positive
        pr(4, "Add Qux", 3, 2, "d" * 40, "feat/qux"),          # PASS merged 2 days ago: unknown
        pr(5, "WIP", 5, None, None, "feat/wip", state="open"),  # PASS on an open PR: unknown
        pr(6, "Add Quux", 14, 12, "e" * 40, "feat/quux"),      # BLOCKING -> PASS -> contradicted
        pr(7, 'Revert "Add Foo (#1)"', 10, 10, "f" * 40, "revert-1-feat/foo",
           body="Reverts o/r#1"),
        pr(8, "fix: x", 15, 14, "1" * 40, "auto-fix/post-deploy-1234567"),
        pr(9, "fix: y", 15, None, None, "auto-fix/deploy-failure-2345678"),
        pr(10, "fix: z", 1, None, None, "auto-fix/post-deploy-3456789", state="open"),
    ]
    commits = [{"sha": "f" * 40, "date": d(10),
                "message": 'Revert "Add Foo (#1)" (#7)\n\nThis reverts commit ' + "a" * 40 + "."}]
    runs = [
        {"run_id": "101", "pr": 1, "created_at": d(21), "conclusion": "success"},
        # pr None: the runs API dropped the PR; the reviewer comment links it.
        {"run_id": "201", "pr": None, "created_at": d(19), "conclusion": "failure"},
        {"run_id": "301", "pr": 3, "created_at": d(17), "conclusion": "success"},  # acked re-run
        {"run_id": "401", "pr": 4, "created_at": d(3), "conclusion": "success"},
        {"run_id": "501", "pr": 5, "created_at": d(5), "conclusion": "success"},
        {"run_id": "601", "pr": 6, "created_at": d(14), "conclusion": "failure"},
        {"run_id": "602", "pr": 6, "created_at": d(13), "conclusion": "success"},
        # A PASS whose run nevertheless FAILED: the ledger cannot say which is true.
        {"run_id": "603", "pr": 6, "created_at": d(12, 1), "conclusion": "failure"},
        # An errored review is not a verdict at all.
        {"run_id": "604", "pr": 6, "created_at": d(12, 2), "conclusion": "failure"},
    ]
    comments = {
        1: [c(1, REVIEW_AUTHOR, d(21), _lane("101", passing))],
        2: [c(2, REVIEW_AUTHOR, d(19), _lane("201", blocking)),
            # An ack from a read-only user does not count.
            c(3, "drive-by", d(18, 12), "/review-ack\n1. false-positive — it is fine, trust me",
              "User")],
        3: [c(4, REVIEW_AUTHOR, d(17), _lane("301", blocking)),
            c(5, "writer", d(16, 12),
              "/review-ack\n1. false-positive — Foo#bar is only called after the nil guard in x.rb:12",
              "User")],
        4: [c(6, REVIEW_AUTHOR, d(3), _lane("401", passing)),
            # The interactive @claude lane: same author, a run that is not a review run.
            c(12, REVIEW_AUTHOR, d(2, 12), _lane("999", blocking))],
        5: [c(7, REVIEW_AUTHOR, d(5), _lane("501", passing))],
        6: [c(8, REVIEW_AUTHOR, d(14), _lane("601", blocking)),
            c(9, REVIEW_AUTHOR, d(13), _lane("602", passing)),
            c(10, REVIEW_AUTHOR, d(12, 1), _lane("603", passing)),
            c(11, REVIEW_AUTHOR, d(12, 2),
              "**Claude encountered an error after 3s** —— [View job](https://github.com/o/r/"
              "actions/runs/604)")],
    }
    triage = [
        # THE planted wrong triage: clean/high on a deploy that was then reverted.
        {"source": "artifact", "path": "success", "pr": 1, "sha": "a" * 40, "at": d(19),
         "verdict": "clean", "confidence": "high", "suspect_files": [], "later_touches": []},
        # likely problem/high whose suspect file was fixed 3 days later.
        {"source": "artifact", "path": "success", "pr": 2, "sha": "b" * 40, "at": d(17),
         "verdict": "likely problem", "confidence": "high", "suspect_files": ["app/bar.rb"],
         "later_touches": [{"sha": "9" * 40, "date": d(14)}]},
        # clean/medium, nothing followed, old enough: a confirmed clean.
        {"source": "comment", "path": "success", "pr": 3, "sha": None, "at": d(15),
         "verdict": "clean", "confidence": "medium", "suspect_files": [], "later_touches": []},
        # clean/low one day old: UNKNOWN, not a clean.
        {"source": "comment", "path": "success", "pr": 4, "sha": None, "at": d(1),
         "verdict": "clean", "confidence": "low", "suspect_files": [], "later_touches": []},
        # failure path: flake/transient, same SHA redeployed fine.
        {"source": "artifact", "path": "failure", "pr": 6, "sha": "e" * 40, "at": d(11),
         "verdict": "flake / transient", "confidence": "medium", "suspect_files": [],
         "later_touches": [], "redeployed_ok": True},
        # failure path with nothing to follow: UNKNOWN.
        {"source": "artifact", "path": "failure", "pr": None, "sha": None, "at": d(9),
         "verdict": "code regression", "confidence": "low", "suspect_files": [],
         "later_touches": [], "redeployed_ok": None},
    ]
    facts = {"repo": "o/r", "pulls": pulls, "commits": commits, "review_runs": runs,
             "comments": comments, "permissions": {"writer": "write", "drive-by": "read"},
             "triage": triage}
    expected = {
        "claude-review": {"PASS": {"reverted": 1, "merged": 0, "superseded": 1},
                          "BLOCKING": {"merged_anyway": 1, "acked_false_positive": 1,
                                       "later_pass": 1},
                          "unknown": 3, "verdicts": 8},  # #4 fresh, #5 open, run 603 contradicted
        "post-deploy-triage": {"clean/high": {"reverted": 1, "no_follow_up": 0},
                               "likely problem/high": {"suspect_files_touched_later": 1},
                               "clean/medium": {"no_follow_up": 1},
                               "clean/low": {"no_follow_up": 0},
                               "unknown": 1, "verdicts": 4},
        "post-deploy-failure-triage": {"flake / transient/medium": {"same_sha_redeployed_ok": 1},
                                       "unknown": 1, "verdicts": 2},
        "autofix": {"post-deploy": {"merged": 1}, "deploy-failure": {"closed_unmerged": 1},
                    "unknown": 1, "verdicts": 3},
    }
    return facts, expected


def self_test(compute_fn=None, now=None):
    """Returns a list of failures (empty = pass)."""
    compute_fn = compute_fn or compute
    now = now or dt.datetime(2026, 11, 18, 12, 0, tzinfo=dt.timezone.utc)
    facts, expected = planted_world(now)
    ledger = compute_fn(facts, now)
    failures = []
    agents = {a["agent"]: a for a in ledger.get("agents", [])}
    if ledger.get("schema") != SCHEMA:
        failures.append("schema is not 1")
    for name, want in expected.items():
        a = agents.get(name)
        if not a:
            failures.append(f"{name}: agent missing")
            continue
        total = sum(sum(v.values()) for v in a["outcomes"].values()) + a["unknown"]
        if total != a["verdicts"] or sum(a["by_verdict"].values()) != a["verdicts"]:
            failures.append(f"{name}: outcomes + unknown ({total}) != verdicts ({a['verdicts']})")
        for key in ("unknown", "verdicts"):
            if a[key] != want[key]:
                failures.append(f"{name}: {key} is {a[key]}, planted {want[key]}")
        for verdict, outs in want.items():
            if verdict in ("unknown", "verdicts"):
                continue
            for outcome, n in outs.items():
                got = a["outcomes"].get(verdict, {}).get(outcome, 0)
                if got != n:
                    failures.append(f"{name}: {verdict} -> {outcome} is {got}, planted {n}")
    return failures


# --------------------------------------------------------------------------- CLI
def summary_markdown(ledger):
    lines = [f"### Outcome ledger — `{ledger['repo']}` ({ledger['window_days']} days to "
             f"{ledger['generated_at']})", "",
             "| agent | verdict | n | outcomes |", "|---|---|---|---|"]
    for a in ledger["agents"]:
        for v, n in a["by_verdict"].items():
            outs = ", ".join(f"{k} {x}" for k, x in a["outcomes"].get(v, {}).items())
            lines.append(f"| {a['agent']} | {v} | {n} | {outs} |")
        lines.append(f"| {a['agent']} | **unknown** | {a['unknown']} | of {a['verdicts']} |")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description="Outcome ledger for this repo's CI agents.")
    ap.add_argument("--self-test", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("compute")
    c.add_argument("--repo", required=True)
    c.add_argument("--window-days", type=int, default=30)
    c.add_argument("--settle-days", type=int, default=7)
    c.add_argument("--pace", type=float, default=1.0, help="seconds between API calls")
    c.add_argument("--out")
    c.add_argument("--facts-out", help="also write the gathered facts")
    c.add_argument("--facts-in", help="recompute from previously gathered facts (no API)")
    c.add_argument("--now", help="ISO time to compute as of (with --facts-in)")
    c.add_argument("--verbose", action="store_true")
    p = sub.add_parser("publish")
    p.add_argument("--repo", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--branch", default="outcome-ledger")
    a = ap.parse_args()

    if a.self_test:
        failures = self_test()
        for f in failures:
            print(f"SELF-TEST FAIL {f}")
        if failures:
            print("self-test: FAILED")
            sys.exit(1)
        print("self-test: ok (every planted wrong verdict landed in its bad bucket; "
              "no unknown was folded into a good outcome)")
        if not a.cmd:
            return
    if a.cmd is None:
        ap.error("a command (compute / publish) or --self-test is required")

    if a.cmd == "compute":
        if a.facts_in:
            with open(a.facts_in) as fh:
                facts = json.load(fh)
            now = parse_ts(a.now) if a.now else dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
            calls = 0
        else:
            token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
            if not token:
                sys.exit("GH_TOKEN is not set")
            api = Api(a.repo, token, pace=a.pace, verbose=a.verbose)
            now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
            facts = collect(api, now, a.window_days, log=lambda m: print(m, file=sys.stderr))
            calls = api.calls
            if a.facts_out:
                with open(a.facts_out, "w") as fh:
                    json.dump(facts, fh, indent=1)
        ledger = compute(facts, now, a.window_days, a.settle_days)
        text = json.dumps(ledger, indent=2) + "\n"
        if a.out:
            with open(a.out, "w") as fh:
                fh.write(text)
        else:
            sys.stdout.write(text)
        print(f"{calls} API call(s)", file=sys.stderr)
        step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if step_summary:
            with open(step_summary, "a") as fh:
                fh.write(summary_markdown(ledger))
    elif a.cmd == "publish":
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token:
            sys.exit("GH_TOKEN is not set")
        with open(a.file) as fh:
            ledger = json.load(fh)
        if ledger.get("schema") != SCHEMA or ledger.get("repo") != a.repo:
            sys.exit("refusing to publish: schema or repo mismatch")
        api = Api(a.repo, token, pace=0.5)
        print(f"published {publish(api, ledger, a.branch)} to {a.branch}")


if __name__ == "__main__":
    main()
