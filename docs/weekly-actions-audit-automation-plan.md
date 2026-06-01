# Weekly Actions Audit Automation — Plan

Status: **deferred** (not started)
Owner: jaryl
Drafted: 2026-05-28 · Rewritten: 2026-06-02 to reconcile against the now-shipped weekly-maintenance machinery
Tracking issue: [octo-brain#9](https://github.com/sidekick-labs/octo-brain/issues/9)

## Goal

Run the existing `/actions-audit` skill on a weekly cadence across the sidekick-labs org, and have it auto-open PRs for high-confidence, deterministic CI fixes. Human merges remain the gate (workspace rule #7).

## What changed since the original draft (read this first)

The original plan assumed we'd build the auto-PR machinery from scratch (`recommendations.py`, a bespoke `weekly-actions-audit.yml`, hand-rolled idempotency, committed dated reports). Most of that now **already exists** in `sidekick-labs/.github`:

- **`.github/workflows/reusable-weekly-maintenance.yml`** (PR #5, ~670 lines) already does: AI-engine-driven fixes that open a signed PR, a **TODO/FIXME census with week-over-week deltas via `actions/cache`** (not committed reports), CodeQL alert review, and a **Linear fallback** for judgment-call items (`linear-fallback` input). It is **per-repo** — each repo's own workflow calls it with `secrets: inherit`.
- The AI engine is now **configurable: `engine: claude | codex`** (Claude→Codex migration in flight — see memory `project_codex_migration`). Anything we build MUST take an `engine` input, not hardcode `anthropics/claude-code-action`.
- Model is pinned via `CLAUDE_MODEL` / `CODEX_MODEL` Actions vars with literal fallbacks (`claude-opus-4-8`).

**Implication:** don't rebuild primitives. The actions-audit is the one thing weekly-maintenance does NOT cover, because it is **org-level / cross-repo** (it analyses Actions *usage* across all repos), whereas weekly-maintenance is per-repo dependency/security hygiene. So actions-audit is a **separate org-level scheduled workflow** in `.github`, but it should **reuse weekly-maintenance's conventions and proven steps** rather than reinvent them.

## Architecture (revised): org-level scheduled workflow in `.github`, reusing weekly-maintenance conventions

Confirmed still correct from the original draft: **Option B (`.github` repo + GitHub Actions)** over a remote `/schedule` agent or mac-mini-1 cron — auditable in the Actions UI, GH-signed commits (no host 1Password dependency), no mac-mini/SSH-tunnel reliance, natural org home. The weekly-maintenance workflow validates this choice in practice.

### Layout

```
sidekick-labs/.github  (org repo)
├── .github/workflows/
│   └── weekly-actions-audit.yml       # cron + workflow_dispatch — ORG-LEVEL sweep (NOT a reusable per-repo workflow)
├── audit/
│   ├── audit.py                       # ported from /actions-audit skill
│   └── recommendations.py             # partitions findings: auto-PR-able vs Linear/report-only
└── (NO committed .actions-reports/ — use actions/cache for WoW deltas, mirroring weekly-maintenance's census cache)
```

### Conventions to inherit from `reusable-weekly-maintenance.yml` (don't reinvent)

| Concern | What weekly-maintenance already does — copy the pattern |
|---|---|
| AI engine | `engine: claude \| codex` input; validate-secrets step; `CLAUDE_MODEL`/`CODEX_MODEL` env with literal fallback. Actions-audit must be engine-agnostic too. |
| Judgment-call items | `linear-fallback` input → opens a Linear issue instead of dropping risky/non-deterministic findings. This **replaces** the original "report-only markdown" idea — route report-only findings to Linear, not a committed `.md`. |
| WoW deltas | `actions/cache` keyed by `github.repository_id` stores the previous census; diff against it. Use the same approach for audit metrics — **drop the committed dated `.md` report idea.** |
| PR creation | Agent prompt ends with "You MUST create a PR with fixes when changes are produced"; commits as `github-actions[bot]`, GH-signed. |
| Pinned actions | `actions/checkout@v6`, `create-github-app-token`, etc. are pinned by SHA — match that. |

### Cross-repo auth

actions-audit writes to *many* target repos, so it needs broader auth than weekly-maintenance (which runs inside each repo with `github.token`). Mint an installation token via `actions/create-github-app-token` from an org-wide GitHub App (perms: `contents:write`, `pull-requests:write`, `actions:read`, `metadata:read`).

## Weekly Run Flow

1. Cron fires (Monday ~09:00 SGT) → workflow checks out `sidekick-labs/.github`.
2. Mint installation token via `actions/create-github-app-token`.
3. Run `audit/audit.py --scope auto --window 7` → produces findings JSON.
4. `audit/recommendations.py` partitions findings:
   - **Auto-PR-able** (safe, deterministic patterns) → start with **only**: missing `concurrency:` block on a workflow with ≥10% cancel rate. Expand later (paths filters; `runs-on: macos-*` → `ubuntu-latest` when no macOS-only steps).
   - **Judgment-call** (flake root-causes, job reordering, anything needing config-reading) → **open a Linear issue** via the same `linear-fallback` mechanism weekly-maintenance uses (not a committed report).
5. For each auto-PR-able recommendation:
   - Stable rec ID = hash(repo, workflow file, change type); branch `actions-audit/<rec-id>`.
   - Idempotency: skip if that branch OR an open PR with that head already exists.
   - Clone target repo with the installation token.
   - Invoke the configured engine (`claude` or `codex`) with the recommendation + cloned repo.
   - **Required prompt instruction (Skill Rule #1):** read the current YAML; if the fix is already applied, abort and emit a "false positive" note; only edit when verifiably missing.
   - Open a GH-signed PR templated from the recommendation.
6. WoW metrics deltas via `actions/cache` (mirror the census-cache step); surface in `$GITHUB_STEP_SUMMARY`.
7. Post a Slack summary linking the run + opened PRs + Linear issues (@-mention relevant people — memory `feedback_slack_mentions`).

## Setup Checklist

| Piece | Notes |
|---|---|
| `sidekick-labs/.github` repo | **Exists** (verified 2026-06-02). |
| GitHub App | `contents:write`, `pull-requests:write`, `actions:read`, `metadata:read`; installed org-wide. Check whether the weekly-maintenance rollout already created a suitable App before making a new one. |
| Secrets in `.github` repo | `APP_ID`, `APP_PRIVATE_KEY`; engine creds (`CLAUDE_CODE_OAUTH_TOKEN` — never `ANTHROPIC_API_KEY`, memory `feedback_oauth_token_only`; or `CODEX_AUTH_JSON` for codex); `LINEAR_API_KEY`; `SLACK_WEBHOOK_URL`. |
| Auto-PR allowlist | Start with `concurrency:` block addition only. Expand once a few weeks of clean output build trust. |
| `audit.py` port | Parameterize paths (`$GITHUB_WORKSPACE`), use `GH_TOKEN` env instead of local `gh auth`. |

## Risks & Mitigations

1. **Skill Rule #1 in CI.** Prompt MUST verify the fix isn't already present before editing, or we get "add concurrency block" PRs against workflows that already have one. Mitigation: verification step in the prompt + cheap pre-check in `recommendations.py`. (weekly-maintenance's prompt has the same "verify before edit" discipline — copy its phrasing.)
2. **Engine drift.** Hardcoding claude-code-action will rot mid-migration. Mitigation: take an `engine` input exactly like weekly-maintenance.
3. **The audit workflow is itself a workflow.** It'll appear in next week's audit. Exclude `sidekick-labs/.github` from scope, or accept ~2 min/week.
4. **Stale bot PRs.** Idempotency prevents re-opening; consider auto-close-after-N-weeks if PRs go stale.
5. **CI runner cost.** ~5–10 min/week total. Acceptable.
6. **Workspace rule #7 still applies.** Nothing here merges. PRs land in target repos, run their CI, wait for human merge.
7. **GitHub App key rotation.** Document storage + rotation before going live. Reuse the weekly-maintenance App's rotation runbook if it shares the App.

## Suggested Build Order (when un-deferred)

1. Decide: reuse the weekly-maintenance GitHub App, or create a dedicated audit App. (Check existing App perms first.)
2. Port `audit.py` into `.github/audit/` with parameterized paths + `GH_TOKEN`.
3. Write `recommendations.py` with `concurrency:` as the sole auto-PR pattern; route everything else to Linear.
4. Write `weekly-actions-audit.yml` with `workflow_dispatch` first (no cron), `engine` input, copying weekly-maintenance's validate-secrets / engine-selection / cache / PR steps.
5. Dry-run end-to-end on a single repo; review the generated PR manually.
6. Enable cron once a clean dry-run produces a useful PR.
7. Add auto-PR patterns one at a time, each gated by ≥2 weeks of clean output.

## Open Questions

- Slack channel for the weekly summary? (Default: wherever CI alerts already land.)
- Reuse the weekly-maintenance GitHub App or provision a dedicated one for cross-repo audit writes?
- Cap on auto-PRs per week? (Suggest: 5 max, queue the rest.)
- Exclude `sidekick-labs/.github` from its own audit scope, or include for completeness?
```
