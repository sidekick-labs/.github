#!/usr/bin/env bash
# Fetch DO App Platform logs for the most recent deployment of an app.
# Used by the failure-triage path: when `doctl apps update --wait` fails,
# the GitHub Actions log only shows the wrapper's "deployment failed:
# phase=ERROR" message. The actual error — container startup crash,
# health-check rejection, missing env var, migration failure — lives in
# DO's deploy/run logs and needs a separate API call.
#
# We fetch --type=deploy (container startup phase: command execution,
# health-check probes, migration output) and --type=run (post-startup
# stdout/stderr if the container reached a running state before failing).
# We do NOT fetch --type=build: the Docker image is built in the GH
# Actions pipeline and pushed to DOCR, so DO's "build" phase is just an
# image pull with no useful failure detail for our setup.
#
# Writes a markdown summary to OUT_PATH. Always exits 0. Skips with a
# placeholder when:
#   - DO_APP_ID is unset (production not provisioned, or unsupported env)
#   - doctl can't read the app (missing/expired token, or a PAT without
#     app:read)
#   - the identified deployment reached ACTIVE — the deploy succeeded and
#     the job failed in a later step
#   - there's no upstream artifact AND no failed step name implicating the
#     DO deploy — the upstream failure is in Docker build, GH API, or
#     appspec patch, where DO logs would be noise from an unrelated prior
#     deployment
#   - no candidate deployment can be identified (no upstream artifact
#     and no ERROR/CANCELED in the recent list)
#
# Authoritative vs heuristic deployment ID:
#   The 5th argument (`upstream-deployment-id`) is the deployment ID
#   captured by deploy-staging.yml's `do_deploy` step and uploaded as a
#   workflow artifact. When provided, we use it directly — this is the
#   only reliable way to identify our deployment, since DO auto-rolls
#   back failed deploys by promoting a fresh ACTIVE deployment that
#   shadows our ERROR'd one in `list-deployments`.
#   When the artifact is missing (e.g., upstream failed before do_deploy
#   ran), we fall back to scanning the recent deployments for the most
#   recent ERROR or CANCELED phase entry.
#
# Usage: fetch-do-deploy-logs.sh <do-app-id-or-empty> <out-path> <failed-steps-json> [tail-lines] [upstream-deployment-id]
#   <do-app-id-or-empty>       empty when no DO app is configured for the env
#   <upstream-deployment-id>   empty when no upstream artifact is available;
#                              the script falls back to phase-based search
# Requires: doctl authenticated (DIGITALOCEAN_ACCESS_TOKEN env or
#           `doctl auth init` previously run in the same job).

set -euo pipefail

# The fallback-path gate decision lives in do-log-gate.sh so the regression
# tests can source the same code the action runs. Resolved relative to this
# script so it works both from $GITHUB_ACTION_PATH and from the tests.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=do-log-gate.sh
source "$HERE/do-log-gate.sh"

# APP_ID is intentionally optional (empty = "no DO app configured"). The
# empty-string guard below handles that branch.
APP_ID="${1:-}"
OUT="${2?usage: fetch-do-deploy-logs.sh <app-id-or-empty> <out-path> <failed-steps-json> [tail-lines] [upstream-deployment-id]}"
FAILED_STEPS_FILE="${3?usage: fetch-do-deploy-logs.sh <app-id-or-empty> <out-path> <failed-steps-json> [tail-lines] [upstream-deployment-id]}"
TAIL_LINES="${4:-300}"
UPSTREAM_DEPLOY_ID="${5:-}"

if [ -z "$APP_ID" ]; then
  echo "(no DO app id configured — DO_STAGING_APP_ID unset)" > "$OUT"
  echo "No DO app id — wrote placeholder to $OUT"
  exit 0
fi

# Defensive: catch a doctl that can't read this app up front, so the
# problem surfaces as a recognizable placeholder rather than an empty
# log section further down.
#
# Probe with `apps get` — the exact capability this script needs — and
# NOT `account get`. The deploy PAT is scoped to app/registry/database
# only (no `account:read`), so `account get` 403s on a perfectly valid
# token. `doctl auth init` validates against the OAuth token-info
# endpoint, which ignores scopes, so auth appears green while this
# probe fails: the fetch was skipped on every run.
#
# Keep doctl's own stderr: a missing token, an expired token, a token
# without app:read, a wrong-team token and a transient DO API blip all
# reach this branch, and the placeholder is the only thing the next
# person (or Claude) sees. Guessing at one cause in the message is how
# the previous version sent triage chasing a doctl-auth problem that
# didn't exist. `2>&1 >/dev/null` captures stderr only.
if ! PROBE_ERR=$(doctl apps get "$APP_ID" 2>&1 >/dev/null); then
  {
    echo "(doctl could not read app ${APP_ID} — no DO logs for this failure.)"
    echo "(Most often DIGITALOCEAN_ACCESS_TOKEN is missing/expired, lacks app:read,"
    echo " or belongs to another DO team — but a transient API error lands here too."
    echo " doctl reported:)"
    printf '%s\n' "${PROBE_ERR:-<no stderr from doctl>}" | head -5
  } > "$OUT"
  echo "doctl cannot read app $APP_ID — wrote placeholder to $OUT"
  exit 0
