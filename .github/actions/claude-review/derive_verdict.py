#!/usr/bin/env python3
"""Derive the review verdict from the reviewer's own posted comment.

WHY THIS EXISTS (sidekick-labs/sre-brain#568, option 2)
-------------------------------------------------------
The gate used to ask the MODEL to end its comment with a nonce-bound HTML marker
(`<!-- claude-review-verdict: PASS run=... -->`). Three prompt iterations failed:
the instruction provably reached the model and the comment still carried no
marker. On sidekick-glasses-test#133 the tracking comment contained ZERO `<!--`
sequences, consistent with the `update_claude_comment` path stripping HTML
comments, in which case no prompt could ever have fixed it.

What the model DOES reliably produce is severity-labelled findings (`BLOCKING` /
`ADVISORY`), because the prompt has asked for them since the gate was built.
So the verdict is now DERIVED here, deterministically, from the reviewer's comment,
and the nonce-bound marker is AUTHORED BY THE COMPOSITE (see action.yml). Nothing
the model or a human writes into a comment is ever read as a marker.

THE RULE (exact; tests/review-gate-exit-codes.py pins every clause)
-------------------------------------------------------------------
1. Candidate comments are those whose author login == --author (REST spelling,
   `claude[bot]`). Anything else is ignored; a non-reviewer comment that carries a
   verdict marker or imitates this run's lane header is COUNTED AND REPORTED as an
   ignored forgery, never obeyed.
2. A LANE COMMENT is a reviewer comment whose first non-blank line is the
   action's own tracking header — `**Claude finished ...` or
   `**Claude encountered an error ...` — AND which links `/actions/runs/<run-id>`
   for THIS run. The selected lane comment is the LAST such comment (API order =
   creation order) created at or after --since (the review window's start, minus
   SKEW_SECONDS of clock-skew allowance). Never "latest bot comment".
3. Fail closed (UNDETERMINED) when: there is no lane comment for this run; the
   selected lane comment is `Claude encountered an error`; its first line is
   neither known header; or the review carries no substance (nothing but the
   header, the job link, rules, headings and task checkboxes — a zero-turn run).
4. The findings scanned are the selected lane comment PLUS any other reviewer
   comment created inside the window that carries no lane header (a review the
   model posted with `gh pr comment` instead of the tracking comment). Scanning
   more can only add BLOCKING, never remove it.
5. BLOCKING iff at least one BLOCKING-LABELLED finding exists (see
   `blocking_findings`). Otherwise PASS.

   A LABEL, not the word. `No BLOCKING findings`, `No **BLOCKING** findings`,
   `### BLOCKING fix verified — #1 resolved` and `the `ADVISORY` tier` are all
   prose and never count. Recognised label shapes (case-insensitive prefix):
     * `**BLOCKING — title**`, `BLOCKING: title`, `- **BLOCKING** title`,
       `[BLOCKING] title`, `### BLOCKING — title`, `1. **BLOCKING:** title`
     * a bare section heading/label (`### BLOCKING`, `**BLOCKING findings:**`)
       followed by content — counted unless its next non-blank line is an
       explicit "none" or the next heading (an empty section)
     * `BLOCKING: none` / `BLOCKING findings — None.` are explicit empties
     * a heading or bold line ENDING in `— BLOCKING`, `(BLOCKING)`, `[BLOCKING]`
       (uppercase only)
     * a table row with a cell that is exactly `BLOCKING`
   Lines inside CLOSED fenced code blocks are ignored (an unclosed trailing fence
   is not honoured, so it cannot hide a label; a reviewer quoting a diff of this
   very prompt must not red its own PR).

   PASS therefore means "a completed review for THIS run contains zero
   BLOCKING-labelled findings". A completed review with no labels at all is PASS —
   that is exactly what it would have been under the marker contract, where the
   model chose PASS. Prose like "No BLOCKING findings" is never what makes it PASS;
   it simply is not a label, so it cannot make it BLOCKING either.

I/O: reads JSON lines `{id, login, created_at, html_url, body}` on stdin (the shape
the composite's `gh api ... | tojson` emits) and prints ONE JSON object:
`{verdict: PASS|BLOCKING|UNDETERMINED, reason, comment_url, blocking: [..],
ignored_forgeries: n, ignored_reviewer_markers: n}`. Exit 0 whenever it produced
that object; any crash is a non-zero exit, which the composite treats as fail-closed.
"""
import argparse
import datetime as dt
import json
import re
import sys

HEADER_FINISHED = re.compile(r"^\*\*Claude finished\b")
HEADER_ERRORED = re.compile(r"^\*\*Claude encountered an error\b")
LANE_HEADER = re.compile(r"^\*\*Claude (finished|encountered an error)\b")
MARKER = re.compile(r"<!--\s*claude-review-verdict:", re.I)

