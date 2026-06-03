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
- **`reusable-sentry-autofix.yml`** — Sentry-issue triage with auto-fix PRs
  (the apply step mirrors the post-deploy autofix safety model) and a
  GitHub-issue fallback. Dispatched per-project by the `sre-brain` sweep.

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
| `linear-fallback` | boolean | no | `false` | When true, the agent opens a Linear issue for risky/judgment-call items. Requires `linear-api-key` secret. |
| `engine` | string | no | `claude` | AI engine. `claude` uses `anthropics/claude-code-action` + `claude-code-oauth-token`. `codex` uses `openai/codex-action` + `CODEX_AUTH_JSON` (runs in a `workspace-write` sandbox with network enabled, reusing the same assembled prompt; Linear MCP via `config.toml`). See [codex-migration.md](codex-migration.md). |
| `additional-allowed-tools` | string | no | `""` | Comma-separated entries appended to `--allowed-tools` (claude engine only). |
| `todo-fixme-paths` | string | no | `.` | Space-separated paths scanned for TODO/FIXME. |
| `todo-fixme-exclude` | string | no | (sensible defaults) | Space-separated globs excluded from the census. |
| `timeout-minutes` | number | no | `45` | Job-level timeout. |
| `claude-timeout-minutes` | number | no | `25` | Timeout for the Claude action step. |

### Secrets

| Secret | Required | Description |
|---|---|---|
| `claude-code-oauth-token` | when `engine: claude` | OAuth token for `anthropics/claude-code-action`. |
| `CODEX_AUTH_JSON` | when `engine: codex` | Codex subscription `auth.json` (org secret), supplied via `secrets: inherit`. See [codex-migration.md](codex-migration.md). |
| `linear-api-key` | no | Required only when `linear-fallback: true`. |

### Behavior

- Top-level `permissions: {}`; the job re-grants `contents: write`,
  `pull-requests: write`, `id-token: write` for the signed-commit + PR flow.
- All third-party actions are SHA-pinned (checkout, ruby/setup-ruby,
  setup-node, anthropics/claude-code-action). KMP-only setup-java and
  setup-gradle remain on floating major tags pending org-wide pinning.
- Linear MCP server is always declared but only consulted when
  `linear-fallback: true` (Claude only authorises the `mcp__linear__*` tools
  in that mode).
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

### Example — Node library with Linear fallback (sidekick-ui)

