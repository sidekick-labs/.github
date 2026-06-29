# Production release Slack thread

Every production release of **sidekick-web / sidekick-harness / sidekick-inference**
narrates itself in the Slack **#releases** channel as a single thread.

## What a thread looks like

```
🚀 Releasing `sidekick-web` to production
   <AI "what's shipping" summary>            ← claude-code-action over the PRs
   *Changes* (`<sha>`, N):
   • <pr|#1139> …  • <pr|#1138> …  • …and K more   ← linked PRs (commit link fallback)
   _triggered by @user · rollout starting…_
 └ ⚙️ DO rollout started
 └ ⏳ BUILDING → 🔧 DEPLOYING → ✅ ACTIVE (Xm)    ← live per-phase, from the DO poll loop
 └ ✅ Production deploy complete — `<sha>` is live (Xm) · app · deploy run · DO console
 └ 🤖 post-deploy triage verdict               ← only when Sentry/DO signal exists
```

If a dispatched deploy **never starts** (startup_failure / cancelled), promote posts a
terminal *"deploy failed to start"* reply so the thread is never orphaned (the watchdog).

## The chain

```
/release ──▶ promote-production.yml (caller, @v3)
              └▶ reusable-promote-production.yml
                   • gather PRs (commits/{sha}/pulls) → linked bullets
                   • AI summary (claude-code-action) → /tmp/release-summary.md
                   • post thread ROOT (slack-deploy-thread, mode=start) → thread_ts
                   • FF-push main→production, dispatch deploy with the thread_ts
                   • watchdog: confirm the deploy started, else reply in-thread
              └▶ deploy-production.yml (caller, @v3)
                   └▶ reusable-deploy-production.yml
                        • do-app-deploy → per-phase replies into the thread
                        • summary reply (slack-deploy-thread, mode=reply)
                        • writes thread_ts into the triage-context artifact
              └▶ post-deploy-triage.yml (workflow_run)
                   • Sentry/DO signal triage → verdict reply into the thread
```

`slack-deploy-thread` (composite) is the single Slack-posting path: `start` posts a root
and returns its `thread_ts`; `reply` threads under it. It **soft-degrades by design** —
missing token/channel/ts or a Slack error is a `::notice::`/`::warning::` and `exit 0`,
never a failed release. (It also accepts an optional `blocks` input for Slack Block Kit;
**currently unused** — the favicon context block was removed as it rendered poorly — kept
for a possible future redesign.)

## The `@v3` pin model — how to ship a change

The production **and staging** deploy/promote **callers** and the reusables' **inner
composite** refs all pin the **moving `v3` tag** (staging joined `@v3` in core-platform-brain#108;
before that staging callers were SHA-pinned and needed a 3-repo re-pin cascade). So shipping a
reusable/composite change — production *or* staging — is:

1. Edit the reusable/composite on a branch, open a PR, merge to `.github` `main`.
2. Advance the tag: `git tag -f v3 <merge-sha> && git push -f origin v3`.
3. Done — every caller picks it up. **No caller re-pin PRs.**

`v3` is a *deliberately-advanced release pointer*, not auto-tracking `main`: move it only
after the change is merged and you're ready for it to go live across all three repos.
Third-party actions (checkout, create-github-app-token, upload-artifact) stay SHA-pinned.

> Pin drift is moot under this model — all three callers pin `@v3` for both production and
> staging. To verify:
> `for r in sidekick-web sidekick-harness sidekick-inference; do git -C $r grep -h 'reusable-\(promote\|deploy\)-\(production\|staging\).yml@' origin/main; done` — all should read `@v3`.

## Config

| What | Where |
|------|-------|
| `#releases` channel id | repo var `RELEASE_SLACK_CHANNEL_ID` (= `C0BA53B52S2`) on each repo |
| Slack bot token | org secret `SLACK_BOT_TOKEN`, repo access incl. web/harness/inference |
| AI summary auth | org secret `CLAUDE_CODE_OAUTH_TOKEN` (flows via `secrets: inherit`; the reusable secret is named with underscores so inherit matches) |

Everything Slack-side soft-degrades: with the channel/token unset, releases run exactly as
before and post nothing.
