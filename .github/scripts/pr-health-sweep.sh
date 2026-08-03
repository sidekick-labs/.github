#!/usr/bin/env bash
#
# PR-health sweep — the two failure modes that make a PR look stuck when it is not.
#
# COMPLEMENTS dependabot-sweep.sh, which solves exactly these problems for
# dependabot PRs only: it gates on `--author app/dependabot` and rebases via a
# `@dependabot rebase` comment, a mechanism no other author has. Human- and
# agent-authored PRs hit the same two walls with nothing mopping up after them.
#
# PHASE 1 — ARMED BUT BEHIND.
#   GitHub's native auto-merge NEVER updates a branch. On a repo with
#   "require branches to be up to date" (companion-kit, web, harness), an armed
#   PR that falls BEHIND stalls forever: fully green, mergeable, and going
#   nowhere. Observed 2026-08-03: three companion-kit PRs (#427, #428, #433) each
#   needed a manual `gh pr update-branch`, and #427 needed TWO because #428
#   merged underneath it. Rate-limited to ONE update per repo per pass, the same
#   O(N)-not-O(N^2) discipline dependabot-sweep.sh Phase 2 uses — each update
#   re-runs that repo's CI, and updating a whole stack at once burns minutes to
#   produce a fresh set of BEHIND PRs anyway.
#
# PHASE 2 — HEAD COMMIT WITH NO CHECKS AT ALL (report-only).
#   Worse than a stale failure, because the PR shows nothing pending: the red X
#   from the PREVIOUS commit is what a reviewer sees. Observed 2026-08-03:
#   companion-kit#370's head `a4d85fc` (pushed 2026-07-28, and the commit that
#   FIXED the failure) had exactly one check-run on it — a skipped auto-merge.
#   The fix sat correct and invisible for six days while the PR advertised the
#   old failure. `ci.yml` has no draft guard and `pull_request` fires on
#   `synchronize`, so the run should have happened; it simply did not. Nothing in
#   the estate notices "head SHA has zero check-runs", which is why six days
#   passed. This phase only reports — re-dispatching someone else's workflow on a
#   guess is how you get duplicate deploys.
#
# Deliberately NOT here: merging. Phase 1 hands a PR back to native auto-merge,
# which lands it the moment CI goes green. This script never merges anything, so
# it cannot land work that a human has not already armed.
#
# DRY_RUN=true logs decisions and changes nothing.
set -euo pipefail
ORG="${ORG:?set ORG}"; DRY_RUN="${DRY_RUN:-true}"
STALE_DRAFT_DAYS="${STALE_DRAFT_DAYS:-5}"

# GNU `date -d` does not exist on macOS and BSD `date -j -f` does not exist on
# Linux. Without both, a local run silently fell back to "now" and reported 0
# stale drafts while two 5-day-old drafts were open — a wrong answer that looked
# like a clean one.
iso_to_epoch() {
  date -u -d "$1" +%s 2>/dev/null \
    || date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "$1" +%s 2>/dev/null \
    || date -u +%s
}

# A repo with no CI at all has zero check-runs on every commit, so flagging its
# PRs as "CI never ran" is noise. Two conditions, both required, probed once per
# repo: the repo has >=1 Actions workflow, AND its default-branch head carries
# check-runs. Either alone gives false positives — `skl_glasses_mobile` has 0
# workflows but 1 stray check-run on main (verified 2026-08-03).
CI_REPOS=""; NOCI_REPOS=""
repo_runs_ci() {
  case "$NOCI_REPOS" in *"|$1|"*) return 1 ;; esac
  case "$CI_REPOS"   in *"|$1|"*) return 0 ;; esac
  # Distinguish "API said 0" from "API call FAILED". Swallowing a 403 into 0 is
  # what silently disabled this whole phase on the first live run: the token
  # lacked actions:read, every repo scored 0 workflows, and the sweep reported a
  # confident all-clear. On an unreadable API we fail toward REPORTING (assume
  # the repo runs CI) — a false positive is visible and cheap; a false all-clear
  # is invisible and cost six days on companion-kit#370.
  if ! _wf=$(gh api "repos/$1/actions/workflows" --jq ".total_count" 2>&1); then
    echo "::warning::cannot read actions/workflows for $1 ($_wf) — assuming it runs CI; check the token has actions:read"
    CI_REPOS="$CI_REPOS|$1|"; return 0
  fi
  _db=$(gh api "repos/$1" --jq '.default_branch' 2>/dev/null || echo main)
  _c=$(gh api "repos/$1/commits/$_db/check-runs" --jq '.total_count' 2>/dev/null || echo 0)
  if [ "${_wf:-0}" -gt 0 ] && [ "${_c:-0}" -gt 0 ]; then CI_REPOS="$CI_REPOS|$1|"; return 0
  else NOCI_REPOS="$NOCI_REPOS|$1|"; return 1; fi
}

