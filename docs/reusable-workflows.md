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
- **`skills-portability.yml`** — reusable PR gate that fails when a committed
  `.claude/` or `.agents/` file references a skill by filesystem path instead of
  invoking the plugin skill. See [Skills portability](#skills-portability).
- **`sentry-fix-trailer.yml`** — ADVISORY PR check that posts a sticky comment
  when a PR references a Sentry short-code (`SIDEKICK-WEB-1C`) but neither the
  PR description nor a commit carries `Fixes <code>`, so the Sentry issue would
  not auto-resolve.
  See [Sentry fix-trailer guard](#sentry-fix-trailer-guard).
- **`docs-hygiene.yml`** / **`docs-hygiene-drift.yml`** — the docs-shape
  convention's enforcement: a PR gate (links, backticked paths, frontmatter,
  index, orphans; warn → block ratchet) and a weekly report-only `covers:` drift
  report. See [Docs hygiene](#docs-hygiene).

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
- **`pin-sweep.yml`** (ACTUATOR, runs in `sidekick-labs/sre-brain`) — weekly
  (+ `workflow_dispatch`) self-healer.
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

## Skills portability

`skills-portability.yml` fails a PR when a committed `.claude/` or `.agents/`
file tells the agent to **read a skill off somebody's local disk**.

The motivating bug: `/ship` in five of eight repos ended its babysit hand-off
with

```
Read `~/Workspace/sidekick-labs/.claude/skills/babysit/SKILL.md` and follow its loop
```

The workspace root is not a git repo, so nothing distributes that file — it
resolved on one laptop. Everywhere else the step read a non-existent file and the
babysit loop (CI-fixing, review-addressing, the autonomous merge under workspace
Rule #7) silently never started, while `/ship` still printed a PR URL and
reported success. Nothing went red for months. The shared skills are distributed
as the `sidekick-workflows` marketplace plugin, so a cross-repo skill reference
belongs to the plugin (`/babysit`), never to a path.

**Scope is deliberately narrow:** only home-relative or absolute paths pointing
into a `.claude/skills` or `.agents/skills` tree. Repos on estate-sync keep their
skills in `.agents/skills` (`.claude/skills` is a symlink to it), so the default
scope covers every tracked file under either tree. It does *not* flag every
`~/` or `/Users/` string under `.claude/` or `.agents/`, because some are
legitimate (a documented devcontainer `REMOTE_PATH` default, a table of env-var
defaults). `settings.local.json` is skipped — per-developer machine state, not an
instruction. A tracked directory link (the `.claude/skills` symlink) is skipped
only when it resolves inside the repo's own `.claude/` or `.agents/` tree; a link
elsewhere, or a file `grep` can't read, makes the result UNKNOWN (exit 1).

**It carries its own controls.** Each run first asserts the matcher still flags
the known-bad line and still passes the correct plugin wording; either control
failing makes the result UNKNOWN (exit 1), never "clean"
(`check-positive-controls.md`). A repo with no tracked `.claude/` files reports
"vacuously clean — this gate asserted nothing here" rather than a silent pass.

### Per-repo adoption

```yaml
# .github/workflows/skills-gate.yml in the consumer repo
name: Skills Gate
on:
  pull_request:
    types: [opened, synchronize, reopened]
permissions: {}
jobs:
  skills-portability:
    permissions:
      contents: read
    # @main is deliberate for now, matching pin-check adoption: pin to a tag
    # once a v3 or later cuts.
    uses: sidekick-labs/.github/.github/workflows/skills-portability.yml@main
```

## Docs hygiene

`docs-hygiene.yml` enforces rule 5 of the
[docs-shape convention](https://github.com/sidekick-labs/octo-brain/blob/main/.claude/conventions/docs-shape.md)
(decision: sidekick-labs/octo-brain#573). It runs a zero-dependency Node script,
`.github/actions/docs-hygiene/docs-hygiene.mjs`, over `CLAUDE.md`, `AGENTS.md`,
`docs/**`, `.claude/**` and `.agents/**`:

| Check | Finding codes |
|---|---|
| links | `broken-link`, `broken-path`, `broken-path-glob` |
| frontmatter (`docs/**/*.md`, README indexes excepted) | `frontmatter-missing`, `frontmatter-unparseable`, `frontmatter-key-missing`, `frontmatter-bad-type`, `frontmatter-bad-status`, `frontmatter-bad-verified`, `frontmatter-bad-covers`, `covers-unmatched` |
| index (a `\| Doc \| Read when \|` table, or a bullet list under a "Read when relevant" heading) | `index-missing`, `index-too-long`, `read-when-table-missing` (the code keeps its name for either form) |
| orphans | `orphan-doc` |
| drift (`docs-hygiene-drift.yml` only) | `covers-drift` |

**Exit codes:** 0 clean (or any findings in warn/drift mode), 1 findings in block
mode, 2 usage error, 3 UNKNOWN (not a git work tree, unreadable, shallow clone
for drift). Every non-zero fails the job.

**Backticked paths are a heuristic,** tuned against 11 code repos. A span is
treated as a path only when it contains `/`, has no whitespace or placeholder
syntax, and starts with a top-level entry of the repo. It is skipped when its
line (or the line before) names another repo or speaks in the past tense or the
negative ("removed", "not under"), when it is gitignored, or when the doc is
point-in-time (`type: adr`/`report`, `status: archived`/`superseded`, or an
ADR/reports folder before frontmatter lands).

**Intentionally missing paths** (a removed file a doc names on purpose, a
directory created only at deploy time, a local secret location) are allowed in
one of three scopes, narrowest first:

- one line: `<!-- docs-hygiene-ignore -->` on that line;
- one doc: `<!-- docs-hygiene: allow-missing config/attestation_roots/ app/services/claim_resolver.rb -->`
  anywhere in the doc (entries are exact paths, `dir/` prefixes, or globs);
- the repo: `.docs-hygiene.yml` with `allow-missing: [config/attestation_roots/]`.
  A file the script can't read as that list makes the run UNKNOWN, not clean.

**It carries its own controls.** Every run starts with `--self-test` against
`tests/fixtures/docs-hygiene/`: `clean` must produce no findings, each bad
fixture exactly its own, and every finding code must fire at least once.
`test-docs-hygiene.yml` also drives the composite action against materialised
fixtures and asserts block mode goes red on a broken tree.

### Per-repo adoption

```yaml
# .github/workflows/docs-hygiene.yml in the consumer repo
name: Docs Hygiene
on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
permissions: {}
concurrency:
  group: docs-hygiene-${{ github.event.pull_request.number }}
  cancel-in-progress: true
jobs:
  docs-hygiene:
    permissions:
      contents: read
    uses: sidekick-labs/.github/.github/workflows/docs-hygiene.yml@main
    with:
      mode: warn   # flip to `block` after one warn-only cycle (the ratchet)
```

```yaml
# .github/workflows/docs-drift.yml in the consumer repo
name: Docs Drift Report
on:
  schedule:
    - cron: '23 1 * * 1'   # weekly, Monday 01:23 UTC
  workflow_dispatch:
permissions: {}
jobs:
  docs-drift:
    permissions:
      contents: read
    uses: sidekick-labs/.github/.github/workflows/docs-hygiene-drift.yml@main
    with:
      brain-repo: sidekick-labs/core-platform-brain   # the repo's owning brain
    secrets: inherit   # SIDEKICK_RELEASE_BOT_PRIVATE_KEY, to mint the brain token
```

**The drift issue.** With `brain-repo` set, each run also files the report as
ONE issue per repo in that brain (rule 5), written by
`.github/actions/docs-hygiene/drift-issue.mjs`:

- title `[docs-drift] <repo>: N docs changed since verified`, label `docs-drift`,
  and a table of doc, `covers:` globs, commits since verified, verified date and
  last commit;
- found again by label plus the title prefix `[docs-drift] <repo>:` among open
  issues, and refreshed in place (title and body), so reruns never duplicate it;
- closed with a comment when drift reaches zero; a later drift opens a new one.

The brain write uses a release-bot App token scoped to `issues: write` on that
brain only, minted from `SIDEKICK_RELEASE_BOT_PRIVATE_KEY` (the credential the
sidekick-system-tests failure sink already uses). Filing stays report-only: a
caller the key isn't shared with (public repos, per sre-brain#642), or any API
error, gets a `::warning::` and the step summary, never a red run. A failed read
of the brain's issues is "could not tell", so nothing is filed that run rather
than risk a duplicate. Only the scan's exit 3 (UNKNOWN) fails a drift run.

`dry-run: true` prints the would-be title and body instead of writing; locally:

```bash
node .github/actions/docs-hygiene/docs-hygiene.mjs --drift --root ../sidekick-web --json-out /tmp/drift.json
GH_TOKEN=$(gh auth token) node .github/actions/docs-hygiene/drift-issue.mjs --dry-run \
  --report /tmp/drift.json --repo sidekick-labs/sidekick-web --brain sidekick-labs/core-platform-brain
```

With a token the dry run also reads the brain and says whether it would create,
refresh or close. `drift-issue.mjs --self-test` is its positive control (an
in-memory GitHub), run by the action whenever `brain-repo` is set and by
`test-docs-hygiene.yml`.

**Refs.** Both workflows call the composite action at `@v3`, which
`advance-major-tag.yml` moves to every new `main` commit. So the action lags the
workflow by at most one push to `main`, and a caller pinned to `@main` can briefly
run a newer workflow against the previous action. Keep workflow/action changes
backward compatible across one release (e.g. add a new `mode` to the action
before a workflow passes it).

Run it locally against a checkout:
`node .github/actions/docs-hygiene/docs-hygiene.mjs --root ../sidekick-web --warn-only`
(add `--json` for machine output, `--drift` for the drift report).

## Sentry fix-trailer guard

`sentry-fix-trailer.yml` catches a fix that will silently fail to resolve its
Sentry issue. Sentry resolves an issue when a merged commit message, or the PR
description, carries `Fixes <SHORT-CODE>` (also Resolves/Closes). A PR that only
closes its tracker issue leaves the Sentry issue `unresolved`. The guard reads
mentions and `Fixes` lines from the PR description (re-read through the API, so
a re-run sees the current body) and from the commits, and upserts one sticky
comment (marker `<!-- sentry-fix-trailer-guard -->`) listing the gap. It deletes
the comment once every mentioned code is covered.

**The PR description is where the line belongs.** The Sentry repos squash-merge
with `squash_merge_commit_message=PR_BODY`, so the commit on main is the PR
title plus description, and branch commit messages are discarded. The harness
original (sidekick-harness#847) counted only commit trailers and said the opposite; that was
fixed when the guard moved here (octo-brain#558). Commit messages still count,
for a rebase merge.

It is **advisory and never blocks**: a PR may legitimately mention an issue it
does not fix. Do not make it a required check. Every comment write is wrapped so
a read-only fork token degrades to a `::warning::` instead of a red run.

The Sentry project prefix list lives only in this workflow. Keep it in sync with
sre-brain `sources.yaml sentry.projects`. Logic tests:
`tests/sentry-fix-trailer-logic.py` (run by `test-sentry-fix-trailer.yml`).
Born inline as sidekick-harness#847, moved here for octo-brain#558.

### Per-repo adoption

```yaml
# .github/workflows/sentry-fix-trailer.yml in the consumer repo
name: Sentry fix-trailer check
on:
  pull_request:
    types: [opened, synchronize, reopened, edited]
permissions: {}
concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true
jobs:
  sentry-fix-trailer:
    name: Sentry fix-trailer check
    permissions:
      contents: read
      pull-requests: write
    uses: sidekick-labs/.github/.github/workflows/sentry-fix-trailer.yml@main
```

The check reports as `Sentry fix-trailer check / Sentry fix-trailer check`.
Its runner follows the caller's `vars.RUNNER_LABEL`, else `ubuntu-latest`.
