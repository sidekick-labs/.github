#!/usr/bin/env python3
"""
Open/refresh `ci-audit` issues for the actions-audit's findings (the SENSOR sink).

ALL actionable findings (concurrency, flakes, slow-failing workflows, burners) land
as `ci-audit` issues in a CENTRAL GitHub tracker — sidekick-labs/sre-brain, the
infra/CI-health brain that already owns health sweeps + weekly retros. NOT the
audited source repos, and NOT Linear (retired). One `ci-audit` issue per finding.
(`.github` was the first pick but has Issues disabled — sre-brain is the home.)

These issues are the INPUT to the actuator — sre-brain's ci-fix-sweep + ci-fix-
engine (sre-brain#109). So each body carries the TARGET REPO + the offending
workflow file in a MACHINE-PARSEABLE form the engine reads deterministically:

    repo: <owner/repo>
    workflow: .github/workflows/<file>.yml
    category: <concurrency|flakes|failed-minutes|burners>

(rendered inside a ```` ```ci-target ... ``` ```` fenced block AND as a stable
`<!-- ci-target repo=<owner/repo> workflow=<path> category=<cat> -->` HTML comment,
so the engine can parse either the fence or the comment — robust to body edits).
`workflow` is `(none)` only when the audit couldn't locate the file; the engine
then locates it itself.

Idempotency: every issue body also ends with a hidden marker
`<!-- actions-audit:<rec_id> -->`. Before creating, we search OPEN `ci-audit`
issues for that marker; if one exists we SKIP (leave it — don't stack duplicates),
and only create when absent. rec_id is the stable hash(repo, workflow_file,
category) that recommendations.py stamps on every finding.

RECONCILE (auto-close cleared findings): create-only used to mean every cleared
finding lingered as an open issue until a human closed it — the sensor had NO
close half, so the tracker only grew. After the create pass we now RECONCILE: list
every open `ci-audit` issue carrying our marker and CLOSE the ones whose rec_id is
NOT in this run's `judgment` set — i.e. the finding cleared (or dropped below the
issue-worthiness threshold and moved to the digest). Two safety rails so a partial
or errored scan never mass-closes live issues:
  1. Marker-scoped: only issues bearing `<!-- actions-audit:<rec_id> -->` are
     touched — human-filed `ci-audit` issues (no marker) are never closed.
  2. Scanned-scoped: an issue is only closeable if its target repo is in this
     run's `scanned_repos`. A repo that wasn't scanned (API error, no active runs)
     keeps its issues — absence of a finding for an unscanned repo proves nothing.
If `scanned_repos` is empty (collect produced nothing), reconcile is skipped
entirely. `--no-close` disables the reconcile pass for manual/validation runs.

Reads recommendations.json (the partition output): `judgment[]` (issues to open)
and `scanned_repos[]` (the close-scope). Auth: GH_TOKEN env (read by `gh`) — the
minted sidekick-release-bot token, which has issues:write org-wide. Diagnostics +
created/skipped/closed counts to stderr.

The workflow gates this to non-dry-run, exactly as the old Linear step was gated.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

# Central tracker = sre-brain (infra/CI-health). Per-repo routing is the
# alternative: file in the audited repo (sidekick-release-bot has org-wide
# issues:write) — switchable via the AUDIT_TRACKER_REPO env or this default.
TRACKER_REPO = os.environ.get("AUDIT_TRACKER_REPO", "sidekick-labs/sre-brain")
LABEL = "ci-audit"
# Owner that the audited repo NAMES belong to (audit.py emits owner-stripped names,
# e.g. "sidekick-web"). The engine needs a fully-qualified owner/name, so we
# re-qualify here. Mirrors recommendations.py's AUDIT_ORG default.
ORG = os.environ.get("AUDIT_ORG", "sidekick-labs")


def full_repo(repo: str) -> str:
    """Re-qualify an owner-stripped repo name to owner/name for the engine."""
    return repo if "/" in repo else f"{ORG}/{repo}"


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def ensure_label() -> None:
    """Idempotently ensure the ci-audit label exists (--force = create-or-update)."""
    r = run([
        "gh", "label", "create", LABEL, "--repo", TRACKER_REPO,
        "--color", "BFD4F2",
        "--description", "Weekly actions-audit finding; input to the ci-fix engine",
        "--force",
    ])
    if r.returncode != 0:
        print(f"[open_issues] WARNING: could not ensure label {LABEL}: "
              f"{r.stderr.strip()}", file=sys.stderr)


def marker(rec_id: str) -> str:
    return f"<!-- actions-audit:{rec_id} -->"


def existing_open_issue(rec_id: str) -> str | None:
    """Return the URL of an OPEN ci-audit issue carrying this rec_id marker, else None."""
    r = run([
        "gh", "issue", "list", "--repo", TRACKER_REPO,
        "--state", "open", "--label", LABEL,
        "--search", f"{rec_id} in:body",
        "--json", "url,body",
    ])
    if r.returncode != 0:
        print(f"[open_issues] WARNING: issue search failed for {rec_id}: "
              f"{r.stderr.strip()}", file=sys.stderr)
        return None
    try:
        issues = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return None
    # `--search` is full-text and fuzzy; confirm the exact hidden marker is present.
    needle = marker(rec_id)
    for issue in issues:
        if needle in (issue.get("body") or ""):
            return issue.get("url")
    return None


def metrics_block(j: dict) -> str:
    """Compact, readable metrics list from the finding's `metrics` dict."""
    metrics = j.get("metrics", {})
    lines = []
    for k, v in metrics.items():
        if isinstance(v, float):
            v = f"{v:.2f}".rstrip("0").rstrip(".")
        lines.append(f"- **{k}:** {v}")
    jobs = j.get("top_failing_jobs", [])
    if jobs:
        lines.append(f"- **top_failing_jobs:** {', '.join(str(x) for x in jobs)}")
    return "\n".join(lines) if lines else "_(no metrics)_"