echo "::group::PR-health sweep — org=$ORG dry_run=$DRY_RUN"

# Every open PR in the org EXCEPT dependabot's — those are dependabot-sweep.sh's
# job, and double-driving them would fight its one-per-repo pacing.
# `while read` rather than `mapfile`, and plain strings rather than `declare -A`:
# both bash-4-only, and macOS ships bash 3.2, so this way the script is runnable
# (and DRY_RUN-testable) on a maintainer's laptop and not only on the runner.
# NOTE the quoting: `-- -author:app/dependabot` (unquoted, before the flags)
# silently returns ZERO results rather than erroring — verified 2026-08-03,
# 0 vs 34. The raw qualifier must be quoted and passed AFTER the flags.
ROWS=$(gh search prs --owner "$ORG" --state open \
  --limit 200 --json repository,number \
  --jq '.[]|"\(.repository.nameWithOwner)\t\(.number)"' -- '-author:app/dependabot' 2>/dev/null || true)
echo "open non-dependabot PRs: $(printf '%s\n' "$ROWS" | grep -c . || true)"
echo "::endgroup::"

BEHIND=""; NOCHECKS=""; STALE_DRAFTS=""; CONFLICTED=""; UNREADABLE=""; INDETERMINATE=""

while IFS= read -r row; do
  [ -n "$row" ] || continue
  repo="${row%%$'\t'*}"; n="${row##*$'\t'}"

  # LOUD, not silent. `2>/dev/null || continue` here meant any PR the token
  # cannot read vanished from the sweep with no trace — and `gh search` DOES
  # surface repos the App is not installed on, so the runner silently swept a
  # narrower set than it reported counting. That is how a live run said
  # `conflicted=0` while the same script on a laptop PAT said 2, at the same
  # moment, with both PRs verifiably still DIRTY.
  if ! _pr=$(gh pr view "$n" -R "$repo" \
    --json autoMergeRequest,mergeStateStatus,mergeable,isDraft,headRefOid,updatedAt \
    --jq '[(if .autoMergeRequest then "armed" else "no" end), .mergeStateStatus, .mergeable,
           (.isDraft|tostring), .headRefOid, .updatedAt]|@tsv' 2>&1); then
    echo "::warning::cannot read $repo#$n — skipped. Is the App installed on $repo? ($_pr)"
    UNREADABLE="$UNREADABLE$repo#$n\n"
    continue
  fi
  IFS=$'\t' read -r armed state mergeable isdraft head updated <<EOF2
$_pr
EOF2

  # `mergeStateStatus` and `mergeable` are computed LAZILY by GitHub: the first
  # read of a PR that has not been touched in a while returns UNKNOWN, and the
  # request itself is what kicks off the computation. Classifying on a single
  # read is therefore a coin flip — observed 2026-08-03: claude-plugins#6 landed
  # in `conflicted` on one local run and `no-checks` on the next, with the PR
  # verifiably unchanged, and a live runner reported conflicted=0 while a laptop
  # reported 2 at the same moment. Re-read once, then refuse to guess.
  if [ "$state" = "UNKNOWN" ] || [ "$mergeable" = "UNKNOWN" ]; then
    sleep 3
    if _pr2=$(gh pr view "$n" -R "$repo" \
      --json mergeStateStatus,mergeable --jq '[.mergeStateStatus,.mergeable]|@tsv' 2>/dev/null); then
      IFS=$'\t' read -r state mergeable <<EOF3
