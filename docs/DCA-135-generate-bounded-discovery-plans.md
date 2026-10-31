# DCA-135 — Generate bounded discovery plans

Stage 12 — Existing AI-service integration. Key: `ai-plan`. Source sections 4, 8, 9.

## What this delivers

One end-to-end AI capability: a **bounded discovery plan** — a proposal of the source *tasks* the agent
could run to discover prospects — generated through the one DCA AI chokepoint, grounded in the business's
**confirmed India ICP**, and bounded to the sources it is **already approved + entitled** to. The plan is
a **DRAFT proposal**: no task is executed and it is never auto-approved. The model cannot add a source,
expand India scope, request an un-permitted field, or exceed the task/query caps — any attempt is
clamped/excluded (recorded), enforced both at the gateway and defensively on the DCA side.

A later story (Stage 13, the discovery pipeline) executes an accepted plan; execution stays disabled here.

## New AI capability (effortlesspromo-ai)

A new narrowly-scoped capability `discovery_plan` was added to the existing gateway registry (DCA needed
no compatible existing capability). It follows the DCA-134 pattern exactly:

- `app/gateway/registry.py` — `_DISCOVERY_PLAN` `Capability` + its `rule_check` (`_discovery_plan_rule`),
  added to `_DEFAULTS`.
  - **Input (trusted only):** `icpVersion`, confirmed ICP targeting (`industries`, `sizeBands`, `roles`,
    `geography`), `allowedSources[]` (`{sourceKey, permittedFields}`), and the `maxTasks` /
    `maxQueriesPerTask` caps. No prospect PII, no external content → `accepts_untrusted=False`.
  - **Output:** `tasks[]` (`{sourceKey, fields, filters{industries,sizeBands,roles,geography}, maxQueries}`),
    `rationale`, `confidence`.
  - **The load-bearing bound — `rule_check`:** a model output is rejected (validation failure) if any task
    references a source not in `allowedSources`, requests a field that source does not permit, names a
    geography outside the confirmed ICP, exceeds `maxTasks`, or exceeds `maxQueriesPerTask`. The model can
    never widen its own mandate.
- `app/gateway/backend.py` — `_plan_discovery(ctx)`: a deterministic, network-free stub that proposes one
  task per allowed source (capped at `maxTasks`), requests only permitted fields, carries ICP-derived
  filters (geography never widened) and stays within the query cap — so its own output passes `rule_check`.
- `app/gateway/prompts.py`, `app/gateway/providers.py` — registration parity.
- `tests/test_discovery_plan_capability.py` — registration parity, schema contract, a bounded grounded
  plan, determinism, empty-sources → empty plan, and the real `rule_check` rejecting a new source /
  scope expansion / over-cap output.

## DCA service (effortlesspromo-dca)

- `app/db/models.py` — new `DiscoveryPlan` (`TenantOwned`, RLS): insert-only, versioned draft plan
  (`tasks`, `excluded`, `task_cap`, `query_cap`, `icp_version`, `rationale`, `overall_confidence`,
  `provenance`). `(business_id, version)` unique; `status` always `'draft'`.
- `migrations/versions/0054_discovery_plans.py` — dual-path create + RLS, same guard style as 0053.
- `app/services/discovery_planner.py` — `plan_discovery(...)`, the one integration:
  1. Capability/transport gate (fail-closed) → `discovery_plan_capability_disabled`.
  2. Grounds in the **confirmed ICP** (`IcpConfirmationStore`); none → `no_confirmed_icp` (stages nothing).
  3. Resolves allowed sources + permitted fields ONLY through the sanctioned DCA-048 `SourcePreflight`
     gate (catalogued+enabled ∧ platform-approved ∧ business-entitled `COLLECT` for `prospect_discovery`).
  4. Invokes the capability via the DCA-128 facade (trusted context only; DCA-131 minimised).
  5. A rejected / failed-validation gateway output → `discovery_plan_output_rejected` (stages nothing).
  6. **Re-enforces every bound DCA-side**, clamping/excluding each task (unentitled source, un-permitted
     field, geography outside the ICP, over the query cap) and truncating to the task cap — each recorded
     in `excluded`.
  7. Stages a `DiscoveryPlan` draft. Never executed, never auto-approved.
- `app/ai/contract.py` — `discovery_plan` added to `KNOWN_CAPABILITIES`.
- `app/ai/context_minimizer.py` — `discovery_plan` allowlist (the 8 trusted context keys).
- `app/api/routes/discovery_plans.py` — `POST/GET /v1/tenants/{business_id}/discovery/plans[/{version}]`
  (X-Service-Auth, idempotent). Refusals map to 503 (disabled) / 409 (no confirmed ICP) / 502 (rejected).
  Wired in `app/main.py`.
- `tests/test_discovery_planner.py` — service + HTTP tests for all of the above.
- `tests/test_migrations.py`, `tests/test_rls.py` — `discovery_plans` added to the expected table sets.

## Boundaries honoured

- All model execution via the existing effortlesspromo-ai gateway over HTTP; no DCA provider SDK/key/routing.
- Tenant isolation: `DiscoveryPlan` is `TenantOwned` (RLS) + app-level `business_id` scoping.
- Source rights / consent / entitlement: sources resolved only through the DCA-048 preflight gate.
- Live/paid + execution disabled: AI off by default (fixture transport in dev/test, no spend); a plan is a
  proposal — no task is run and nothing is auto-approved.

## Verify

```bash
# effortlesspromo-dca
.venv/Scripts/python.exe -m pytest tests/test_discovery_planner.py -q
.venv/Scripts/python.exe -m pytest -q                       # full suite
.venv/Scripts/python.exe scripts/manual_check.py DCA-135     # inspectable scenario

# effortlesspromo-ai
.venv/Scripts/python.exe -m pytest tests/test_discovery_plan_capability.py -q
AI_BACKEND=stub .venv/Scripts/python.exe -m pytest -q        # full suite
```

Postgres-backed RLS/migration tests (`test_migrations.py` / `test_rls.py`) require
`DCA_TEST_DATABASE_URL` pointing at a disposable Postgres; they skip otherwise.

**Ready for manual review.** Uncommitted; no story files moved; no live/paid call; no plan execution.
