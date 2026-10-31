# DCA-129 — Call the existing AI service over HTTP (the authenticated adapter)

**Status:** Ready for manual review. No live/paid call; no migration; **no change to effortlesspromo-ai**.
Key: `ai-provider` · source sections 4, 8, 9. Prerequisite DCA-128 (and the earlier sequence) present —
the `app/ai/contract.py` facade and `app/services/ai_invoker.py` durable recorder exist; this story
**hardens the HTTP adapter they delegate to**.

## Boundary (what this story is, and is NOT)

The story title in the pack filename is "route-to-configured-model-providers", but the story body is
**"Call the existing AI service over HTTP"**: implement the *authenticated DCA HTTP adapter* to the
verified effortlesspromo-ai endpoint. **Provider routing / failover stays the gateway's job** (it owns
`AiProvider` + the resolver on the .NET side and provider config on the FastAPI side). The DCA adapter
only *suggests* an optional `modelHint` and **records which provider/model the gateway reports it used** —
it never selects one. **No provider SDK, provider key, model routing, or fallback model execution exists
in the DCA, and none ever may.**

## What changed (DCA only)

- `app/clients/ai_client.py` — hardened the HTTP adapter to the DCA-129 surface (mirroring the DCA-032
  platform transport):
  - **Allowlisted service URL** — pinned single origin (scheme+host+port) under a fixed `/v1/<capability>`
    path; the capability (the only caller-controlled segment) is validated as one safe segment, so a `/`,
    `..`, scheme, protocol-relative value or whitespace is **refused before any socket is opened**
    (`AiEndpointNotAllowed`); base-URL scheme must be http(s); **redirects are not followed**.
  - **Explicit deadlines** — separate connect + overall `httpx.Timeout` (never unbounded).
  - **Correlation ids** — adds `X-Correlation-Id` (current request's id or a minted one), **distinct**
    from the deterministic `X-Request-Id` idempotency key (B4), which a retry must replay unchanged.
  - **Redacted errors** — the service token is registered with the DCA-012 redactor at construction;
    `__repr__` never shows it; a 4xx raises `AiServiceRejected` carrying **only** the status code (never
    the response body / token / PII).
  - **Fail-safe failure classification** — new `AiServiceTimeout(AiServiceUnavailable)` for a read/write
    timeout (the request may already have been **submitted** → *uncertain*); connect failure/timeout,
    pool timeout and 5xx stay `AiServiceUnavailable` (not submitted / definite → safe to retry fresh);
    a 4xx is `AiServiceRejected` (non-retryable).
- `app/services/ai_invoker.py` — a timed-out (possibly submitted) call records `status="uncertain"`
  instead of `"unavailable"`, so recovery reconciles via a **same-request-id** replay against the
  gateway's idempotency cache rather than a blind re-run.
- `app/core/config.py` — new `ai_connect_timeout_seconds` (default 5.0) + a positive-value boot check for
  both AI timeouts.
- `app/ai/fake.py` — the live-client builder passes the new connect timeout through.
- `app/db/models.py` — `AiInvocation.status` comment now documents the `uncertain` value (free-text
  `String(16)`; no migration — `auto_create_schema` in dev/test, and the width already fits).
- `tests/test_ai_http_adapter.py` — focused DCA-129 tests (19): allowlist refusal before I/O, scheme
  validation, service-auth + distinct correlation/request headers, minted correlation id, redacted 4xx,
  non-leaking repr, read-timeout → uncertain, connect-failure → unavailable, 5xx, body-level failure as
  data, no provider/key surface, and the durable `uncertain`/`unavailable` rows via `invoke_recorded`.
- `scripts/manual_check.py` — registered scenario `DCA-129` (drives an **in-process fake gateway** via
  `httpx.MockTransport`; prints the authorized call's headers + the provider/model the gateway selected,
  the allowlist refusal, the redacted 4xx, and the durable `uncertain`/`unavailable` rows).
- `docs/DCA-129-call-the-existing-ai-service-over-http.md` — this file.

## Acceptance criteria → where satisfied

| Criterion | Where |
|---|---|
| Unauthorized calls fail safely | wrong token → gateway 401 → `AiServiceRejected` (non-retryable, redacted) |
| Schema mismatches fail safely | the DCA-128 facade's `parse_envelope` fails closed on a mismatched/incompatible envelope |
| Service outages fail safely | connect failure/5xx → `AiServiceUnavailable` (safe to retry fresh) |
| A timed-out possibly-submitted call is marked uncertain; not blindly replayed | read/write timeout → `AiServiceTimeout`; `invoke_recorded` writes `status="uncertain"`; recovery replays the SAME `X-Request-Id` against the gateway idempotency cache |
| Reproduce result + denial without unintended state changes | allowlist refusal raises **before any I/O** (no row/call); manual check prints real outcomes |
| No direct model SDK / provider key / routing / fallback in DCA | adapter exposes no provider/model/key surface (tested); only an optional `modelHint` is forwarded |

## Verification (all run)

```text
.venv/Scripts/python.exe -m pytest tests/test_ai_http_adapter.py -q      # focused: 19 passed
.venv/Scripts/python.exe -m pytest -q                                    # full: 1850 passed, 155 skipped, exit 0
.venv/Scripts/python.exe scripts/manual_check.py DCA-129                 # all 8 printed checks True
.venv/Scripts/python.exe -m ruff check app/clients/ai_client.py app/services/ai_invoker.py app/core/config.py app/ai/fake.py tests/test_ai_http_adapter.py scripts/manual_check.py   # clean
.venv/Scripts/python.exe -m ruff format --check <touched>                # clean
.venv/Scripts/python.exe -m mypy app/clients/ai_client.py app/services/ai_invoker.py app/ai/fake.py app/core/config.py   # clean (4 files)
```

Repo-wide `ruff check .` reports 2 **pre-existing** F401 errors in `app/services/policy_decision_record.py`
— unrelated to this story and untouched.

## Blockers / kept disabled

- **Live execution stays off by default** — `ai_enabled=False` (no `ai_base_url`), so the facade uses the
  dev/test fixture transport and spends nothing. The hardened live adapter is exercised only against the
  in-process fake gateway in tests / the manual check (no socket, no spend).
- Provider routing/failover and prompt/output-validation remain the effortlesspromo-ai gateway's, per the
  DCA boundary. No effortlesspromo-ai change was needed or made.
