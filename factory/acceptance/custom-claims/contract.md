# Contract: custom token-request fields (`customClaims`)

## Source and scope

- **Source change:** [openfga/js-sdk#228](https://github.com/openfga/js-sdk/pull/228).
  The JS SDK added `credentials.config.customClaims?: Record<string, string>`.
  `credentials.ts` spreads `customClaims` into the token form first and then sets
  `client_id`, `client_secret`, `audience` and `grant_type`.
- **In scope:** extra string fields sent in the OAuth2 **client-credentials
  (client secret)** token request.
- **Out of scope:** private-key JWT / client assertions, signing JWT claims,
  API-token auth, retries, token caching policy, error mapping, generated API
  code, and any change to the FGA API request itself.
- **Targets:** Go, Python, Java, .NET. (JS is the reference implementation.)

## Public option per SDK

Each name follows the SDK's existing client-credentials configuration naming.
The type is a string-to-string map. When the option is unset (the default),
behaviour is unchanged.

| SDK | Option | Type | Notes |
|---|---|---|---|
| JS (reference) | `credentials.config.customClaims` | `Record<string, string>` | Already shipped. |
| Go | `credentials.Config.ClientCredentialsCustomClaims` | `map[string]string` | JSON tag `customClaims,omitempty`, matching the `ClientCredentials*` field prefix. `ClientCredentialsTokenRequestFields` was considered and rejected for parity with JS. |
| Python | `CredentialConfiguration(custom_claims=...)` and the `custom_claims` property | `dict[str, str] \| None` | Default `None`. Must work for both the async and the sync client. |
| Java | `ClientCredentials.customClaims(Map<String, String>)` / `getCustomClaims()` | `Map<String, String>` | Fluent setter, like the other `ClientCredentials` options. |
| .NET | `CredentialsConfig.CustomClaims` | `IDictionary<string, string>?` | `OAuth2Client` reads `ICredentialsConfig`. Adding the member to `IClientCredentialsConfig` breaks third-party implementers of that interface, so that is a maintainer decision. The harness only requires the property on `CredentialsConfig`. |

The harnesses for statically typed SDKs locate the option **by reflection**,
using the exact name above. Because of this they compile against the current
SDK. Cases that need the option fail with `option missing` until the option
exists.

## Required behaviours

1. **Custom fields are sent.** Every custom claim is sent as a
   form-urlencoded field of the token request. Values are URL-encoded; the
   test value contains a space, `/` and `&`.
2. **Standard fields win on a key clash.**
   - `client_id`, `client_secret` and `grant_type` always carry the configured
     values. A custom value for these keys is never sent.
   - `audience` carries the configured audience when one is set. When no
     audience is configured, a custom `audience` is **not** injected; the field
     stays absent.
   - `scope`: non-empty configured scopes win over a custom `scope`. If the
     configured scopes are blank or unset, a custom `scope` is sent.
3. **No custom claims means the token request is unchanged.** If the option
   is unset or an empty map, the token form is byte-for-byte the same set of
   fields that the SDK sends today.
4. **The config is not mutated.** The map supplied by the caller is the same
   after the client is built and a token is exchanged.

Invariants that must not regress:

- The token is cached. Two checks on one client cause exactly one token
  exchange.
- The bearer token is forwarded to the FGA API.
- A token-endpoint `401` surfaces as an error with status `401`, and no FGA API
  request is made.

## Native baseline token request

The tests compare the token form **exactly**: the baseline below plus the
expected custom fields.

| SDK | Client credentials sent as | Baseline form fields |
|---|---|---|
| Python, Java, .NET | form fields | `client_id`, `client_secret`, `grant_type=client_credentials`, `audience` (if set), `scope` (if set) |
| Go | HTTP Basic auth (`golang.org/x/oauth2` style) | `grant_type=client_credentials`, `audience` (if set), `scope` (if set) |

For Go, the harness accepts credentials in either Basic auth or the form, as
long as they equal the configured values. Go's auto-detect may retry a
rejected request with form credentials, so the `denied` case allows more than
one exchange.

## Cases

The cases come from the earlier factory scenario `openfga-token-fields`. All
cases use the following configuration:

- `client_id=factory-client`
- `client_secret=factory-secret`
- `audience=factory-audience` (except `no-audience`)
- store `01ARZ3NDEKTSV4RRFFQ69G5FAV`
- model `01ARZ3NDEKTSV4RRFFQ69G5FAW`
- retries disabled

Every successful case checks `user:anne` (allowed) and then `user:bob`
(denied) on one client.

`ORG` is `factory team/one & two`.

| Case | Kind | `customClaims` | Other configuration | Expected extra token fields |
|---|---|---|---|---|
| `default` | preserve | unset | – | none |
| `empty` | change | `{}` | – | none |
| `custom` | change | `{organization: ORG, scope: "read:tuples"}` | – | `organization=ORG`, `scope=read:tuples` |
| `reserved` | change | `{organization: ORG, client_id: "wrong", client_secret: "wrong", grant_type: "wrong", audience: "wrong"}` | – | `organization=ORG`; canonical values for the reserved keys |
| `denied` | preserve | unset | token endpoint returns `401` | none; the check errors with `401`; 0 API requests |
| `scopes` | change | as `custom` | scopes `write:tuples list:objects` | `organization=ORG`, `scope=write:tuples list:objects` |
| `empty-scopes` | change | as `custom` | scopes `""` | `organization=ORG`, `scope=read:tuples` |
| `no-audience` | change | as `reserved` | no audience | `organization=ORG`; `audience` absent |

- **Preserve** cases must pass on the current SDK and on the change.
- **Change** cases are expected to fail on the current SDK (`option missing`)
  and must pass on the change.

Python runs every case through both the async and the sync client.
