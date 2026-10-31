# DCA-128 — the single DCA AI-capability facade (the AI-service contract)

**Status:** implemented client contract + fixture transport. No live/paid call; no migration; no change
to effortlesspromo-ai. Key: `ai-facade` · source sections 4, 8, 9. Prerequisite DCA-127 and the earlier
sequence are present (the `app/clients/ai_client.py` transport and `app/services/ai_invoker.py` durable
recorder already exist; this story adds the typed boundary + chokepoint over them).

## What this is

One chokepoint — `app/ai/contract.py:AiCapabilityFacade` — through which **all** DCA model work is
requested from the existing effortlesspromo-ai gateway over authenticated HTTP. It is a **client
contract, not a second capability registry or gateway**: the gateway owns every provider, model-routing,
prompt, and output-validation decision (ADR-0004 / the DCA boundary). The facade only gates which
capabilities the DCA may request, marshals a DCA-owned typed request onto the verified gateway envelope,
delegates to the transport, and parses the response back into a DCA-owned typed result — failing
**closed** on anything it cannot vouch for.

## Changed / added files (DCA only)

- `app/ai/__init__.py` — package seam + exports.
- `app/ai/contract.py` — DCA-owned typed boundary models (`AiCapabilityRequest`, `AiJobBinding`,
  `AiCapabilityResult` + `AiUntrustedItem`/`AiTokenUsage`/`AiCost`/`AiValidation`/`AiError`), the
  fail-closed capability allowlist (`KNOWN_CAPABILITIES`), the DCA-owned job-id namespace
  (`DCA_JOB_ID_PREFIX = "dca:"`) + `dca_job_binding`, the transport `Protocol` (`AiGatewayTransport`),
  `parse_envelope` (fail-closed response parser), and the facade (`AiCapabilityFacade`).
- `app/ai/fake.py` — the deterministic fixture transport (`FakeAiGatewayTransport`, no network/model/
  spend) + builders (`build_ai_gateway_transport`, `build_ai_capability_facade`) that select the live
  `AiClient` when the gateway is configured, the fixture in dev/test, and **refuse** in production
  (`AiContractDisabled`).
- `tests/test_ai_facade.py` — focused tests (happy path + every fail-closed case + builder selection).
- `scripts/manual_check.py` — registered scenario `DCA-128` (prints a real stubbed invocation + denials).
- `docs/DCA-128-ai-capability-facade.md` — this file.

## Reused semantics (from `docs/DCA-001-integration-contracts.md` §2 — not re-invented)

- **Envelope** (`effortlesspromo-ai/app/gateway/envelope.py`): request carries `jobId`, `capability`,
  `businessId`, `inputContext`, `untrusted[]`, `dryRun`; response carries `status`, `structuredOutput`,
  `confidence`, validation/human-review/token-usage/cost metadata. The DCA models mirror **only** the
  fields the DCA consumes; the gateway stays the schema authority.
- **Job-ID allocation (B2):** a DCA invocation has no .NET `AIJob` row, so a DCA job id lives in its own
  `dca:<AiInvocation id>` namespace (minted by `app/services/ai_invoker.py`). The facade **refuses** any
  job id outside that namespace, so a DCA job can never collide with or imitate a platform-owned AIJob id
  — **existing-service job ownership is respected**.
- **Tenant binding (B3):** the gateway's `X-Service-Auth` authenticates the *service*, not a tenant;
  `businessId` is caller-asserted. The facade requires the binding's `business_id` to equal the request's,
  so a caller can only invoke for the tenant it has itself authorized.
- **Idempotency (B4):** the deterministic `X-Request-Id` (= the `dca:<id>` request id) is carried on the
  binding so a retry replays the same id and the gateway serves it from its cache (no double spend). The
  durable spend/replay record remains the DCA's own `AiInvocation` rows.

## Fail-closed behaviour (acceptance criteria)

| Case | Where | Result |
|---|---|---|
| Unknown capability (not allowlisted) | facade, before any call | `AiCapabilityDisabled` |
| Transport not configured (AI off) | facade | `AiCapabilityDisabled` |
| Job/request id outside `dca:` namespace | facade | `AiContractViolation` |
| Binding tenant ≠ request tenant | facade | `AiContractViolation` |
| Response `jobId` ≠ bound job id | `parse_envelope` | `AiContractViolation` |
| Response `capability` ≠ requested | `parse_envelope` | `AiContractViolation` |
| Missing required envelope field / unknown status (incompatible schema) | `parse_envelope` | `AiContractViolation` |
| Body-level `status=failed` | `parse_envelope` | returned as **data** (a legitimate outcome, not a parse error) |
| Transport/5xx error | transport | `AiServiceUnavailable` (retry with the same request id) |

Production refuses the synthetic fixture transport (`AiContractDisabled`); when the gateway is configured
the live `AiClient` is used (still 401s fail-closed on a wrong token).

## Blockers / unsupported contract extensions (kept disabled, documented — not implemented here)

- **Future capabilities stay disabled.** `KNOWN_CAPABILITIES` is deliberately narrow (currently only
  `contact_research`, the one key a through-DCA-127 accepted story already references — and whose live
  paid fetch is itself disabled). A later AI-capability story adds its reviewed key; listing a key only
  permits *requesting* it and never enables a live/paid call.
- **Live execution is off by default** — the gateway is unconfigured (`ai_enabled = False`), so the
  facade uses the dev/test fixture transport and spends nothing. No existing caller is rewired onto the
  facade in this story (that is later work); the facade is added alongside the existing dict-based seam.
- **Durable allocation stays `ai_invoker`'s job** — `dca_job_binding` reuses the `dca:<id>` convention
  but the persisted `AiInvocation` row (the durable spend/replay record) is still allocated by
  `app/services/ai_invoker.py`; the facade does not write to the DB.

## Verification

```text
.venv/Scripts/python.exe -m pytest tests/test_ai_facade.py -q      # focused: 17 passed
.venv/Scripts/python.exe -m pytest -q                              # full suite (exit 0 authoritative)
.venv/Scripts/python.exe scripts/manual_check.py DCA-128           # prints the stubbed invocation + denials
ruff check app/ai tests/test_ai_facade.py scripts/manual_check.py  # clean
ruff format --check app/ai tests/test_ai_facade.py                 # clean
mypy app/ai                                                        # clean (3 files)
```

`mypy scripts/manual_check.py` reports 56 **pre-existing** errors unrelated to this story (none in the
`DCA-128` scenario region); the baseline count is unchanged.