def issue_title(j: dict) -> str:
    return f"[ci-audit] {j['repo']}/{j['workflow_name']}: {j['category']}"


def target_block(repo_full: str, wf_path: str, category: str) -> str:
    """Machine-parseable target descriptor the ci-fix engine reads.

    Rendered TWICE for robustness: a fenced ```ci-target``` block (human-visible,
    grep-friendly) and a stable HTML comment (survives body re-renders). The engine
    parses either; both name the SAME owner/repo + workflow path + category."""
    wf = wf_path or "(none)"
    fence = (
        "```ci-target\n"
        f"repo: {repo_full}\n"
        f"workflow: {wf}\n"
        f"category: {category}\n"
        "```"
    )
    comment = f"<!-- ci-target repo={repo_full} workflow={wf} category={category} -->"
    return f"{fence}\n\n{comment}"


def issue_body(j: dict, run_url: str) -> str:
    repo_full = full_repo(j["repo"])
    wf_file = j.get("workflow_file", "") or "(none)"
    suggested = j.get("suggested_fix", "")
    suggested_md = (
        f"**Suggested fix (the engine re-verifies + yaml-validates before applying)**\n"
        f"```yaml\n{suggested}\n```\n\n"
        if suggested else ""
    )
    return (
        f"{j.get('note', '').strip()}\n\n"
        f"**Target** — the ci-fix engine fixes this workflow:\n\n"
        f"{target_block(repo_full, j.get('workflow_file', ''), j['category'])}\n\n"
        f"**Source workflow:** `{repo_full}` · `{wf_file}`\n"
        f"**Category:** {j['category']}\n\n"
        f"{suggested_md}"
        f"**Metrics**\n{metrics_block(j)}\n\n"
        f"_Filed by the weekly actions-audit ({run_url}) as the SENSOR half of the "
        f"CI self-healing loop (sre-brain#109). The `ci-fix` engine reads the "
        f"`ci-target` above, proposes a guarded workflow fix, and opens a "
        f"ready-for-review PR that links back here. The audit's weekly reconcile "
        f"pass auto-closes this issue once the finding clears._\n\n"
        f"{marker(j['rec_id'])}"
    )


def create_issue(j: dict, run_url: str) -> str | None:
    r = run([
        "gh", "issue", "create", "--repo", TRACKER_REPO,
        "--title", issue_title(j),
        "--body", issue_body(j, run_url),
        "--label", LABEL,
    ])
    if r.returncode != 0:
        print(f"[open_issues] WARNING: issue create failed for "
              f"{issue_title(j)}: {r.stderr.strip()}", file=sys.stderr)
        return None
    return (r.stdout or "").strip()


# ---- Reconcile (auto-close cleared findings) ---------------------------------

_MARKER_RE = re.compile(r"<!-- actions-audit:([0-9a-f]{6,}) -->")
_CITARGET_REPO_RE = re.compile(r"<!-- ci-target repo=(\S+) ")


def strip_owner(repo: str) -> str:
    """`sidekick-labs/sidekick-web` -> `sidekick-web` (match audit's stripped names)."""
    return repo.split("/")[-1]


def parse_marker(body: str) -> str | None:
    m = _MARKER_RE.search(body or "")
    return m.group(1) if m else None


