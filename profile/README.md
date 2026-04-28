# sidekick-labs

Repos behind the Sidekick product line — companion glasses, harness tooling, supporting services.

## Apps & services

- **sidekick-web** — primary backend / web app (Rails 8 + React 19 via Inertia)
- **sidekick-harness** — engineering harness / dev tooling
- **sidekick-rdp-client** — RDP client integration
- **sidekick-inference** — inference service
- **sidekick-companion-kit / sidekick-admin-kit** — Android (Kotlin) companions
- **sidekick-firmware-test-app** — firmware test bench

## Shared

- **sidekick-ui** — design-system / UI component library

## Conventions

All workflows used across the org live in [`.github/workflows/`](../.github/workflows). Repos consume the shared `reusable-weekly-maintenance.yml` for cron-driven dependency / TODO sweeps. See [`docs/reusable-workflows.md`](../docs/reusable-workflows.md) for the full input contract.

## Security

See [SECURITY.md](../SECURITY.md) for the vulnerability-disclosure policy.
