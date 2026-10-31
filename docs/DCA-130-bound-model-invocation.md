# DCA-130 — Bound an AI-service (model) invocation's cost and retries

**Status:** Ready for manual review. No live/paid call; **no migration**; **no change to effortlesspromo-ai**.
Key: `ai-budget` · source sections 4, 8, 9. Prerequisite DCA-129 (and the earlier sequence) present — the
DCA-128 facade (`app/ai/contract.py`), the DCA-129 HTTP adapter (`app/clients/ai_client.py`) and the
DCA-112..117 cost machinery (`app/cost/*`) all exist; this story **composes them**.

## Boundary (what this story is, and is NOT)

Bound ONE model invocation so its **cost** is bounded and its **retries are not stacked**. It wraps the
single already-built chokepoint — the DCA-128 `AiCapabilityFacade` over the DCA-129 adapter — in the
accepted DCA-113/114/115 cost ledger, exactly as the DCA-125 licensed-provider adapter does for a paid
source. It is the AI-gateway analogue of that adapter.

**Model retry and failover stay the gateway's job.** The invoker calls the gateway **exactly once** and
never loops. It *classifies* the outcome with the accepted DCA-104 discipline (`classify_failure`) so the
**surrounding durable-job layer** can decide — bounded by the job's attempt cap — whether to re-attempt; a
non-retryable error is dead-lettered immediately. No DCA model-retry loop is stacked on the gateway's own.
**No provider SDK/key/routing/second gateway exists here, and none ever may.**

## What changed (DCA only)

- `app/ai/bound_invocation.py` — **new.** `BoundModelInvoker` (tenant-scoped) + `BoundInvocationActor` /
  `BoundInvocationRequest` / `BoundInvocationResult`. The one path: request-shape → authority
  (`BUDGET_MANAGE`) → payload-size bound → capability gate (DCA-128, fail-closed) → idempotency skip →
  **DCA-113 reserve** (over-budget ⇒ denied, no call) → **DCA-115** record PENDING → IN_FLIGHT → **the ONE
  DCA-128 gateway call** under a deterministic `X-Request-Id` (`dca:<invocation_key>`) → terminal
  transition + **DCA-114 reconcile** (settle from the returned actual cost / release on a confirmed failure
  / preserve-uncertain on a timeout). Stages into the caller's session; never commits; performs no real
  call.
- `app/core/config.py` — new `ai_max_payload_chars` (default 20000, mirroring the AI service's
  `untrusted_max_chars`): a request whose serialized input-context + untrusted frames exceed it is refused
  **before** any reserve/call.
- `app/ai/__init__.py` — re-exports the new DCA-130 surface alongside the DCA-128 facade.
- `tests/test_bound_invocation.py` — **new**, 17 focused tests (see below), driven by a deterministic
  in-file **scripted gateway transport** (no network/model/spend; returns a synthetic envelope or raises
  the DCA-129 transport errors).
- `scripts/manual_check.py` — registered scenario **`DCA-130`** (prints the real reserve/settle/release
  rows, the budget remaining, and the DCA-104 retry plan for each outcome).
- `docs/DCA-130-bound-model-invocation.md` — this file.

**No migration.** The story composes the existing `budget_reservations` / `provider_call_records` /
`budget_limits` tables; it adds no column and no table (`auto_create_schema` in dev/test).

## Acceptance criteria → where satisfied

| Criterion | Where |
|---|---|
| Hard limits deny invocation | an over-budget `reserve` (DCA-113) returns a `budget` denial **before** the call; an oversized payload raises `bound_invocation_payload_too_large` before reserving — the gateway is never invoked (tested: `calls == []`) |
| Duplicated receipts cannot double-settle | the reserve / provider-call / reconcile are each idempotent on a key derived from the stable logical invocation key; a whole re-invocation is a no-op (`skipped=True`, no second call, no second settlement) |
| Timeout never implies no spend; unknown cost stays pending (not zero) | `AiServiceTimeout` → provider call `UNCERTAIN` + reservation `MARK_UNCERTAIN`: full hold preserved (`settled_minor=None`, `spent=reserved`), never released/zeroed |
| Retries bounded, not stacked | the gateway is called **once**; the outcome is classified (`as_failure_signal` → DCA-104 `plan_retry`): PERMANENT (4xx / contract violation / non-retryable body-fail) → dead-letter immediately; TRANSIENT (5xx / unreachable) → retry while attempts remain, then dead-letter at the cap |
| Reproduce result + denial without unintended state | shape/authority/payload refusals raise before any state; capability/budget denials return inertly; `test_the_invoker_never_commits` proves a rollback undoes the whole run |

## Verification (all run)

```text
.venv/Scripts/python.exe -m pytest tests/test_bound_invocation.py -q   # focused: 17 passed
.venv/Scripts/python.exe -m pytest -q                                  # full suite: exit 0 (green)
.venv/Scripts/python.exe scripts/manual_check.py DCA-130               # all 6 printed checks True
.venv/Scripts/python.exe -m ruff check <touched>                       # clean
.venv/Scripts/python.exe -m ruff format --check <touched>              # clean
.venv/Scripts/python.exe -m mypy app/ai/bound_invocation.py app/ai/__init__.py app/core/config.py  # clean
```

Manual-check `DCA-130` prints, for one synthetic business (caps INR 1000/run, INR 1000/day): a
within-budget success (reserved 2000, settled 1200, released 800, daily_remaining 98800); an over-budget
request denied with `gateway_calls=0`; a 4xx rejection released + `permanent` → `dead_letter_permanent`; a
5xx/unreachable released + `transient` → `schedule_retry` (1/3) then `dead_letter_exhausted` (3/3); a
read-timeout left `uncertain` (settled None, spent 2000, pending); and an idempotent re-invocation
(`skipped=True`, `gateway_calls=0`).

## Blockers / kept disabled

- **Live execution stays off by default** — `ai_enabled=False` (no `ai_base_url`), so the facade uses the
  dev/test fixture transport and spends nothing; the live DCA-129 adapter is exercised only against
  in-process fakes.
- **Currency note:** the DCA ledger is INR; the reservation is a conservative INR hold (ideally from a
  DCA-112 dry-run quote) and settlement ceils the gateway's reported actual cost to INR minor units. A
  gateway that reports a non-INR model price is a known contract question for the India-only DCA — the
  gateway capability is expected INR-priced; no FX conversion is fabricated (the DCA-112 stance).
- Provider routing/failover and prompt/output-validation remain the effortlesspromo-ai gateway's. No
  effortlesspromo-ai change was needed or made. Resolving a preserved `UNCERTAIN` hold is the existing
  DCA-116 resolver's job.
