# DCA-033 — Provide a local contract-test HTTP server

**Status:** implemented. Adds `app/testing/fake_server.py` — a reusable, **non-production** fake
HTTP endpoint for contract tests. It is the generalised form of the ad-hoc `FakeHost` the DCA-032
manual check wired inline: a configurable fixture server that the real `PlatformHttpTransport` (or
any `httpx`-based client) can be pointed at **without a network**, so a single transport contract —
an outcome, a classified response, a denial — can be driven, reviewed and accepted in isolation.

No real network, no AI, no provider SDK/key, no paid action, no database writes. Future capabilities
stay disabled: the harness ships **no** domain fixtures of its own.

## What it does

| Capability | How |
|---|---|
| **Simulates transport outcomes** | A `FixtureRoute` maps `(method, path)` → `FixtureOutcome`. An outcome is either a `RESPONSE` (any status + JSON/text/headers), a `TIMEOUT` (raises `httpx.ConnectTimeout`/`ReadTimeout` — the transport classifies both as a stable `timeout`), or `UNREACHABLE` (raises `httpx.ConnectError` → `unreachable`). This exercises a caller's whole classification surface: success / 4xx / 5xx / 429 / timeout / unreachable. |
| **Configurable delay** | `FixtureOutcome.delay_seconds` applies a real sleep before the outcome (defaults to 0 so tests never sleep unasked). `sleep` is injectable so a test asserts the delay without a wall-clock wait. |
| **Records every call** | Each served request is captured as a `RecordedCall` (method, path, correlation id, body, and **redacted** headers — `Authorization`/`X-Service-Auth`/etc. are masked). `matched` records whether a fixture existed, so an unknown-contract denial is visible in the call log. |
| **Never invents a missing contract** | A request to a path with **no** registered fixture returns a deliberate `404 {"error":"no_fixture_configured", "synthetic":true}` — it does **not** fabricate a plausible success. The call is still recorded, as unmatched. |
| **Cannot run in production** | `build_fake_contract_server(settings)` raises `FakeServerForbiddenInProduction` under production configuration (mirrors the identity/entitlement fakes and the dev-only seed). The server also carries an explicit `NON_PRODUCTION_BANNER` via `.banner()`. |
| **Reuses only existing fixtures** | `register_identity_entitlement_fixtures(server)` is **opt-in** and seeds only the already-implemented identity (`app/identity/fake.py`) and entitlement (`app/entitlements/fake.py`) synthetic fixtures as read routes for the two synthetic businesses A/B. Every later adapter story (suppression, CRM, delivery, …) registers its **own** fixtures; nothing domain-specific is pre-wired. |

### Mount prefix

`FakeContractServer(base_path="/svc")` strips the mount prefix (e.g. the client's base-URL path)
from an incoming request path before matching, so fixtures are registered relative to the service
root regardless of where the endpoint is mounted. `build_fake_contract_server(..., base_path=...)`
passes it through.

### Usage

```python
from app.testing import FakeContractServer, FixtureOutcome
from app.clients.platform_transport import PlatformHttpTransport

server = FakeContractServer(base_path="/svc")
server.set_route("GET", "/tenants/A", FixtureOutcome.response(200, json={"tenant": "A"}))
transport = PlatformHttpTransport("https://platform.internal/svc", "cred",
                                  transport=server.as_transport())
transport.request("GET", "tenants/A")          # -> SUCCESS
server.set_route("GET", "/tenants/A", FixtureOutcome.timeout())
transport.request("GET", "tenants/A")          # -> raises PlatformTransportError(kind=timeout)
```

## Tests

`tests/test_fake_contract_server.py` (16 tests) drives the real `PlatformHttpTransport` over the
fake: success + call recording + credential masking; every classified HTTP outcome; connect/read
timeouts; unreachable; a configured delay; a route switched to a timeout; the unknown-contract 404
(the failure case); production refusal + banner; and identity/entitlement fixture reuse.

## Manual check

```text
python scripts/manual_check.py DCA-033
```

Prints the synthetic-mode banner, serves a registered route as a success, switches that same route
to a timeout, drives an unreachable route, hits an unregistered path (deliberate 404, recorded as
unmatched), and shows the builder refusing production settings. Local synthetic only; no paid action.

## Boundary

Stayed inside the DCA Python project (`app/testing/`, `tests/`, `scripts/manual_check.py`, this doc).
No AI-service, Web, Admin, .NET, or other-service changes. No migrations (the harness touches no DB).
