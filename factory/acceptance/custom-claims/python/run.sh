#!/usr/bin/env bash
# Usage: run.sh <openfga-python-sdk checkout>
# Copies the acceptance tests to test/factory_acceptance/ (replacing anything
# there) and runs them with the SDK's native runner (uv + pytest).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
sdk="$(cd "${1:?usage: run.sh <sdk-checkout-dir>}" && pwd)"
dest="$sdk/test/factory_acceptance"

rm -rf "$dest"
mkdir -p "$dest"
: > "$dest/__init__.py"
cp "$here/custom_claims_acceptance_test.py" "$dest/"

cd "$sdk"
# -o addopts= drops the repository's coverage flags for this focused run.
exec uv run --frozen pytest -o addopts= -p no:cacheprovider -v test/factory_acceptance/