FENCE = re.compile(r"^\s{0,3}(```|~~~)")
HEADING = re.compile(r"^\s{0,3}(#{1,6})\s")
# Leading markup that may precede a label: quote markers, list bullets, ordered-list
# numbers, heading hashes, emphasis and opening brackets.
LEAD = re.compile(r"^(?:\s|>)*(?:(?:[-*+]|\d+[.)])\s+)?(?:#{1,6}\s+)?(?P<open>[*_\[(]*)\s*")
LABEL = re.compile(r"^BLOCKING(?![A-Za-z])(?P<words>\s+(?:findings?|issues?|defects?))?", re.I)
WRAP_CLOSE = re.compile(r"^(?P<close>[*_\])]*)")
DELIM = re.compile(r"^[:—–-]")
SUFFIX = re.compile(r"(?:[—–:-]\s*\**\s*BLOCKING|\(\s*BLOCKING\s*\)|\[\s*BLOCKING\s*\])\s*[*_]*\s*$")
NONE_TEXT = re.compile(
    r"^(?:none|nothing|n/?a|0|zero|nil|"
    r"no(?:\s+blocking)?\s+(?:findings?|issues?|defects?|problems?|concerns?)"
    r"(?:\s+(?:found|raised|identified))?|none\s+(?:found|raised|identified))[.!]?$",
    re.I,
)
CHECKBOX = re.compile(r"^\s*[-*+]\s+\[[ xX]\]\s")
RULE = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$")


def _clean(text):
    """Strip emphasis/whitespace/trailing punctuation for 'is this an explicit none?'."""
    return re.sub(r"[*_`\s]+", " ", text).strip(" .:;—–-")


def _is_none(text):
    return bool(NONE_TEXT.match(_clean(text)))


def _unfenced_lines(body):
    """Yield (index, line, in_fence) for every line; fenced lines are flagged.

    An UNCLOSED fence does not hide the rest of the comment: if the body ends
    inside a fence, the lines after that last opener are treated as unfenced, so
    a broken fence can never suppress a BLOCKING label (fail-safe direction)."""
    lines = body.split("\n")
    in_fence, opener = False, None
    for i, line in enumerate(lines):
        if FENCE.match(line):
            in_fence = not in_fence
            opener = i if in_fence else None
    in_fence = False
    for i, line in enumerate(lines):
        if opener is not None and i > opener:
            yield i, line, False
            continue
        if FENCE.match(line):
            in_fence = not in_fence
            yield i, line, True
            continue
        yield i, line, in_fence


def _next_content(lines, i):
    for j in range(i + 1, len(lines)):
        if lines[j].strip():
            return lines[j]
    return ""


def _label_kind(line):
    """Classify a line: None (no BLOCKING label), 'finding', 'empty' (explicit none),
    or 'section' (a bare label whose content follows on later lines)."""
    stripped = line.strip()
    if not stripped:
        return None

    # Table row: any cell that is exactly BLOCKING.
    if stripped.startswith("|"):
        cells = [_clean(c) for c in stripped.strip("|").split("|")]
        if not any(c.upper() == "BLOCKING" for c in cells):
            return None
        others = [c for c in cells if c and c.upper() != "BLOCKING"]
        # A count/summary row (`| BLOCKING | 0 |`, `| BLOCKING | none |`) is empty.
        return "empty" if others and all(_is_none(c) for c in others) else "finding"

    m = LEAD.match(line)
    rest = line[m.end():]
    opened = m.group("open")
    lm = LABEL.match(rest)
    if lm:
        after = rest[lm.end():]
        closed = WRAP_CLOSE.match(after).group("close")
        after = after[len(closed):].lstrip()
        wrapped = bool(opened) and bool(closed)
        if "<short title>" in after:
            return None  # the prompt's own template line, quoted back
        if DELIM.match(after):
            tail = after[1:].strip()
            tail_clean = _clean(tail)
            if not tail_clean:
                return "section"
            return "empty" if _is_none(tail) else "finding"
        if not after.strip(" *_"):
            # `### BLOCKING`, `**BLOCKING**`, `BLOCKING findings` alone on a line.
            return "section"
        if wrapped:
            # `**BLOCKING** title`, `[BLOCKING] title`.
            return "empty" if _is_none(after) else "finding"
        # `BLOCKING fix verified — ...`, `Blocking the main thread ...`: prose.
        return None

    # Suffix label on a heading or a bold-led line: `### Title — BLOCKING`.
    if (HEADING.match(line) or stripped.startswith("**")) and SUFFIX.search(stripped):
        return "finding"
    return None


