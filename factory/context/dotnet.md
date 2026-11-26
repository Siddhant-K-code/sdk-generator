# OpenFGA .NET SDK Context

> Factory worker context for `openfga/dotnet-sdk` (conventions adapted from `openfga/go-sdk/pull/288` `AGENTS.md`/`.claude/rules/*` and verified against `dotnet-sdk`).

## Stack & Verification

- **Project:** `OpenFga.Sdk` (`src/OpenFga.Sdk/OpenFga.Sdk.csproj`), C# `LangVersion 10.0`, multi-targeted `netstandard2.0;net48;net8.0;net9.0`.
- **Key deps:** `System.Text.Json`, `System.Diagnostics.Metrics` (OpenTelemetry-compatible), `Microsoft.Bcl.AsyncInterfaces`; tests: xUnit + Moq.
- **Verified command (Linux):** `make lint test-net8 test-net9`
  - **Important:** `net48` is Windows-only, so do **not** run `make test` or `make check` on Linux runners; leave `net48` to SDK CI.
  - Auto-format before `make lint`: `make fmt` (`dotnet format ./OpenFga.Sdk.sln`).
  - Targeted test: `dotnet test --framework net8.0 --filter "FullyQualifiedName~OAuth2ClientTests"`.

## Repository Layout: Generated vs. Hand-Written

Check `.openapi-generator/FILES` (191 entries) before editing. Never edit generated files by hand in an SDK PR; route those changes to `sdk-generator` (`config/clients/dotnet/template/`).

- **Generated (do not hand-edit):**
  - `src/OpenFga.Sdk/Api/OpenFgaApi.cs` (low-level API)
  - `src/OpenFga.Sdk/Constants/FgaConstants.cs` (shared SDK constants)
  - `src/OpenFga.Sdk/Model/*.cs` (91 API models)
  - `docs/**`, `README.md`, `CONTRIBUTING.md`, `OpenFga.Sdk.sln`, `VERSION.txt`
- **Ignored in `.openapi-generator-ignore` (hand-maintained):** `src/OpenFga.Sdk/Client/*`, `src/OpenFga.Sdk/OpenFga.Sdk.csproj`, `src/OpenFga.Sdk.Test/{Api,Client,Model}/*`, `src/OpenFga.Sdk.Test/OpenFga.Sdk.Test.csproj`, `src/OpenFga.Sdk/ApiClient.cs`.
- **Quirk:** `src/OpenFga.Sdk/Client/ApiClient.cs` is an **empty file**; the real `ApiClient` is `src/OpenFga.Sdk/ApiClient/ApiClient.cs`.

### Where Core Concerns Live (Hand-Written, under `src/OpenFga.Sdk/`)

| Concern | Locations |
| --- | --- |
| High-level client wrapper | `Client/Client.cs` (`OpenFgaClient`), `Client/IOpenFgaClient.cs`, `Client/ClientConfiguration.cs`, `Client/Utils/ClientUtils.cs`, `Client/Extensions/LinqExtensions.cs` |
| HTTP & executor | `ApiClient/{ApiClient,ApiExecutor,ApiResponse,BaseClient,RequestBuilder,Utils}.cs`, `Configuration/Configuration.cs` |
| Credentials & OAuth2 | `Configuration/Credentials.cs`, `Configuration/ApiTokenIssuerNormalizer.cs`, `ApiClient/OAuth2Client.cs` |
| Retry & backoff | `ApiClient/RetryHandler.cs`, `Client/Model/RetryParams.cs` (wired in `ApiClient/ApiClient.cs` and `ApiClient/OAuth2Client.cs`) |
| Telemetry (OTel) | `Telemetry/{Attributes,Counters,Histograms,Meters,Metrics}.cs`, `Configuration/TelemetryConfig.cs` |
| Errors & redaction | `Exceptions/{ApiError,ApiException,Api*Error,RateLimitExceededError,ValidationError,RequiredParamError,GenericError,SensitiveDataRedactor}.cs`, `Exceptions/Parsers/{ApiErrorParser,RateLimitParser}.cs` |
| Models | Generated `Model/*.cs`; hand-written client models/options in `Client/Model/*.cs` |
| Tests | `src/OpenFga.Sdk.Test/{Api,ApiClient,Client,Configuration,Exceptions,Models,Telemetry}/*Tests.cs` |

