# Migrating from Claude Code Action to OpenAI Codex

We are moving the AI-driven GitHub Actions (PR review, the `@`-mention agent,
Sentry autofix, weekly maintenance) off `anthropics/claude-code-action` onto
**OpenAI Codex** (`openai/codex-action`), authenticated with a **ChatGPT/Codex
subscription** rather than an API key.

The migration runs in parallel with the Claude workflows and is removed only
after a validation period. This document covers the foundation that everything
else depends on.

## Auth model (read this first)

Codex's subscription auth lives in an `auth.json` that contains a **refresh
token**. Codex refreshes it when it is older than ~8 days and rewrites the file.
On ephemeral GitHub-hosted runners that refresh is lost unless we persist it, so
we use a **single-writer, many-reader** design:

- **`CODEX_AUTH_JSON`** (org secret) — the seeded `auth.json`. Every Codex job
  restores it into a job-local `CODEX_HOME` via the
  [`setup-codex-auth`](../.github/actions/setup-codex-auth/action.yml) composite
  action and treats it as **read-only**.
- **[`codex-auth-refresh.yml`](../.github/workflows/codex-auth-refresh.yml)** —
  the *only* writer. Runs twice weekly (Mon & Thu), restores the secret, runs a
  trivial `codex` command (which refreshes the tokens when due), and writes the
  post-run `auth.json` back to `CODEX_AUTH_JSON`. Twice-weekly keeps the session
  comfortably under the ~8-day staleness window.
- **`CODEX_AUTH_REFRESH_TOKEN`** (org secret) — a fine-grained PAT with
  org **secrets: write**, used only by the refresher to update `CODEX_AUTH_JSON`.

> **Security note.** With subscription auth (no `openai-api-key`), the action
> does **not** start the Responses API proxy and does **not** drop sudo — both
> are gated on an API key being present. That means the Codex **sandbox mode is
> the only guardrail**. The PR-review flow therefore runs `sandbox: read-only`,
> and `auth.json` is laid down with `0600` perms in a job-local `CODEX_HOME`.

## One-time setup (manual prerequisites)

These cannot be automated and must be done before the workflows function.

### 1. Seed `CODEX_AUTH_JSON`

Use a **dedicated ChatGPT Business seat as the CI service identity** — not a
teammate's personal seat. Tying the org's automation to one person's account
means CI breaks when they rotate 2FA / leave, and shares their Codex rate-limit
pool. A dedicated seat is the org-managed, admin-controlled service account.

On a trusted machine logged into that service-identity account:

```bash
# Ensure file-based credential storage, then log in via the browser flow.
mkdir -p ~/.codex
printf 'cli_auth_credentials_store = "file"\n' >> ~/.codex/config.toml
codex login

# Verify it is a refreshable subscription credential.
jq '{auth_mode, has_refresh_token: ((.tokens.refresh_token // "") != "")}' ~/.codex/auth.json
# Expect: auth_mode == "chatgpt", has_refresh_token == true
```

Store the **file contents** as the org secret `CODEX_AUTH_JSON`
(`gh secret set CODEX_AUTH_JSON --org sidekick-labs --visibility all < ~/.codex/auth.json`).
Treat it like a password.

### 2. Create `CODEX_AUTH_REFRESH_TOKEN`

Create a fine-grained PAT scoped to the `sidekick-labs` org with
**Secrets: Read and write** (organization permission). Store it as the org
secret `CODEX_AUTH_REFRESH_TOKEN`.

### 3. Verify

Run `codex-auth-refresh.yml` via **workflow_dispatch**. Confirm the `codex` step
prints `OK`, the job's persist step reports an update, and the `CODEX_AUTH_JSON`
secret's `updated_at` advances.

> **Monitor the refresher.** It is the single point of failure for the whole
> Codex integration — if it silently stops, sessions go stale after ~8 days and
> every Codex job fails org-wide. Rely on GitHub's default failed-scheduled-run
> email at minimum, and note that scheduled workflows auto-disable after 60 days
> of repo inactivity (the `.github` repo's other crons keep it active, but
> re-enable it if GitHub ever disables it).

