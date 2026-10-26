# DCA-015 — Validate API authentication (end-user bearer tokens)

**Status:** implemented. Adds inbound end-user authentication to the DCA service. No other service
changed. The identity/authorization half (DCA-014) is unchanged; this is the authentication half of
the same auth-port seam.

## What it does

A FastAPI dependency, `require_authenticated_subject` (`app/core/security.py`), authenticates the end
user whose bearer token the caller forwards. It validates a JWT's **issuer, audience, signature and
expiry** and rejects anything that fails. It is distinct from `require_service_auth`, which
authenticates the *calling service* via `X-Service-Auth` (per DCA-001 B3 the shared secret is service
identity, never tenant/user authority).

- Port: `TokenVerifier` (`app/identity/tokens.py`) — `verify(token) -> AuthenticatedPrincipal`.
- Real impl: `JwtTokenVerifier` — **HS256 only**. Enforces `algorithms=["HS256"]` (so `alg: none`
  and algorithm-confusion are rejected) and requires the `exp`/`iss`/`aud`/`sub` claims.
- Dev/test impl: `FakeTokenVerifier` — accepts `fake:<subject>[:role,role]`, no crypto. **Never
  constructed in production** (`build_token_verifier`).
- Diagnostic route: `GET /v1/whoami` — echoes the validated principal. **Read-only**: no DB access,
  no state change, no authorization decision.

## Selection (`build_token_verifier`) — fail-closed

| Config present (issuer+audience+signing secret) | Environment | Verifier |
|---|---|---|
| yes | any | `JwtTokenVerifier` (real validation) |
| no | development / testing | `FakeTokenVerifier` (dev convenience) |
| no | production | `None` → dependency returns **401** (contract disabled) |

Every rejection (missing / malformed / forged / expired / wrong-audience / wrong-issuer / no subject
/ `alg: none`) collapses to a single generic **401 `unauthorized`** so the response is never an oracle
for *why* a token failed.

## Configuration (env prefix `EFFORTLESSPROMO_DCA_`)

| Key | Meaning |
|---|---|
| `AUTH_TOKEN_ISSUER` | expected `iss` |
| `AUTH_TOKEN_AUDIENCE` | expected `aud` |
| `AUTH_TOKEN_SIGNING_SECRET` | HS256 shared secret (value injected out-of-band) |
| `AUTH_TOKEN_SIGNING_SECRET_REF` | where that secret lives (required only for a non-env `SECRET_SOURCE`) |
| `AUTH_TOKEN_ALGORITHM` | default `HS256`; any other value fails boot |
| `AUTH_TOKEN_LEEWAY_SECONDS` | clock-skew tolerance for `exp`/`nbf` (default 60) |

Boot (`validate_required`) rejects an unsupported algorithm and a **partially** configured trio (all
three of issuer/audience/signing-secret, or none — never a half-wired auth that silently disables).

## Deferred (a missing contract stays disabled)

Asymmetric signing (RS256 / JWKS) and the **trusted introspection contract** are **not** implemented
here — they need the live platform identity adapter (DCA-001 blocker B1). They slot in later as
additional `TokenVerifier` implementations behind the same port and dependency, with no route change.

## Verify it

- Tests: `pytest tests/test_api_authentication.py`
- Manual check (local synthetic only, no paid/model action): `python scripts/manual_check.py DCA-015`
