#!/usr/bin/env bash
# Regression tests for do-app-deploy's Phase-3 outcome-based reconciliation —
# the fix from .github PR #74 / commit 77accbf for the SUPERSEDED-but-actually-
# ACTIVE spurious failure (observed on a sidekick-inference prod release,
# 2026-06-16: the tracked deployment went SUPERSEDED while a concurrent deploy
# carried the SAME SHA to ACTIVE, and the run failed despite a healthy prod).
# See core-platform-brain#122.
#
# The per-poll reconcile DECISION is the single source of truth in
#   .github/actions/do-app-deploy/reconcile.sh
# which the live action sources via "$GITHUB_ACTION_PATH/reconcile.sh" and this
# test sources directly — so there is NO copied logic that can drift.
# reconcile_eval is pure (snapshot + SHA → verdict); only the doctl/sleep/emit/
# Slack I/O lives in the action's loop. That loop's EXIT structure (bounded
# window; deadline checked only on the WAIT path, after the reconcile + give-up
# checks) is the one thing re-created here — as reconcile_loop with an injected
# clock — so the timeout branch is testable offline. reconcile_loop deliberately
# MIRRORS action.yml's Phase-3 loop; the decision it calls is the shared
# reconcile_eval, so only this thin scaffold is test-local.
#
# Cases (per core-platform-brain#122): success / different-SHA-superseder /
# in-flight-superseder / timeout, plus JSON-guard + partial-rollout edges.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../actions/do-app-deploy/reconcile.sh
source "$HERE/../actions/do-app-deploy/reconcile.sh"

fail=0
assert_eq() { if [ "$2" = "$3" ]; then echo "ok: $1"; else echo "FAIL: $1"; echo "  exp: [$2]"; echo "  got: [$3]"; fail=1; fi; }

