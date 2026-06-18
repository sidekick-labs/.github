# .github

Shared GitHub Actions workflows for the Sidekick Labs organization.

## Reusable Workflows

### `build-and-push-image.yml`

Builds a Docker image and pushes it to DigitalOcean Container Registry (DOCR). Any repo in the org can call this workflow.

**Inputs:**

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `dockerfile` | Yes | - | Path to the Dockerfile |
| `image_name` | Yes | - | Image name in DOCR (e.g., `sidekick-web-base`) |
| `tags` | Yes | - | Comma-separated image tags (e.g., `ruby-3.4.4,latest`) |
| `context` | No | `.` | Docker build context path |
| `build_args` | No | `''` | Docker build arguments (multiline, `KEY=VALUE` format) |
| `registry` | No | `sidekick-labs` | DOCR registry name |
| `timeout` | No | `20` | Job timeout in minutes |

**Required secrets:** `DIGITALOCEAN_ACCESS_TOKEN` (set as a GitHub org secret).

#### Example: calling from a repo

```yaml
# .github/workflows/build-base-images.yml
name: Build Base Images

on:
  push:
    paths:
      - 'Dockerfile.base'
      - 'Dockerfile.deps'
  workflow_dispatch:

jobs:
  build-base:
    uses: sidekick-labs/.github/.github/workflows/build-and-push-image.yml@main
    with:
      dockerfile: ./Dockerfile.base
      image_name: sidekick-web-base
      tags: ruby-3.4.4,latest
    secrets: inherit

  build-deps:
    needs: build-base
    uses: sidekick-labs/.github/.github/workflows/build-and-push-image.yml@main
    with:
      dockerfile: ./Dockerfile.deps
      image_name: sidekick-web-deps
      tags: latest
      build_args: |
        BASE_TAG=ruby-3.4.4
      build_secrets: |
        NODE_AUTH_TOKEN=${{ secrets.GITHUB_TOKEN }}
    secrets: inherit
```

> **Note on `build_args` vs `build_secrets`:** anything passed via `build_args`
> is baked into the image metadata and visible to anyone who can pull the
> image (`docker history --no-trunc`). Use `build_secrets` for tokens, API
> keys, or anything else that must not leak. Reference them inside the
> Dockerfile with:
>
> ```dockerfile
> RUN --mount=type=secret,id=NODE_AUTH_TOKEN \
>     NODE_AUTH_TOKEN=$(cat /run/secrets/NODE_AUTH_TOKEN) \
>     npm ci
> ```
>
> BuildKit mounts the secret at `/run/secrets/<id>` only for the duration
> of the `RUN` step — it's never written to image layers.

### `deploy-production.yml`

Promotes the main branch to production with release tagging and Sentry deploy notification.

### `sentry-release.yml`

Creates a Sentry release with optional frontend source map upload.

## DOCR Setup

See [DOCR-SETUP.md](DOCR-SETUP.md) for the manual provisioning checklist.

## Cognition operating standard

The cognition/`-ops` repos in this org follow the paradigm + standards recorded in
**sidekick-labs/octo-brain `decisions/`**: DEC-OCTO-0003 (sensors/actuators on an issue
spine, Slack lifecycle), DEC-OCTO-0004 (cross-org Slack channel taxonomy + 5-day/SRE-daily
scheduling). That decision log is the source of truth.
