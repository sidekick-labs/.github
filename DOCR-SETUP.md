# DigitalOcean Container Registry (DOCR) Setup

## Prerequisites

- DigitalOcean account with billing enabled
- GitHub organization admin access (for secrets)

## 1. Create DOCR Registry

1. Go to [DigitalOcean Container Registry](https://cloud.digitalocean.com/registry)
2. Create a registry named `sidekick-labs`
3. Choose the **Starter** plan ($5/month, 5 GB storage)
4. Region: choose closest to your DO App Platform apps

## 2. Enable DO App Platform Integration

1. In DO console, go to **Settings → Integrations**
2. Enable the container registry integration
3. This allows App Platform to pull from DOCR without additional auth

## 3. Create API Token

1. Go to **API → Tokens**
2. Create a new token with:
   - Name: `github-actions-docr`
   - Scopes: Read/Write access to Container Registry
3. Copy the token

## 4. Add GitHub Secret

1. Go to GitHub org settings: **Settings → Secrets and variables → Actions**
2. Add organization secret:
   - Name: `DIGITALOCEAN_ACCESS_TOKEN`
   - Value: the API token from step 3

## 5. Verify

Push a test image from a GitHub Actions workflow:

```yaml
- uses: digitalocean/action-doctl@v2
  with:
    token: ${{ secrets.DIGITALOCEAN_ACCESS_TOKEN }}
- run: doctl registry login --expiry-seconds 1200
- run: docker pull hello-world && docker tag hello-world registry.digitalocean.com/sidekick-labs/test:latest && docker push registry.digitalocean.com/sidekick-labs/test:latest
```

## Usage

Repos call the shared workflow to build and push images:

```yaml
jobs:
  build:
    uses: sidekick-labs/.github/.github/workflows/build-and-push-image.yml@main
    with:
      dockerfile: ./Dockerfile.base
      image_name: my-app-base
      tags: latest,v1.0
    secrets: inherit
```

See `build-and-push-image.yml` for all available inputs.

## Image Naming Convention

| App | Image | Tags |
|-----|-------|------|
| sidekick-web | sidekick-web-base | ruby-3.4.4, latest |
| sidekick-web | sidekick-web-build | ruby-3.4.4-node-20, latest |
| sidekick-web | sidekick-web-deps | latest |
| sidekick-harness | sidekick-harness-base | node-25, latest |
| sidekick-harness | sidekick-harness-deps | latest |

## Cost

- Starter plan: $5/month, 5 GB storage
- Estimated usage: ~2-3 GB (5 images)
