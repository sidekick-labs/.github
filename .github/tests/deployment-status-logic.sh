#!/usr/bin/env bash
# Regression tests for how the GitHub Deployment status is decided + how
# do-app-deploy guarantees a phase is always emitted.
#
# Part 1 — gh-deployment-finish STATE decision. This is a COPY of that action's
# decision block — KEEP IN SYNC. It locks in the rule that the deployment status
# is sourced from the DigitalOcean phase, not job.status: a cosmetic post-deploy
# step failing must NOT mark a healthy (ACTIVE) rollout as failed, and a
# non-ACTIVE phase must read failure regardless of job.status.
#
# Part 2 — do-app-deploy's EXIT trap. It locks in that an unhandled crash before
# the normal emit still leaves a non-empty phase in $GITHUB_OUTPUT (the hole that
# let a workflow-side crash mislabel a rollout DO actually completed).
set -uo pipefail
fail=0
assert_eq() { if [ "$2" = "$3" ]; then echo "ok: $1"; else echo "FAIL: $1"; echo "  exp: [$2]"; echo "  got: [$3]"; fail=1; fi; }

# ── Part 1: STATE decision (SYNC: gh-deployment-finish) ──
decide_state() {  # $1=JOB_STATUS  $2=DO_PHASE  → echoes STATE
  local JOB_STATUS="$1" DO_PHASE="$2" STATE
  if [ "$JOB_STATUS" = "cancelled" ]; then
    STATE="error"
  elif [ "$DO_PHASE" = "ACTIVE" ]; then
    STATE="success"
  elif [ -n "$DO_PHASE" ]; then
    STATE="failure"
  else
    if [ "$JOB_STATUS" = "success" ]; then STATE="success"; else STATE="failure"; fi
  fi
  printf '%s' "$STATE"
}

assert_eq "active+job-success → success"      success "$(decide_state success ACTIVE)"
assert_eq "active+job-FAILURE → success"      success "$(decide_state failure ACTIVE)"   # cosmetic step failed; deploy fine
assert_eq "ERROR phase → failure"             failure "$(decide_state failure ERROR)"
assert_eq "TIMEOUT phase → failure"           failure "$(decide_state failure TIMEOUT)"
assert_eq "UNKNOWN phase → failure"           failure "$(decide_state failure UNKNOWN)"
assert_eq "cancelled wins over ACTIVE"        error   "$(decide_state cancelled ACTIVE)"
assert_eq "empty phase + job-failure → fail"  failure "$(decide_state failure '')"        # deploy never started
assert_eq "empty phase + job-success → ok"    success "$(decide_state success '')"

# ── Part 2: do-app-deploy EXIT trap always emits a phase (SYNC: do-app-deploy) ──
# A crash before the normal emit must still leave a phase in $GITHUB_OUTPUT.
OUT=$(mktemp)
bash -euo pipefail -c '
  export GITHUB_OUTPUT="'"$OUT"'"
  APP_ID=app; DEPLOY_ID=dep1
  doctl() { echo "ACTIVE"; }          # DO reports ACTIVE at exit time
  emit() { echo "$1=$2" >> "$GITHUB_OUTPUT"; }
  PHASE_EMITTED=0
  finalize_phase() {
    [ "${PHASE_EMITTED:-0}" = "1" ] && return 0
    local p=""
    if [ -n "${DEPLOY_ID:-}" ] && [ -n "${APP_ID:-}" ]; then
      p=$(doctl apps get-deployment "$APP_ID" "$DEPLOY_ID" --format Phase --no-header 2>/dev/null | tr -d "[:space:]" || true)
    fi
    p="${p:-UNKNOWN}"
    emit phase "$p"
    if [ "$p" = "ACTIVE" ]; then emit outcome "success"; else emit outcome "failure"; fi
    PHASE_EMITTED=1
  }
  trap finalize_phase EXIT
  false   # simulate an unhandled crash under -e, before any emit
  emit phase "NEVER_REACHED"
' 2>/dev/null || true
TRAP_PHASE=$(grep -E '^phase=' "$OUT" | tail -1 | cut -d= -f2)
TRAP_OUTCOME=$(grep -E '^outcome=' "$OUT" | tail -1 | cut -d= -f2)
rm -f "$OUT"
assert_eq "trap emits phase on crash"     ACTIVE  "$TRAP_PHASE"
assert_eq "trap emits outcome on crash"   success "$TRAP_OUTCOME"

if [ "$fail" = 0 ]; then echo "ALL PASS"; else echo "TESTS FAILED"; exit 1; fi
