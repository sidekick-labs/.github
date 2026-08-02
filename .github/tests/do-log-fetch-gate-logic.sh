#!/usr/bin/env bash
# Regression tests for the fetch-do-deploy-logs fallback gate — the decision
# "does this run's failure implicate the DO deploy, so should we pull DO logs?"
#
# Why these exist: the gate required a failed step named "…wait for deploy",
# which stopped matching when the deploy moved into the do-app-deploy composite
# action (GitHub's jobs API never surfaces a composite's inner steps, and the
# action exits 0 by design so a SEPARATE step fails the job). Nothing broke
# loudly — triage just silently wrote a placeholder instead of DO logs on every
# failed deploy, for months, across three repos. See sidekick-web#1596.
#
# The gate DECISION is the single source of truth in
#   .github/actions/fetch-do-deploy-logs/do-log-gate.sh
# which the live script sources via "$HERE/do-log-gate.sh" and this test sources
# directly — so there is NO copied logic that can drift.
#
# The load-bearing cases are COUPLING ones: they read the step names out of
# reusable-deploy-staging.yml / reusable-deploy-production.yml *by behaviour*
# (the step that gates on do_deploy's outcome; the step that uses the
# do-app-deploy action) and assert the gate matches them. Rename a gate step
# there and this test fails, instead of DO log fetching silently going dark.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../actions/fetch-do-deploy-logs/do-log-gate.sh
source "$HERE/../actions/fetch-do-deploy-logs/do-log-gate.sh"

fail=0
assert_eq() { if [ "$2" = "$3" ]; then echo "ok: $1"; else echo "FAIL: $1"; echo "  exp: [$2]"; echo "  got: [$3]"; fail=1; fi; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# gate <json> -> "match" | "nomatch"; writes the JSON to a temp file first,
# exercising implicates_do_deploy exactly as the live script calls it.
gate() {
  printf '%s' "$1" > "$TMP/steps.json"
  if implicates_do_deploy "$TMP/steps.json"; then echo "match"; else echo "nomatch"; fi
}

# steps_json <step-name>... -> the shape `gh run view --json jobs` produces
steps_json() {
  local out='[{"job":"deploy / Deploy to staging","steps":['
  local first=1 s
  for s in "$@"; do
    [ $first -eq 1 ] || out+=','
    first=0
    out+=$(printf '%s' "$s" | jq -R .)
  done
  printf '%s]}]' "$out"
}

# --- Coupling: the gate must match what the deploy workflows actually emit ---
# Found by behaviour, not by name, so a rename is still located and then
# asserted against — which is the whole point.
for env in staging production; do
  wf="$HERE/../workflows/reusable-deploy-$env.yml"

  gate_step=$(yq '.jobs.*.steps[] | select((.if // "") | contains("do_deploy.outputs.outcome")) | .name' "$wf")
  assert_eq "$env: a deploy-gate step exists in reusable-deploy-$env.yml" \
    "yes" "$([ -n "$gate_step" ] && echo yes || echo no)"
  assert_eq "$env: gate matches its deploy-gate step ($gate_step)" \
    "match" "$(gate "$(steps_json "$gate_step")")"

  composite_step=$(yq '.jobs.*.steps[] | select((.uses // "") | contains("do-app-deploy")) | .name' "$wf")
  assert_eq "$env: a do-app-deploy step exists in reusable-deploy-$env.yml" \
    "yes" "$([ -n "$composite_step" ] && echo yes || echo no)"
  assert_eq "$env: gate matches its do-app-deploy step ($composite_step)" \
    "match" "$(gate "$(steps_json "$composite_step")")"
done

# The composite's own inner step name — only reachable for a caller that still
# inlines the pre-composite steps, but kept working on purpose.
assert_eq "legacy inline step still matches" \
  "match" "$(gate "$(steps_json 'Apply spec and wait for deploy')")"

# --- Negative: steps that must NOT trigger a DO log fetch ---
assert_eq "docker build failure does not match" \
  "nomatch" "$(gate "$(steps_json 'Build and push app image')")"
# The near-miss that motivated the original narrow pattern: this step runs no
# doctl, so matching bare "deployment" would wrongly pull logs.
assert_eq "Create GitHub deployment does not match" \
  "nomatch" "$(gate "$(steps_json 'Create GitHub Deployment + mark in_progress')")"
assert_eq "unrelated step does not match" \
  "nomatch" "$(gate "$(steps_json 'Set up job')")"

# --- Mixed / structural ---
assert_eq "matches when a deploy step is among several failures" \
  "match" "$(gate "$(steps_json 'Build and push app image' 'Fail the job if the deploy did not succeed')")"
assert_eq "empty steps array does not match" \
  "nomatch" "$(gate '[{"job":"deploy","steps":[]}]')"
# The triage workflow writes this sentinel when `gh run view` fails. It carries
# no step names, so it must not be read as "the DO deploy failed".
assert_eq "gh-run-view sentinel does not match" \
  "nomatch" "$(gate '[{"job":"<unknown — gh run view failed>","steps":[]}]')"
assert_eq "empty file does not match" \
  "nomatch" "$(gate '')"
assert_eq "malformed json does not match" \
  "nomatch" "$(gate 'not json at all')"

# Missing file — implicates_do_deploy is called before any file is guaranteed.
if implicates_do_deploy "$TMP/definitely-absent.json"; then res=match; else res=nomatch; fi
assert_eq "missing file does not match" "nomatch" "$res"

# Step names come from humans and casing drifts; the gate is case-insensitive.
assert_eq "matching is case-insensitive" \
  "match" "$(gate "$(steps_json 'FAIL THE JOB IF THE DEPLOY DID NOT SUCCEED')")"

exit $fail
