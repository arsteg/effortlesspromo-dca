# DCA-032 — Create the platform HTTP transport

**Status:** implemented. Adds `PlatformHttpTransport` (`app/clients/platform_transport.py`) — a
hardened, generic HTTP transport **mechanism** for calling the .NET platform API. It is only the
transport: single allowlisted origin, mandatory scoped credential, explicit timeouts, correlation
ids, and a stable, safe error mapping. It has **no** knowledge of any platform capability. Business
adapters (suppression, CRM, entitlement, delivery, …) are deliberately **separate and stay disabled**
until DCA-001 blocker **B1** (a scoped service-to-service contract into the .NET API) is designed and
accepted. No provider SDKs/keys, no model routing, no second AI gateway, no writes to another
service's database. Not wired into any live route — a ready, independently reviewable capability.
Future capabilities stay disabled.

## What it does

`PlatformHttpTransport` performs one hardened request against the platform endpoint and classifies
the outcome. It is distinct from the AI gateway client (`app/clients/ai_client.py`): the AI client
targets effortlesspromo-ai on a fixed contract; this is the general platform transport the B1 adapters
will be built on.

| Guarantee | How |
|---|---|
| **Allowlisted base URL** | Every call is pinned to the one configured origin (scheme+host+port) and base path. A path that is an absolute URL, protocol-relative (`//host`), or uses a `..` traversal is **refused before any socket opens** (`PlatformEndpointNotAllowed`). Redirects are **not** followed, so a 3xx cannot bounce a call off-allowlist. |
| **Scoped credentials** | A scoped credential is **mandatory** (fail-closed: no credential ⇒ the transport refuses to construct). It is sent on a configurable header (`Authorization: Bearer <cred>` by default) and is **registered with the DCA-012 redactor** at construction, so its literal can never appear in a log line. It is never placed in an exception message or `repr`. |
| **Explicit timeouts** | Separate connect and overall timeouts are always set (`httpx.Timeout`) — never an unbounded wait. |
| **Correlation ids** | Every request carries `X-Correlation-Id` + `X-Request-Id`, reusing the current request's correlation id (the DCA-012 contextvar) when the caller does not supply one, so a trace spans services. |
| **Safe, stable error mapping** | Transport failures map to `TransportErrorKind` — **every** timeout phase (connect/read/write/pool) classifies identically as `timeout`; other network failures as `unreachable`. HTTP responses (incl. 4xx/5xx) are **returned**, not raised, as a `TransportResult` with a stable `ResponseClass` (`success`/`unauthorized`/`forbidden`/`rate_limited`/`client_error`/`server_error`). Exception messages carry only the classification, never request detail or the credential. |

### Off by default (fail-closed)

The transport is disabled until **both** `EFFORTLESSPROMO_DCA_PLATFORM_BASE_URL` and
`EFFORTLESSPROMO_DCA_PLATFORM_SERVICE_CREDENTIAL` are set (`Settings.platform_transport_configured`).
`validate_required` rejects a base URL with no credential and non-positive timeouts at boot, so the
transport is never half-wired. `build_platform_transport(settings)` returns `None` when unconfigured
(same shape as the identity/entitlement factories) — so any adapter built on it simply has no
transport and stays off (B1). The credential **value** is injected out-of-band; config carries only
`platform_service_credential_secret_ref`, required when `secret_source` is non-env.

### Config keys (names only; values are out-of-band secrets)

`platform_base_url`, `platform_service_credential` (+ `_secret_ref`), `platform_credential_header`
(default `Authorization`), `platform_credential_scheme` (default `Bearer`; `""` ⇒ bare credential),
`platform_connect_timeout_seconds` (5.0), `platform_timeout_seconds` (30.0). Env prefix
`EFFORTLESSPROMO_DCA_`.

## Tests

`tests/test_platform_transport.py` (every exchange via `httpx.MockTransport` — no network) pins:
disabled without base URL/credential; non-http base rejected; the factory returns `None` when
unconfigured; a relative path stays within the configured origin; an absolute/protocol-relative/`..`
path is **refused before any I/O** (the fake host's call counter stays at 0 — the denial changes
nothing); a redirect is not followed; the scoped credential + correlation headers are sent; a
correlation id is minted when absent; a custom header + bare scheme; the `repr` never contains the
credential; the credential is redacted from every emitted log line; **every** timeout phase classifies
as `timeout` and the raised message omits the credential; network failures classify as `unreachable`;
and the full status→`ResponseClass` table is stable. `tests/test_config.py` adds the DCA-032
configuration cases (off by default; base-without-credential rejected; configured passes;
non-positive timeout rejected; non-env secret-source requires the credential ref).

Quality gate (all clean): `ruff check .` · `ruff format --check .` · `mypy` · `pytest`
(343 passed, 32 skipped — the skips are the Postgres-gated suites, unrelated to this story; this
story needs no database, migration, or Postgres).

## Manual check

```
python scripts/manual_check.py DCA-032
```

Against a **fake host** (`httpx.MockTransport`, no network), for the one allowlisted endpoint, it
shows side by side: (1) a success → `SUCCESS`, exactly one call on the wire, credential sent as
`Authorization: Bearer <cred>`; (2) a 401 → `UNAUTHORIZED` (returned, not raised); (3) a connect-phase
**and** a read-phase timeout → both classify identically as a stable `timeout`, with the credential
absent from the raised message; (4) off-allowlist paths (`https://evil…`, `//evil…`, `../../escape`)
→ **refused before any I/O** (zero calls reach the fake host). It then greps the emitted transport
logs and confirms the credential literal never appears. Local synthetic only; no real network, no AI,
no paid action, no state changed.

## Boundary

Inside the DCA only: `app/clients/platform_transport.py` (new), the DCA-032 config keys +
`platform_transport_configured` property + `validate_required` checks (`app/core/config.py`), one
redactor registration for the platform credential (`app/core/logging.py`),
`tests/test_platform_transport.py`, the DCA-032 config tests (`tests/test_config.py`), this doc, and
the DCA-032 manual-check scenario (`scripts/manual_check.py`). No migration (no new tables). No UI, no
AI-service internals, no other service. The transport is **not** wired into any route or adapter —
the B1 business adapters remain a separate, disabled capability.
