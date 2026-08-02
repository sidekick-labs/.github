#!/usr/bin/env bash
# Single source of truth for "does this run's failure implicate the DO deploy?"
#
# Sourced by fetch-do-deploy-logs.sh (the live path) and by
# .github/tests/do-log-fetch-gate-logic.sh (the regression tests), so the
# decision cannot drift between the two.
#
# This gate exists only for the FALLBACK path — when the upstream deploy run
# left no deployment-ID artifact, so we'd otherwise be guessing at which
# deployment to pull logs from. A Docker-build failure would then surface logs
# from an unrelated PRIOR deploy that failed at DO, which is worse than no logs
# at all. When the artifact IS present the ID belongs to this run and no
# heuristic is needed.
#
# Why the pattern is what it is — this drifted silently once and cost us every
# DO log fetch for months, so read before editing:
#
#   * "Apply spec and wait for deploy" is a step INSIDE the do-app-deploy
#     composite action. GitHub's jobs API never surfaces a composite's inner
#     steps, so it can only match for a caller that still inlines the old
#     pre-composite steps. Kept for exactly that case.
#   * do-app-deploy ALWAYS exits 0 by design (it reports via outputs so the
#     always() status steps can read them). A separate step gates the job, so
#     on a real DO failure the API reports "Fail the job if the deploy did not
#     succeed" — this is the pattern that fires in practice today.
#   * "deploy to DigitalOcean" covers the composite step itself
#     ("Patch appspec & deploy to DigitalOcean") should it ever fail outright,
#     e.g. an action-resolution or input error.
#
# Matching bare "deploy"/"deployment" would false-match "Create GitHub
# deployment", which runs no doctl at all.
#
# Note these are unanchored substrings, not exact step names — a future step
# like "Skip deploy to DigitalOcean if unchanged" would match. That is the
# deliberate trade: over-matching costs one wasted log fetch on the fallback
# path, while under-matching silently loses the logs, which is the failure
# we're here to prevent. Keep fragments specific enough to stay one-sided.
#
# The tests assert this pattern against the step names actually declared in
# reusable-deploy-staging.yml / reusable-deploy-production.yml, so renaming a
# gate step there fails CI here instead of silently disabling DO log fetching.
DO_DEPLOY_STEP_PATTERN='wait for deploy|deploy did not succeed|deploy to DigitalOcean'

# implicates_do_deploy <failed-steps-json>
#   Exit 0 when a failed step name implicates the DO deploy, 1 otherwise.
#   A missing, empty or malformed file returns 1 — and so does the sentinel
#   ([{"job":"<unknown — gh run view failed>","steps":[]}]) that the triage
#   workflow writes when `gh run view` fails, since it carries no step names.
#   Better to omit DO logs than to fetch unrelated ones.
implicates_do_deploy() {
  local failed_steps_file=$1

  [ -s "$failed_steps_file" ] || return 1

  jq -e --arg re "$DO_DEPLOY_STEP_PATTERN" \
    '[.[].steps[]?] | any(test($re; "i"))' \
    "$failed_steps_file" >/dev/null 2>&1
}