```yaml
name: Weekly Maintenance
on:
  schedule:
    - cron: '0 0 * * 0'
  workflow_dispatch:

permissions: {}

jobs:
  maintenance:
    uses: sidekick-labs/.github/.github/workflows/reusable-weekly-maintenance.yml@v1
    with:
      stack: node-lib
      node-version: '20'
      linear-fallback: true
      lint-commands: |
        npm run lint
        npm run check
      test-commands: |
        npm run test:run
        npm run build
    secrets:
      claude-code-oauth-token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
      linear-api-key: ${{ secrets.LINEAR_API_KEY }}
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

## `reusable-sentry-autofix.yml`

Per-project Sentry sweep. Picks the top unresolved Sentry issue for the project
that does NOT already have an open or recently-closed auto-fix PR, hands it to
Claude for triage, and — when the verdict is high-confidence — opens a PR with a
minimal forward-fix. With the release-bot App wired (see below) the PR opens
**ready-for-review** so CI fires immediately; without it, it falls back to a
draft. Validation mirrors the post-deploy-triage autofix composite: hard path
blocklist (migrations, workflows, `config/credentials*`, `config/master.key`,
lockfiles) plus a 50-line diff cap. **Never merges.**

When the autofix path does NOT open a PR — `mode: triage-only`, the gate didn't
pass (low confidence / no suspect files / not actionable), or the apply produced
no valid diff — the workflow files a **deduplicated GitHub issue** (label
`sentry-autofix`) so the finding isn't dropped. (This replaced the former Linear
fallback.) Target:

- **`followup-issue-repo` unset** → files in the caller repo via `GITHUB_TOKEN`
  (requires Issues enabled there).
- **`followup-issue-repo: owner/name`** → files in a central tracker via a
  cross-repo release-bot token (issues:write). The sidekick-labs callers set
  this to `sidekick-labs/sre-brain` because Issues are org-disabled on the app
  repos. The tracker's open `sentry-autofix` issues are also what the sweep
  dedups against, so a non-autofixable issue is filed once and not re-dispatched.

Either way it degrades gracefully: when the issue can't be created (Issues
disabled, or no cross-repo token), the would-be body is captured in the run
summary instead of failing.

The fetch step queries Sentry for the project's top issues, then dedups against
auto-fix PRs by matching the HTML marker `<!-- sentry-autofix: <SHORT_ID> -->`
in PR bodies (open + closed within `dedup-window-days`). That same marker is
stamped into every autofix PR opened by this workflow, closing the loop.

**Scheduling:** consumers expose `workflow_dispatch` + `repository_dispatch`
(type `sentry-autofix`) and do NOT schedule themselves. A single daily sweep in
`sre-brain` (`sentry-sweep.yml`) fetches + dedups across all projects and only
dispatches the repos that actually have an actionable candidate — so the heavy
toolchain setup runs only on days there's something to fix, not N times daily.

### Inputs

| Input | Type | Required | Default | Description |
|---|---|---|---|---|
| `sentry-org` | string | no | `sidekick-labs` | Sentry organization slug. |
| `sentry-project` | string | yes | — | Sentry project slug (e.g. `sidekick-web`). |
| `stack` | string | yes | — | One of `rails`, `ruby-gem`, `node-lib`, `node-app`, `kmp`. Drives toolchain setup for the apply step. |
| `mode` | string | no | `autofix` | `autofix` attempts a PR when triage is high-confidence; `triage-only` skips the apply step and only files a GitHub issue. All stacks (incl. `kmp`) support `autofix`. |
| `engine` | string | no | `claude` | AI engine. `claude` uses `anthropics/claude-code-action` + `claude-code-oauth-token`. `codex` uses `openai/codex-action` + the `CODEX_AUTH_JSON` subscription credential (triage via `output-schema`, apply in a `workspace-write` sandbox). The gate / validation / signed-commit / PR / issue-fallback logic is shared. See [codex-migration.md](codex-migration.md). |
| `max-candidates` | number | no | `10` | Top N Sentry issues considered before dedup filtering. Only ONE is triaged per run. |
| `issue-query` | string | no | `is:unresolved` | Sentry search query. Append modifiers (e.g. `is:unresolved level:error`) to narrow. |
| `ruby-version-file` | string | no | `.ruby-version` | Ruby version file (rails, ruby-gem stacks). |
| `ruby-version` | string | no | `""` | Explicit ruby-version override. |
| `node-version` | string | no | `lts/*` | Node version (rails, node-lib, node-app stacks). |
| `jdk-version` | string | no | `17` | JDK version for the `kmp` stack apply path (JDK + Gradle are set up so `lint-commands` like `./gradlew ktlintCheck` can run). |
| `lint-commands` | string | no | `""` | Multiline lint commands run post-apply. All must exit 0 or the fix is rejected. Keep fast — for `kmp`, prefer lint only (a full Gradle test run risks the job timeout). |
| `test-commands` | string | no | `""` | Multiline test commands run post-apply. Use a focused subset, not the full suite — runs daily. |
| `diff-line-cap` | number | no | `50` | Hard cap on total insertions+deletions in the apply diff. |
| `dedup-window-days` | number | no | `30` | How far back to scan closed PRs for the autofix marker. |
| `followup-issue-repo` | string | no | `""` | Where to file the non-autofixable GitHub-issue fallback. Empty = caller repo via `GITHUB_TOKEN` (needs Issues enabled). An `owner/name` (e.g. `sidekick-labs/sre-brain`) files in a central tracker via a cross-repo release-bot token — requires the App installed there with `issues:write`. |
| `timeout-minutes` | number | no | `30` | Job-level timeout. |
| `claude-timeout-minutes` | number | no | `15` | Advisory — composite-action steps can't enforce this; job timeout is the hard bound. |
| `additional-allowed-tools` | string | no | `""` | Comma-separated entries appended to the apply step `--allowed-tools` (e.g. `Bash(bin/rspec:*)`). **Note:** entries are passed through unsanitised, and entries like `Bash(...)` widen Claude's blast radius beyond the default `Read,Edit,Grep,Glob`. Use sparingly and prefer tightly-scoped wildcards. |
| `release-bot-client-id` | string | no | `""` | GitHub App Client ID (or numeric App ID) for the bot that opens auto-fix PRs. Required together with `release-bot-private-key` to bypass the GITHUB_TOKEN event-suppression rule and trigger CI on bot-opened PRs. See "Setting up the auto-fix bot" below. When unset, the workflow falls back to GITHUB_TOKEN — the PR opens as a draft and CI must be triggered by a human nudge (close/reopen or `git push`). |

### Secrets

| Secret | Required | Description |
|---|---|---|
| `claude-code-oauth-token` | when `engine: claude` | OAuth token for `anthropics/claude-code-action`. |
| `CODEX_AUTH_JSON` | when `engine: codex` | Codex subscription `auth.json` (org secret), supplied via `secrets: inherit`. See [codex-migration.md](codex-migration.md). |
| `sentry-api-token` | yes | **Read-scoped** Sentry token (`event:read` + `project:read`). NOT the deploy-release write token. See "Setting up `SENTRY_API_TOKEN`" below. |
| `release-bot-private-key` | no | GitHub App private key (.pem contents). Required together with `release-bot-client-id` to bypass the GITHUB_TOKEN event-suppression rule. See "Setting up the auto-fix bot" below. |

### Triggers

Consumers expose `workflow_dispatch` (manual) and `repository_dispatch` (type
`sentry-autofix`) and do **not** schedule themselves. The daily cadence is owned
by the central sweep in `sre-brain` (`sentry-sweep.yml`), which fetches + dedups
across every project and only `repository_dispatch`es the repos with an
actionable candidate. This keeps the heavy per-repo toolchain run off idle days.

### Example — Rails app (sidekick-web)

```yaml
name: Sentry Autofix
on:
  workflow_dispatch:
  repository_dispatch:
    types: [sentry-autofix]

permissions: {}

jobs:
  sentry-autofix:
    uses: sidekick-labs/.github/.github/workflows/reusable-sentry-autofix.yml@v2
    with:
      sentry-project: sidekick-web
      stack: rails
      # Open auto-fix PRs as ready-for-review via the sidekick-release-bot
      # App so CI fires automatically. Without these two, the PR opens as
      # a draft and needs a human nudge.
      release-bot-client-id: ${{ vars.RELEASE_BOT_CLIENT_ID }}
      lint-commands: |
        bin/rubocop --force-exclusion
      test-commands: |
        bin/rspec --tag ~slow
      additional-allowed-tools: 'Bash(bin/rspec:*)'
    secrets:
      claude-code-oauth-token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
      sentry-api-token: ${{ secrets.SENTRY_API_TOKEN }}
      release-bot-private-key: ${{ secrets.RELEASE_BOT_PRIVATE_KEY }}
```

### Example — Node app (sidekick-harness)

```yaml
name: Sentry Autofix
on:
  workflow_dispatch:
  repository_dispatch:
    types: [sentry-autofix]

permissions: {}

jobs:
  sentry-autofix:
    uses: sidekick-labs/.github/.github/workflows/reusable-sentry-autofix.yml@v2
    with:
      sentry-project: sidekick-harness
      stack: node-app
      node-version: '24'
      # Open auto-fix PRs as ready-for-review via the sidekick-release-bot
      # App so CI fires automatically.
      release-bot-client-id: ${{ vars.RELEASE_BOT_CLIENT_ID }}
      lint-commands: |
        npm run lint
        npm run typecheck
    secrets:
      claude-code-oauth-token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
      sentry-api-token: ${{ secrets.SENTRY_API_TOKEN }}
      release-bot-private-key: ${{ secrets.RELEASE_BOT_PRIVATE_KEY }}
```

### Example — Ruby gem (sidekick-rdp-client)

```yaml
name: Sentry Autofix
on:
  workflow_dispatch:
  repository_dispatch:
    types: [sentry-autofix]

permissions: {}

jobs:
  sentry-autofix:
    uses: sidekick-labs/.github/.github/workflows/reusable-sentry-autofix.yml@v2
    with:
      sentry-project: sidekick-rdp-client
      stack: ruby-gem
      release-bot-client-id: ${{ vars.RELEASE_BOT_CLIENT_ID }}
      lint-commands: |
        bundle exec rubocop
    secrets:
      claude-code-oauth-token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
      sentry-api-token: ${{ secrets.SENTRY_API_TOKEN }}
      release-bot-private-key: ${{ secrets.RELEASE_BOT_PRIVATE_KEY }}
```

### Example — KMP / Android (autofix)

```yaml
name: Sentry Autofix
on:
  workflow_dispatch:
  repository_dispatch:
    types: [sentry-autofix]

permissions: {}

jobs:
  sentry-autofix:
    uses: sidekick-labs/.github/.github/workflows/reusable-sentry-autofix.yml@v2
    with:
      sentry-project: sidekick-companion-kit
      stack: kmp
      # KMP sets up JDK + Gradle for the apply path. Keep the gate to lint
      # only — a full Gradle test run is too slow for a sweep and risks the
      # job timeout. Compose/KMP diffs are bounded by the same path blocklist
      # + 50-line cap + review gate as every other stack.
      release-bot-client-id: ${{ vars.RELEASE_BOT_CLIENT_ID }}
      lint-commands: |
        ./gradlew ktlintCheck
    secrets:
      claude-code-oauth-token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
      sentry-api-token: ${{ secrets.SENTRY_API_TOKEN }}
      release-bot-private-key: ${{ secrets.RELEASE_BOT_PRIVATE_KEY }}
```

### Setting up the auto-fix bot

GitHub deliberately suppresses workflow runs for events caused by `GITHUB_TOKEN`
("anti-loop"). A bot-opened PR from the autofix workflow would therefore sit
with **zero CI checks** — no required-check status to satisfy, no path to merge
without a human pushing a commit or closing/reopening the PR.

The fix is to mint a short-lived **GitHub App installation token** instead of
using `GITHUB_TOKEN` for the push + PR open. Events caused by an App token
*do* fire downstream workflows.

The org already has a `sidekick-release-bot` App configured for the same
purpose in `promote-production.yml`. Reuse it:

1. **Install the App on each caller repo** that runs the autofix workflow.
   Visit https://github.com/apps/sidekick-release-bot/installations/select_target
   → pick `sidekick-labs` org → "Only select repositories" → add the repo.
2. **Set the App's Client ID as a repo variable** (it's not a secret — Client IDs
   are public). The variable name doesn't matter; the caller workflow passes
   its value as `release-bot-client-id`.
   ```bash
   # If you don't have the Client ID, copy from the App's General Settings page
   # at https://github.com/organizations/sidekick-labs/settings/apps/sidekick-release-bot
   gh variable set RELEASE_BOT_CLIENT_ID --repo sidekick-labs/<repo> --body '<numeric-id-or-Iv1-string>'
   ```
3. **Set the App's private key as a repo secret** (or, for less per-repo
   maintenance, an org-level secret with visibility restricted to the relevant
   repos).
   ```bash
   gh secret set RELEASE_BOT_PRIVATE_KEY --repo sidekick-labs/<repo> --body "$(cat path/to/sidekick-release-bot.pem)"
   ```
4. **Wire the caller** to pass both values to the reusable workflow:
   ```yaml
   with:
     # ...
     release-bot-client-id: ${{ vars.RELEASE_BOT_CLIENT_ID }}
   secrets:
     # ...
     release-bot-private-key: ${{ secrets.RELEASE_BOT_PRIVATE_KEY }}
   ```

When **either** input is missing, the workflow falls back to `GITHUB_TOKEN`
and opens the PR as a draft with a reviewer-checklist note explaining the
manual nudge needed.

### Setting up `SENTRY_API_TOKEN`

The org-level `SENTRY_AUTH_TOKEN` secret is scoped to `project:releases:write`
for the deploy / sentry-release workflows. Issue reads need a separate
read-only token.

1. Visit https://sidekick-labs.sentry.io/settings/auth-tokens/
2. Create a **user auth token** (or, preferably, an **Internal Integration**
   under `/settings/developer-settings/new-internal/` for org-owned tokens
   that survive employee turnover).
3. Required scopes: `event:read`, `project:read`. Optional: `org:read` if you
   later want to query org-wide issue lists.
4. Add as a GitHub **organization secret** named `SENTRY_API_TOKEN`,
   visibility "All repositories" (or "Private repositories" matching the
   existing `SENTRY_AUTH_TOKEN`).
5. Verify with `gh secret list --org sidekick-labs`.

### Versioning

The `v2` tag bundles both reusable workflows. New input contracts will
land on `v2`; breaking changes will publish under `v3`. Pin to a SHA if
you need stricter immutability.
