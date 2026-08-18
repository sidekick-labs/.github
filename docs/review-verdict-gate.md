# The review verdict gate

`review / Claude Code Review` used to answer one question — **did a review run?** —
and concluded `success` either way once one had. A review that found a null
dereference and a review that found nothing produced the same green tick.

Under workspace rule #7, merging in this estate is autonomous *because* the check
set is dense. A check that cannot go red on a real defect is a gate in name only,
so this is the `check-positive-controls.md` defect class one level up from
`core-platform-brain#340`: #340 was a green over a review that never ran; this was
a green over a review that ran **and objected**.

The gate now answers a second question — **what did the review say?**

## Contract

| situation | job conclusion |
|---|---|
| review raised a **correctness or security** finding | **fail** |
| review raised only simplification / style / nit / advisory findings | pass |
| review raised nothing | pass |
| review could not run (both attempts) | fail (`Verify a review actually ran`) |
| the action returned without calling the model (workflow-validation no-op) | **fail**, classified distinctly |
| verdict undeterminable — no marker, stale marker, unreadable comments | **fail (closed)** |
| lane skipped — dependabot, fork, mechanical PR | pass, with a durable reason |

## Where the logic lives, and why it is not in the workflow

`claude-code-action` exchanges its OIDC token for a GitHub App token at
`api.anthropic.com/api/github/github-app-token-exchange`. That endpoint validates,
**server-side**, that the workflow file the job runs from exists on the repository's
**default branch with identical content**. On a mismatch it returns
`workflow_not_found_on_default_branch`; the action catches it, logs *"Skipping
action due to workflow validation"*, and **returns — leaving the step concluding
`success` having done nothing.**

That condition is **false by construction on any PR that edits the workflow
carrying the action.** Under the old contract it produced a silent false green:
`.github#127` merged that way, with six *"Claude encountered an error"* comments and
a green tick. Under the verdict contract it correctly goes **red** — which would
have made every future edit to the review unmergeable.

So the orchestration lives in **`.github/actions/claude-review/action.yml`**, and
`.github/workflows/claude-code-review.yml` is a thin shim that only decides the job
runs and passes three values a composite cannot resolve (`secrets`, `vars`,
`github.token`). Editing review logic no longer edits the validated file.

**What was confirmed, and how** — measured against real runs, not inferred:

| claim | evidence |
|---|---|
| validation is server-side, on the OIDC exchange | action's own `src/github/token.ts` at the pinned SHA |
| it is **not** "any changed workflow file" | `#146` changed `pin-check.yml` → 0 validation failures, 2 real review comments |
| it **is** the workflow carrying the action | `#127` changed only `claude-code-review.yml`, not the caller `pr.yml`, and hit the guard |
| **composite actions are not validated** | `#145` + `#147` changed `.github/actions/gh-deployment-start/action.yml` → 0 validation failures, real reviews posted |

The last row is what the refactor rests on. Note the checks on `#145`/`#146`/`#147`
all read `SUCCESS`, which under the old contract meant nothing — so each was
verified by reading the run log for validation failures *and* confirming a review
comment exists, not by trusting the green.

**Two consequences to keep in mind:**

- The shim references the composite at a **moving major tag** (`@v3`, auto-advanced
  on every push to `main` by `advance-major-tag.yml`; first-party `@vN` is allowed
  by `pin-check`). So a PR editing the composite is reviewed by the version on
  `main`, not by its own. `tests/review-gate-exit-codes.py` is therefore the real
  gate on composite changes, and it runs against the PR's copy.
- **Editing the shim still trips the guard.** That is unavoidable and is why the
  shim is near-frozen. Such a PR needs a human review and an admin merge.

### Landing this: the bootstrap gap

The shim resolves the composite at `@v3`, and that tag cannot point at a file that
exists only on a branch. So on the introducing PR the step fails at action
resolution (*"Can't find 'action.yml' … @v3"*) and **the composite gets no live
exercise before merge** — its first real execution is post-merge, in ~21 repos at
once.

That gap is structural, not an oversight, and it is mitigated two ways:

1. `tests/review-gate-exit-codes.py` gained a **composite validity** section that
   catches exactly the class of error placement makes invisible — parses the
   action, asserts `using: composite`, asserts every `run` step declares a `shell`
   (the most common composite authoring error, and one that fails at *run* time,
   not parse time), asserts every `uses` is SHA-pinned or first-party, asserts no
   `secrets.`/`vars.` reference (those resolve to **empty** inside a composite
   rather than erroring, so a blank model id or a "missing" token would look like
   an outage), and asserts every `inputs.*` read is declared.
