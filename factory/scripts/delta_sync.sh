#!/usr/bin/env bash
# Regenerates one SDK before and after a change, then applies only the
# difference to the SDK's checked-out branch. Lines the change does not touch
# keep the SDK's values, such as release versions and hand edits.
#
# Usage, from the generator root, with the SDK checked out in
# clients/fga-<language>-sdk:
#   delta_sync.sh <language>
#
# Environment:
#   BASE_GENERATOR_DIR  generator checkout to build before the change
#                       (default: the current directory)
#   NEW_OPENAPI_REF     OPEN_API_REF to build after the change
#                       (default: the Makefile pin)
#
# Exits 1 when the change touches lines that differ between the generator and
# the SDK. Those files need a person.

set -euo pipefail

language=${1:?usage: delta_sync.sh <language>}
hub=$PWD
sdk="$hub/clients/fga-$language-sdk"
base_dir=${BASE_GENERATOR_DIR:-$hub}

in_sdk() { git -C "$sdk" -c core.quotePath=false "$@"; }

snapshot() {
  in_sdk add -A
  in_sdk -c user.name=sdk-factory -c user.email=sdk-factory@users.noreply.github.com \
    -c commit.gpgsign=false commit -q --no-verify --allow-empty -m "$1"
  in_sdk rev-parse HEAD
}

build() {
  local dir=$1
  shift
  echo "::group::make build-client-$language in $dir $*"
  (cd "$dir" && make "build-client-$language" CLIENTS_OUTPUT_DIR="$hub/clients" "$@")
  echo "::endgroup::"
}

summary() {
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    printf '%s\n' "$@" >>"$GITHUB_STEP_SUMMARY"
  fi
}

if [[ -n "$(in_sdk status --porcelain)" ]]; then
  echo "::error::$sdk has uncommitted changes." >&2
  exit 1
fi
start=$(in_sdk symbolic-ref -q --short HEAD || in_sdk rev-parse HEAD)
main=$(in_sdk rev-parse HEAD)
in_sdk checkout -q --detach

# Both builds start from the SDK's tree, so they differ only by the change.
build "$base_dir"
before=$(snapshot "factory: before the change")
in_sdk read-tree -u --reset "$main"
build "$hub" ${NEW_OPENAPI_REF:+"OPEN_API_REF=$NEW_OPENAPI_REF"}
after=$(snapshot "factory: after the change")
in_sdk checkout -q "$start"

if in_sdk diff --quiet "$before" "$after"; then
  echo "The change does not alter the $language SDK."
  summary "### $language" "" "The change does not alter this SDK."
  exit 0
fi
echo "The change alters $(in_sdk diff --shortstat "$before" "$after")."

skipped=()
if ! picked=$(in_sdk cherry-pick --no-commit "$after" 2>&1); then
  conflicts=()
  unmerged=$(in_sdk diff --name-only --diff-filter=U)
  while IFS= read -r path; do
    [[ -n "$path" ]] || continue
    if in_sdk cat-file -e ":2:$path" 2>/dev/null; then
      conflicts+=("$path")
    else
      # Generated files the SDK does not track stay out of the sync.
      in_sdk rm -q -- "$path"
      skipped+=("$path")
    fi
  done <<<"$unmerged"
  if ((${#conflicts[@]} + ${#skipped[@]} == 0)); then
    echo "::error::Could not apply the change: $picked" >&2
    exit 1
  fi
  if ((${#conflicts[@]})); then
    echo "::error::The change touches lines that the $language SDK has changed by hand. Merge these files by hand:"
    printf '  %s\n' "${conflicts[@]}"
    in_sdk --no-pager diff -- "${conflicts[@]}"
    summary "### $language: needs a person" "" \
      "The change touches lines that the SDK has changed by hand. Merge these files by hand:" "" \
      "$(printf -- '- %s\n' "${conflicts[@]}")"
    exit 1
  fi
fi
in_sdk cherry-pick --quit 2>/dev/null || true

if ((${#skipped[@]})); then
  echo "Left out ${#skipped[@]} generated file(s) that the SDK does not track:"
  printf '  %s\n' "${skipped[@]}"
fi
echo "::group::Kept as the SDK has them (a full regeneration would change these)"
in_sdk --no-pager diff --cached --stat "$after"
echo "::endgroup::"
summary "### $language" "" \
  "Applied: $(in_sdk diff --cached --shortstat)." \
  "Kept as the SDK has them: $(in_sdk diff --cached --shortstat "$after" || true)."
in_sdk reset -q