$_pr2
EOF3
    fi
  fi
  if [ "$state" = "UNKNOWN" ] || [ "$mergeable" = "UNKNOWN" ]; then
    INDETERMINATE="$INDETERMINATE$repo#$n\n"
    continue
  fi

  # --- Phase 2 signal: does the head commit have ANY check-run? -------------
  # Counts check-runs only (not commit statuses); a PR whose sole entry is the
  # skipped auto-merge check is the exact shape that hid companion-kit#370.
  total=$(gh api "repos/$repo/commits/$head/check-runs" --jq '.total_count' 2>/dev/null || echo "?")
  real=$(gh api "repos/$repo/commits/$head/check-runs" \
    --jq '[.check_runs[]|select((.name|test("auto-merge";"i"))|not)]|length' 2>/dev/null || echo "?")
  # A DIRTY (conflicted) PR has no computable merge ref, so `pull_request`
  # workflows CANNOT run against it — that is a rebase problem, not a missing-CI
  # problem, and conflating the two makes this report untrustworthy. Reported in
  # its own bucket. (product-brain#250 was the case that exposed this.)
  if [ "$real" = "0" ] && repo_runs_ci "$repo"; then
    if [ "$state" = "DIRTY" ]; then
      CONFLICTED="$CONFLICTED$repo#$n\n"
    else
      NOCHECKS="$NOCHECKS$repo#$n|$head|$total\n"
    fi
  fi

  # --- stale draft signal (report-only) ------------------------------------
  if [ "$isdraft" = "true" ]; then
    now=$(date -u +%s)
    then_=$(iso_to_epoch "$updated")
    age=$(( (now - then_) / 86400 ))
    [ "$age" -ge "$STALE_DRAFT_DAYS" ] && STALE_DRAFTS="$STALE_DRAFTS$repo#$n|${age}d\n"
    continue   # never touch a draft's branch — the draft flag is intent
  fi

  # --- Phase 1 candidates --------------------------------------------------
  # Gate on ARMED, exactly as dependabot-sweep.sh does: arming is a human (or
  # ship-skill) decision that this PR should land once green, so acting on it
  # cannot land anything nobody asked for.
  [ "$armed" = "armed" ] || continue
  [ "$mergeable" = "MERGEABLE" ] || { echo "skip $repo#$n — $mergeable/$state"; continue; }
  [ "$state" = "BEHIND" ] && BEHIND="$BEHIND$repo#$n\n"
done <<EOF
$ROWS
EOF

echo "::group::Phase 1 — update ONE armed+BEHIND PR per repo ($(printf "%b" "$BEHIND" | grep -c . || true) candidates)"
SEEN=""
while IFS= read -r pr; do
  [ -n "$pr" ] || continue
  repo="${pr%#*}"; n="${pr#*#}"
  case "$SEEN" in *"|$repo|"*) echo "defer $repo#$n — already updated one in $repo this pass"; continue ;; esac
  echo "update-branch $repo#$n (armed, strict+behind)"
  if [ "$DRY_RUN" != "true" ]; then
    gh pr update-branch "$n" -R "$repo" >/dev/null || echo "  update-branch failed for $repo#$n"
  fi
  SEEN="$SEEN|$repo|"
done <<EOF
$(printf "%b" "$BEHIND")
EOF
echo "::endgroup::"

echo "::group::Phase 2 — head commits with NO checks ($(printf "%b" "$NOCHECKS" | grep -c . || true)) — REPORT ONLY"
printf "%b" "$NOCHECKS" | while IFS='|' read -r pr head total; do
  [ -n "$pr" ] || continue
  echo "no-checks $pr head=${head:0:8} total_check_runs=$total"
  echo "  ^ CI never reported on this commit. The PR is showing the PREVIOUS commit's result."