fi

DEPLOY_ID=""
PHASE=""
SOURCE=""

if [ -n "$UPSTREAM_DEPLOY_ID" ]; then
  # Authoritative path: upstream told us exactly which deployment is
  # ours. Just look up the phase for the markdown header. UNKNOWN if
  # `get-deployment` fails — we still fetch logs by ID below.
  DEPLOY_ID="$UPSTREAM_DEPLOY_ID"
  PHASE=$(doctl apps get-deployment "$APP_ID" "$DEPLOY_ID" \
    --format Phase --no-header 2>/dev/null | tr -d '[:space:]' || true)
  PHASE="${PHASE:-UNKNOWN}"
  SOURCE="upstream artifact"

  # This ID was captured by THIS run's `do_deploy` step, so its logs
  # cannot be from an unrelated prior deployment — the reason the
  # step-name gate below exists doesn't apply here. Skip only when the
  # deployment itself went ACTIVE: the job then failed somewhere after
  # a healthy deploy, so DO's logs aren't the story.
  if [ "$PHASE" = "ACTIVE" ]; then
    {
      echo "## DO logs"
      echo ""
      echo "Skipped — DO deployment \`${DEPLOY_ID}\` reached ACTIVE, so the deploy itself"
      echo "succeeded and the job failed in a later step. See \`/tmp/fail-log.txt\` and"
      echo "\`/tmp/failed-steps.json\` for the actual failure."
    } > "$OUT"
    echo "DO deployment is ACTIVE — wrote placeholder to $OUT"
    exit 0
  fi
else
  # Step-name gate, fallback path only: with no upstream artifact we'd be
  # guessing at a deployment, so only proceed when a failed step name says
  # the DO deploy is implicated. Without this, a Docker-build failure would
  # surface logs from a prior deploy that failed at DO — irrelevant noise
  # that would mislead Claude. See do-log-gate.sh for why the pattern
  # matches what it does.
  if ! implicates_do_deploy "$FAILED_STEPS_FILE"; then
    {
      echo "## DO logs"
      echo ""
      echo "Skipped — the upstream failure is not in the DO deploy step. DO build/run logs"
      echo "would be from an unrelated prior deployment. See \`/tmp/fail-log.txt\` and"
      echo "\`/tmp/failed-steps.json\` for the actual failure."
    } > "$OUT"
    echo "Failure not in DO deploy step — wrote placeholder to $OUT"
    exit 0
  fi

  # Fallback: the upstream artifact wasn't available. Scan the recent
  # deployments for the most recent ERROR or CANCELED. Limit to 10 rows
  # so we don't reach back to old, unrelated failures. We can't trust
  # row 1 because DO's auto-rollback creates a new ACTIVE deployment
  # that shadows our failed one.
  DEPLOY_LINE=$(doctl apps list-deployments "$APP_ID" \
    --format ID,Phase --no-header 2>/dev/null \
    | head -n 10 \
    | awk '$2 == "ERROR" || $2 == "CANCELED" {print; exit}' || true)

  if [ -z "$DEPLOY_LINE" ]; then
    {
      echo "## DO logs"
      echo ""
      echo "No upstream deployment ID artifact and no ERROR/CANCELED deployment"
      echo "found in the last 10 entries for app \`${APP_ID}\`. The deploy may"
      echo "still be in progress, or the failure may pre-date the visible window."
      echo "See \`/tmp/fail-log.txt\` for the GH Actions side of the failure."
    } > "$OUT"
    echo "No candidate DO deployment found — wrote placeholder to $OUT"
    exit 0
  fi

  DEPLOY_ID=$(awk '{print $1}' <<<"$DEPLOY_LINE")
  PHASE=$(awk '{print $2}' <<<"$DEPLOY_LINE")
  SOURCE="phase fallback"
fi

fetch_log_type() {
  local log_type=$1
  echo "### \`--type=${log_type}\` (last ${TAIL_LINES} lines)"
  echo ""
  echo '```'
  if ! doctl apps logs "$APP_ID" \
    --deployment "$DEPLOY_ID" \
    --type "$log_type" \
    --tail "$TAIL_LINES" 2>&1; then
    echo ""
    echo "(doctl apps logs --type=${log_type} failed for deployment ${DEPLOY_ID})"
  fi
  echo '```'
  echo ""
}

{
  echo "## DO logs (deployment \`${DEPLOY_ID}\`, phase=\`${PHASE}\`, source: ${SOURCE})"
  echo ""
  fetch_log_type deploy
  fetch_log_type run
} > "$OUT"

echo "Wrote DO logs (deployment $DEPLOY_ID, phase=$PHASE, source=$SOURCE) to $OUT"
