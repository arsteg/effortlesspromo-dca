# DCA-113 — Reserve spend atomically

Stage 10 — *Cost reservations and provider accounting*. Key: `budget-reserve` (source sections 8, 13).
Builds on the DCA-112 estimator (`app/cost/estimate.py`) and the DCA-042 `BudgetLimit` caps.

## What this adds

A single capability: **atomically reserve a precise amount of spend against a business's in-force budget
caps**, so that concurrent reservations can never collectively overshoot any cap. A *reservation* is a
**HOLD**, not a charge and not a send — the DCA has no spend or send path. Reserving only sets aside
headroom against the caps; settling/releasing a hold and recording actual provider spend remain later,
**disabled** stories.

- New store: `app/cost/reserve.py` — `BudgetReservationStore` (the single sanctioned writer),
  `ReservationRequest`, `ReservationActor`, `ReservationWindow`, and the stable refusal codes.
- New model: `app.db.models.BudgetReservation` (+ `ReservationStatus`) — a tenant-owned, RLS-protected
  hold ledger. Amounts are **integer INR minor units (paise)** — exact, never a float.
- New migration: `migrations/versions/0044_budget_reservations.py` (dual-path create + RLS, like 0042).

## The load-bearing guarantees (acceptance criteria)

1. **No reservation can overshoot any cap; a refusal changes nothing.** A reservation is admitted only if
   the amount fits within **every** cap in force: the per-run cap, a platform per-reservation hard
   ceiling, the day's remaining daily cap, the month's remaining monthly cap, an optional per-provider
   cap, and a platform daily hard ceiling. A request that would exceed **any** cap is refused with a
   stable code **before** any row is staged, so a denial stages no hold and no audit and leaves every
   total exactly as it was. Rejected requests make no external call (there is none to make here).
2. **Concurrent reservations serialize (transaction-safe).** Before summing existing `ACTIVE` holds and
   deciding, `reserve()` takes a `SELECT … FOR UPDATE` lock on the business's latest `BudgetLimit` row.
   Every concurrent reservation for the same business contends for that one row, so PostgreSQL serializes
   them: the second transaction blocks until the first commits, then reads the first's committed hold.
   The total reserved therefore never exceeds the limit even under a race. (The lock is a no-op on
   SQLite, which has no row locking; the serialization is a PostgreSQL property, proven in the
   Postgres-gated test.)
3. **Idempotent on a key.** `(business_id, idempotency_key)` is unique. Re-submitting the same key — even
   a concurrent duplicate that races past the fast-path check — returns the **existing** reservation and
   stages no second row and no second hold.
4. **Authorized; a denial changes nothing.** Authority is checked **first**: a caller whose roles lack
   `Scope.BUDGET_MANAGE` is refused (`ReservationAuthorizationError`) before any state is read or written.

### Windows

The daily/monthly caps are evaluated in the caps' own IANA zone (India: `Asia/Kolkata`), so a reservation
is counted in the **India-local** civil day/month (`day_key` / `month_key`), not UTC. `budget_version`
pins which `BudgetLimit` version the hold was taken against.

### Refusal codes (`app/cost/reserve.py`)

`reservation_not_authorized`, `reservation_missing_idempotency_key`, `reservation_invalid_amount`,
`reservation_no_budget_configured`, `reservation_per_run_cap_exceeded`, `reservation_daily_cap_exceeded`,
`reservation_monthly_cap_exceeded`, `reservation_provider_cap_exceeded`,
`reservation_platform_cap_exceeded`.

## Boundary / disabled capabilities

- A reservation **holds** budget — it never spends, charges, bills, sends, or invokes a model/provider.
- No HTTP route is added: reserving is an internal mechanism consumed by the (disabled) execution path.
  The store exposes a structured contract for a later Web/Admin/worker consumer.
- Releasing/settling a hold (`ReservationStatus.RELEASED`) is reserved for a later story; nothing here
  writes it, and the cap sums count only `ACTIVE` rows so a future release drops out automatically.
- No `.NET`, no second AI gateway, no other service's database touched.

## Tests

`tests/test_budget_reserve.py`:

- **SQLite unit tests** — authority-first; a successful reserve decrements remaining + audits; over-budget
  refused with no change; exact-fit then overshoot; per-run / monthly / provider / platform-ceiling caps;
  idempotent key; no-budget / invalid-amount refusals; per-business scoping; India-local civil window;
  store never commits.
- **Postgres concurrency test** (`DCA_TEST_DATABASE_URL`, same skip guard as `tests/test_rls.py`) — eight
  reservations of ₹20 race a ₹50 daily cap; exactly two are admitted, the rest refused with the daily
  code, and the summed hold is ₹40 — never ₹60+ (which an unserialized read-modify-write would allow).

Also: `tests/test_migrations.py` pins `budget_reservations` into `DCA_TABLES` and adds the 0044 dual-path
branch test; `tests/test_rls.py` adds it to the tenant-owned set and adds isolation + write-check tests.

## Commands

```bash
# focused
.venv/Scripts/python.exe -m pytest tests/test_budget_reserve.py -q
# full suite (exit code 0 authoritative)
.venv/Scripts/python.exe -m pytest -q
# inspectable manual scenario (prints real before/after budget + reservation rows)
.venv/Scripts/python.exe scripts/manual_check.py DCA-113

# migration / RLS behaviour on disposable Postgres (optional):
export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55432/dca"
.venv/Scripts/python.exe -m pytest tests/test_migrations.py tests/test_rls.py tests/test_budget_reserve.py -q
```
