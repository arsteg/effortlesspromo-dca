# DCA-114 — Reconcile reserved and actual costs

Stage: Cost reservations and provider accounting · key `budget-settle` · source sections 8, 13.

## What this adds

Once a run's **actual** cost is known, DCA-114 reconciles the DCA-113 `BudgetReservation` HOLD against
it — **exactly once** — so the budget ledger reflects what truly happened instead of the conservative
hold taken up front. A reconciliation **only adjusts the ledger**: it charges nothing, sends nothing and
invokes no model/provider (the DCA has no such path). Builds directly on DCA-112 (estimate) → DCA-113
(reserve) → DCA-114 (reconcile).

New writer: `app/cost/reconcile.py` → `BudgetReconciliationStore` (tenant-scoped, bound to one business,
stages into the caller's session and never commits — mirrors the DCA-113 `BudgetReservationStore`).

### Three terminal outcomes (`ReconcileOutcome`)

| Outcome | When | Effect on the hold | Budget effect |
|---|---|---|---|
| `SETTLE` | actual cost is known | status → `settled`, `settled_amount_minor = min(actual, reserved)` | the **unused difference** `reserved − actual` is released back to the budget |
| `RELEASE` | run abandoned / cost nothing | status → `released`, settled 0 | the **whole hold** is freed |
| `MARK_UNCERTAIN` | external result is UNKNOWN | status → `uncertain`, full reserved amount preserved | **nothing is freed** — no blind refund, no retry |

**Over-run is clamped, never charged past the cap.** A hold was admitted only because it fit under the
caps at reserve time, so settling an actual **above** the reserved amount CLAMPS the settled amount to the
reserved amount and records the excess in `overspend_minor` (flagged for review) — it is never
retroactively charged past a cap the reserve path already enforced.

### Load-bearing guarantees (the acceptance criteria)

- **Idempotent on the provider-call identity; a duplicate settlement never double-spends.** Each
  reconciliation carries an `outcome_key` (the provider-call identity). A re-submission against a hold
  already reconciled under the **same** key returns the stored result unchanged (`replayed=True`); a
  **different** key against an already-reconciled hold is refused (`reconciliation_already_reconciled`).
  A reservation is therefore reconciled **once**.
- **Missing / foreign / already-settled → refused with no change.** An unknown id, or another tenant's id
  (which reads exactly like not-found), is refused (`reconciliation_reservation_not_found`); each refusal
  is raised before the row is mutated, so it stages no update and no audit and leaves every total intact.
- **An unknown external result never blindly refunds or retries.** `MARK_UNCERTAIN` preserves the full
  hold against the caps and records no actual — resolving an uncertain charge later is a separate,
  still-disabled capability.
- **Serialized (transaction-safe).** The writer takes a `FOR UPDATE` lock on the reservation row, so
  concurrent reconciliations of the same hold serialize: the first wins, the rest see the committed
  terminal state (idempotent replay under the same key, refusal under a different one). Proven on real
  Postgres; a no-op on SQLite.
- **Authorized first.** A caller lacking `budget.manage` is refused (`ReconciliationAuthorizationError`)
  before any state is read or written — exactly like reserving.
- **Atomic audit.** A DCA-026 audit entry (opaque references only) is staged in the SAME unit of work as
  the reservation update.

## Budget accounting

The cap sums in `app/cost/reserve.py` `_reserved_total` now count the **effective** amount per status:
`active`/`uncertain` → the full reserved amount, `settled` → its `settled_amount_minor`, `released` → 0.
Reconciling a hold therefore frees exactly the unused headroom, which a later `reserve()` / `window()`
observes as restored remaining budget. DCA-113 behaviour is unchanged (it only ever writes `active` holds).

## Schema

Migration `0045_reservation_reconcile` — add-column branch on `budget_reservations` (same dual-path guard
style as 0011/0015/0020/0040): `reconciliation_key` (indexed, nullable), `actual_minor`,
`settled_amount_minor`, `overspend_minor` (NOT NULL default 0), `reconciled_at`,
`reconciled_by_actor_id`, `reconciled_by_actor_type`. No new table and **no RLS change** — the 0044
tenant-isolation policy already covers the table, so the new columns inherit tenant isolation unchanged.
`ReservationStatus` gains `SETTLED` and `UNCERTAIN` alongside the existing `ACTIVE`/`RELEASED`.

## Tests

`tests/test_budget_reconcile.py`:

- under-spend settle releases the unused difference (freed headroom observable via the window);
- exact settle releases nothing; over-run is clamped to the reserved amount and the excess flagged;
- release frees the whole hold; mark-uncertain preserves the full hold (no refund);
- duplicate settlement under the same identity is an idempotent replay (no second adjustment/audit);
- a second reconciliation under a different identity is refused (reconciled once);
- unknown / foreign reservation id refused with no change; tenant-scoped; authority checked first;
  invalid actual and missing outcome key refused; released headroom is reservable again; store never
  commits;
- **(Postgres-gated)** several reconciliations racing on the same hold serialize on the `FOR UPDATE` lock
  — exactly one applies, the rest refused — so a duplicate settlement never double-spends.

`tests/test_migrations.py::test_0045_adds_reconciliation_columns_on_a_preexisting_database`
(Postgres-gated) exercises the add-column branch and the backfill of a pre-existing ACTIVE hold.

## Manual check

```
python scripts/manual_check.py DCA-114
```

Seeds synthetic Business A with in-force caps, then PRINTS real before/after reservation rows and
remaining budget for: an under-spend settle (releases the difference), a duplicate settlement under the
same identity (idempotent replay, not double-freed), a second reconciliation under a different identity
(refused), an over-run settle (clamped + flagged), a release (frees the hold) and a mark-uncertain
(preserves it). Local synthetic only; no AI/paid action, no network, no spend, no send.

## Boundary

Python DCA only. No UI, no AI-service internals, no .NET, no provider SDKs/keys. Reconciliation adjusts
only this service's own ledger; future/execution/paid capabilities stay disabled.
