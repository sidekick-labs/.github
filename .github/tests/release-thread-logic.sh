#!/usr/bin/env bash
# Regression tests for the release-thread bash logic in
# reusable-promote-production.yml. These are intentional COPIES of that file's
# `to_mrkdwn` function and `gather_prs` bullet/LLM-line construction — KEEP IN
# SYNC when you change them there. They lock in the two bugs that shipped to
# live releases: PR-body text/footers leaking into bullets (tab-tuple + cut),
# and GitHub-markdown rendering literally in Slack (no normalization).
set -uo pipefail
fail=0
assert_eq() { if [ "$2" = "$3" ]; then echo "ok: $1"; else echo "FAIL: $1"; echo "  exp: [$2]"; echo "  got: [$3]"; fail=1; fi; }
assert_no() { if printf '%s' "$3" | grep -q "$2"; then echo "FAIL: $1 (found '$2')"; fail=1; else echo "ok: $1"; fi; }

# ── to_mrkdwn — GitHub markdown → Slack mrkdwn (SYNC: reusable-promote) ──
to_mrkdwn() {
  sed -E \
    -e 's/\[([^]]+)\]\(([^)]+)\)/<\2|\1>/g' \
    -e 's/(^|[^*])\*\*([^*]+)\*\*/\1*\2*/g' \
    -e 's/(^|[^_])__([^_]+)__/\1*\2*/g' \
    -e 's/~~([^~]+)~~/~\1~/g' \
    -e 's/^[[:space:]]*#{1,6}[[:space:]]+(.*)$/*\1*/' \
    -e 's/^([[:space:]]*)[-*][[:space:]]+/\1• /'
}
assert_eq "heading→bold"    '*Title*'                 "$(printf '## Title' | to_mrkdwn)"
assert_eq "**bold**→*"      'a *b* c'                 "$(printf 'a **b** c' | to_mrkdwn)"
assert_eq "__bold__→*"      'a *b* c'                 "$(printf 'a __b__ c' | to_mrkdwn)"
assert_eq "~~strike~~→~"    '~x~'                     "$(printf '~~x~~' | to_mrkdwn)"
assert_eq "dash list→•"     '• item'                  "$(printf -- '- item' | to_mrkdwn)"
assert_eq "star list→•"     '• item'                  "$(printf '* item' | to_mrkdwn)"
assert_eq "md link→slack"   '<https://x|t>'           "$(printf '[t](https://x)' | to_mrkdwn)"
assert_eq "plain unchanged" 'just text, no markup'    "$(printf 'just text, no markup' | to_mrkdwn)"

# ── gather_prs bullet/LLM parse (SYNC: reusable-promote) ──
# Mock PR JSON whose body has newlines + a tab + a 🤖 footer — the exact shape
# that corrupted the old tab-tuple+cut parse.
J='[{"number":1139,"title":"ci(deploy): text summary","html_url":"https://github.com/x/y/pull/1139","body":"## Changes\n\n- bump pin.\nLine\twith tab.\n\n🤖 Generated with Claude Code"}]'
NUM=$(printf '%s' "$J" | jq -r '.[0].number // empty')
TITLE=$(printf '%s' "$J" | jq -r '.[0].title // ""')
URL=$(printf '%s' "$J" | jq -r '.[0].html_url // ""')
BODY=$(printf '%s' "$J" | jq -r '.[0].body // ""' | sed -E 's/(🤖|:robot_face:).*$//' | tr '\n\t' '  ' | tr -s ' ' | cut -c1-200)
BULLET=$(printf '• <%s|#%s> %s' "$URL" "$NUM" "$TITLE")
LLM=$(printf -- '- #%s %s — %s' "$NUM" "$TITLE" "$BODY")
assert_eq "bullet single line"     1 "$(printf '%s' "$BULLET" | grep -c .)"
assert_eq "bullet clean+linked"    '• <https://github.com/x/y/pull/1139|#1139> ci(deploy): text summary' "$BULLET"
assert_no "no footer in bullet"    'Generated with' "$BULLET"
assert_no "no body leak in bullet" 'Changes'        "$BULLET"
assert_eq "LLM line single line"   1 "$(printf '%s' "$LLM" | grep -c .)"
assert_no "footer stripped (LLM)"  'Generated with' "$LLM"

if [ "$fail" = 0 ]; then echo "ALL PASS"; else echo "TESTS FAILED"; exit 1; fi
