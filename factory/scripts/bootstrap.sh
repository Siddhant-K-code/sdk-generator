#!/usr/bin/env bash
# Set up an SDK factory hub and its forks. Safe to run more than once.
#
#   factory/scripts/bootstrap.sh <owner>/<hub-repo>
#
# Run it once the factory is on the hub's default branch, so GitHub has
# registered the workflows. It needs gh, signed in as the owner of the hub and
# the forks, and yq. It never reads or sets secret values. It names the missing
# secrets and prints the commands that set them.
set -euo pipefail

die() {
  echo "error: $*" >&2
  exit 1
}
step() { printf '\n== %s\n' "$*"; }
lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

[ "$#" -eq 1 ] || die "usage: $0 <owner>/<hub-repo>"
hub="$1"
[[ "$hub" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "the hub must look like owner/name, got '$hub'"
for tool in gh yq; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is not installed"
done

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
registry="$root/factory/sdks.yaml"
[ -f "$registry" ] || die "$registry not found"

owner="${hub%%/*}"
upstream="$(yq -r '.generator.upstream' "$registry")"
[ "$(lower "$hub")" != "$(lower "$upstream")" ] || die "$hub is the upstream generator. Run this on your own copy."
[ "$(gh api "repos/$hub" --jq '.permissions.admin' 2>/dev/null || true)" = "true" ] ||
  die "you need admin access to $hub"

forks=()
while IFS= read -r name; do
  [ -n "$name" ] && forks+=("$owner/$name")
done < <(yq -r '.sdks[].repo' "$registry")
[ "${#forks[@]}" -gt 0 ] || die "$registry lists no SDKs"
api_fork="$owner/$(yq -r '.api.repo' "$registry")"
for repo in "$api_fork" "${forks[@]}"; do
  gh api "repos/$repo" --jq '.full_name' >/dev/null 2>&1 || die "$repo does not exist. Fork it first."
done

step "Hub settings"
gh repo edit "$hub" --enable-issues >/dev/null
gh api -X PUT "repos/$hub/actions/permissions" -F enabled=true >/dev/null
gh api -X PUT "repos/$hub/actions/permissions/workflow" \
  -f default_workflow_permissions=read -F can_approve_pull_request_reviews=false >/dev/null
# The hub merges upstream main, so a push to main must not open sync PRs.
gh variable set FACTORY_SKIP_PUSH_SYNC --repo "$hub" --body true >/dev/null
echo "$hub: issues on, Actions on, read-only default token, FACTORY_SKIP_PUSH_SYNC=true"

label() {
  gh label create "$2" --repo "$1" --color "$3" --description "$4" --force >/dev/null
}

step "Labels"
label "$hub" "factory:propagate" 5319E7 "Starts the SDK factory router"
label "$hub" "factory:candidate" C5DEF5 "An upstream SDK change the factory could port"
label "$hub" "factory:api-drift" FBCA04 "The API spec moved past the generator pin"
echo "$hub: factory:propagate, factory:candidate, factory:api-drift"
for fork in "${forks[@]}"; do
  label "$fork" "factory" 5319E7 "Opened by the SDK factory"
  label "$fork" "sync-with-generator" 0E8A16 "Automated sync from the SDK generator"
  label "$fork" "factory:verified" 0E8A16 "Checks pass, and the acceptance tests fail on the base and pass here"
  label "$fork" "factory:checks-passed" C2E0C6 "Checks pass. This change has no acceptance tests"
  label "$fork" "factory:needs-human" D93F0B "Verify could not decide. A maintainer has to look"
  label "$fork" "factory:verify-failed" B60205 "A check or acceptance test fails because of this PR"
  echo "$fork: factory, sync-with-generator and the four verify labels"
done

step "Workflows"
registered="$(gh api "repos/$hub/actions/workflows?per_page=100" --jq '.workflows[] | "\(.path) \(.state)"')"
missing=0
for file in "$root"/.github/workflows/factory-*.yml "$root"/.github/workflows/sync-sdks.yaml; do
  name="$(basename "$file")"
  state="$(printf '%s\n' "$registered" | awk -v p=".github/workflows/$name" '$1 == p { print $2 }')"
  case "$state" in
    active) echo "$name: active" ;;
    "")
      echo "$name: not registered"
      missing=1
      ;;
    *)
      gh api -X PUT "repos/$hub/actions/workflows/$name/enable" >/dev/null
      echo "$name: enabled (was $state)"
      ;;
  esac
done
if [ "$missing" -eq 1 ]; then
  echo "GitHub registers a workflow when it reaches the default branch. Merge the factory into main."
  echo "If GitHub still lists none, open https://github.com/$hub/actions, enable workflows, and run this again."
fi

step "Secrets"
secrets="$(gh secret list --repo "$hub" --json name --jq '.[].name')"
has() { printf '%s\n' "$secrets" | grep -qx "$1"; }
if has COPILOT_GITHUB_TOKEN; then
  echo "COPILOT_GITHUB_TOKEN: set"
else
  cat <<EOF
COPILOT_GITHUB_TOKEN: missing. The port worker uses it for model calls.
  1. Create a fine-grained token with your user account as the resource owner and
     Account permissions > Copilot Requests: Read:
     https://github.com/settings/personal-access-tokens/new?name=COPILOT_GITHUB_TOKEN&user_copilot_requests=read
  2. gh secret set COPILOT_GITHUB_TOKEN --repo $hub
EOF
fi
if has FACTORY_TOKEN; then
  echo "FACTORY_TOKEN: set"
else
  cat <<EOF
FACTORY_TOKEN: missing. The factory uses it to sync forks, push branches and open PRs.
  1. Create a fine-grained token with resource owner $owner, limited to these repositories:
     $hub $api_fork ${forks[*]}
     Repository permissions, each Read and write: Contents, Issues, Pull requests, Workflows.
  2. gh secret set FACTORY_TOKEN --repo $hub
EOF
fi

step "Next"
echo "gh workflow run factory-sync-forks.yml --repo $hub"
echo "Then open https://github.com/$hub/issues/new/choose, pick \"SDK factory change\" and tick Plan only."
