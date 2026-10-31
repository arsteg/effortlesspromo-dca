# DCA-115 — Track external provider call lifecycle

Stage: Cost reservations and provider accounting · key `provider-call` · source sections 8, 13.

## What this adds

Tracks the lifecycle of ONE external provider call — its **intent**, **request identity**,
**source/version**, **outcome/usage** and **ambiguous completion state** — so cost accounting and the
DCA-114 reconciliation have an **authoritative** record of what happened. **No actual
provider/model/network/paid call is ever made here** — this records lifecycle ONLY; performing the call
stays a disabled future capability. Nothing here spends, charges, bills, sends or invokes a model.

New writer: `app/cost/provider_call.py` → `ProviderCallStore` (tenant-scoped, bound to one business,
stages into the caller's session and never commits — mirrors the DCA-113 `BudgetReservationStore` and
DCA-114 `BudgetReconciliationStore`). New model: `ProviderCallRecord` + `ProviderCallStatus` enum in
`app/db/models.py`.

### The safe state machine (`ProviderCallStatus`, `VALID_TRANSITIONS`)

| From | Valid targets |
|---|---|
| `PENDING` (intent recorded, not dispatched — no cost possible) | `IN_FLIGHT`, `FAILED` |
| `IN_FLIGHT` (dispatched — external acceptance now possible) | `SUCCEEDED`, `FAILED`, `UNCERTAIN` |
| `SUCCEEDED` / `FAILED` / `UNCERTAIN` (terminal) | *(none)* |

- `record()` stages a `PENDING` record and is **idempotent on the call key** — a retry replays the
  existing row, never a second one.
- `transition()` advances the record along a valid edge only. An invalid target, a self/duplicate
  transition, or any transition out of a terminal state is refused (`provider_call_invalid_transition`)
  **before** the row is mutated, so a refusal stages no change and no audit. A `FOR UPDATE` lock
  serializes concurrent transitions on PostgreSQL, so a duplicate can never both apply.

### Load-bearing guarantees (the acceptance criteria)

- **A timeout after possible external acceptance is distinguishable from a confirmed no-call failure.**
  `UNCERTAIN` (dispatched then unconfirmed — a billable cost **MAY** exist) is a distinct terminal state
  from `FAILED` (a **confirmed** no-call failure — no cost). `dispatched` records whether external
  acceptance was ever possible, and `ProviderCallRecord.cost_possible` (`True` for `SUCCEEDED`/`UNCERTAIN`)
  surfaces the distinction cost accounting / DCA-114 reconciliation reads to decide whether the matching
  hold must be preserved or may be released. A never-dispatched call can never become `UNCERTAIN`.
- **Reproduce the result and relevant denial without unintended state changes.** The store is
  deterministic: the same inputs reproduce the same record/transition, and every refusal
  (unauthorized / missing key / missing capability / not-found / invalid transition) is raised before any
  mutation, so it changes nothing.
- **Idempotent on the call key.** `(business_id, call_key)` is unique.
- **Authorized first.** A caller lacking `budget.manage` is refused (`ProviderCallAuthorizationError`)
  before any state is read or written.
- **Atomic audit.** A DCA-026 audit entry (opaque references only — ids / keys / codes / counts, never a
  raw payload) is staged in the SAME unit of work as the record / transition.

### What is persisted (no raw payloads)

`call_key` (identity), `request_id` (gateway idempotency identity), `capability` + `intent`,
`provider` / `model` / `source_version` (source), optional `reservation_id` link to the DCA-113 hold,
`status` + `dispatched`, `outcome_code` (terminal), numeric token usage (`prompt_tokens` /
`completion_tokens` / `total_tokens`, recorded only on `SUCCEEDED`), opaque actor and lifecycle
timestamps. Only opaque references, codes and integer counts — never a raw request/response payload.

## Schema

Migration `0046_provider_call_records` — create branch for `provider_call_records` (same dual-path guard
style as 0044): on a fresh DB the metadata baseline (0001) creates the table and 0002 builds its RLS
policy, so 0046 is a no-op; on a DB that predates the table, 0046 creates it and adds its
tenant-isolation RLS policy + app-role grant. Tenant-owned (`TenantOwned`), so it is covered by the
reflection-driven `tenant_owned_tables()` set and inherits tenant isolation like every other tenant table.
`(business_id, call_key)` is unique (`uq_provider_call_key`).

## Tests

`tests/test_provider_call.py`:

- record stages a `PENDING` row + atomic audit; record is idempotent on the call key (no second
  row/audit); authority checked first; missing call key / capability refused;
- success path (dispatch → succeed records numeric usage + `resolved_at`);
- **the acceptance distinction**: a dispatched-then-lost call → `UNCERTAIN` (`cost_possible=True`) vs a
  pre-dispatch `FAILED` (`cost_possible=False`) — the two terminal states are never collapsed;
- invalid transition refused (`PENDING`→`SUCCEEDED`, `PENDING`→`UNCERTAIN`); duplicate / self transition
  refused; terminal state accepts nothing; unknown key refused; tenant-scoped; authority checked first on
  transition; store never commits;
- **(Postgres-gated)** several transitions racing on the same record serialize on the `FOR UPDATE` lock —
  exactly one applies, the rest refused — so a duplicate transition never applies twice.

`tests/test_migrations.py::test_0046_creates_provider_call_records_on_a_preexisting_database`
(Postgres-gated) exercises the create branch and the RLS policy; `DCA_TABLES` gains
`provider_call_records`. `tests/test_rls.py` adds `provider_call_records` to the tenant-owned set and
tenant-isolation / write-check tests.

## Manual check

```
python scripts/manual_check.py DCA-115
```

Drives the store against a disposable synthetic DB and PRINTS real rows / transitions / refusals for: a
success path (dispatch → succeed with usage); a dispatched-then-lost `UNCERTAIN` vs a pre-dispatch
`FAILED`; an invalid transition (`pending → succeeded`) and a duplicate transition (self loop) refused
with no change; and idempotent re-recording on the call key. Local synthetic only; no AI/paid action, no
network, no spend, no send, and no actual provider call.

## Boundary

Python DCA only. No UI, no AI-service internals, no .NET, no provider SDKs/keys. The record tracks
lifecycle in this service's own database; future/execution/paid capabilities stay disabled.
