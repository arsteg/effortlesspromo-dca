# DCA-131 — minimize the AI-service context before egress

**Status:** implemented (pure/deterministic screen, wired into the DCA-128 facade). No live/paid call; no
migration; no change to effortlesspromo-ai. Key: `ai-minimize` · source sections 4, 8, 9. Prerequisite
DCA-130 and the earlier sequence are present (`app/ai/contract.py` facade + `app/ai/bound_invocation.py`
invoker already exist; this story adds the minimization screen they apply before egress).

## What this is

Capability-specific **context minimization** that runs inside the DCA before any data is sent to the
existing effortlesspromo-ai gateway. For each capability the DCA may request, only the fields that
capability needs — the **authorized evidence and provenance references** — are allowed to egress;
secrets, the internal send sentinel, other-tenant evidence and unnecessary personal fields are stripped
or redacted, and the total size is bounded. The DCA does this **itself**, in addition to the gateway's own
privacy screens, so a recording fake AI HTTP service in front of the gateway can never observe a forbidden
field — the DCA never sends one.

It is the AI-context counterpart of the DCA-125 licensed-provider `PERMITTED_ORG_FIELDS` allowlist and the
DCA-058 field-minimization screen, reused at the gateway seam rather than re-invented.

## Changed / added files (DCA only)

- `app/ai/context_minimizer.py` — **new.** The pure, deterministic, allowlist-first minimizer:
  `CAPABILITY_CONTEXT_ALLOWLIST` (per-capability permitted fields), `FORBIDDEN_CONTEXT_KEYS`
  (always-dropped secret/credential/sentinel keys, reusing DCA-012 `SENSITIVE_KEYS`),
  `INTERNAL_SEND_SENTINEL`, `DEFAULT_MAX_CONTEXT_CHARS = 20000`, the `ContextMinimizer` class +
  `minimize_context`/`default_context_minimizer`, the `MinimizedContext` inspectable result
  (value-free `log_summary`), and `ContextMinimizationError` (a subclass of `AiContractViolation`).
- `app/ai/contract.py` — the DCA-128 `AiCapabilityFacade` now takes an injectable `minimizer` (defaults to
  the process-wide one via a lazy import that avoids the cycle) and **minimizes `input_context` per
  capability BEFORE calling the transport**. An un-minimizable/oversize context raises `AiContractViolation`
  (fail closed, no call made).
- `app/ai/fake.py` — the fixture transport is now a **recording** fake: it captures the exact
  `input_context`/`untrusted` it was handed, so a test can inspect the outgoing envelope and prove no
  forbidden field egressed.
- `app/core/logging.py` — added `Redactor.redact_secrets(text)` (secret literals only, no email/phone
  masking) so the minimizer can scrub a smuggled secret out of an otherwise-legitimate evidence value
  while a legitimate role email survives. `redact_text` now delegates to it (behaviour unchanged).
- `app/ai/__init__.py` — package exports for the new symbols.
- `tests/test_ai_context_minimization.py` — **new.** Focused tests (below).
- `scripts/manual_check.py` — registered scenario `DCA-131` (prints BEFORE vs AFTER context + the
  minimization account + an oversize refusal).
- `docs/DCA-131-minimize-model-context.md` — this file.

## The minimization order (pure, deterministic, per field)

1. **Capability allowlist lookup** — an unmapped capability is refused (`context_capability_not_minimizable`,
   fail closed). Future capabilities stay disabled: each ships its own reviewed allowlist in its own story.
2. **Forbidden key** → drop (`dropped_forbidden_key`) — before the allowlist is even consulted.
3. **Not allowlisted** → drop (`dropped_not_allowlisted`) — out-of-scope PII and other-tenant evidence.
4. **DCA-058 prohibited personal class** → drop (`dropped_prohibited_class`) — a wrongly-allowlisted or
   mislabelled field cannot launder a private mobile / sensitive / unnecessary identifier.
5. **Keep** the allowlisted field after scrubbing the internal send sentinel + any registered secret
   literal from its value (`redacted_secret_or_sentinel`); legitimate contact evidence (a role email) is
   not masked.
6. **Bound** the serialized size; oversize is refused (`context_too_large`) before any egress.

`contact_research` (the one DCA-128-allowlisted capability) permits only company-identity evidence
(`org_name`, `legal_name`, `org_identifier`, `industry`, `employee_count`, `annual_revenue_band`,
`hq_state`, `region`, `website`) and provenance references (`org_ref`, `source_key`, `source_ref`,
`source_version`, `evidence_ref`, `provenance`) — no personal contact channel.

## Acceptance criteria → evidence

| Criterion | Evidence |
|---|---|
| A recording fake AI service cannot observe forbidden sentinel fields | `test_facade_egresses_only_minimized_context` asserts the recording `FakeAiGatewayTransport` observed only allowlisted fields — no secret, no `INTERNAL_SEND_SENTINEL` (as key or smuggled in a value), no out-of-scope personal field. |
| DCA minimizes even though the gateway also has privacy screens | Minimization runs in `AiCapabilityFacade.invoke` before the transport call — the DCA's own screen, independent of the gateway. |
| Relevant denial without unintended state changes | `test_facade_refuses_oversize_context_before_egress` — oversize raises `AiContractViolation` and `transport.calls == []` (nothing egressed); the minimizer is pure and mutates no state. |

## Fail-closed behaviour

- **Unknown/unmapped capability** → `ContextMinimizationError(context_capability_not_minimizable)`.
- **Oversize context** → `ContextMinimizationError(context_too_large)` with the offending size in `detail`.
- Both are subclasses of `AiContractViolation`, so the DCA-130 bound invoker treats them as non-retryable
  contract violations: no egress, no spend, reserved hold released.

## Verification

```text
.venv/Scripts/python.exe -m pytest tests/test_ai_context_minimization.py -q   # focused: 14 passed
.venv/Scripts/python.exe -m pytest -q                                         # full suite (exit 0 authoritative)
.venv/Scripts/python.exe scripts/manual_check.py DCA-131                      # prints before/after + oversize refusal
ruff check app/ai app/core/logging.py tests/test_ai_context_minimization.py scripts/manual_check.py   # clean
ruff format --check app/ai/context_minimizer.py tests/test_ai_context_minimization.py                 # clean
mypy app/ai/context_minimizer.py app/ai/contract.py app/ai/fake.py app/core/logging.py                # clean
```

## Boundary / not in scope

- No change to effortlesspromo-ai; no new capability enabled (future capabilities stay disabled). Adding a
  capability's allowlist is part of that capability's own AI-capability story.
- No live/paid call: the facade uses the dev/test fixture transport (off by default); minimization is a
  pure in-process screen with no I/O.
- Only `inputContext` is minimized (the story's "context"); role-separated `untrusted` frames are handled
  by the existing DCA-128 contract and the gateway's isolation.
- No migration (pure logic).
