#!/usr/bin/env bash
# Usage: run.sh <openfga-go-sdk checkout>
# Copies the acceptance tests to factoryacceptance/customclaims/ (replacing
# anything there) and runs them with the SDK's native runner (go test).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
sdk="$(cd "${1:?usage: run.sh <sdk-checkout-dir>}" && pwd)"
dest="$sdk/factoryacceptance/customclaims"

rm -rf "$dest"
mkdir -p "$dest"
cp "$here/customclaims_acceptance_test.go" "$dest/"

cd "$sdk"
exec go test -count=1 -v ./factoryacceptance/customclaims/
