#!/usr/bin/env bash
# Shared, side-effect-free core of do-app-deploy's Phase-3 outcome-based
# reconciliation — the fix from .github PR #74 / commit 77accbf for the
# SUPERSEDED-but-actually-ACTIVE spurious failure (see core-platform-brain#122).
#
# This file is SOURCED by two callers, so there is a SINGLE source of truth and
# nothing to keep in sync by hand:
#   1. action.yml — the live action sources it via "$GITHUB_ACTION_PATH/reconcile.sh"
#      and drives reconcile_eval inside its bounded poll loop.
#   2. ../../tests/do-app-deploy-reconcile-logic.sh — sources it and drives the
#      same function against canned `doctl apps get --output json` fixtures.
#
# reconcile_eval is PURE: one `doctl apps get --output json` snapshot + the
# target SHA → a verdict string on stdout. All I/O (the doctl call, sleeps,
# $GITHUB_OUTPUT emits, Slack updates) stays in the action's loop, which cannot
# be unit-tested offline. Keeping ONLY the decision here is what makes the
# bug-prone part testable without inventing a doctl mock for the whole action.

# Decide the verdict for a single reconciliation poll. Prints exactly one of:
#   RECONCILED:<active_deployment_id>  app is ACTIVE and EVERY component is on
#                                      the target SHA → flip the deploy to success
#   WAIT:<in_progress_deployment_id>   not reconciled yet but a deploy is still
#                                      in flight → keep polling until the window elapses
#   GIVE_UP                            not reconciled and nothing in flight → no
#                                      superseder can land our SHA; keep the failure
reconcile_eval() {  # $1=ACTIVE_JSON  $2=DEPLOY_SHA
  local ACTIVE_JSON="$1" DEPLOY_SHA="$2"
  # An empty / unparseable response means "nothing known" — same guard the loop
  # applied inline before this was extracted.
  echo "$ACTIVE_JSON" | jq empty 2>/dev/null || ACTIVE_JSON="[]"
  local ACTIVE_PHASE OFF_TAG ON_SHA INPROG RECONCILED_ID
  ACTIVE_PHASE=$(echo "$ACTIVE_JSON" | jq -r '.[0].active_deployment.phase // ""' 2>/dev/null || echo "")
  # Names of components whose live active image tag is NOT our SHA.
  # Empty string ⇒ no component is off-SHA. On a jq failure we emit the
  # `__PARSE_ERR__` sentinel — chosen to be non-empty (so the `-z "$OFF_TAG"`
  # reconcile guard below stays false: a parse error must never read as "all on
  # SHA") and deliberately unlike any real component name, so the check can't be
  # confused with a component that merely happens to be off-SHA.
  OFF_TAG=$(echo "$ACTIVE_JSON" | jq -r --arg sha "$DEPLOY_SHA" '
    [ (.[0].active_deployment.spec // {}) | (.services[]?, .workers[]?, .jobs[]?)
      | select(.image.tag != $sha) | .name ] | join(",")' 2>/dev/null || echo "__PARSE_ERR__")
  # Count of components actually pinned to our SHA. Empty OFF_TAG alone is not
  # enough to declare success: a spec with ZERO parseable components (a fetch
  # glitch, an unexpected shape) also yields an empty OFF_TAG, which would
  # otherwise reconcile to "success" without confirming a single component runs
  # DEPLOY_SHA. Require positive evidence — at least one component on the SHA.
  ON_SHA=$(echo "$ACTIVE_JSON" | jq -r --arg sha "$DEPLOY_SHA" '
    [ (.[0].active_deployment.spec // {}) | (.services[]?, .workers[]?, .jobs[]?)
      | select(.image.tag == $sha) | .name ] | length' 2>/dev/null || echo 0)
  INPROG=$(echo "$ACTIVE_JSON" | jq -r '.[0].in_progress_deployment.id // ""' 2>/dev/null || echo "")
  if [ "$ACTIVE_PHASE" = "ACTIVE" ] && [ -z "$OFF_TAG" ] && [ "${ON_SHA:-0}" -gt 0 ]; then
    RECONCILED_ID=$(echo "$ACTIVE_JSON" | jq -r '.[0].active_deployment.id // ""' 2>/dev/null || echo "")
    printf 'RECONCILED:%s' "$RECONCILED_ID"; return 0
  fi
  if [ -z "$INPROG" ]; then printf 'GIVE_UP'; return 0; fi
  printf 'WAIT:%s' "$INPROG"
}

# Canonicalize a raw `doctl apps get-deployment --format Phase` value (already
# whitespace-stripped). doctl prints the literal "<nil>" when the deployment's
# phase field is null — observed on config-only ("Deployed configuration
# changes") deploys, where get-deployment reports "<nil>" indefinitely even
# though the app is already ACTIVE — and prints "" on a missing/failed read.
# Both must read as UNKNOWN so the poll loop's UNKNOWN branch (which cross-checks
# the live active deployment) handles them, instead of treating "<nil>" as an
# unrecognized in-flight phase and spinning to the full poll timeout. Real phase
# strings (ACTIVE / BUILDING / DEPLOYING / ERROR / …) pass through unchanged.
normalize_phase() {  # $1 = raw phase
  case "$1" in
    ""|"<nil>"|"<none>"|null|NULL) printf 'UNKNOWN' ;;
    *)                              printf '%s' "$1" ;;
  esac
}
