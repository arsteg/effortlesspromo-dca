# DCA-107 — Store per-business recurring schedules

Stage: Durable Python execution · key `schedule-store` · source sections 2, 3, 8.

## What this adds

A DCA-owned **per-business recurring-schedule store**: a tenant-scoped table (`recurring_schedules`)
and a single sanctioned writer (`app/services/schedule_store.py`) that persists an **active** recurring
schedule and its **due time** (`next_run_at`) as durable PostgreSQL data, pinned to the versioned
profile references it was created against.

This story persists schedules **only**. It does **not** run them — storing a schedule starts no thread,
opens no loop, enqueues no job, and performs no I/O. Advancing a due schedule and uniquely enqueuing each
cycle is the **separate next story (DCA-108)** and stays a disabled future capability. A stored schedule
therefore implies **no endless per-tenant model process**; it is durable data a restarted scheduler
re-reads.

Storing a schedule grants nothing and sends nothing — the DCA has no send path.

## Design

- **The row** (`RecurringSchedule`, `app/db/models.py`) is tenant-owned (`business_id` + DCA-018 RLS),
  carrying: `schema_version` (the record contract version), `schedule_key` (the logical recurring work),
  `interval_seconds` (the cadence), `next_run_at` (the persisted DUE time — the load-bearing field),
  `status` (`active`/`paused`, via `ScheduleStatus`; always `active` here), the versioned profile
  references `icp_version` (the confirmed active ICP version, DCA-040, snapshotted at creation) and
  `profile_version` (optional link to a DCA-036 business-profile draft version), plus the opaque
  `actor_id`/`actor_type` and `created_at`/`updated_at`.
- **One schedule per logical key per business.** `(business_id, schedule_key)` is unique
  (`uq_recurring_schedule_key`). Re-creating an existing key is a `ScheduleConflict` rejected before any
  write — never a silent overwrite (updating/advancing it is DCA-108). Verified under real contention on
  PostgreSQL (two concurrent stores → one row).
- **Gated for a confirmed, entitled business — authority FIRST.** `RecurringScheduleStore.create` checks,
  in order, each **before the session is mutated** so a refusal leaves no row:
  1. **Authority** — the caller must hold `Scope.SCHEDULE_MANAGE` (a business-plane **administrator**
     scope added in `app/identity/permissions.py`); otherwise `ScheduleAuthorizationError`
     (`schedule_not_authorized`). An ordinary user, a read-only viewer, a non-member, and a platform
     operator (no membership) are each refused.
  2. **Shape** — empty `schedule_key` (`missing_schedule_key`), non-positive interval
     (`invalid_interval`), missing `next_run_at` (`missing_next_run_at`).
  3. **Confirmed** — the business must have a confirmed active ICP (DCA-040); otherwise
     `schedule_business_not_confirmed`. The confirmed version is snapshotted as `icp_version`.
  4. **Entitled** — the business must have an **active, non-lapsed** cached entitlement projection
     (DCA-023); otherwise `schedule_business_not_entitled`. This is a **read-only** check against the
     bounded `EntitlementProjection` cache — it performs **no** live/network resolution (that is the
     DCA-023 gate's job).
  5. **Profile version** — if `profile_version` is supplied it must name an existing business-profile
     draft version of this business; otherwise `profile_version_not_found`.
  6. **Duplicate key** — an existing schedule for the key is a `ScheduleConflict`
     (`schedule_already_exists`).
- **Atomic with the caller.** The store stages the row **and** an atomic DCA-026 audit entry (opaque
  references only — ids / schedule key / cadence / pinned versions / due time, never a raw value) into
  the caller's session and never commits. A denial raises before any staging, so it reproduces exactly
  and changes nothing.
- **Tenant-scoped.** Every read is filtered to the bound `business_id`; a by-id or by-key read of another
  business's schedule resolves to not-found, and two businesses may reuse the same `schedule_key` as two
  independent schedules. The read-only `due(now)` helper *inspects* which active schedules are due
  without advancing `next_run_at` or enqueuing anything.

## Migration

`migrations/versions/0042_recurring_schedules.py` (revision `0042_recurring_schedules`, down-revision
`0041_identity_merges`). Dual-path safe like 0037/0041: on a fresh DB the metadata baseline (0001)
creates the table and 0002 builds its RLS policy (it is a `TenantOwned` table), so the migration is a
no-op; on a DB that reached 0041 before the table existed, it creates the table (with its unique
`(business_id, schedule_key)` constraint) and adds the tenant-isolation RLS policy + app-role grant.
`downgrade` is a deliberate no-op (teardown is owned by the 0001/0002 baseline; dropping here would race
`0002.down`). Expand→migrate→contract (BR-201).

## Tests

- `tests/test_recurring_schedule.py` — always-on SQLite logic tests: store for a confirmed + entitled
  business (ICP version pinned); **survives a restart** (a fresh engine over the same file DB re-reads
  the same `next_run_at`); storing enqueues **no** durable job; optional profile version pinned; and
  each denial (unauthorized viewer, unconfirmed, unentitled, lapsed entitlement, unknown profile
  version, bad interval, duplicate key) refuses with no state change; tenant isolation. Plus a
  disposable-PostgreSQL test (`DCA_TEST_DATABASE_URL`) proving two concurrent stores of the same key
  collapse to one row via the unique constraint.
- `tests/test_migrations.py` — `recurring_schedules` added to `DCA_TABLES`; new
  `test_0042_creates_recurring_schedules_on_a_preexisting_database` exercises the 0042 create branch +
  RLS on Postgres.
- `tests/test_rls.py` — `recurring_schedules` added to the tenant-owned set assertion; new
  tenant-isolation + write-check (foreign-owner) tests on Postgres.

PostgreSQL-backed tests **skip** when `DCA_TEST_DATABASE_URL` is unset (same convention as the rest of
the suite); run them with a disposable Postgres.

## Manual check

```
python scripts/manual_check.py DCA-107
```

Seeds synthetic Businesses (confirmed+entitled A, entitled-not-confirmed, confirmed-not-entitled),
stores A's schedule, simulates a scheduler **restart** with a brand-new engine over the same durable DB
(persisted next-run re-read unchanged), proves **no** durable job was enqueued, then reproduces each
denial (unauthorized, unconfirmed, unentitled, duplicate key) — each changing nothing. Prints the real
rows/decisions, never a bare PASS. Local synthetic only; no AI/paid action, no network, no send.

## Boundary

Stayed strictly inside DCA-107: a new tenant-owned table + store + one scope + migration + tests + this
doc. No DCA-108 work (no due-schedule advancement/enqueue), no execution, no routes, no AI/host changes.
Future capabilities remain disabled.