## Enabling Codex PR review on a repo

[`codex-code-review.yml`](../.github/workflows/codex-code-review.yml) is a
reusable workflow. To run it in a repo (alongside the existing Claude review),
add a thin caller, e.g. `.github/workflows/codex-code-review.yml`:

```yaml
name: Codex Code Review
on:
  pull_request:
    types: [opened, synchronize]
jobs:
  review:
    uses: sidekick-labs/.github/.github/workflows/codex-code-review.yml@<tag>
    secrets: inherit
```

The review runs read-only and returns its feedback as the Codex `final-message`;
a second job posts that as the PR comment via `actions/github-script`.

> **Treat AI-review output as untrusted on untrusted PRs.** The PR title/body
> feed the review prompt and the model's reply is posted verbatim, so a hostile
> PR can attempt to steer the comment (phishing links, suppressing findings).
> Mitigations are in place — `read-only` sandbox, `persist-credentials: false`,
> and the message passed via `env` (not interpolated into the `script:` body) —
> but reviewers should not auto-trust the content. These are private org repos
> with no untrusted fork PRs today; revisit if that changes.

## Release checklist: pinning the composite ref

The reusable workflows reference the [`setup-codex-auth`](../.github/actions/setup-codex-auth/action.yml)
composite as `sidekick-labs/.github/.github/actions/setup-codex-auth@main`.
`@main` is intentional during the parallel-trial phase, but it means a consumer
pinning a reusable workflow `@v*` would still get the composite at `@main`,
bypassing their pin. **When a reusable-workflow version is tagged, repin every
`setup-codex-auth@main` reference to that release tag/SHA** so the composite is
immutable and matches the workflow version. Grep before tagging:

```bash
grep -rn "setup-codex-auth@main" .github/workflows/
```

## Security: the `@codex` agent and credential exposure

`codex-agent.yml` runs Codex in `sandbox: workspace-write` against an
**attacker-influenceable instruction** (whoever triggers the `@codex` mention
controls the request body). Two facts compound:

- With subscription auth there is **no API key**, so the action skips both the
  proxy and `drop-sudo` — Codex runs as the (sudo-capable) default user.
- `workspace-write` restricts *writes* to the workspace but does **not** restrict
  *reads*, so `auth.json` in `CODEX_HOME` is filesystem-readable by the run. The
  soft prompt line *"never include secrets"* is not a real defense against a
  crafted request, and `final-message` is posted verbatim as a comment.

**What actually bounds this:** `openai/codex-action` runs an unconditional
*"Check repository write access"* step, so only actors with **write access** to
the repo can trigger a run (these are private org repos — no untrusted fork
PRs). Fork PRs can't be pushed to (`IS_FORK` guard), and the `issues` path only
ever *opens a PR* that still needs human review. So the residual risk is a
write-access collaborator exfiltrating the **shared Business-seat credential** —
an escalation over their own repo access.

**Hardening to consider before broad rollout:** restrict the agent to a smaller
`allow-users` allowlist, and/or use the **API-key auth path for the agent
specifically** (there the action drops sudo and isolates the key behind its
proxy, so a prompt-injected read can't reach a plaintext credential). Tracked as
a follow-up, not a phase-2 blocker.

## Remaining phases (tracked separately)

- `codex-agent.yml` — the `@codex` mention responder (workspace-write +
  signed-commit-via-API + comment), with per-repo `codex.yml` wrappers.
- `engine: codex` branch in `reusable-weekly-maintenance.yml` (structured
  triage via `output-schema`, dependency updates as shell steps). NOTE: the
  sentry-autofix engine moved to `sre-brain/sentry-autofix-engine.yml`
  (one-workflow-per-org, claude-only — the codex branch was deliberately
  dropped there; re-add in sre-brain if/when the codex migration resumes).
- Per-repo wrapper rollout, then removal of the Claude workflows and the
  `CLAUDE_CODE_OAUTH_TOKEN` secret.
