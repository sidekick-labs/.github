#!/usr/bin/env bash
# Regression tests for do-app-deploy's appspec patch + post-update drift check
# (.github/actions/do-app-deploy/spec.sh), the wave-2 fix for spec stripping:
# an old doctl (1.98.1) dropped spec fields it didn't know on a spec
# read-modify-write, and staging lost liveness_health_check / maintenance /
# disable_email_obfuscation / ingress match.authority. Root cause:
# https://github.com/fundbright/delivery-ops/issues/1019#issuecomment-6795304856
#
# The action sources spec.sh and this test sources the same file, so there is
# no copied logic that can drift. Fixtures under fixtures/do-app-spec/ are
# synthetic (this repo is public) but mirror the shape of the live sidekick
# specs: fetched.json is what GET /v2/apps/{id} returns as .app.spec, and
# patched.json is the expected result of patching it to example-app@2222….
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../actions/do-app-deploy/spec.sh
source "$HERE/../actions/do-app-deploy/spec.sh"
FIX="$HERE/fixtures/do-app-spec"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail=0
assert_eq() { if [ "$2" = "$3" ]; then echo "ok: $1"; else echo "FAIL: $1"; echo "  exp: [$2]"; echo "  got: [$3]"; fail=1; fi; }

REPO=example-app
SHA=2222222222222222222222222222222222222222

# What DO stores after an image-only update: the patched spec, plus the DOCR
# `registry` DO fills into each image block (see spec_drift's normalisation).
spec_patch "$FIX/fetched.json" "$REPO" "$SHA" > "$TMP/expected.json"
jq -c '
  reduce ("services", "workers", "jobs") as $k (.;
    if (.[$k] | type) == "array" then .[$k] |= map(.image.registry = "example-registry") else . end)
' "$TMP/expected.json" > "$TMP/stored.json"

# ── the patch ────────────────────────────────────────────────────────────────
assert_eq "patch matches the pinned expected spec" \
  "$(jq -S . "$FIX/patched.json")" "$(jq -S . "$TMP/expected.json")"

assert_eq "patch stays inside image/envs/build-source fields" \
  "" "$(spec_patch_scope_violations "$FIX/fetched.json" "$TMP/expected.json")"

assert_eq "patch keeps exactly one SENTRY_RELEASE per component" \
  "1,1,1" "$(jq -r '[(.services, .workers, .jobs)[] | [.envs[] | select(.key == "SENTRY_RELEASE")] | length] | map(tostring) | join(",")' "$TMP/expected.json")"

assert_eq "patch leaves a spec with no component arrays untouched" \
  '{"name":"x","maintenance":{"enabled":true}}' \
  "$(printf '%s' '{"name":"x","maintenance":{"enabled":true}}' > "$TMP/bare.json"; spec_patch "$TMP/bare.json" "$REPO" "$SHA")"