done
[ "$(printf "%b" "$NOCHECKS" | grep -c . || true)" -eq 0 ] && echo "none"
echo "::endgroup::"

echo "::group::INDETERMINATE — GitHub had not computed merge state after a retry ($(printf "%b" "$INDETERMINATE" | grep -c . || true))"
printf "%b" "$INDETERMINATE" | while IFS= read -r e; do [ -n "$e" ] || continue; echo "indeterminate $e — not classified this pass; next pass will re-read"; done
[ -z "$INDETERMINATE" ] && echo "none"
echo "::endgroup::"

echo "::group::UNREADABLE — skipped, token cannot see them ($(printf "%b" "$UNREADABLE" | grep -c . || true))"
printf "%b" "$UNREADABLE" | while IFS= read -r e; do [ -n "$e" ] || continue; echo "unreadable $e"; done
[ -z "$UNREADABLE" ] && echo "none"
echo "::endgroup::"

echo "::group::Conflicted — CI cannot run until rebased ($(printf "%b" "$CONFLICTED" | grep -c . || true)) — REPORT ONLY"
printf "%b" "$CONFLICTED" | while IFS= read -r e; do [ -n "$e" ] || continue; echo "conflicted $e — DIRTY, so pull_request CI has no merge ref to run against"; done
[ -z "$CONFLICTED" ] && echo "none"
echo "::endgroup::"

echo "::group::Stale drafts (>=${STALE_DRAFT_DAYS}d untouched) ($(printf "%b" "$STALE_DRAFTS" | grep -c . || true)) — REPORT ONLY"
printf "%b" "$STALE_DRAFTS" | while IFS= read -r e; do [ -n "$e" ] || continue; echo "stale-draft ${e%|*} idle=${e#*|}"; done
[ "$(printf "%b" "$STALE_DRAFTS" | grep -c . || true)" -eq 0 ] && echo "none"
echo "::endgroup::"

# Surface in the run summary so the cron is readable without opening logs.
{
  echo "### PR-health sweep (dry_run=$DRY_RUN)"
  echo ""
  echo "| signal | count |"
  echo "|---|---|"
  echo "| armed + BEHIND (updated one per repo) | $(printf "%b" "$BEHIND" | grep -c . || true) |"
  echo "| head commit with NO checks | $(printf "%b" "$NOCHECKS" | grep -c . || true) |"
  echo "| indeterminate (merge state uncomputed) | $(printf "%b" "$INDETERMINATE" | grep -c . || true) |"
  echo "| unreadable (token blind) | $(printf "%b" "$UNREADABLE" | grep -c . || true) |"
  echo "| conflicted (CI cannot run) | $(printf "%b" "$CONFLICTED" | grep -c . || true) |"
  echo "| stale drafts (>=${STALE_DRAFT_DAYS}d) | $(printf "%b" "$STALE_DRAFTS" | grep -c . || true) |"
  if [ "$(printf "%b" "$NOCHECKS" | grep -c . || true)" -gt 0 ]; then
    echo ""
    echo "**Head commits with no checks** — these PRs display the previous commit's result:"
    printf "%b" "$NOCHECKS" | while IFS='|' read -r pr head _; do [ -n "$pr" ] || continue; echo "- \`$pr\` @ \`${head:0:8}\`"; done
  fi
} >> "${GITHUB_STEP_SUMMARY:-/dev/null}"

echo "sweep complete (dry_run=$DRY_RUN): behind=$(printf "%b" "$BEHIND" | grep -c . || true) repos-updated=$(printf "%s" "$SEEN" | tr "|" "\n" | grep -c . || true) no-checks=$(printf "%b" "$NOCHECKS" | grep -c . || true) conflicted=$(printf "%b" "$CONFLICTED" | grep -c . || true) unreadable=$(printf "%b" "$UNREADABLE" | grep -c . || true) indeterminate=$(printf "%b" "$INDETERMINATE" | grep -c . || true) stale-drafts=$(printf "%b" "$STALE_DRAFTS" | grep -c . || true)"
