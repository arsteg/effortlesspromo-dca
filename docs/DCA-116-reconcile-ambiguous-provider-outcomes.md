# DCA-116 — Reconcile ambiguous provider outcomes

Stage: Cost reservations and provider accounting · key `provider-reconcile` · source sections 8, 13.

## What this adds

Resolves a previously **AMBIGUOUS** provider outcome once the truth is known — a DCA-115
`ProviderCallRecord` left `UNCERTAIN` (dispatched then unconfirmed, a billable cost **MAY** exist)
together with the DCA-113 `BudgetReservation` hold DCA-114 preserved as `UNCERTAIN`. This is the
capability DCA-114 explicitly deferred ("resolving an uncertain charge later is a separate, still-disabled
capability"): now enabled, but strictly as a **ledger/record adjustment** — it never performs, retries,
spends, charges, bills, sends or invokes a model/provider (the DCA has no such path).

New writer: `app/cost/provider_reconcile.py` → `UncertainOutcomeResolver` (tenant-scoped, bound to one
business, stages into the caller's session and never commits — mirrors the DCA-113 `BudgetReservationStore`,
DCA-114 `BudgetReconciliationStore` and DCA-115 `ProviderCallStore`). New enum + columns:
`ProviderCallResolution` and the `resolution*` columns on `ProviderCallRecord` in `app/db/models.py`.

### How ambiguity is resolved (the story's one task)

> *"Query supported provider status/receipts or route unsupported cases to review before retrying
> chargeable work."*

- **Supported provider** (one whose status/receipt can be consulted — a synthetic allowlist
  `DEFAULT_RECONCILABLE_PROVIDERS = {"platform"}`; wiring a real provider's receipt lookup is a later,
  still-disabled capability). The caller supplies the receipt evidence (`happened=True/False`):
  - *It DID happen* → provider call resolved **`CONFIRMED`**; the tied `UNCERTAIN` hold is **SETTLED** at
    the actual cost (unused difference released, over-run clamped and flagged — never charged past the cap).
  - *It did NOT happen* → provider call resolved **`VOID`**; the tied hold is **RELEASED** in full, freeing
    the preserved headroom back to the budget.
  - *No evidence* → refused (`provider_outcome_no_evidence`): certainty is never claimed from nothing.
- **Unsupported provider** (no receipt/status lookup) → routed to **`NEEDS_REVIEW`**; nothing is settled,
  released or retried. A human resolves it later. **No uncontrolled repeat charge.**

The DCA-115 terminal `UNCERTAIN` **status is never reopened** — the confirmed truth is recorded in the
separate DCA-116 `resolution` dimension, and the tied hold is settled/released.

### `ProviderCallResolution`

| Value | Meaning |
|---|---|
| `""` (`UNRESOLVED`) | not yet reconciled (default while `UNCERTAIN`) |
| `needs_review` | unsupported provider — routed to human review (NOT final; may be resolved later) |
| `confirmed` | **final** — evidence confirmed it happened; tied hold settled |
| `void` | **final** — evidence confirmed it did not; tied hold released |

### Load-bearing guarantees (the acceptance criteria)

- **Reconciliation never claims certainty without evidence; no uncontrolled repeat charges.** A supported
  provider is resolved only with receipt evidence; an unsupported one is routed to review, never
  auto-confirmed and never retried.
- **Only an ambiguous outcome is resolvable.** A non-`UNCERTAIN` record is refused
  (`provider_outcome_not_uncertain`) with no change.
- **Idempotent; a conflicting resolution of a FINAL outcome is refused with no change.** Each resolution
  carries a `resolution_key` (the receipt/evidence identity). The SAME key replays the stored outcome
  unchanged; a DIFFERENT key against a final (`confirmed`/`void`) resolution is refused
  (`provider_outcome_already_resolved`). `needs_review` is deliberately not final.
- **Serialized (transaction-safe).** Both the provider-call record and the tied reservation are re-read
  `FOR UPDATE` before the decision, so concurrent resolutions serialize on PostgreSQL — exactly one
  applies, the rest are refused. A duplicate settlement never double-spends.
- **Authorized first.** A caller lacking `budget.manage` is refused
  (`OutcomeResolutionAuthorizationError`) before any state is read or written.
- **Atomic audit.** A DCA-026 audit entry (`provider.outcome.resolve`, opaque references only) is staged in
  the SAME unit of work as the record / reservation update.

## Schema

Migration `0047_provider_call_resolution` — **add-column** branch for `provider_call_records` (same
dual-path guard style as 0045): on a fresh DB the metadata baseline (0001) already created the columns, so
0047 is a no-op; on a DB that predates them, 0047 adds `resolution` (NOT NULL, server default `""` →
existing rows backfill to "unresolved"), `resolution_key`, `resolution_outcome_code`,
`resolution_actor_id`, `resolution_actor_type`, `resolution_at`. **No RLS change** — the 0046 policy still
covers the table. No new table, so `DCA_TABLES` / `tenant_owned_tables()` are unchanged.

## Tests

`tests/test_provider_outcome_resolve.py`:

- confirmed-happened settles the tied hold (unused difference freed); confirmed-not-happened releases the
  whole hold; unsupported provider routed to `needs_review` with no ledger change and no retry;
- supported-without-evidence refused; settle requires a non-negative actual;
- duplicate under the same receipt identity → idempotent replay (no second audit / no double-free);
  conflicting resolution under a different identity against a final outcome → refused with no change;
- a non-`UNCERTAIN` (succeeded) outcome cannot be resolved; unknown key refused; missing keys refused;
  tenant-scoped; authority checked first; store never commits;
- **(Postgres-gated)** eight resolutions racing on the same uncertain outcome serialize on the
  `FOR UPDATE` lock — exactly one applies, the rest refused — so a duplicate settlement never
  double-spends.

`tests/test_migrations.py::test_0047_adds_resolution_columns_on_a_preexisting_database` (Postgres-gated)
exercises the add-column branch and the backfill (`resolution` → `""`), with RLS unchanged.

## Manual check

```
python scripts/manual_check.py DCA-116
```

Drives the resolver against a disposable synthetic DB and PRINTS real before/after provider-call +
reservation + budget-remaining rows for: a supported provider confirmed-HAPPENED settling the tied hold
(then an idempotent duplicate replay and a refused conflicting resolution); a supported
confirmed-NOT-HAPPENED releasing the whole hold; and an **unsupported** provider left visibly pending
`NEEDS_REVIEW` with the hold preserved and no retry. Local synthetic only; no AI/paid action, no network,
no spend, no send, and no actual provider call made or retried.

## Boundary

Python DCA only. No UI, no AI-service internals, no .NET, no provider SDKs/keys. The resolution adjusts
record/ledger state in this service's own database; future/execution/paid capabilities stay disabled.
