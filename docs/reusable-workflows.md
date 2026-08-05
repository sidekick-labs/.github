# Reusable Workflows

This repo hosts reusable GitHub Actions workflows shared across sidekick-labs
repos. Consumers reference workflows here via:

```yaml
uses: sidekick-labs/.github/.github/workflows/<name>.yml@v1
```

Pin to the `v2` tag (or a specific SHA) — `@main` works but does not give you
a stable contract.

Available reusable workflows:

- **`reusable-weekly-maintenance.yml`** — weekly dependency-update / lint /
  test / CodeQL-alert sweep across every stack.
- **`pin-check.yml`** — reusable PR gate that fails when a third-party action
  isn't SHA-pinned (the actions-pinning self-healer's SENSOR). See
  [Actions pinning self-healer](#actions-pinning-self-healer).

> **Removed:** `reusable-sentry-autofix.yml` — the Sentry autofix moved to the
> one-workflow-per-org model: `sidekick-labs/sre-brain`'s `sentry-sweep.yml` +
> `sentry-autofix-engine.yml` now run the triage/fix cross-repo via the
> release-bot App token (sre-brain#17). Config lives in sre-brain's
> `sources.yaml sentry.autofix`.

## `reusable-weekly-maintenance.yml`

Single reusable workflow that drives the weekly maintenance cron across every
stack in the org (Rails apps, Ruby gems, Node libraries, Node apps, Kotlin
Multiplatform). Replaces per-repo `weekly-maintenance.yml` files.

A run does the following:

1. Validates the `stack` input and required secrets (fails fast before
   checkout).
2. Sets up the toolchain for the chosen stack (Ruby/Node/JDK+Gradle).
3. Captures a TODO/FIXME census, restoring last week's snapshot from cache and
   computing a delta.
4. Hands off to `anthropics/claude-code-action` with a stack-aware prompt that
   runs the dependency updates, runs the verification commands you supply
   (`lint-commands`, `test-commands`, or `gradle-test-command`), and — only
   when verification passes — opens a signed PR via the GitHub API.
5. Uploads `tmp/maintenance/` (prompt, TODO/FIXME census + diff) as an
   artifact for inspection.

### Inputs

| Input | Type | Required | Default | Description |
|---|---|---|---|---|
| `stack` | string | yes | — | One of `rails`, `ruby-gem`, `node-lib`, `node-app`, `kmp`. |
| `ruby-version-file` | string | no | `.ruby-version` | Used for the `rails` and `ruby-gem` stacks unless `ruby-version` is set. |
| `ruby-version` | string | no | `""` | Explicit Ruby version override. Wins over `ruby-version-file` when non-empty. |
| `node-version` | string | no | `lts/*` | Used for the `rails`, `node-lib`, `node-app` stacks. |
| `jdk-version` | string | no | `17` | Used for the `kmp` stack. |
| `bundle-update-strategy` | string | no | `lock-update` | `lock-update`, `conservative`, or `none`. Controls how the prompt asks Claude to update Bundler. |
| `run-bundler-audit` | boolean | no | `true` | Add a `bundler-audit check --update` step to the prompt (Ruby stacks). |
| `run-brakeman` | boolean | no | `false` | Add a `bin/brakeman` step to the prompt (Rails stack). |
| `run-sorbet-rbi` | boolean | no | `false` | Regenerate Sorbet RBIs via `bin/tapioca dsl/gems/annotations` and include drift in the PR (Rails stack). |
| `run-npm-audit` | boolean | no | `true` | Add an `npm audit fix` step (stacks with `package.json`). |
| `lint-commands` | string | no | `""` | Multiline shell — every line is a verification command (e.g. `bin/rubocop`, `npm run lint`). |
| `test-commands` | string | no | `""` | Multiline shell — full test-suite verification commands. |
| `gradle-test-command` | string | no | `./gradlew test` | KMP test command. |
| `additional-allowed-tools` | string | no | `""` | Comma-separated entries appended to `--allowed-tools`. |
| `todo-fixme-paths` | string | no | `.` | Space-separated paths scanned for TODO/FIXME. |
| `todo-fixme-exclude` | string | no | (sensible defaults) | Space-separated globs excluded from the census. |
| `timeout-minutes` | number | no | `45` | Job-level timeout. |
| `claude-timeout-minutes` | number | no | `25` | Timeout for the Claude action step. |

### Secrets

| Secret | Required | Description |
|---|---|---|
| `claude-code-oauth-token` | yes | OAuth token for `anthropics/claude-code-action`. |

### Behavior

- Top-level `permissions: {}`; the job re-grants `contents: write`,
  `pull-requests: write`, `id-token: write` for the signed-commit + PR flow.
- All third-party actions are SHA-pinned (checkout, ruby/setup-ruby,
  setup-node, anthropics/claude-code-action). KMP-only setup-java and
  setup-gradle remain on floating major tags pending org-wide pinning.
- **Crash heartbeat.** A second job, `alert`, runs `if: always()` after
  `maintenance` and turns a *missed* beat into a durable record: it opens ONE
  deduped `beat failure: weekly-maintenance` issue and closes it on recovery
  (recovery is gated on an actual `success`, so an all-skipped run is not read as
  recovery). It exists because the maintenance job's own reporting only helps if
  the job REACHED it — a run that dies at token mint, `npm ci` or a runner death
  would otherwise be invisible.
  **This makes `issues: write` a mandatory caller grant** (see the note above the
  examples): a reusable's token is capped by the caller's, so a caller missing it
  lands in `startup_failure`. Every `gh issue` call is best-effort (`|| true`)
  because most consumer repos have Issues DISABLED — there the miss degrades to a
  `::warning::` annotation rather than turning a green run red.
- TODO/FIXME census uses `actions/cache` to keep the previous run's snapshot
  scoped per `repository_id`; week-over-week delta surfaces in `$GITHUB_STEP_SUMMARY`
  and in the PR body.

### Example — Rails app (sidekick-web)

```yaml
name: Weekly Maintenance
on:
  schedule:
    - cron: '0 0 * * 0'
  workflow_dispatch:

permissions: {}

jobs:
  maintenance:
    # A reusable workflow's token is capped by the CALLER's permissions. A
    # job-level block fully replaces the top-level one for this job, so every
    # permission the reusable needs must be listed HERE.
    permissions:
      contents: write
      pull-requests: write
      issues: write         # crash-heartbeat `alert` job
      id-token: write
      security-events: read
    uses: sidekick-labs/.github/.github/workflows/reusable-weekly-maintenance.yml@v1
    with:
      stack: rails
      run-brakeman: true
      run-sorbet-rbi: true
      lint-commands: |
        bin/rubocop
        npm run lint
        npm run check
      test-commands: |
        bin/rspec
        npm run test:run
      additional-allowed-tools: 'Bash(bin/rspec:*),Bash(npm run test:run:*)'
    secrets:
      claude-code-oauth-token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
```

### Example — Ruby gem (sidekick-rdp-client)

```yaml
name: Weekly Maintenance
on:
  schedule:
    - cron: '0 0 * * 0'
  workflow_dispatch:

permissions: {}

jobs:
  maintenance:
    # A reusable workflow's token is capped by the CALLER's permissions. A
    # job-level block fully replaces the top-level one for this job, so every
    # permission the reusable needs must be listed HERE.
    permissions:
      contents: write
      pull-requests: write
      issues: write         # crash-heartbeat `alert` job
      id-token: write
      security-events: read
    uses: sidekick-labs/.github/.github/workflows/reusable-weekly-maintenance.yml@v1
    with:
      stack: ruby-gem
      bundle-update-strategy: conservative
      lint-commands: |
        bundle exec rubocop
      test-commands: |
        bundle exec rspec
    secrets:
      claude-code-oauth-token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
```

### Example — Node library (sidekick-ui)

```yaml
name: Weekly Maintenance
on:
  schedule:
    - cron: '0 0 * * 0'
  workflow_dispatch:

permissions: {}

jobs:
  maintenance:
    # A reusable workflow's token is capped by the CALLER's permissions. A
    # job-level block fully replaces the top-level one for this job, so every
    # permission the reusable needs must be listed HERE.
    permissions:
      contents: write
      pull-requests: write
      issues: write         # crash-heartbeat `alert` job
      id-token: write
      security-events: read
    uses: sidekick-labs/.github/.github/workflows/reusable-weekly-maintenance.yml@v1
    with:
      stack: node-lib
      node-version: '20'
      lint-commands: |
        npm run lint
        npm run check
      test-commands: |
        npm run test:run
        npm run build
    secrets:
      claude-code-oauth-token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
```

### Example — Node app (sidekick-harness)

```yaml
name: Weekly Maintenance
on:
  schedule:
    - cron: '0 0 * * 0'
  workflow_dispatch:

permissions: {}

jobs:
  maintenance:
    # A reusable workflow's token is capped by the CALLER's permissions. A
    # job-level block fully replaces the top-level one for this job, so every
    # permission the reusable needs must be listed HERE.
    permissions:
      contents: write
      pull-requests: write
      issues: write         # crash-heartbeat `alert` job
      id-token: write
      security-events: read
    uses: sidekick-labs/.github/.github/workflows/reusable-weekly-maintenance.yml@v1
    with:
      stack: node-app
      node-version: '24'
      lint-commands: |
        npm run format:check
        npm run lint
        npm run typecheck
      test-commands: |
        npm test
    secrets:
      claude-code-oauth-token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
```

### Versioning

The `v2` tag carries the reusable workflows. New input contracts will
land on `v2`; breaking changes will publish under `v3`. Pin to a SHA if
you need stricter immutability.

## Actions pinning self-healer

Two workflows keep every third-party GitHub Action across the org SHA-pinned
(a mutable tag can be re-pointed to malicious code — a supply-chain risk). They
mirror the reference impl in `rarebit-one/.github` and are org-agnostic: the
only sidekick-labs-specific wiring is the release-bot credential in the sweep.

- **`pin-check.yml`** (SENSOR) — a `workflow_call` PR gate. Runs **zizmor**
  (blocks only on `unpinned-uses`; other findings are informational) plus a
  **pinact `--check`**. First-party `sidekick-labs/*` actions at `@main`/`@vN`
  are allowed; every third-party action must be hash-pinned. The pinact check
  uses a **runtime-synthesized** `/tmp/pinact.yaml` derived from
  `github.repository_owner` — it deliberately does **not** rely on a committed
  `.pinact.yaml`, so it behaves identically on every repo.
- **`pin-sweep.yml`** (ACTUATOR) — weekly (+ `workflow_dispatch`) self-healer.
  Enumerates non-archived org repos via the **release-bot App token**
  (`vars.SIDEKICK_RELEASE_BOT_APP_ID` + `secrets.SIDEKICK_RELEASE_BOT_PRIVATE_KEY`),
  runs `pinact run` against the same runtime-synthesized config, and opens a
  **squash auto-merge** fix PR (server-signed via the API, so it lands on
  require-signed-commits repos). Idempotent (skips repos with an open `pin-fix`
  PR). Dispatch inputs: `dry_run` and `only_repo`.

Dependabot (`github-actions` ecosystem, already enabled in each repo's
`.github/dependabot.yml`) bumps the already-pinned SHAs forward; the sweep pins
anything that slips in unpinned. The two are complementary: Dependabot
freshens, pin-sweep pins, pin-check blocks new drift.

### `anthropics/claude-code-action` exemption

We SHA-pin `anthropics/claude-code-action` to a **main-branch commit** (ahead
of release), annotated `# main@<ver>`. `pinact --check` flags that as a missing
semver comment, so both the runtime config and the committed `.pinact.yaml`
ignore it. It **stays SHA-pinned** — this only silences pinact's semver-comment
nit; zizmor's `unpinned-uses` still enforces the full SHA.

### Per-repo adoption (fan-out)

This PR wires the gate onto **`sidekick-labs/.github`'s own PRs** via
`pin-check-caller.yml` and confines all new files to this repo (so nothing
touches the `*-brain` `sync-check`ed workflow set). To adopt the gate in
another repo — start with the busiest product repo, **`sidekick-web`** — add a
thin caller pointing at this reusable:

```yaml
# .github/workflows/pin-gate.yml in the consumer repo
name: Pin Gate
on:
  pull_request:
    types: [opened, synchronize, reopened]
permissions: {}
jobs:
  pin-check:
    permissions:
      contents: read
    uses: sidekick-labs/.github/.github/workflows/pin-check.yml@main
```

Also ensure the consumer's `.github/dependabot.yml` has the `github-actions`
weekly block. The `pin-sweep` covers every non-archived repo automatically —
no per-repo wiring needed for the actuator.
