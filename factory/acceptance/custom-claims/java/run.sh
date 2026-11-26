#!/usr/bin/env bash
# Usage: run.sh <java-sdk checkout>
# Copies the acceptance tests to src/test/java/dev/openfga/sdk/factoryacceptance/
# (replacing anything there) and runs them with the SDK's native runner
# (Gradle wrapper). Uses the JDK from JAVA_HOME (the SDK targets Java 17+).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
sdk="$(cd "${1:?usage: run.sh <sdk-checkout-dir>}" && pwd)"
dest="$sdk/src/test/java/dev/openfga/sdk/factoryacceptance"

rm -rf "$dest"
mkdir -p "$dest"
cp "$here/CustomClaimsAcceptanceTest.java" "$dest/"

cd "$sdk"
exec ./gradlew --no-daemon --console=plain test --tests 'dev.openfga.sdk.factoryacceptance.*' -i
