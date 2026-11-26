# OpenFGA Java SDK Context

> Factory worker context for `openfga/java-sdk` (conventions adapted from `openfga/go-sdk/pull/288` `AGENTS.md`/`.claude/rules/*` and verified against `java-sdk`).

## Stack & Verification

- **Artifact:** `dev.openfga:openfga-sdk`, Java 17 (`build.gradle`), Gradle wrapper (`./gradlew`).
- **Key deps:** **Jackson 3** (`tools.jackson.core:jackson-databind` 3.2.4) paired with `com.fasterxml.jackson.core:jackson-annotations:2.22`, OpenTelemetry (`io.opentelemetry:opentelemetry-api`), Spotless (`palantirJavaFormat`), JUnit 5 + AssertJ + Mockito + WireMock + `HttpClientMock`.
- **Verified command:** `make lint test` (there is **no** `make check` target; `make lint` runs `./gradlew check` including `spotlessCheck`, and `make test` runs `./gradlew test`).
  - Auto-format before checking: `make fmt` (`./gradlew spotlessApply`).
  - Single test class: `./gradlew test --tests "dev.openfga.sdk.api.auth.OAuth2ClientTest"`.

## Repository Layout: Generated vs. Hand-Written

Check `.openapi-generator/FILES` (193 entries) before editing. Never edit generated files by hand in an SDK PR; route those changes to `sdk-generator` (`config/clients/java/template/`).

- **Generated (do not hand-edit):**
  - `src/main/java/dev/openfga/sdk/api/OpenFgaApi.java`, `BaseStreamingApi.java`, `StreamedListObjectsApi.java`
  - `src/main/java/dev/openfga/sdk/api/model/*.java` (90 API models including `StreamResult.java`)
  - `src/main/java/dev/openfga/sdk/constants/FgaConstants.java` (shared SDK constants)
  - `docs/**`, `README.md`, `CONTRIBUTING.md`, `VERSION.txt`
- **Ignored in `.openapi-generator-ignore` (hand-maintained):** `ApiClient.java`, `ApiResponse.java`, `build.gradle`, `settings.gradle`, `gradlew*`, `**/test/**/model/*`, etc.

### Where Core Concerns Live (Hand-Written, under `src/main/java/dev/openfga/sdk/`)

| Concern | Locations |
| --- | --- |
| High-level client wrapper | `api/client/OpenFgaClient.java`, `api/configuration/ClientConfiguration.java`, `api/configuration/Client*Options.java` |
| HTTP, executor & streaming | `api/client/ApiClient.java`, `api/client/ApiExecutor.java`, `api/client/ApiExecutorRequestBuilder.java`, `api/client/StreamingApiExecutor.java`, `api/client/HttpRequestAttempt.java`, `api/client/Jackson3JsonSerializer.java`, `api/configuration/{Configuration,ConfigurationOverride}.java` |
| Credentials & OAuth2 | `api/configuration/{Credentials,CredentialsMethod,ApiToken,ClientCredentials}.java`, `api/auth/{OAuth2Client,CredentialsFlowRequest,CredentialsFlowResponse,AccessToken}.java`, and `CredentialsCacheKey` inside `api/client/ApiClient.java` |
| Retry & backoff | `util/RetryStrategy.java`, `util/ExponentialBackoff.java`, `util/RetryAfterHeaderParser.java` (called by `api/client/HttpRequestAttempt.java`) |
| Telemetry (OTel) | `telemetry/{Telemetry,Metrics,Attributes,Attribute,Counters,Counter,Histograms,Histogram,Metric}.java`, `api/configuration/TelemetryConfiguration.java` |
| Errors & validation | `errors/{FgaError,ApiException,FgaApi*Error,FgaValidationError,FgaInvalidParameterException,HttpStatusCode,SdkSerializationException}.java`, `util/Validation.java`, `util/StringUtil.java` |
| Models | Generated `api/model/*.java`; hand-written client DTOs in `api/client/model/Client*.java` |
| Tests | `src/test/java/dev/openfga/sdk/**` (unit tests); Docker Testcontainers integration suite in `src/test-integration/` |

## Shaping a Hand-Written Change

### Worked Pointer: Add a Client-Credentials Option

1. **Config builder (`api/configuration/ClientCredentials.java`):** Add the private field, fluent setter `myOption(T value)` returning `this` (normalize blank strings with `StringUtil.isNullOrWhitespace(v) ? null : v` as `apiAudience` and `scopes` do), getter `getMyOption()`, and validation in `assertValid()` if required.
   - **Quirk:** `Credentials.assertValid()` only checks `clientCredentials != null` and does **not** currently invoke `clientCredentials.assertValid()`. Keep `ClientCredentials.assertValid()` accurate and tested.
2. **Token request payload (`api/auth/CredentialsFlowRequest.java`):** Add a `*_PARAM_NAME` constant and setter populating `this.parameters` (null entries are filtered out in `buildFormRequestBody()`). Never allow callers to clobber `client_id`, `client_secret`, or `grant_type`.
3. **OAuth2 wiring (`api/auth/OAuth2Client.java`):** Pass `clientCredentials.getMyOption()` to `this.authRequest` in the `OAuth2Client` constructor. Preserve lock-free `AtomicReference<AccessToken>` reads, synchronized in-flight `CompletableFuture` deduplication, and `AccessToken` expiry threshold + jitter.
4. **Cache key (`api/client/ApiClient.java`, easy to miss!):** `ApiClient` caches `OAuth2Client` instances in `oAuth2Clients` keyed by `CredentialsCacheKey`. Add the new option to `CredentialsCacheKey` (field, constructor, `equals`, `hashCode`; SHA-256 hash if sensitive) so distinct configs do not share a cached token.
5. **Security & telemetry:** Never log secrets/tokens in exceptions or body logs; keep `telemetry.metrics().credentialsRequest(...)` intact.
6. **Tests:** Update `src/test/java/dev/openfga/sdk/api/configuration/ClientCredentialsTest.java`, `api/auth/OAuth2ClientTest.java`, and `api/client/ApiClientTest.java` (verify cache isolation) using JUnit 5 + Mockito / WireMock / `HttpClientMock`; no live network calls.

## Idioms & Guardrails

- **Builder style:** Fluent setters match field names (`new ClientCredentials().clientId(...).scopes(...)`) and return `this`; getters use `get*()`. Client operations are async and return `CompletableFuture<T>`.
- **Jackson 3 split:** Use `tools.jackson.core.*` for runtime serialization (`Jackson3JsonSerializer`) and `com.fasterxml.jackson.annotation.*` for annotations.
- **Formatting:** Spotless (`palantirJavaFormat`, unused import removal, import ordering) is enforced by `make lint`. Always run `make fmt` before `make lint test`.
- **Retry & errors:** Retry only 429 and 5xx except 501; never retry other 4xx; respect `Retry-After` capped at 1800s (`RetryStrategy` + `RetryAfterHeaderParser`).
- **Telemetry:** `fga_client_*` metrics, no PII, high-cardinality attributes off by default.

## PR Rules

- **PR title:** Conventional Commits (`feat`, `fix`, `docs`, `test`, `refactor`, `ci`, `perf`, `chore`, `revert`, `release`, optional scope). Do **not** add a title prefix; factory PRs carry the `factory` label.
- **Do not edit:** `CHANGELOG.md`, `.release-please-manifest.json`, `release-please-config.json`, `version = '...' // x-release-please-version` in `build.gradle`, or generated files in `.openapi-generator/FILES`.
- **Generated changes:** Route to `sdk-generator` (`config/clients/java/template/`), never hand-patch in `java-sdk`.