2. **It is being landed as two PRs**, which removes the risk rather than bounding
   it. **PR A** adds only the composite (leaving the workflow untouched, so
   `claude-code-action` validates fine, the review lane reviews it normally, and
   merging advances `v3` to a commit where the composite exists). **PR B** then
   flips the shim to resolve it. Only B trips the guard, and only B needs the
   admin merge.

   Until B lands, the workflow carries its own duplicate copy of the
   orchestration. That duplication is deliberate and short-lived — folding the
   shim change into A would have made A unreviewable, which is the whole problem
   being solved.

The no-op is now classified rather than lumped in with a missing marker: the
verdict step reads the action's `conclusion` output, which the source shows is set
only *after* the model call, so an empty conclusion on a `success` step is the exact
signature of "the action returned without running". It stays **red** — a sharper
message, not a new green.

## Mechanism

The reviewer already posts its findings to the PR with `gh pr comment`. The prompt
now requires that comment to end with exactly one machine-readable marker:

```
<!-- claude-review-verdict: BLOCKING run=<run_id>-<run_attempt> -->
<!-- claude-review-verdict: PASS     run=<run_id>-<run_attempt> -->
```

A deterministic step (`Determine the review VERDICT`) reads the PR's comments and
maps the marker onto the job's exit code.

Three properties are deliberate:

- **No second review pass.** The verdict rides the artefact a human reads anyway.
  PR review is already the estate's largest single consumer of shared Claude
  capacity; a parallel adjudication pass would double it to answer a question the
  first pass already knew.
- **No new tool grants.** The reviewer stays read-only plus `gh pr comment`, so the
  fork-safety and prompt-injection posture is unchanged. It is never given `Write`.
- **The run nonce is load-bearing.** Without it the verdict would attach to the *PR*
  rather than to *this run's diff*, and a PASS from an earlier push would certify a
  later, defective one. A marker for a different run does not count.

## Skips stay green, and stay legible

A skipped lane must still conclude `success`. The skip is **step**-gated, never
job-gated: a skipped *job* reports its status context as not-satisfied, so a
*required* check would stay forever-pending and auto-merge would hang. That
property predates this change and must be preserved.

Because a green here now arms auto-merge, every skip leaves a record that outlives
the run:

- **Mechanical PRs** (bot-authored `sync/`, `weekly/`, `cycle/`, `quarterly/`,
  `direction/`) get a **PR comment**, idempotent per head SHA, plus a `::notice`
  and the step summary. Volume is low by construction and this is the one path
  where a PR merges with no review at all, so the reason belongs next to the merge
  it authorised. Posting is best-effort — a caller missing `pull-requests: write`
  gets a warning, never a failed gate.
- **Fork / dependabot PRs** get a `::notice` on the check plus the step summary, but
  no comment: dependabot volume would make it noise, and those PRs are decided by
  semver plus the repo's own tests.

Policy for what may skip at all: `.claude/conventions/mechanical-pr-review-skip.md`.

## Positive control — required before trusting this green

`tests/review-gate-exit-codes.py` (run by `test-review-gate.yml`) pins the exit-code
contract against fixtures: BLOCKING → 1, PASS → 0, advisory-only → 0, stale nonce →
1, absent marker → 1, unreadable comments → 1, unrecognised value → 1,
BLOCKING+PASS → 1. Its own ability to go red was checked by mutation — flipping the
BLOCKING branch to `exit 0` fails three checks.

**That proves the PARSER. It says nothing about the CONSEQUENCE** — that a real
correctness bug actually makes the reviewer emit `BLOCKING`. Per
`check-positive-controls.md`, a check whose green gates a decision needs one live
positive before its green is evidence. Force one, once, and record the run URL here.

### How to force one

1. Branch off `main` in this repo (`.github`) — its own `pr.yml` calls the reusable,
   and `Claude Code Review / Claude Code Review` is already required on its `main`,
   so no ruleset change is needed to observe the behaviour.
2. Add a file with **one unambiguous correctness defect** — not a style question.
   Something that is a defect on its face and needs no repo context, e.g. a shell
   helper that returns success on an unreadable input, or an off-by-one in a loop
   bound with the intended behaviour stated in a docstring above it. Say in the PR
   body that it is a deliberate positive control for the verdict gate.
3. Open the PR and watch the `Claude Code Review` check.

Expected, and all three must hold:

- the review comment lands and its findings are severity-prefixed;
- its last line is `<!-- claude-review-verdict: BLOCKING run=… -->` with **this**
  run's id;
- the check concludes **failure**, with the `Claude Code Review — BLOCKING findings`
  step summary.

4. Then push a commit fixing only that defect and confirm the same PR flips to a
   `PASS` marker and a green check. That second half matters: a gate that reds on
   everything is as uninformative as one that greens on everything.
5. Close the PR without merging, and paste both run URLs into the table below.