def blocking_findings(body):
    """Return the list of BLOCKING-labelled finding lines in `body`."""
    lines = body.split("\n")
    found = []
    for i, line, fenced in _unfenced_lines(body):
        if fenced:
            continue
        kind = _label_kind(line)
        if kind == "finding":
            found.append(line.strip())
        elif kind == "section":
            nxt = _next_content(lines, i)
            if not nxt.strip() or _is_none(nxt):
                continue
            nh, ch = HEADING.match(nxt), HEADING.match(line)
            if nh and ch and len(nh.group(1)) <= len(ch.group(1)):
                continue  # empty section: straight into the next sibling heading
            if nh and not ch and _label_kind(nxt) is None and "ADVISORY" in nxt.upper():
                continue
            found.append((line.strip() + " " + nxt.strip())[:200])
    return found


def has_substance(body):
    """True when the body holds something beyond the tracking scaffolding."""
    for _, line, fenced in _unfenced_lines(body):
        s = line.strip()
        # Fenced lines (fence markers AND their content) are not review prose:
        # a body that is only a quoted snippet is not a completed review.
        if not s or fenced:
            continue
        if LANE_HEADER.match(s) or RULE.match(s) or CHECKBOX.match(line):
            continue
        if HEADING.match(line):
            continue
        return True
    return False


def _ts(value):
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def _first_line(body):
    for line in body.split("\n"):
        if line.strip():
            return line.strip()
    return ""


# Runner clock (window start) vs GitHub clock (created_at). The tracking comment
# is created well after the window opens (action setup), and an earlier attempt's
# comment is minutes older, so a small allowance costs nothing.
SKEW_SECONDS = 30


def derive(comments, author, run_id, since=None):
    run_link = re.compile(r"/actions/runs/" + re.escape(str(run_id)) + r"(?!\d)")
    since_ts = _ts(since) if since else None
    if since_ts is not None:
        since_ts -= dt.timedelta(seconds=SKEW_SECONDS)
    elif since:
        raise ValueError(f"unparseable --since {since!r}")

    def in_window(c):
        if since_ts is None:
            return True
        t = _ts(c.get("created_at") or "")
        return t is not None and t >= since_ts

    forged = 0
    reviewer = []
    for c in comments:
        body = c.get("body") or ""
        if c.get("login") != author:
            if MARKER.search(body) or (LANE_HEADER.match(_first_line(body)) and run_link.search(body)):
                forged += 1
            continue
        reviewer.append(c)

    reviewer_markers = sum(1 for c in reviewer
                           if in_window(c) and MARKER.search(c.get("body") or ""))
    out = {"verdict": "UNDETERMINED", "reason": "", "comment_url": "", "blocking": [],
           "ignored_forgeries": forged, "ignored_reviewer_markers": reviewer_markers}

    lanes = [c for c in reviewer
             if LANE_HEADER.match(_first_line(c.get("body") or ""))
             and run_link.search(c.get("body") or "") and in_window(c)]
    if not lanes:
        out["reason"] = (f"No review comment from {author} for run {run_id} was found "
                         "(no `Claude finished` / `Claude encountered an error` tracking "
                         "comment linking this run).")
        return out

    lane = lanes[-1]
    body = lane.get("body") or ""
    out["comment_url"] = lane.get("html_url") or ""
    first = _first_line(body)
    if HEADER_ERRORED.match(first):
        out["reason"] = ("The reviewer's comment for this run says `Claude encountered an "
                         "error`: the review did not complete.")
        return out
    if not HEADER_FINISHED.match(first):
        out["reason"] = "The reviewer's comment for this run has an unrecognised header."
        return out

    extra = [c for c in reviewer
             if c is not lane and in_window(c)
             and not LANE_HEADER.match(_first_line(c.get("body") or ""))]
    bodies = [body] + [c.get("body") or "" for c in extra]
    if not any(has_substance(b) for b in bodies):
        out["reason"] = ("The reviewer's comment for this run says `Claude finished` but "
                         "contains no review content (a zero-turn run).")
        return out

    for b in bodies:
        out["blocking"].extend(blocking_findings(b))
    if out["blocking"]:
        out["verdict"] = "BLOCKING"
        out["reason"] = f"{len(out['blocking'])} BLOCKING-labelled finding(s)."
    else:
        out["verdict"] = "PASS"
        out["reason"] = "A completed review with zero BLOCKING-labelled findings."
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--author", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--since", default="")
    a = ap.parse_args()
    comments = []
    for line in sys.stdin:
        line = line.strip()
        if line:
            comments.append(json.loads(line))
    print(json.dumps(derive(comments, a.author, a.run_id, a.since or None)))


if __name__ == "__main__":
    main()