## Shaping a Hand-Written Change

### Worked Pointer: Add a Client-Credentials Option

1. **Interface + struct (`Configuration/Credentials.cs`):** Add the nullable `PascalCase` property with XML doc comments to **both** `IClientCredentialsConfig` **and** the `CredentialsConfig` struct (which implements `ICredentialsConfig : IClientCredentialsConfig, IApiTokenConfig`).
2. **Validation (`Configuration/Credentials.cs`):** Validate the option in `Credentials.EnsureValid()` under `case CredentialsMethod.ClientCredentials:` (throw `FgaRequiredParamError` or `FgaValidationError`).
3. **Token request wiring (`ApiClient/OAuth2Client.cs`):**
   - In the `OAuth2Client` constructor, populate `_authRequest` when the option is non-null/non-whitespace (matching `ApiAudience` and `Scopes`). Never allow overwriting `client_id`, `client_secret`, or `grant_type`.
   - **Quirk:** `OAuth2Client.ExchangeTokenAsync` sets `ContentType = "application/x-www-form-urlencode"` (without trailing `d`), and `RequestBuilder.FormEncodedBody` checks that exact string before creating `FormUrlEncodedContent`. Do not change only one side.
   - Preserve `_exchangeLock` + `_inflightExchange` deduplication, `FgaConstants.TokenExpiryThresholdBufferInSec` + `TokenExpiryJitterInSec`, and `metrics.BuildForClientCredentialsResponse(...)`.
4. **Security:** Never expose secrets/tokens in exception messages or logs; update `Exceptions/SensitiveDataRedactor.cs` if introducing new sensitive fields.
5. **Tests (`src/OpenFga.Sdk.Test/`):** Add unit tests in `ApiClient/OAuth2ClientTests.cs` (and `Configuration/` tests for validation) using xUnit + Moq + mocked `HttpMessageHandler`; no live network calls.

## Idioms & Guardrails

- **Multi-target C# 10 discipline:** `netstandard2.0` and `net48` do **not** have `ImplicitUsings` enabled and target `LangVersion 10.0`. Always add explicit `using` statements, avoid C# 11+ syntax, and guard .NET 8+ APIs with `#if NET8_0_OR_GREATER` (or `#if NETSTANDARD2_0 || NET48`).
- **Naming & async:** `PascalCase` public types/members, `I*` interfaces (`IOpenFgaClient`, `IClientCredentialsConfig`), `Async` suffix on async methods accepting `CancellationToken cancellationToken = default`, `.ConfigureAwait(false)` in library awaits, nullable reference types enabled (`string?`), XML docs on public APIs (package builds XML documentation).
- **Backward compatibility quirk:** Do not rename existing public files/types with historical typos (e.g., `ClientReadAuthorizaionModelOptions.cs`).
- **Retry & errors:** Retry only 429 and 5xx except 501; never retry other 4xx; respect `Retry-After` capped at 1800s via `RetryHandler`.
- **Telemetry:** `fga_client_*` metrics via `System.Diagnostics.Metrics`, no PII, high-cardinality attributes off by default.

## PR Rules

- **PR title:** Conventional Commits (`feat`, `fix`, `docs`, `test`, `refactor`, `ci`, `perf`, `chore`, `revert`, `release`, optional scope). Do **not** add a title prefix; factory PRs carry the `factory` label.
- **Do not edit:** `CHANGELOG.md`, `.release-please-manifest.json`, `release-please-config.json`, `<Version>...<!-- x-release-please-version -->` in `OpenFga.Sdk.csproj`, or generated files in `.openapi-generator/FILES`.
- **Generated changes:** Route to `sdk-generator` (`config/clients/dotnet/template/`), never hand-patch in `dotnet-sdk`.
