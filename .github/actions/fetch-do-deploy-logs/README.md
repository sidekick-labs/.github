# `fetch-do-deploy-logs` composite action

Fetches DigitalOcean App Platform **deploy** + **run** logs for a failed
deployment and writes them as markdown for post-deploy triage to read.

This is the de-duplicated form of `.github/scripts/fetch-do-deploy-logs.sh`,
which existed as three byte-identical copies in `sidekick-web`,
`sidekick-harness` and `sidekick-inference`. Keeping it here means a fix lands
once, and a new app repo gets it by adding one `uses:` step.

## Why it exists

When a DO deploy fails, the GitHub Actions log only shows the wrapper's
`deployment failed: phase=ERROR`. The actual error — container crash,
health-check rejection, missing env var, migration abort — is only in DO's
logs, behind a separate API call.

`--type=build` is deliberately not fetched: images are built in the Actions
pipeline and pushed to DOCR, so DO's build phase is just an image pull.

## Usage

```yaml
# Prerequisites earlier in the job: doctl installed + `doctl auth init`
# (token needs app:read), and /tmp/failed-steps.json already written.
- name: Fetch DO deploy logs
  uses: sidekick-labs/.github/.github/actions/fetch-do-deploy-logs@v3
  with:
    app_id: ${{ steps.ctx.outputs.do_app_id }}
```

Everything else defaults to the paths the triage workflows already use:
`/tmp/do-logs.md`, `/tmp/failed-steps.json`, `300` tail lines, and the
deployment ID at `/tmp/triage-context/do-deployment-id.txt`.

## Picking the right deployment

DO auto-rolls back a failed deploy by promoting a fresh ACTIVE deployment,
which shadows the ERROR'd one in `list-deployments` — so "most recent" is the
wrong answer. Instead:

1. **Authoritative.** `reusable-deploy-*.yml` persists the deployment ID it
   tracked as the `triage-context` artifact; triage downloads it and this
   action reads it back. That ID belongs to *this* run, so its logs cannot be
   from an unrelated prior deployment.
2. **Fallback.** No artifact means the upstream failed before the deploy step
   ran (typically a Docker build failure). Only then do we scan recent
   deployments for an ERROR/CANCELED — and only when a failed step name
   implicates the DO deploy, so build failures don't surface stale logs.

## Never fails the job

Triage context is best-effort, so every giving-up path writes an explanatory
placeholder to `out_path` and exits 0:

- no `app_id` (environment has no DO app)
- `doctl` can't read the app (missing/expired token, or a PAT without `app:read`)
- the deployment reached `ACTIVE` — deploy succeeded, job failed later
- no artifact **and** no failed step implicating the DO deploy
- no candidate deployment found

Note the `app:read` case: `doctl auth init` validates against the OAuth
token-info endpoint, which **ignores scopes**, so an under-scoped token
authenticates cleanly and only fails at the first real API call. The action
probes `doctl apps get` up front so that shows up as a legible placeholder
instead of an empty log section.
