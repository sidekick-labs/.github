#!/usr/bin/env bash
# Shared, side-effect-free core of do-app-deploy's appspec read-modify-write:
# the image patch and the post-update drift check.
#
# Why this exists: an old doctl (1.98.1, reached via action-doctl's silent
# fallback) dropped every spec field it didn't know during `doctl apps spec get`
# -> edit -> `doctl apps update` (liveness_health_check, maintenance,
# disable_email_obfuscation, ingress match.authority). Root cause:
# https://github.com/fundbright/delivery-ops/issues/1019#issuecomment-6795304856
# The action now reads and writes the spec as JSON over the DO REST API, so no
# client model sits between DO and the patch, and it proves after the write that
# DO stored exactly the fetched spec plus the intended patch.
#
# This file is SOURCED by two callers, so there is a SINGLE source of truth:
#   1. action.yml — via "$GITHUB_ACTION_PATH/spec.sh".
#   2. ../../tests/do-app-deploy-spec-logic.sh — drives the same functions
#      against fixture specs under ../../tests/fixtures/do-app-spec/.
#
# All functions take FILE PATHS (specs are large, and env values must never be
# passed on a command line) and print to stdout. None of them prints a spec
# VALUE: env values can be secrets, so drift is reported by path and kind only.

# The component arrays the patch walks. Missing arrays are skipped, so the patch
# stays correct if the spec later grows a worker or scheduled job.
SPEC_COMPONENT_KINDS='["services","workers","jobs"]'

# spec_patch SPEC_FILE IMAGE_REPO DEPLOY_SHA -> patched spec JSON
#
# Exactly what the yq patch did before: on every service/worker/job, drop the
# source build (`github` / `dockerfile_path` / `source_dir`), pin `image` to
# DOCR <IMAGE_REPO>:<DEPLOY_SHA>, and upsert one SENTRY_RELEASE=<DEPLOY_SHA>
# RUN_TIME env (any existing SENTRY_RELEASE is replaced, not duplicated).
# Nothing else in the spec is touched.
spec_patch() {
  jq -c --arg repo "$2" --arg sha "$3" --argjson kinds "$SPEC_COMPONENT_KINDS" '
    def patch_component:
      del(.dockerfile_path, .source_dir, .github)
      | .image = {"registry_type": "DOCR", "repository": $repo, "tag": $sha}
      | .envs = (((.envs // []) | map(select(.key != "SENTRY_RELEASE")))
                 + [{"key": "SENTRY_RELEASE", "value": $sha, "scope": "RUN_TIME"}]);
    reduce $kinds[] as $k (.;
      if (.[$k] | type) == "array" then .[$k] |= map(patch_component) else . end)
  ' "$1"
}

# jq definitions shared by the two diff functions below.
#   spec_diff(a; b): one {path, kind} per difference between a and b, where kind
#   is "missing" (in a, not in b), "added" (in b, not in a) or "changed". It
#   reports at the highest differing level: a dropped liveness_health_check is
#   one entry, not one per leaf. Arrays compare by index; DO keeps array order.
#   fmt_path: [ "services", 0, "image" ] -> .services[0].image
# shellcheck disable=SC2016 # jq program text; the $vars are jq's, not bash's
SPEC_DIFF_JQ='
  def spec_diff($a; $b; $p):
    if ($a | type) == "object" and ($b | type) == "object" then
      (($a | keys) + ($b | keys) | unique[]) as $k
      | if ($a | has($k)) and ($b | has($k)) then spec_diff($a[$k]; $b[$k]; $p + [$k])
        elif ($a | has($k)) then {path: ($p + [$k]), kind: "missing"}
        else {path: ($p + [$k]), kind: "added"} end
    elif ($a | type) == "array" and ($b | type) == "array" then
      range(0; [($a | length), ($b | length)] | max) as $i
      | if $i < ($a | length) and $i < ($b | length) then spec_diff($a[$i]; $b[$i]; $p + [$i])
        elif $i < ($a | length) then {path: ($p + [$i]), kind: "missing"}
        else {path: ($p + [$i]), kind: "added"} end
    elif $a == $b then empty
    else {path: $p, kind: "changed"} end;
  def spec_diff($a; $b): spec_diff($a; $b; []);
  def fmt_path:
    if length == 0 then "." else
      map(if type == "number" then "[\(.)]" else ".\(.)" end) | join("") end;
'

# spec_patch_scope_violations SAVED_FILE PATCHED_FILE -> one line per violation
#
# Pre-write guard on the patch itself: every difference between the fetched
# spec and the patched spec must sit under a patched component's `image`,
# `envs`, `github`, `dockerfile_path` or `source_dir`. Anything else means the
# patch touched a field it has no business touching. Empty output = in scope.
spec_patch_scope_violations() {
  jq -nr --slurpfile a "$1" --slurpfile b "$2" --argjson kinds "$SPEC_COMPONENT_KINDS" "$SPEC_DIFF_JQ"'
    spec_diff($a[0]; $b[0])
    | select(
        (.path | length) < 3
        or (.path[0] as $k | $kinds | index($k) | not)
        or (.path[1] | type) != "number"
        or (.path[2] as $f | ["image", "envs", "github", "dockerfile_path", "source_dir"] | index($f) | not))
    | "\(.path | fmt_path) (\(.kind))"
  '
}

# spec_drift EXPECTED_FILE ACTUAL_FILE -> one line per drifted path
#
# EXPECTED = the fetched spec with spec_patch applied; ACTUAL = the spec DO
# stored after the update. Any difference is drift, reported as
# "<path> (missing|added|changed)". Empty output = no drift.
#
# Normalised (things DO itself legitimately rewrites; nothing else is):
#   - image.registry on DOCR components. The patch writes a DOCR image block
#     without `registry` (as the yq patch always did) and DO fills in the
#     account's registry name ("sidekick-labs"). Confirmed with
#     POST /v2/apps/propose against all six sidekick apps on 2026-11-11: it was
#     the ONLY field DO added or changed. Removed from ACTUAL only where the
#     expected image is DOCR and has no registry, so a registry that WE set
#     and DO changed still counts as drift.
spec_drift() {
  jq -nr --slurpfile e "$1" --slurpfile a "$2" --argjson kinds "$SPEC_COMPONENT_KINDS" "$SPEC_DIFF_JQ"'
    $e[0] as $exp
    | ($a[0]
       | reduce (
           $kinds[] as $k
           | ($exp[$k] // [] | if type == "array" then to_entries[] else empty end)
           | select((.value.image | type) == "object"
                    and .value.image.registry_type == "DOCR"
                    and (.value.image | has("registry") | not))
           | [$k, .key, "image"]
         ) as $img (.;
           if (getpath($img) | type) == "object" then delpaths([$img + ["registry"]]) else . end)
      ) as $act
    | spec_diff($exp; $act)
    | "\(.path | fmt_path) (\(.kind))"
  '
}