def parse_target_repo(body: str) -> str | None:
    """Owner-stripped target repo from the issue's `ci-target` HTML comment."""
    m = _CITARGET_REPO_RE.search(body or "")
    return strip_owner(m.group(1)) if m else None


def list_open_ci_audit_issues() -> list[dict]:
    """Every open ci-audit issue with number+body (for the reconcile pass)."""
    r = run([
        "gh", "issue", "list", "--repo", TRACKER_REPO,
        "--state", "open", "--label", LABEL, "--limit", "500",
        "--json", "number,body",
    ])
    if r.returncode != 0:
        print(f"[reconcile] WARNING: could not list open issues: "
              f"{r.stderr.strip()}", file=sys.stderr)
        return []
    try:
        return json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return []


def close_issue(number: int, comment: str) -> bool:
    r = run([
        "gh", "issue", "close", str(number), "--repo", TRACKER_REPO,
        "--comment", comment,
    ])
    if r.returncode != 0:
        print(f"[reconcile] WARNING: could not close #{number}: "
              f"{r.stderr.strip()}", file=sys.stderr)
        return False
    return True


def reconcile(live_rec_ids: set[str], scanned_repos: set[str], run_url: str) -> int:
    """Close marker-bearing ci-audit issues whose finding cleared this run.

    SAFE-BY-CONSTRUCTION: only closes an issue when (a) it carries our
    `actions-audit:<rec_id>` marker, (b) that rec_id is NOT in this run's live
    judgment set, AND (c) its target repo was actually scanned this run. Unscanned
    repos and human-filed (markerless) issues are never touched. Caller must ensure
    `scanned_repos` is non-empty (an empty scan can't conclude anything cleared)."""
    closed = 0
    for issue in list_open_ci_audit_issues():
        body = issue.get("body") or ""
        rid = parse_marker(body)
        if not rid:
            continue  # human-filed ci-audit issue (no marker) — never auto-close
        if rid in live_rec_ids:
            continue  # finding still present this run
        repo = parse_target_repo(body)
        if repo is None or repo not in scanned_repos:
            continue  # repo not scanned this run — absence proves nothing
        num = issue["number"]
        comment = (
            f"Auto-closed by the weekly actions-audit reconcile pass: this finding "
            f"(`{rid}`) is no longer present for `{repo}` as of {run_url} — it "
            f"cleared, or dropped below the issue-worthiness threshold and moved to "
            f"the digest. Reopen (or it will re-file) if it recurs."
        )
        if close_issue(num, comment):
            print(f"[reconcile] closed #{num} (rec_id {rid}, repo {repo})",
                  file=sys.stderr)
            closed += 1
    return closed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("recommendations", help="Path to recommendations.json (partition output).")
    ap.add_argument("--run-url", default="", help="Audit run URL, embedded in each issue body.")
    ap.add_argument("--no-close", action="store_true",
                    help="Skip the reconcile/close pass (manual/validation runs).")
    args = ap.parse_args()

    with open(args.recommendations) as f:
        data = json.load(f)

    judgment = data.get("judgment", [])
    scanned_repos = set(data.get("scanned_repos", []))

    ensure_label()

    # ---- Create pass: open an issue per NEW issue-worthy finding (idempotent).
    created = skipped = 0
    for j in judgment:
        rid = j.get("rec_id")
        if not rid:
            print(f"[open_issues] WARNING: judgment item missing rec_id, skipping: "
                  f"{j.get('repo')}/{j.get('workflow_name')}", file=sys.stderr)
            continue
        existing = existing_open_issue(rid)
        if existing:
            print(f"[open_issues] open issue exists for {rid} ({existing}) — "
                  f"skipping (idempotent).", file=sys.stderr)
            skipped += 1
            continue
        url = create_issue(j, args.run_url)
        if url:
            print(f"[open_issues] created {url} for {rid}", file=sys.stderr)
            created += 1

    # ---- Reconcile pass: close issues whose finding cleared this run.
    closed = 0
    if args.no_close:
        print("[reconcile] --no-close: skipping reconcile pass.", file=sys.stderr)
    elif not scanned_repos:
        print("[reconcile] scanned_repos is empty — skipping reconcile (an empty "
              "scan can't conclude any finding cleared).", file=sys.stderr)
    else:
        live_rec_ids = {j["rec_id"] for j in judgment if j.get("rec_id")}
        closed = reconcile(live_rec_ids, scanned_repos, args.run_url)

    print(f"[open_issues] created={created} skipped={skipped} closed={closed} "
          f"total_judgment={len(judgment)} scanned_repos={len(scanned_repos)}",
          file=sys.stderr)
    # Emit the created count on stdout for the workflow to capture.
    print(created)
    return 0


if __name__ == "__main__":
    sys.exit(main())
