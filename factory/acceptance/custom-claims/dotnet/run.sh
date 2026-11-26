#!/usr/bin/env bash
# Usage: run.sh <dotnet-sdk checkout>
# Copies the acceptance tests to src/OpenFga.Sdk.Test/FactoryAcceptance/
# (replacing anything there) and runs them with `dotnet test` for one target
# framework: DOTNET_TEST_FRAMEWORK, default net8.0.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
sdk="$(cd "${1:?usage: run.sh <sdk-checkout-dir>}" && pwd)"
dest="$sdk/src/OpenFga.Sdk.Test/FactoryAcceptance"

rm -rf "$dest"
mkdir -p "$dest"
cp "$here/CustomClaimsAcceptanceTests.cs" "$dest/"

cd "$sdk"
exec dotnet test src/OpenFga.Sdk.Test/OpenFga.Sdk.Test.csproj \
  -f "${DOTNET_TEST_FRAMEWORK:-net8.0}" \
  --filter "FullyQualifiedName~OpenFga.Sdk.Test.FactoryAcceptance" \
  --logger "console;verbosity=normal"