| date | run (BLOCKING) | run (PASS after fix) | by |
|---|---|---|---|
| _pending — this gate's failure path has NOT yet been observed live_ | | | |

Until that row is filled in, treat a green `Claude Code Review` as "a review ran and
did not object", **not** as "the blocking path works".

## The caller contract — "always reports, exactly once"

A required check is only as good as its guarantee that it *reports at all*. Two
hangs had to be closed before this context could be required; both are now fixed,
and both fixes are pinned by `tests/review-gate-exit-codes.py`.

### Hang 1 — `paths-ignore` suppressed the trigger

Every caller carried a `paths-ignore` list (`**/*.md`, `docs/**`, lockfiles,
`gradle/wrapper/**`, `sorbet/rbi/**`, `.github/dependabot.yml`). A PR touching only
those paths never triggered the workflow, so **no check run was created at all** and
a required context would sit at *"Expected — waiting for status to be reported"*
forever. Not theoretical: 3 of the last 30 merged PRs in `sidekick-companion-kit`
(#563, #558, #557) and 3 of the last 50 in `sidekick-inference` were entirely
within the ignore list.

**Fix: the list moved into the reusable**, as a step-gated
`Classify — is there anything here to REVIEW?` skip. The trigger now always fires,
the job always runs, the context always reports — and the saving is kept, because
the skip happens before any Claude call.

The alternative — a second, path-filtered no-op job reporting the same context —
was rejected on a hard technical ground, not taste. GitHub path filters are
*any-file-matches*, so `paths:` and `paths-ignore:` are **not complements**: a PR
touching one docs file *and* one code file matches **both** workflows. Two jobs
would report one context and a no-op green could land after (or instead of) a real
review. There is no filter expression that makes them mutually exclusive, so
"exactly once" is unachievable that way.

Semantics are reproduced exactly: GitHub skips only when **every** changed file
matches, so one reviewable file anywhere re-arms review. One deliberate tightening —
`.github/dependabot.yml` is no longer skippable, because `.github/**` is already a
hard never-skip invariant (the workflow *is* the gate) and two skip classes
disagreeing about it is the drift this consolidation removes. A tightening can only
cause more review, never a false green.

This is **not** a mechanical skip and is named differently on purpose. The
mechanical three-part test asks whether a *deterministic gate already provides the
guarantee review would provide*; nothing asserts a docs change is correct. The
honest, narrower claim is that these paths carry no code for the reviewer to reason
about, and have been excluded since long before the check was a gate. Widening the
list is a policy change, not a maintenance edit.

### Hang 2 — the draft transition (`sidekick-web`)

The reusable job-gates on `draft == false`. That is **deliberate and stays**: a
draft cannot merge, and reporting a green *"skipped: draft"* would be a false green
sitting there ready to be inherited the moment the PR is marked ready. The
trade-off only holds if the ready transition itself fires an event.

`sidekick-web`'s caller filtered exactly that event for humans
(`if: github.event.action != 'ready_for_review' || …user.type == 'Bot'`), so a human
PR opened as a draft and later marked ready, with no subsequent push, got the
context from neither event. `.github`'s own `pr.yml` was one event short of the same
hang — it did not list `ready_for_review` at all.

**Fix:** drop the caller-side `if:`, and list `ready_for_review` in `types:`
everywhere.

### The three caller obligations

Documented at the top of the reusable's job, where a caller author will see them:

1. **No `paths:` / `paths-ignore:` filter.** Path exclusions live in the reusable.
2. **`ready_for_review` in `types:`.**
3. **No job-level `if:` on the caller's job** that can suppress the ready transition.

### Why "exactly once" holds

1. One caller workflow per repo declares this context.
2. It has **no path filter**, so every PR event of its declared types triggers
   exactly one run — the only mechanism that could suppress the trigger is gone.
3. The run contains **exactly one job** that emits the name. Verified by grep across
   the four target repos: nothing else emits `review / Claude Code Review` (the
   interactive `claude.yml` lane is `Claude Code / claude`, a different context).
4. That job's only `if:` is the draft gate, and `ready_for_review` guarantees a run
   at the moment the PR becomes mergeable. Every other skip is **step**-gated, so
   the job always reaches a conclusion — never `skipped`, which GitHub reads as
   not-satisfied.
5. `concurrency: cancel-in-progress` bounds in-flight runs to one per PR, and
   GitHub evaluates the most recent check run of a given name. With no second
   writer there is nothing to race, so no cheap green can outvote a real review.

## Making the check required

Only after the live positive control above is recorded. Then, per repo, add
`review / Claude Code Review` to the `main` ruleset's required status checks —
preserving every existing check — and confirm first that the repo's caller honours
all three obligations above. The 17 callers outside the four target repos still
carry `paths-ignore`; that is harmless while the context is not required there, and
must be fixed before it is.
