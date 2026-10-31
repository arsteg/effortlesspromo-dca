# DCA-117 — Expose business spend summaries

Stage: Cost reservations and provider accounting · key `spend-view` · source sections 8, 13.

## What this adds

A **read-only** aggregation of a business's own spend ledger — the DCA-113 `BudgetReservation` rows as
DCA-114 reconciliation left them — exposed through a tenant-scoped, authenticated HTTP endpoint. It
reports, in exact INR **minor units (paise)**, how much is:

- **reserved** — `ACTIVE` holds not yet reconciled,
- **incurred** — `SETTLED` holds' actual cost,
- **uncertain** — `UNCERTAIN` holds (a dispatched-then-unconfirmed provider call whose cost **MAY** exist),
- **released** — `RELEASED` holds (freed back to the budget),
- **committed** — what still counts toward the caps (`reserved + incurred + uncertain`),
- **remaining** — the headroom left in the current civil day / month against the in-force DCA-042 caps,

broken down by **run**, **civil day** and **civil month**.

It writes nothing, holds nothing, spends nothing and sends nothing — a pure read a reviewer or a later
Web/Admin consumer can inspect and reproduce. No provider/model/paid call is ever made.

## Files

- **DCA** `app/cost/summary.py` — new `BudgetSpendSummaryStore` (tenant-scoped, read-only) + the value
  objects `SpendSummary`, `SpendBucket` (a per-window status roll-up) and `RunSpendLine` (one run =
  one reservation). It reuses the DCA-113 `BudgetReservationStore.window()` for the current-window
  caps/remaining, and the **same effective-amount convention** the reserve path enforces
  (`ACTIVE`/`UNCERTAIN` → full reserved amount, `SETTLED` → settled amount, `RELEASED` → 0), so the
  figures reconcile with the ledger and with what the next reservation would see.
- **DCA** `app/api/routes/spend_summary.py` — new `GET /v1/tenants/{business_id}/spend/summary`.
- **DCA** `app/main.py` — router registered (after `budget.router`).
- **DCA** `tests/test_spend_summary.py` — focused tests (service + HTTP).
- **DCA** `scripts/manual_check.py` — `@register("DCA-117", …)` inspectable scenario.
- **Docs** this file.

No migration: the summary is a pure aggregation over the existing `budget_reservations` / `budget_limits`
tables (pinned into the RLS/migration sets by DCA-042/113). No new table, column or enum.

## The HTTP contract

`GET /v1/tenants/{business_id}/spend/summary` → the DCA-011 envelope on failure, else a JSON summary:
`total` (headline roll-up), `byDay`, `byMonth`, `byRun`, plus the current-window `*CapMinor` /
`*RemainingMinor` (and derived `*Major` rupee views for display only — the minor-unit integer is the
source of truth).

### Authorization (the acceptance criteria)

Uses the DCA-019 end-user permission model (like the sibling `budget` write routes), **not** the coarse
`X-Service-Auth` gate:

- **Authority-gated with the appropriate read scope.** Spend / cost figures are budget-control accounting
  data, so — exactly like **reserving** and **reconciling** that spend (DCA-113/114/115 are *all*
  `budget.manage`) — reading them requires the **`budget.manage`** administrator scope on a
  membership-verified tenant context. An ordinary business user (a viewer holding only `candidate.read`)
  and a platform operator (who holds no membership in any client business) are both denied with **403**,
  and the decision is audited by the permission dependency. An unauthenticated request is **401**.
- **Another tenant cannot read cost details.** The path `business_id` is the row scope: the dependency
  requires an active membership in *that* business before the scope check, and the aggregator is bound to
  the verified business and confines every query to its `business_id`. So a non-member of the path
  business (including a different business's admin, e.g. a revoked membership) is refused with **403**
  before any row is read, and even an authorized caller only ever sees their own tenant's rows.
- **Uncertain amounts are not falsely shown as free budget.** An `UNCERTAIN` hold is reported in its own
  `uncertain` bucket **and** counted in `committed` (so it reduces `remaining`); it is **never** folded
  into `released` / free budget. Only a `RELEASED` hold frees budget. A caller can therefore never mistake
  an unconfirmed possible charge for reclaimed headroom.
- **Reproduces with no state change.** Summarizing is a pure read — no write, no audit row, no spend, no
  send — so the answer and every denial reproduce unchanged.

## Verification

- Focused: `.venv/Scripts/python.exe -m pytest tests/test_spend_summary.py -q` → 13 passed.
- Full suite: `.venv/Scripts/python.exe -m pytest -q`.
- Manual: `.venv/Scripts/python.exe scripts/manual_check.py DCA-117` — seeds isolated synthetic
  Businesses A/B (A with active / settled / uncertain / released holds across two civil months, B with a
  single hold) and prints the real aggregated summary, proving the totals reconcile, uncertain stays
  committed (not free), the by-day/by-month/by-run breakdown, and that B never shows A's spend.

## Boundaries

Python DCA only; no .NET, no AI-service, no UI. Reading a summary performs no provider/model/paid call and
holds, charges, spends and sends nothing — all of that stays a disabled future capability.
