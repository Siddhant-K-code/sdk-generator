# OpenFGA Python SDK Context

> Factory worker context for `openfga/python-sdk` (conventions adapted from `openfga/go-sdk#288` `AGENTS.md`/`.claude/rules/*` and verified against `python-sdk`).

## Stack & Verification

- **Package:** `openfga-sdk` (`requires-python = ">=3.10"`, `hatchling`, managed with `uv` + `uv.lock`).
- **Key deps:** `aiohttp` (async transport), `urllib3` (sync transport), `opentelemetry-api`, `python-dateutil`; dev: `ruff`, `pytest`, `pytest-asyncio` (`asyncio_mode = "strict"`), `mypy`.
- **Verified command:** `make check` (runs `uv run ruff check .`, `uv run ruff format .`, and `uv run pytest -m "not integration"`).
  - First-time env setup: `make sync` (`uv sync`).
  - Targeted test: `make test TEST=test/oauth2_test.py::test_name`.
  - Note: `make check` runs `ruff format .` in place — inspect `git diff` so unrelated files are not reformatted.

## Repository Layout: Generated vs. Hand-Written

Check `.openapi-generator/FILES` (189 entries) before editing. Never edit generated files by hand in an SDK PR; route those changes to `sdk-generator` (`config/clients/python/template/`).

- **Generated (do not hand-edit):**
  - `openfga_sdk/api/open_fga_api.py` (async low-level API — **quirk:** `.openapi-generator-ignore` lists `openfga_sdk/api/open_fga_api` without `.py`, so this file is still regenerated!)
  - `openfga_sdk/constants.py` (shared SDK constants; verified by `test/constants_consistency_test.py`)
  - `openfga_sdk/models/*.py` (89 API models + `__init__.py`; excluded from `mypy` in `pyproject.toml`)
  - `docs/**`, `README.md`, `CONTRIBUTING.md`
- **Ignored in `.openapi-generator-ignore` (hand-maintained):** `openfga_sdk/__init__.py`, `openfga_sdk/api/__init__.py`, `openfga_sdk/sync/open_fga_api.py`, `openfga_sdk/api_client.py`, `openfga_sdk/configuration.py`, `openfga_sdk/_version.py`, `openfga_sdk/exceptions.py`, `openfga_sdk/rest.py`, `test/*`, `VERSION.txt`.
  - **Quirk:** `openfga_sdk/sync/open_fga_api.py` is ignored in the SDK repo but has a generator template (`config/clients/python/template/api_sync.mustache`). Route API method generation changes through `sdk-generator` for both async and sync APIs.

### Where Core Concerns Live (Hand-Written — Async & Sync Twins)

Every behavioral change in transport, OAuth2, or high-level client code must be applied to **both** the async default package and its `openfga_sdk/sync/` twin.

| Concern | Async (default) | Sync twin | Shared |
| --- | --- | --- | --- |
| High-level client | `openfga_sdk/client/client.py`, `client/execute_api_request_builder.py` | `openfga_sdk/sync/client/client.py`, `sync/client/execute_api_request_builder.py` | `openfga_sdk/client/configuration.py`, `client/models/*.py` |
| HTTP client & retry | `openfga_sdk/api_client.py`, `openfga_sdk/rest.py` | `openfga_sdk/sync/api_client.py`, `openfga_sdk/sync/rest.py` | `openfga_sdk/configuration.py` (`Configuration`, `RetryParams`) |
| Credentials & OAuth2 | `openfga_sdk/oauth2.py` (`asyncio.Lock`) | `openfga_sdk/sync/oauth2.py` (`threading.Lock`) | `openfga_sdk/credentials.py`, `openfga_sdk/oauth2_common.py` |
| Telemetry (OTel) | — | — | `openfga_sdk/telemetry/{telemetry,metrics,attributes,configuration,counters,histograms}.py` |
| Errors & redaction | — | — | `openfga_sdk/exceptions.py`, `openfga_sdk/redaction.py`, `openfga_sdk/validation.py` |
| Models | — | — | Generated `openfga_sdk/models/*.py`; hand-written `openfga_sdk/client/models/*.py` |
| Tests | `test/oauth2_test.py`, `test/client/client_test.py`, `test/rest_test.py` | `test/sync/oauth2_test.py`, `test/sync/client/client_test.py`, `test/sync/rest_test.py` | `test/credentials_test.py`, `test/configuration_test.py`, `test/telemetry/*_test.py`, `test/redaction_test.py` |

