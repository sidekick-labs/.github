# `do-app-deploy` composite action

Patches a live DigitalOcean App Platform appspec to a DOCR image pinned at a
given SHA, applies it, and polls the resulting deployment to a terminal phase.

This is the de-duplicated form of the **"Fetch live appspec…"** + **"Apply spec
and wait for deploy"** steps that were copy-pasted across the staging and
production deploy workflows in `sidekick-web` and `sidekick-harness` (~130 lines
× 4 copies). One fix to the polling/patch logic now lives here.

## Usage

```yaml
# Prerequisites earlier in the job: doctl installed + `doctl auth init` (used
# only for read-only deployment polling), plus jq and curl (preinstalled on
# ubuntu-latest). The appspec itself is read and written over the REST API
# with `do_token`.
- name: Patch appspec & deploy to DigitalOcean
  id: do_deploy
  uses: sidekick-labs/.github/.github/actions/do-app-deploy@main
  with:
    app_id: ${{ vars.DO_STAGING_APP_ID }}
    image_repo: sidekick-harness
    deploy_sha: ${{ env.DEPLOY_SHA }}
    do_token: ${{ secrets.DIGITALOCEAN_ACCESS_TOKEN }}

# Gate the job's success immediately, BEFORE any success-only steps
# (release tag, Sentry notify). The action itself always exits 0.
# Gate on `outcome` rather than `phase` so the caller doesn't depend on the
# exact success-phase string (insulated from DO phase renames).
- name: Fail if deploy did not succeed
  if: steps.do_deploy.outputs.outcome != 'success'
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
| `do_token` | no | `""` | DO API token for the appspec GET/PUT; empty falls back to a `DIGITALOCEAN_ACCESS_TOKEN` env var |
| `poll_timeout_seconds` | no | `1800` | Deadline before giving up polling |
| `poll_interval_seconds` | no | `10` | Polling cadence |

| Output | Description |
|--------|-------------|
| `do_deploy_id` | DO deployment ID (empty if the update never produced one) |
| `do_deploy_url` | Cloud console URL for the deployment |
| `phase` | `ACTIVE` on success; on failure `ERROR`/`CANCELED`/`SUPERSEDED`, `TIMEOUT`, or a sentinel (`UPDATE_FAILED`/`PARSE_FAILED`/`NO_DEPLOY_ID`/`INVALID_INPUT`/`SPEC_DRIFT`) |
| `outcome` | `success` when `phase == ACTIVE`, else `failure` |

## Appspec read-modify-write and the drift check

The spec never passes through a client's spec model. An old doctl (1.98.1,
reached through action-doctl's silent fallback) dropped every field it didn't
know on `doctl apps spec get` → edit → `doctl apps update`, and staging lost
`liveness_health_check` and other fields that way
([root cause](https://github.com/fundbright/delivery-ops/issues/1019#issuecomment-6795304856)).
So the action:

1. `GET /v2/apps/{id}` and keeps `.app.spec` as JSON, unchanged.
2. Patches it with jq (`spec_patch` in [`spec.sh`](spec.sh)): on every
   service/worker/job, drop the source build, pin `image` to DOCR
   `<image_repo>:<deploy_sha>`, and upsert `SENTRY_RELEASE`. This is exactly
   what the previous yq patch did, and the test checks the two are equal.
   A pre-write guard refuses any patch that touches other fields.
3. `PUT /v2/apps/{id}` with `{"spec": <patched>}`. This call is never retried.
4. Re-reads the app and diffs the stored spec against the patched one
   (`spec_drift`). Any missing, added or changed path fails the action with
   phase `SPEC_DRIFT` and the list of paths. Values are never printed, because
   env values can be secrets. A passing check logs `Spec drift check passed`.

The only normalisation is `image.registry` on DOCR components: the patch
writes the image block without it, and DO fills in the registry name. Checked
with `POST /v2/apps/propose` against all six sidekick apps; nothing else
differed.

doctl is still used for read-only deployment polling, behind the callers'
doctl version guard. Tests: [`../../tests/do-app-deploy-spec-logic.sh`](../../tests/do-app-deploy-spec-logic.sh),
run by `test-release-thread.yml`.
