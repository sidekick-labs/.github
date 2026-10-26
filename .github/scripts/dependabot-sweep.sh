#!/usr/bin/env bash
# Dependabot sweep — single-pass, minute-frugal auto-lander across the org.
#
# COMPLEMENTS the per-PR dependabot-auto-merge.yml (which approves minor/patch +
# arms GitHub native auto-merge). Native auto-merge lands a PR the moment it is
# CLEAN, for free — but it never updates a branch, so on strict-protected repos a
# PR that falls BEHIND stalls forever. This sweep mops up that tail:
#
#   Phase 1 (free): MERGE every dependabot PR the per-PR gate already APPROVED
#                   (= confirmed minor/patch) that is CLEAN + MERGEABLE.
#   Phase 2 (adaptive, minute-frugal): for the still-BEHIND approved ones,
#                   update exactly ONE branch per repo per pass. (BEHIND only ever
#                   appears on repos that REQUIRE up-to-date branches; non-strict
#                   repos show CLEAN even when behind, so they land in Phase 1
#                   with zero updates.) One-per-repo keeps CI cost O(N), not the
#                   O(N^2) thundering-herd you get from updating the whole stack.
#
# Phase 2 uses the REST update-branch call (a GitHub-signed merge of the base
# into the PR branch), NOT an `@dependabot rebase` comment. Dependabot only obeys
# commands from users with push access, so the App's comments were answered with
# "Sorry, only users with push access can use that command." and nothing moved:
# sidekick-inference#409 collected four of those replies 10-24..10-26, and
# rdp-client#226/#227 sat BEHIND for 7 days. The cost: once the branch carries a
# non-Dependabot commit, Dependabot stops rebasing that PR itself. That is fine
# here, because only already-armed PRs are updated and they land on green.
#
# GitHub computes mergeability lazily, so the first read of a PR nobody has
# looked at lately returns UNKNOWN. A PR is re-read once after a short pause
# before it is skipped; otherwise a quiet repo's PRs are skipped on every pass.
#
# Majors are never touched: they are not approved by the per-PR gate, so the
# APPROVED filter skips them. DRY_RUN=true logs decisions, changes nothing.
set -euo pipefail
ORG="${ORG:?set ORG}"; DRY_RUN="${DRY_RUN:-true}"
echo "::group::Dependabot sweep — org=$ORG dry_run=$DRY_RUN"

mapfile -t ROWS < <(gh search prs --owner "$ORG" --author app/dependabot --state open \
  --limit 200 --json repository,number \
  --jq '.[]|"\(.repository.nameWithOwner)\t\(.number)"')
echo "open dependabot PRs: ${#ROWS[@]}"
echo "::endgroup::"

declare -a READY=() BEHIND=()
for row in "${ROWS[@]}"; do
  repo="${row%%$'\t'*}"; n="${row##*$'\t'}"
  for attempt in 1 2; do
    IFS=$'\t' read -r armed state mergeable head < <(gh pr view "$n" -R "$repo" \
      --json autoMergeRequest,mergeStateStatus,mergeable,headRefOid \
      --jq '[(if .autoMergeRequest then "armed" else "no" end), .mergeStateStatus, .mergeable, .headRefOid]|@tsv')
    [ "$mergeable" = "UNKNOWN" ] && [ "$attempt" = 1 ] && [ "$armed" = "armed" ] || break
    sleep 5   # mergeability is computed lazily; the first read kicked it off
  done
  # Gate: act only on PRs the per-PR auto-merge already ARMED (= it confirmed
  # minor/patch + approved). reviewDecision is unreliable here (0 required
  # reviewers leaves it null even after the bot approves), so arming is the
  # durable signal. Majors are never armed -> skipped.
  if [ "$armed" != "armed" ]; then echo "skip $repo#$n -- not armed by per-PR gate"; continue; fi
  if [ "$mergeable" != "MERGEABLE" ]; then echo "skip $repo#$n — $mergeable/$state"; continue; fi
  case "$state" in
    CLEAN)  READY+=("$repo#$n") ;;
    BEHIND) BEHIND+=("$repo#$n@$head") ;;
    *)      echo "wait $repo#$n — $state" ;;
  esac
done

echo "::group::Phase 1 — merge ready (${#READY[@]})"
for pr in "${READY[@]}"; do
  repo="${pr%#*}"; n="${pr#*#}"
  echo "merge $repo#$n"
  [ "$DRY_RUN" = "true" ] || gh pr merge "$n" -R "$repo" --squash --delete-branch || echo "  merge failed for $repo#$n"
done
echo "::endgroup::"

echo "::group::Phase 2 — update ONE behind branch per repo (${#BEHIND[@]} candidates)"
declare -A DONE=()
for pr in "${BEHIND[@]}"; do
  head="${pr##*@}"; pr="${pr%@*}"
  repo="${pr%#*}"; n="${pr#*#}"
  if [ -n "${DONE[$repo]:-}" ]; then echo "defer $repo#$n — already updated one in $repo this pass"; continue; fi
  echo "update-branch $repo#$n (strict+behind)"
  # expected_head_sha: if the branch moved since we read it, GitHub refuses (422)
  # rather than merging into a head we never looked at.
  [ "$DRY_RUN" = "true" ] || gh api -X PUT "repos/$repo/pulls/$n/update-branch" \
    -f expected_head_sha="$head" >/dev/null || echo "  update-branch failed for $repo#$n"
  DONE[$repo]=1
done
echo "::endgroup::"
echo "sweep complete (dry_run=$DRY_RUN): ready=${#READY[@]} behind-candidates=${#BEHIND[@]} repos-updated=${#DONE[@]}"
