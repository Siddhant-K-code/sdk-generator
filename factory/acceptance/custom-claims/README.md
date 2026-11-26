# Acceptance pack: custom token-request fields (`customClaims`)

This pack holds black-box acceptance tests for porting
`openfga/js-sdk/pull/228` (custom
token-request fields on the client-credentials flow) to the Go, Python, Java and
.NET SDKs. [`contract.md`](contract.md) defines the per-SDK option name, the
required behaviour and the cases.

The SDK change is written elsewhere; these files only judge it. **An agent that
implements the change must never edit this directory or the copied test
paths.**

## Layout

| Path | Copied into the SDK checkout at | Native runner |
|---|---|---|
| `python/custom_claims_acceptance_test.py` | `test/factory_acceptance/` (plus `__init__.py`) | `uv run --frozen pytest -o addopts= -p no:cacheprovider -v test/factory_acceptance/` |
| `go/customclaims_acceptance_test.go` | `factoryacceptance/customclaims/` | `go test -count=1 -v ./factoryacceptance/customclaims/` |
| `java/CustomClaimsAcceptanceTest.java` | `src/test/java/dev/openfga/sdk/factoryacceptance/` | `./gradlew test --tests 'dev.openfga.sdk.factoryacceptance.*'` |
| `dotnet/CustomClaimsAcceptanceTests.cs` | `src/OpenFga.Sdk.Test/FactoryAcceptance/` | `dotnet test src/OpenFga.Sdk.Test/OpenFga.Sdk.Test.csproj -f $DOTNET_TEST_FRAMEWORK --filter FullyQualifiedName~OpenFga.Sdk.Test.FactoryAcceptance` |

Each test starts its own local HTTP server on `127.0.0.1`. The server acts as
both the OAuth2 token endpoint (`POST /oauth/token`) and a fake FGA API
(`POST /stores/{store}/check`). The tests use only the SDK's public client.
They need no network access beyond loopback and no secrets.

## `run.sh` contract

```sh
<lang>/run.sh <sdk-checkout-dir>
```

- Deletes and recreates the fixed test directory in the checkout, then copies
  in the authoritative test file. Any copy that a PR placed there is replaced.
- Runs only these tests with the SDK's native runner.
- Exits `0` only if every case passes. Otherwise it exits non-zero.
- Leaves the copied files in the checkout, so use a **disposable checkout**.

Environment:

- Python: `uv` on `PATH`.
- Go: the toolchain in the SDK's `go.mod`, or set `GOTOOLCHAIN=local` to use
  an installed Go.
- Java: `JAVA_HOME` set to JDK 17 or newer. Passing cases are not printed;
  per-case results are in `build/test-results/test/*.xml`.
- .NET: `dotnet` on `PATH`. `DOTNET_TEST_FRAMEWORK` defaults to `net8.0`.

## Using this in a verify job

1. Check out the PR **base** in a disposable directory and run `run.sh`. The
   expected result is that the preserve cases (`default`, `denied`) pass and
   every change case fails with `option missing`. If the base does not match,
   the pack or the base is wrong, so stop.
2. Check out the PR **head** in a fresh directory and run `run.sh`. Every
   case must pass.
3. Reject the PR if it touches the fixed test paths above. Also reject it if
   it changes test configuration that could skip or alter these tests, for
   example `conftest.py`, pytest options, Gradle test filters, or `.csproj`
   compile excludes.
4. Run the SDK's own native test suite on the head as a separate step.

## Evidence

All runs used the forks `Siddhant-K-code/<sdk>` at the listed `main` commit.
The local toolchains were:

- Python 3.13.15 via uv 0.12.3
- Go 1.26.4 with `GOTOOLCHAIN=local` (the SDK's `go.mod` asks for 1.27.3)
- OpenJDK 21.0.12 with the Gradle wrapper
- .NET SDK 9.0.321, run with `DOTNET_TEST_FRAMEWORK=net9.0`

| SDK | Base | Fail-before on base | Pass-after |
|---|---|---|---|
| Python | `214ec02` | exit 1: 4 passed, 12 failed (`default`/`denied` × async/sync pass; all failures `option missing`) | exit 0: **16 passed**. The earlier factory's verified Python candidate was applied with `git apply --3way`. The only conflicts were in the import blocks of `openfga_sdk/oauth2.py` and `openfga_sdk/sync/oauth2.py`. The SDK's native suite then gave `624 passed, 12 deselected` (`-m "not integration"`). |
| Go | `779d560` | exit 1: 2 passed (`default`, `denied`), 6 failed with `option missing: credentials.Config.ClientCredentialsCustomClaims` | No candidate. A throwaway harness self-check passed 8/8. |
| Java | `039e1c5` | exit 1: 8 tests, 2 passed (`default`, `denied`), 6 failed with `option missing: ClientCredentials.customClaims(Map<String, String>)` | No candidate. A throwaway harness self-check passed 8/8. |
| .NET | `2d4a815` | exit 1 (net9.0): 2 passed (`default`, `denied`), 6 failed with `option missing: CredentialsConfig.CustomClaims (IDictionary<string, string>)` | No candidate. A throwaway harness self-check passed 8/8 on net9.0. |

**Harness self-checks** were minimal local edits, written only to prove that
every case can be satisfied against the real SDK code. They are not candidates
and were not published. As a mutation check, a Java variant that let custom
fields override standard fields failed `reserved`, `scopes` and `no-audience`.

**Unverified locally:**

- .NET on `net8.0` (the `run.sh` default), `netcoreapp3.1` and `net48`. Only
  the .NET 9 runtime was available.
- Go with its pinned 1.27.3 toolchain.
- Python on interpreters other than 3.13.
- Native suites for Go, Java and .NET, because no candidate exists yet.
