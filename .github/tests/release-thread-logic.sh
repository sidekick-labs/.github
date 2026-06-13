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

# ── do-app-deploy slack_status under bash -e (SYNC: do-app-deploy) ──
# GitHub runs composite `shell: bash` steps as `bash -e …`; the step's own
# `set -uo pipefail` does NOT clear that inherited -e. slack_status is called
# bare (not in a condition/||), so if it ever returns non-zero the deploy step
# dies silently — exactly the bug that aborted a live prod rollout (the DO
# rollout reached ACTIVE in the background but the workflow reported failure).
# This runs the helper under -e on BOTH the post (kickoff) and update paths and
# asserts the script reaches the end. KEEP IN SYNC with do-app-deploy.
SS_RC=$(bash -euo pipefail -c '
  SLACK_BOT_TOKEN=x; SLACK_CHANNEL=C; SLACK_THREAD_TS=1.2
  curl() { echo "{\"ok\":true,\"ts\":\"9.9\"}"; }
  STATUS_TS=""
  slack_status() {
    if [ -z "$SLACK_BOT_TOKEN" ] || [ -z "$SLACK_CHANNEL" ] || [ -z "$SLACK_THREAD_TS" ]; then return 0; fi
    local url payload resp
    if [ -z "$STATUS_TS" ]; then url=post; payload=$(jq -n --arg t "$1" "{text:\$t}")
    else url=update; payload=$(jq -n --arg t "$1" "{text:\$t}"); fi
    resp=$(curl -sS "$url" --data "$payload" 2>/dev/null) || { echo "::warning::post failed"; return 0; }
    if printf "%s" "$resp" | jq -e ".ok == true" >/dev/null 2>&1; then
      if [ -z "$STATUS_TS" ]; then STATUS_TS=$(printf "%s" "$resp" | jq -r ".ts // empty"); fi
    else echo "::warning::rejected"; fi
    return 0
  }
  slack_status kickoff   # post path
  slack_status update    # update path — STATUS_TS now set; the regression point
  echo REACHED_END
' 2>/dev/null; echo "rc=$?")
assert_eq "slack_status survives -e"  'REACHED_END
rc=0' "$SS_RC"

if [ "$fail" = 0 ]; then echo "ALL PASS"; else echo "TESTS FAILED"; exit 1; fi
