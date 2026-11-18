#!/usr/bin/env python3
"""A human acknowledgement of the review's BLOCKING findings (EVAL-028).

WHY THIS EXISTS
---------------
`review / Claude Code Review` goes red on any BLOCKING-labelled finding, and until
now the only way back to green was a new commit and a new review. A reviewer that
is wrong had no override, and the estate had no record of how often it was wrong.
This is the override, modelled on fundbright-web's `/findings-ack`
(`.github/scripts/findings-gate.mjs` `parseAck`) but keyed on this lane's own
BLOCKING derivation rather than on a findings count. It is ALSO the outcome
ledger's best ground truth: every ack is a human verdict on a model verdict.

THE GRAMMAR (exact; tests/review-gate-exit-codes.py pins every clause)
---------------------------------------------------------------------
A PR comment whose first `/review-ack` line (outside fenced code) starts the ack,
followed by ONE numbered line per BLOCKING finding, numbered as the check's step
summary numbers them:

    /review-ack
    1. false-positive — the nil branch is unreachable: the caller guards it at x.rb:12
    2. fixed — moved the check into the transaction in 4f2a1c9

* Verdicts: `fixed` or `false-positive` (`false positive` is accepted). Nothing
  else: each is a claim a human can be held to, and the ledger counts them.
* `<n>. <verdict> — <evidence>`; the separator is an em/en dash, a spaced
  hyphen or a colon. Evidence is at least 10 characters.
* Every finding 1..N must be covered exactly once. A missing, repeated,
  out-of-range, unknown-verdict or evidence-free line makes the whole ack
  invalid: a partial ack never turns the check green.

WHO MAY ACKNOWLEDGE, AND WHICH ACK COUNTS
-----------------------------------------
* A human (`user.type == "User"`), never the review author (`claude[bot]`), whose
  repository permission is `admin`, `maintain` or `write`. The permission comes
  from `GET /repos/{repo}/collaborators/{login}/permission`, looked up by the
  composite; a login whose permission could not be read is NOT authorized
  (fail closed). `author_association` is deliberately not used: under a
  workflow's GITHUB_TOKEN it reads NONE for private-org members (measured on
  fundbright-web#1141).
* Only acks created AFTER the reviewer's comment for this run count, and the
  NEWEST authorized one is the one evaluated (a later ack supersedes an earlier
  one, including one that was valid).
* The PR author is allowed. That is a deliberate choice for a team where most PRs
  are opened from the one human account; it is listed as an open question on the
  introducing PR, not settled here.

I/O
---
stdin: JSON lines `{id, login, user_type, created_at, html_url, body}` (the
composite's `gh api ... | tojson`). `--permissions FILE` is a JSON object
`{login: permission}`. Prints ONE JSON object:
`{acknowledged, reason, review_url, ack_url, acker, findings: [{n, title,
verdict, evidence}], ignored_unauthorized}`. Exit 0 whenever it printed that.
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import derive_verdict  # noqa: E402

ACK_HEADER = re.compile(r"^\s*/review-ack\b", re.I)
ITEM = re.compile(r"^(\d+)[.)]\s+(.*)$")
SPLIT = re.compile(r"^(.*?)\s*(?:[—–]|\s-\s|:)\s*(.+)$")
VERDICTS = {"fixed": "fixed", "false-positive": "false-positive",
            "false positive": "false-positive", "falsepositive": "false-positive"}
WRITE_PERMISSIONS = {"admin", "maintain", "write"}
MIN_EVIDENCE = 10


def _strip_fences(body):
    out, in_fence = [], False
    for line in body.split("\n"):
        if derive_verdict.FENCE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append(line)
    return out


def is_ack(body):
    return any(ACK_HEADER.match(l) for l in _strip_fences(body or ""))


def parse_ack(body):
    """Parse a `/review-ack` comment. None when the body is not an ack at all;
    otherwise `{lines: {n: (verdict, evidence)}, errors: [..]}`."""
    lines = _strip_fences((body or "").replace("\r\n", "\n"))
    start = next((i for i, l in enumerate(lines) if ACK_HEADER.match(l)), None)
    if start is None:
        return None
    parsed, errors = {}, []
    for raw in lines[start + 1:]:
        line = raw.strip()
        if not line:
            continue
        item = ITEM.match(line)
        if not item:
            continue  # prose between items is allowed
        n, rest = int(item.group(1)), item.group(2).strip()
        split = SPLIT.match(rest)
        if not split:
            errors.append(f"line {n}: not `<verdict> — <evidence>`")
            continue
        verdict = VERDICTS.get(re.sub(r"[*_`]", "", split.group(1)).strip().lower())
        evidence = split.group(2).strip()
        if verdict is None:
            errors.append(f"line {n}: verdict must be `fixed` or `false-positive`")
            continue
        if len(evidence) < MIN_EVIDENCE:
            errors.append(f"line {n}: evidence is too short (at least {MIN_EVIDENCE} characters)")
            continue
        if n in parsed:
            errors.append(f"line {n}: finding {n} is acknowledged twice")
            continue
        parsed[n] = (verdict, evidence)
    return {"lines": parsed, "errors": errors}


def evaluate(comments, author, run_id, permissions):
    """Decide whether this run's BLOCKING findings are all acknowledged.

    Pure over already-fetched facts, so the outcome ledger replays exactly this
    rule rather than a copy of it."""
    out = {"acknowledged": False, "reason": "", "review_url": "", "ack_url": "",
           "acker": "", "findings": [], "ignored_unauthorized": 0}

    # The reviewer's comment for this run, from ANY attempt: the ack lane only
    # runs on a re-run, so the BLOCKING review is an earlier attempt's.
    first = derive_verdict.derive(comments, author, run_id, since=None)
    lanes = [c for c in comments
             if c.get("login") == author and first["comment_url"]
             and c.get("html_url") == first["comment_url"]]
    if first["verdict"] != "BLOCKING" or not lanes:
        out["reason"] = (f"The review for run {run_id} is not BLOCKING "
                         f"({first['verdict']}): there is nothing to acknowledge.")
        return out
    lane_at = lanes[-1].get("created_at") or ""
    # Re-derive inside that comment's own window, so reviewer comments from other
    # runs cannot add findings the check never showed.
    review = derive_verdict.derive(comments, author, run_id, since=lane_at)
    if review["verdict"] != "BLOCKING":
        out["reason"] = f"The review for run {run_id} re-derived as {review['verdict']}."
        return out
    out["review_url"] = review["comment_url"]
    titles = review["blocking"]
    lane_ts = derive_verdict._ts(lane_at)

    acks = []
    for c in comments:
        if c.get("login") == author or not is_ack(c.get("body")):
            continue
        t = derive_verdict._ts(c.get("created_at") or "")
        if t is None or lane_ts is None or t <= lane_ts:
            continue
        authorized = (c.get("user_type") == "User"
                      and permissions.get(c.get("login") or "") in WRITE_PERMISSIONS)
        if not authorized:
            out["ignored_unauthorized"] += 1
            continue
        acks.append(c)
    if not acks:
        out["reason"] = (f"No `/review-ack` from a user with write access was posted after "
                         f"the review ({len(titles)} BLOCKING finding(s)).")
        return out

    ack = acks[-1]
    out["ack_url"] = ack.get("html_url") or ""
    out["acker"] = ack.get("login") or ""
    parsed = parse_ack(ack.get("body"))
    problems = list(parsed["errors"])
    extra = sorted(n for n in parsed["lines"] if not 1 <= n <= len(titles))
    missing = [n for n in range(1, len(titles) + 1) if n not in parsed["lines"]]
    if extra:
        problems.append(f"no finding numbered {', '.join(map(str, extra))} "
                        f"(the review has {len(titles)})")
    if missing:
        problems.append(f"finding(s) {', '.join(map(str, missing))} not acknowledged")
    if problems:
        out["reason"] = "The newest `/review-ack` is not valid: " + "; ".join(problems) + "."
        return out

    for n, title in enumerate(titles, 1):
        verdict, evidence = parsed["lines"][n]
        out["findings"].append({"n": n, "title": title, "verdict": verdict,
                                "evidence": evidence})
    out["acknowledged"] = True
    out["reason"] = f"All {len(titles)} BLOCKING finding(s) acknowledged by {out['acker']}."
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--author", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--permissions", required=True)
    a = ap.parse_args()
    with open(a.permissions) as fh:
        permissions = json.load(fh)
    comments = [json.loads(l) for l in sys.stdin if l.strip()]
    print(json.dumps(evaluate(comments, a.author, a.run_id, permissions)))


if __name__ == "__main__":
    main()
