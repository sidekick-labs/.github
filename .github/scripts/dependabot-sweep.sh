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
#                   rebase exactly ONE per repo per pass. (BEHIND only ever
#                   appears on repos that REQUIRE up-to-date branches; non-strict
#                   repos show CLEAN even when behind, so they land in Phase 1
#                   with zero rebases.) One-per-repo keeps CI cost O(N), not the
#                   O(N^2) thundering-herd you get from rebasing the whole stack.
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
  IFS=$'\t' read -r armed state mergeable < <(gh pr view "$n" -R "$repo" \
    --json autoMergeRequest,mergeStateStatus,mergeable \
    --jq '[(if .autoMergeRequest then "armed" else "no" end), .mergeStateStatus, .mergeable]|@tsv')
  # Gate: act only on PRs the per-PR auto-merge already ARMED (= it confirmed
  # minor/patch + approved). reviewDecision is unreliable here (0 required
  # reviewers leaves it null even after the bot approves), so arming is the
  # durable signal. Majors are never armed -> skipped.
  if [ "$armed" != "armed" ]; then echo "skip $repo#$n -- not armed by per-PR gate"; continue; fi
  if [ "$mergeable" != "MERGEABLE" ]; then echo "skip $repo#$n — $mergeable/$state"; continue; fi
  case "$state" in
    CLEAN)  READY+=("$repo#$n") ;;
    BEHIND) BEHIND+=("$repo#$n") ;;
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

echo "::group::Phase 2 — rebase ONE behind per repo (${#BEHIND[@]} candidates)"
declare -A DONE=()
for pr in "${BEHIND[@]}"; do
  repo="${pr%#*}"; n="${pr#*#}"
  if [ -n "${DONE[$repo]:-}" ]; then echo "defer $repo#$n — already rebased one in $repo this pass"; continue; fi
  echo "rebase $repo#$n (strict+behind)"
  [ "$DRY_RUN" = "true" ] || gh pr comment "$n" -R "$repo" --body "@dependabot rebase" >/dev/null
  DONE[$repo]=1
done
echo "::endgroup::"
echo "sweep complete (dry_run=$DRY_RUN): ready=${#READY[@]} behind-candidates=${#BEHIND[@]} repos-rebased=${#DONE[@]}"