# The jq patch must do exactly what the yq patch it replaced did. yq
# (mikefarah) is preinstalled on ubuntu-latest, so CI always runs this.
if command -v yq >/dev/null 2>&1; then
  OLD=$(IMAGE_REPO="$REPO" DEPLOY_SHA="$SHA" yq -p json -o json '
    (.services[], .workers[], .jobs[]) |= (
      del(.dockerfile_path, .source_dir, .github) |
      .image = {
        "registry_type": "DOCR",
        "repository": strenv(IMAGE_REPO),
        "tag": strenv(DEPLOY_SHA)
      } |
      .envs = ((.envs // []) | map(select(.key != "SENTRY_RELEASE"))) + [{
        "key": "SENTRY_RELEASE",
        "value": strenv(DEPLOY_SHA),
        "scope": "RUN_TIME"
      }]
    )
  ' "$FIX/fetched.json" | jq -S .)
  assert_eq "jq patch == the previous yq patch" "$OLD" "$(jq -S . "$TMP/expected.json")"
else
  echo "skip: yq not on PATH — jq-vs-yq equivalence not checked"
fi

# ── the drift check ──────────────────────────────────────────────────────────
assert_eq "image-only change: no drift (DO-filled image.registry normalised)" \
  "" "$(spec_drift "$TMP/expected.json" "$TMP/stored.json")"

jq 'del(.services[0].liveness_health_check)' "$TMP/stored.json" > "$TMP/no-liveness.json"
assert_eq "dropped liveness_health_check fails" \
  ".services[0].liveness_health_check (missing)" "$(spec_drift "$TMP/expected.json" "$TMP/no-liveness.json")"

jq 'del(.maintenance)' "$TMP/stored.json" > "$TMP/no-maintenance.json"
assert_eq "dropped top-level maintenance fails" \
  ".maintenance (missing)" "$(spec_drift "$TMP/expected.json" "$TMP/no-maintenance.json")"

# The full August-style strip: every field doctl 1.98.1 didn't know, at once.
jq 'del(.maintenance, .disable_email_obfuscation, .services[0].liveness_health_check, .ingress.rules[0].match.authority)' \
  "$TMP/stored.json" > "$TMP/old-doctl.json"
assert_eq "old-doctl strip reports every dropped path" \
  "$(printf '%s\n' '.disable_email_obfuscation (missing)' '.ingress.rules[0].match.authority (missing)' '.maintenance (missing)' '.services[0].liveness_health_check (missing)')" \
  "$(spec_drift "$TMP/expected.json" "$TMP/old-doctl.json")"

jq '.services[0].health_check.failure_threshold = 3 | .region = "nyc" | .features += ["extra"]' "$TMP/stored.json" > "$TMP/changed.json"
assert_eq "changed and added values fail" \
  "$(printf '%s\n' '.features[1] (added)' '.region (changed)' '.services[0].health_check.failure_threshold (changed)')" \
  "$(spec_drift "$TMP/expected.json" "$TMP/changed.json")"

jq '.services[0].envs[0].value = "EV[1:rotated-by-someone]"' "$TMP/stored.json" > "$TMP/secret.json"
OUT=$(spec_drift "$TMP/expected.json" "$TMP/secret.json")
assert_eq "changed secret reported by path" ".services[0].envs[0].value (changed)" "$OUT"
case "$OUT" in *EV\[*) echo "FAIL: drift output leaked an env value"; fail=1 ;; *) echo "ok: drift output carries no env values" ;; esac

jq '.services[0].image.repository = "other-app"' "$TMP/stored.json" > "$TMP/wrong-image.json"
assert_eq "wrong image repository still fails (normalisation is registry-only)" \
  ".services[0].image.repository (changed)" "$(spec_drift "$TMP/expected.json" "$TMP/wrong-image.json")"

# A registry WE set is not normalised away: if DO changed it, that is drift.
jq '.services[0].image.registry = "ours"' "$TMP/expected.json" > "$TMP/exp-reg.json"
jq '.services[0].image.registry = "theirs"' "$TMP/stored.json" > "$TMP/act-reg.json"
assert_eq "registry set by the patch and changed by DO fails" \
  ".services[0].image.registry (changed)" "$(spec_drift "$TMP/exp-reg.json" "$TMP/act-reg.json")"

printf 'null' > "$TMP/null.json"
assert_eq "no stored spec at all fails" ". (changed)" "$(spec_drift "$TMP/expected.json" "$TMP/null.json")"

# ── the pre-write scope guard ────────────────────────────────────────────────
jq 'del(.services[0].liveness_health_check)' "$TMP/expected.json" > "$TMP/bad-patch.json"
assert_eq "scope guard flags a patch that drops a non-image field" \
  ".services[0].liveness_health_check (missing)" "$(spec_patch_scope_violations "$FIX/fetched.json" "$TMP/bad-patch.json")"

jq 'del(.maintenance)' "$TMP/expected.json" > "$TMP/bad-patch2.json"
assert_eq "scope guard flags a patch that drops a top-level field" \
  ".maintenance (missing)" "$(spec_patch_scope_violations "$FIX/fetched.json" "$TMP/bad-patch2.json")"

if [ "$fail" -ne 0 ]; then echo "do-app-deploy spec logic: FAILURES"; exit 1; fi
echo "do-app-deploy spec logic: all passed"