## Shaping a Hand-Written Change

### Worked Pointer: Add a Client-Credentials Option

1. **Config model (`openfga_sdk/credentials.py`):** Add a `snake_case` optional parameter (`: ... | None = None`) to `CredentialConfiguration.__init__()`, store it as `self._<name>`, and add a `@property` getter and setter matching `api_audience`/`scopes`.
2. **Validation (`openfga_sdk/credentials.py`):** Inside `Credentials.validate_credentials_config()` under `if self.method == "client_credentials":`, normalize blank/whitespace values to `None` and raise `ApiValueError` on invalid input.
3. **Async + Sync OAuth2 wiring (`openfga_sdk/oauth2.py` AND `openfga_sdk/sync/oauth2.py`):**
   - In both `OAuth2Client._obtain_token()` implementations, read `configuration.<name>` and populate `post_params` (without overwriting core keys `client_id`, `client_secret`, `grant_type`).
   - Preserve `_TokenState` atomic snapshot, double-checked locking (`asyncio.Lock` in async, `threading.Lock` in sync), retry on 429/5xx except 501, and `TOKEN_EXPIRY_THRESHOLD_BUFFER_IN_SEC` + `TOKEN_EXPIRY_JITTER_IN_SEC`.
   - Never expose secrets in exceptions or logs; verify `openfga_sdk/redaction.py` if new sensitive fields are introduced.
4. **Tests (both twins):** Add unit tests in `test/credentials_test.py`, `test/oauth2_test.py` (`@pytest.mark.asyncio`), and `test/sync/oauth2_test.py`. Mock client requests — never make live HTTP calls. Note: `oauth2_common.jitter()` returns `0` whenever `"pytest" in sys.modules`.

## Idioms & Guardrails

- **Twin parity:** Keep `openfga_sdk.OpenFgaClient` (async) and `openfga_sdk.sync.OpenFgaClient` (sync) identical in method names, parameter names, defaults, and exceptions.
- **Typing & style:** Python 3.10+ unions (`str | None`), `snake_case` APIs, `ruff` 88-char line length, double quotes, isort spacing (`lines-between-types = 1`, `lines-after-imports = 2`).
- **Retry & errors:** Retry only 429 and 5xx except 501; never retry other 4xx; respect `Retry-After` capped at 1800s (`_parse_retry_after_header` in both `api_client.py` twins).
- **Telemetry:** `fga_client_*` metrics, no PII, high-cardinality attributes off by default.
- **Consistency guard:** `test/constants_consistency_test.py` enforces version/retry/UA alignment; do not touch `_version.py` or `constants.py` manually.

## PR Rules

- **PR title:** Conventional Commits (`feat`, `fix`, `docs`, `test`, `refactor`, `ci`, `perf`, `chore`, `revert`, `release`, optional scope). Do **not** add a title prefix — factory PRs carry the `factory` label.
- **Do not edit:** `CHANGELOG.md`, `.release-please-manifest.json`, `release-please-config.json`, `VERSION.txt`, `openfga_sdk/_version.py`, `pyproject.toml` `version`, `uv.lock` (unless adding a required dep), or generated files in `.openapi-generator/FILES`.
- **Generated changes:** Route to `sdk-generator` (`config/clients/python/template/`), never hand-patch in `python-sdk`.
