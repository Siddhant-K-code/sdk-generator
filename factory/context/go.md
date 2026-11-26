# OpenFGA Go SDK Context

> Factory worker context for `openfga/go-sdk` (adapted from `openfga/go-sdk#288`: `AGENTS.md` and `.claude/rules/*`).

## Stack & Verification

- **Module:** `github.com/openfga/go-sdk` (`go 1.26.0`, `toolchain go1.27.3` in `go.mod`).
- **Key deps:** `go.opentelemetry.io/otel` (metrics), `github.com/sourcegraph/conc` (batch concurrency), `github.com/stretchr/testify`, `github.com/jarcoal/httpmock`.
- **Verified command:** `make check` (runs `fmt`, `lint` (`go vet` + `golangci-lint`), `test` (`go test -race`), and `security` (`gosec` + `govulncheck`)).
  - Single package: `go test -race -v ./client/...`
  - Note: `make fmt` runs `gofmt -w .` in place (use `gofmt`, not `goimports`).

## Repository Layout: Generated vs. Hand-Written

Check `.openapi-generator/FILES` (192 entries) before editing any file. Never edit generated files by hand in an SDK PR; route those changes to `sdk-generator` (`config/clients/go/template/`).

- **Generated (do not hand-edit):**
  - `model_*.go` (API data models; excluded from `golangci-lint` in `.golangci.yaml` — keep that exclusion)
  - `api_open_fga.go` (low-level generated API — deprecated in favor of hand-written `api_executor.go`)
  - `internal/constants/constants.go` (shared SDK constants: batch limits, retry defaults, token buffers)
  - `streaming_test.go`, `docs/**`, `README.md`, `CONTRIBUTING.md`, `VERSION.txt`
- **Ignored in `.openapi-generator-ignore` (hand-maintained):** `client.go`, `utils.go`, `configuration.go`, `response.go`, `go.mod`, `api_client.go`, `api/openapi.yaml`, `test/api_open_fga_test.go`.
- **Quirk:** `AGENTS.md` lists `streaming.go` as generated, but no `streaming.go` exists; `StreamingChannel[T]` and `APIExecutorStreamingChannel` live in hand-written `api_executor.go` (while `streaming_test.go` is generated).

### Where Core Concerns Live (Hand-Written)

| Concern | Locations |
| --- | --- |
| High-level client wrapper | `client/client.go` (`OpenFgaClient`, `SdkClient`, `ClientConfiguration`), `client/errors.go` |
| Execution, HTTP & streaming | `api_executor.go` (preferred for new API methods + NDJSON streaming), `api_client.go`, `configuration.go`, `response.go`, `utils.go` |
| Credentials & OAuth2 | `credentials/credentials.go`, `oauth2/clientcredentials/clientcredentials.go`, `oauth2/token.go`, `oauth2/transport.go`, `oauth2/internal/token.go` |
| Retry & backoff | `internal/utils/retryutils/retryparams.go`, `internal/utils/retryutils/retryutils.go` |
| Telemetry (OTel) | `telemetry/{configuration,metrics,attributes,counters,histograms,telemetry}.go`; recorded in `api_executor.go` and `api_client.go` |
| Errors & ULID validation | `errors.go`, `client/errors.go`, `internal/utils/ulid.go` |
| Models | Generated `model_*.go` (API DTOs); hand-written `Client*` request/response structs in `client/client.go` |
| Tests | Colocated `*_test.go` (`api_client_test.go`, `api_executor_test.go`, `client/client_test.go`, `oauth2/**/*_test.go`, `telemetry/*_test.go`); Docker integration suite in `tests/integration/` (separate `go.mod`) |

## Shaping a Hand-Written Change

### Worked Pointer: Add a Client-Credentials Option

1. **Config struct (`credentials/credentials.go`):** Add the field to `credentials.Config` using the `ClientCredentials*` prefix and `json:"...,omitempty"` tag (alongside `ClientCredentialsApiAudience` and `ClientCredentialsScopes`).
2. **Validation (`credentials/credentials.go`):** Validate or normalize the field inside `(*Credentials).ValidateCredentialsConfig()` under `c.Method == CredentialsMethodClientCredentials`. Keep `Credentials` backward-compatible when omitted.
3. **Token request wiring (`credentials/credentials.go` & `oauth2/clientcredentials/clientcredentials.go`):**
   - In `(*Credentials).GetHttpClientAndHeaderOverridesWithBase()` (`case CredentialsMethodClientCredentials:`), populate `ccConfig.EndpointParams` (`url.Values`) for extra form fields, or extend `clientcredentials.Config` and handle it in `tokenSource.Token()` before calling `internal.RetrieveToken(...)`.
   - Do not overwrite `grant_type` or core OAuth parameters unexpectedly; preserve `baseClient` wrapping so custom `Timeout`/`Transport` apply to token calls.
   - Keep OAuth2 token refresh thread-safe and preserve the 300s expiry threshold + 300s jitter (`internal/constants/constants.go`).
4. **Security:** Never log or interpolate secrets/tokens into error strings or debug output.
5. **Tests:** Note `credentials/` currently has no `credentials_test.go` — credential tests live in `api_client_test.go` (or add a new `credentials/credentials_test.go`), plus `oauth2/clientcredentials/clientcredentials_test.go`. Use table-driven subtests, `t.Parallel()` at both top level and inside `t.Run()`, `require`/`assert`, and `httpmock` (no live network, no `time.Sleep`, no wall-clock expiry assertions).

## Idioms & Guardrails

- **Fluent client API:** `client.Method(ctx).Body(...).Options(...).Execute()`. Always take `context.Context` first and propagate it. Add new client methods to the `SdkClient` interface in `client/client.go`.
- **New API methods:** Must call `api_executor.go` (never `api_open_fga.go` or raw HTTP), set `OperationName` for `fga_client_request_method`, pass `storeId`, and validate `StoreId`/`AuthorizationModelId` as ULIDs.
- **Helpers & deprecations:** Use `openfga.ToPtr[T]()` (not deprecated `PtrString`/`PtrBool`/`PtrInt`). Use `ApiUrl` (not deprecated `ApiScheme`/`ApiHost`). Use `github.com/sourcegraph/conc` for concurrent batches.
- **Retry & errors:** Return typed errors from `errors.go`; inspect with `errors.As()`. Retry only HTTP 429 and 5xx excluding 501; never retry 4xx (except 429); respect `Retry-After` / `X-RateLimit-Reset` capped at 1800s; always close HTTP response bodies.
- **Streaming:** Drain both `channel.Results` and `channel.Errors` in a `select` with `<-ctx.Done()` and `defer channel.Close()`; test with `httptest.NewServer`.
- **Telemetry:** Use `fga_client_*` names, no PII, high-cardinality attributes off by default in `DefaultTelemetryConfiguration()`, and `noop.NewMeterProvider()` in tests.
- **Examples:** `example/*/go.mod` are independent submodules not run by root `make check`.

## PR Rules

- **PR title:** Conventional Commits (`feat`, `fix`, `docs`, `test`, `refactor`, `ci`, `perf`, `chore`, `revert`, `release`, optional scope). Do **not** add a title prefix — factory PRs carry the `factory` label.
- **Do not edit:** `CHANGELOG.md`, `.release-please-manifest.json`, `release-please-config.json`, or any generated file in `.openapi-generator/FILES`.
- **Generated changes:** Route to `sdk-generator` (`config/clients/go/template/`), never hand-patch in `go-sdk`.