# Test-only harness mirroring the action's Phase-3 poll loop, with an injected
# clock + no real sleep so the timeout branch is testable offline. The decision
# is the shared reconcile_eval; only this scaffold is local to the test.
#   FIXTURES[]  one JSON snapshot per poll (the last one repeats once exhausted)
#   NOW         fake epoch; advanced by STEP on each WAIT poll
#   WINDOW      reconciliation window in fake seconds (the action uses 300)
#   STEP        fake seconds advanced per WAIT poll (the action sleeps POLL_INTERVAL)
# Echoes the final verdict:  RECONCILED:<id>  |  KEEP_FAILURE
reconcile_loop() {  # $1=DEPLOY_SHA
  local DEPLOY_SHA="$1" i=0 deadline=$(( NOW + WINDOW )) snap v last=$(( ${#FIXTURES[@]} - 1 ))
  while : ; do
    if [ "$i" -le "$last" ]; then snap="${FIXTURES[$i]}"; else snap="${FIXTURES[$last]}"; fi
    v=$(reconcile_eval "$snap" "$DEPLOY_SHA")
    case "$v" in
      RECONCILED:*) printf '%s' "$v"; return 0 ;;
      GIVE_UP)      printf 'KEEP_FAILURE'; return 0 ;;
    esac
    # WAIT:<id> — a deploy is still in flight; wait until the window elapses.
    if [ "$NOW" -ge "$deadline" ]; then printf 'KEEP_FAILURE'; return 0; fi
    NOW=$(( NOW + STEP )); i=$(( i + 1 ))
  done
}

SHA=ba47a848
OTHER=deadbeefcafe

# Snapshot builder keeps fixtures readable; pins a web service + a worker so the
# OFF_TAG walk covers more than one component array.
snap() {  # $1=active_phase $2=active_id $3=svc_tag $4=worker_tag $5=in_progress_id(optional)
  jq -nc --arg ph "$1" --arg id "$2" --arg svc "$3" --arg wk "$4" --arg ip "${5:-}" '
    [ { active_deployment: {
          id: $id, phase: $ph,
          spec: { services: [ {name:"web",    image:{tag:$svc}} ],
                  workers:  [ {name:"worker", image:{tag:$wk}} ] } } }
      + (if $ip == "" then {} else { in_progress_deployment: { id: $ip } } end) ]'
}

# ── Case 1: success — a superseder carried OUR SHA to ACTIVE, none in flight ──
FIXTURES=( "$(snap ACTIVE active-1 "$SHA" "$SHA" "")" ); NOW=1000 WINDOW=300 STEP=10
assert_eq "success: reconciled to active id"        "RECONCILED:active-1" "$(reconcile_loop "$SHA")"

# ── Case 2: different-SHA-superseder — ACTIVE but on another image, none in flight ──
FIXTURES=( "$(snap ACTIVE active-2 "$OTHER" "$OTHER" "")" ); NOW=1000 WINDOW=300 STEP=10
assert_eq "different-SHA superseder stays failure"  "KEEP_FAILURE"        "$(reconcile_loop "$SHA")"

# ── Case 3: in-flight-superseder — still deploying, then lands OUR SHA ──
FIXTURES=(
  "$(snap ACTIVE active-3  "$OTHER" "$OTHER" inprog-3)"   # poll 1: old image live, superseder building
  "$(snap ACTIVE active-3b "$SHA"   "$SHA"   "")"         # poll 2: superseder landed our SHA
); NOW=1000 WINDOW=300 STEP=10
assert_eq "in-flight superseder lands → reconciled"  "RECONCILED:active-3b" "$(reconcile_loop "$SHA")"

# ── Case 4: timeout — a superseder stays in flight past the window ──
FIXTURES=( "$(snap ACTIVE active-4 "$OTHER" "$OTHER" inprog-4)" ); NOW=1000 WINDOW=30 STEP=10
assert_eq "in-flight past window → keep failure"     "KEEP_FAILURE"        "$(reconcile_loop "$SHA")"

# ── Edge: a partially-rolled component (worker off-SHA) must NOT reconcile ──
FIXTURES=( "$(snap ACTIVE active-5 "$SHA" "$OTHER" "")" ); NOW=1000 WINDOW=300 STEP=10
assert_eq "one component off-SHA stays failure"      "KEEP_FAILURE"        "$(reconcile_loop "$SHA")"

# ── Edge: empty / unreachable API ([]) — no superseder coming, keep failure ──
FIXTURES=( "[]" ); NOW=1000 WINDOW=300 STEP=10
assert_eq "empty active json → keep failure"         "KEEP_FAILURE"        "$(reconcile_loop "$SHA")"

# ── Edge: malformed JSON guarded the same as empty ──
FIXTURES=( "not json at all" ); NOW=1000 WINDOW=300 STEP=10
assert_eq "malformed active json → keep failure"     "KEEP_FAILURE"        "$(reconcile_loop "$SHA")"

# ── Edge: ACTIVE deployment with an EMPTY spec must NOT vacuously reconcile ──
# Empty OFF_TAG alone (no component is off-SHA) is satisfied trivially when there
# are no components at all; without the ON_SHA>0 guard this would flip a failed
# deploy to "success" with zero positive evidence.
EMPTY_SPEC='[{"active_deployment":{"id":"a","phase":"ACTIVE","spec":{}}}]'
assert_eq "ACTIVE but empty spec → not reconciled"   "GIVE_UP"             "$(reconcile_eval "$EMPTY_SPEC" "$SHA")"
# Same, but with a deploy in flight ⇒ wait for it rather than give up.
EMPTY_SPEC_INFLIGHT='[{"active_deployment":{"id":"a","phase":"ACTIVE","spec":{}},"in_progress_deployment":{"id":"ip"}}]'
assert_eq "ACTIVE empty spec, in flight → wait"      "WAIT:ip"            "$(reconcile_eval "$EMPTY_SPEC_INFLIGHT" "$SHA")"

# ── Per-poll decision unit checks (reconcile_eval in isolation) ──
assert_eq "eval: active on SHA → RECONCILED"           "RECONCILED:a" "$(reconcile_eval "$(snap ACTIVE a "$SHA"   "$SHA"   "")"  "$SHA")"
# Realistic in-flight-superseder shape: doctl keeps the last good deploy under
# active_deployment (phase ACTIVE) on the OLD image while the superseder runs
# under in_progress_deployment. The off-SHA guard (not the phase) must hold the
# verdict at WAIT and prevent a premature RECONCILED.
assert_eq "eval: ACTIVE on old SHA, superseder in flight → WAIT" "WAIT:ip" "$(reconcile_eval "$(snap ACTIVE a "$OTHER" "$OTHER" ip)" "$SHA")"
# In-flight while the tracked deploy is still mid-build (phase != ACTIVE).
assert_eq "eval: building phase, in flight → WAIT"     "WAIT:ip"      "$(reconcile_eval "$(snap PENDING_DEPLOY a "$OTHER" "$OTHER" ip)" "$SHA")"
assert_eq "eval: off-SHA, nothing in flight → GIVE_UP" "GIVE_UP"      "$(reconcile_eval "$(snap ACTIVE a "$OTHER" "$OTHER" "")"  "$SHA")"

if [ "$fail" = 0 ]; then echo "ALL PASS"; else echo "TESTS FAILED"; exit 1; fi
