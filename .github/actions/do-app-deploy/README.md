# `do-app-deploy` composite action

Patches a live DigitalOcean App Platform appspec to a DOCR image pinned at a
given SHA, applies it, and polls the resulting deployment to a terminal phase.

This is the de-duplicated form of the **"Fetch live appspec…"** + **"Apply spec
and wait for deploy"** steps that were copy-pasted across the staging and
production deploy workflows in `sidekick-web` and `sidekick-harness` (~130 lines
× 4 copies). One fix to the polling/patch logic now lives here.

## Usage

```yaml
# Prerequisites earlier in the job: doctl installed + `doctl auth init`, plus
# yq (mikefarah) and jq — all preinstalled on ubuntu-latest.
- name: Patch appspec & deploy to DigitalOcean
  id: do_deploy
  uses: sidekick-labs/.github/.github/actions/do-app-deploy@main
  with:
    app_id: ${{ vars.DO_STAGING_APP_ID }}
    image_repo: sidekick-harness
    deploy_sha: ${{ env.DEPLOY_SHA }}

# Gate the job's success immediately, BEFORE any success-only steps
# (release tag, Sentry notify). The action itself always exits 0.
- name: Fail if deploy did not reach ACTIVE
  if: steps.do_deploy.outputs.phase != 'ACTIVE'
  run: |
    echo "::error::Deploy ended in phase '${{ steps.do_deploy.outputs.phase }}'"
    exit 1

# `always()` steps (post-deploy triage, GitHub Deployment status, failure
# comment) read steps.do_deploy.outputs.do_deploy_id — populated even on a
# failed deploy so the triage workflow doesn't have to guess which DO
# deployment was ours.
```

## Why the action never exits non-zero

The original inline step did `exit "$UPDATE_EXIT"` to fail the job in place,
while later `if: always()` steps still read its `do_deploy_id` output. A
composite action that `exit 1`s does **not** reliably propagate its outputs, so
the downstream triage/status steps would lose the deployment ID exactly when
they need it (on failure). Instead this action always succeeds and reports via
`phase` / `outcome`; the caller gates on `phase == 'ACTIVE'`. Placing the gate
immediately after the action preserves the original step ordering: success-only
steps skip on failure, `always()` steps still run.

## Inputs / outputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `app_id` | yes | — | DO App Platform app ID |
| `image_repo` | yes | — | DOCR repository to pin every component to |
| `deploy_sha` | yes | — | Image tag (commit SHA) for every component |
| `poll_timeout_seconds` | no | `1800` | Deadline before giving up polling |
| `poll_interval_seconds` | no | `10` | Polling cadence |

| Output | Description |
|--------|-------------|
| `do_deploy_id` | DO deployment ID (empty if the update never produced one) |
| `do_deploy_url` | Cloud console URL for the deployment |
| `phase` | Terminal DO phase (`ACTIVE` on success) |
| `outcome` | `success` when `phase == ACTIVE`, else `failure` |
