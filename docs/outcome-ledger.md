# CI-agent outcome ledger (EVAL-010)

Convention rule 8 in rarebit-sre's
[`llm-evals.md`](https://github.com/rarebit-one/rarebit-sre/blob/main/.claude/conventions/llm-evals.md)
says a CI agent that writes machine-readable output gets a scheduled count of its
verdicts against what actually happened. `.github/actions/outcome-ledger` is that
count for the sidekick-labs CI agents. Each repository runs it with its own
`GITHUB_TOKEN`. No cross-org credential is involved and no model is called.

| agent | verdict | outcome read from |
|---|---|---|
| `claude-review` | PASS / BLOCKING, re-derived from the reviewer's comment with `derive_verdict.py` and cross-checked against the run's conclusion | `/review-ack` (acked_fixed / acked_false_positive), a later push's verdict, merged over BLOCKING, reverts of merged PASS PRs |
| `post-deploy-triage` | `<verdict>/<confidence>` from the `triage-verdict` artifact (or the PR comment for older runs) | reverts of the deployed SHA or its PR, later commits touching `suspect_files` |
| `post-deploy-failure-triage` | `<category>/<confidence>` | as above, plus a later successful deploy of the same SHA |
| `autofix` | the `auto-fix/<label>-<sha>` draft PR | merged, closed unmerged, reverted |

**Unknown is never good.** These count as `unknown`: an open PR, a good-looking
outcome younger than `settle_days` (7), a verdict its own check contradicts, and a
triage verdict with nothing to follow. For every agent,
`sum(outcomes) + unknown == verdicts`.

**Self-test.** `ledger.py --self-test` runs before every compute. It runs the
classifier over a planted repository with known wrong verdicts, and the job fails
unless each one lands in its bad bucket. `tests/outcome-ledger.py` breaks the
classifier eight ways and requires the self-test to go red each time.

**Shape** (schema 1, shared with the fundbright and luminality ledgers; the audit
merges them):

```json
{"schema": 1, "repo": "<org>/<repo>", "generated_at": "<ISO>", "window_days": 30,
 "agents": [{"agent": "…", "verdicts": 0, "by_verdict": {}, "outcomes": {},
             "unknown": 0, "notes": "…"}]}
```

`claude-review` adds one key on top of that shape, `acks`, so readers of
schema 1 that ignore unknown keys are unaffected:

```json
"acks": {"total": 2, "by_pr_author": 1, "not_pr_author": 1, "pr_author_unknown": 0,
         "self_ack_share": 0.5, "by_acker": {"<login>": {"total": 2, "by_pr_author": 1}}}
```

It counts the valid `/review-ack`s behind the acked_* outcomes. The PR author may
acknowledge their own findings (Jaryl's ruling of 18 Nov 2026), so `by_pr_author`
counts those self-acks, and `self_ack_share` is `by_pr_author` divided by the acks
whose PR author is known (`null` when there are none). A high share means the
override is mostly self-certification, not a second pair of eyes. The agent's
`notes` repeat this.

The settle window stays at 7 days for now. That has not been ruled on.

**Caller** (one per repo, weekly plus `workflow_dispatch`): a `compute` job with
`contents`, `pull-requests` and `actions` read, then a `publish` job with
`contents: write` only. The publish job commits `ledger/<YYYY-MM-DD>.json` and
`ledger/latest.json` to the unprotected `outcome-ledger` branch through the git
data API, creating it as an orphan the first time. `main` requires PRs, which is
why the ledger lives on its own branch.

**Local dry run** (read-only):
`GH_TOKEN=… python3 .github/actions/outcome-ledger/ledger.py compute --repo sidekick-labs/<repo> --pace 1 --facts-out facts.json`.
`--facts-in facts.json --now <ISO>` recomputes the same ledger offline.
