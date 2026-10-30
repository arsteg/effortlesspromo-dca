# DCA-108 — Enqueue due schedules exactly once logically

Stage: Durable Python execution · key `schedule-tick` · source sections 2, 3, 8.

## What this adds

A DCA-owned **due-schedule tick**: a tenant-scoped service (`app/services/schedule_tick.py`,
`DueScheduleEnqueuer`) that turns a business's **DUE** DCA-107 recurring schedules
(`RecurringSchedule` rows whose `next_run_at <= now`) into enqueued **durable jobs** (DCA-099
`DurableJobRequest` rows) — **exactly once logically per cycle** — and advances each fired schedule's
persisted `next_run_at` so the same cycle is never fired again.

This story **enqueues** work; it still **executes nothing**. There is no worker drain loop here, no
handler is resolved or run, and nothing is sent. The enqueued job is left `QUEUED`. Running an enqueued
cycle (claim → handler → result → send) stays a **disabled future capability**. The DCA has no send path,
and acceptance of a prospect is not recipient consent.

No schema change: this reuses the existing `recurring_schedules` (DCA-107) and `durable_job_requests`
(DCA-099) tables, so **no new migration** and no change to `test_migrations.py` / `test_rls.py`.

## Design

- **Precondition first (the WHERE clause).** `tick(now, limit, scheduler_id)` selects only `ACTIVE`
  schedules with `next_run_at <= now`, oldest-due first, bounded by `limit`
  (`DEFAULT_TICK_BATCH = 100`). A **not-yet-due** schedule and a **PAUSED** schedule are never selected,
  so they are left exactly as they are. A tick that finds nothing due enqueues nothing, advances nothing
  and commits an empty transaction — the no-op / "relevant denial" changes nothing.

- **Exactly once logically (two schedulers never double-tick).** The enqueue for one cycle is keyed on a
  **deterministic** idempotency key — `schedule_cycle_key(schedule_id, fired_boundary)` =
  `schedule:{schedule_id}:{epoch-seconds-of-next_run_at}` — handed straight to the DCA-099 durable queue,
  whose `(business_id, job_type, idempotency_key)` uniqueness is **the** serialisation point. Two
  schedulers (or a retried tick) that both see the same cycle as due compute the **same** key, so the
  queue collapses the second enqueue onto the first job (`created=False`) — one logical tick, one job,
  never two. This holds **without** relying on locking.

  On PostgreSQL a belt-and-suspenders guard is layered on top: the due rows are claimed
  `SELECT ... FOR UPDATE SKIP LOCKED` and the **enqueue + advance + audit commit in one short
  transaction**, so a concurrent ticker *skips* a row another ticker is mid-tick on (no blocking, no
  double-advance) instead of relying on the unique constraint to catch it after the fact. The row lock is
  released at commit. `FOR UPDATE SKIP LOCKED` is a PostgreSQL feature (the SQLite dialect omits it), which
  is why the concurrency test runs on a disposable Postgres; the deterministic key keeps the serial
  SQLite / manual-check path exactly-once too.

- **Bounded catch-up (no backlog storm).** When a scheduler was down and a schedule is several whole
  intervals overdue, the tick fires it **once** and advances `next_run_at` **arithmetically** (one
  floor-division, O(1) — never a per-interval loop) to the next boundary **strictly after `now`**,
  collapsing the missed-interval gap into that single catch-up tick rather than replaying one job per
  missed interval. So a schedule fires at most once per tick-pass regardless of outage length and then
  resumes on cadence; the number of jobs one tick can enqueue is bounded by the number of distinct due
  schedules (and the batch cap), never by elapsed time. `EnqueuedCycle.missed_intervals` records how many
  boundaries each catch-up collapsed. This is the documented, deliberately bounded policy.

- **No raw PII on the queue.** The schedule id and the fired-boundary epoch live only in the
  (un-screened) `idempotency_key` and in integer job metadata (`cycleEpoch`), never in the DCA-099
  PII-screened `payload_ref` (`dca-schedule://cycle/{schedule_key}`) or string metadata — so the durable
  queue's opaque-pointer rule is preserved.

- **Atomic audit.** Each fired cycle stamps one atomic DCA-026 audit entry (`action = "schedule.tick"`,
  opaque references only — ids / schedule key / cycle epoch / pinned versions / before+after due time) in
  the same unit of work as the enqueue + advance. The tick is a **SYSTEM** action (a background
  scheduler, actor `dca-scheduler` by default), not a user action — there is no user authority to check;
  the precondition is simply ACTIVE + due.

- **Tenant-scoped.** The enqueuer is bound to one verified `business_id`; every read and write is confined
  to it (DCA-017 scoping + DCA-018 RLS beneath it), so a tick can never read, fire or advance another
  tenant's schedule. A scheduler ticks each business's schedules under that business's scope.

## Files

- `app/services/schedule_tick.py` — new. `DueScheduleEnqueuer`, `ScheduleTickReport`, `EnqueuedCycle`,
  `schedule_cycle_key`, and the constants `SCHEDULE_CYCLE_JOB_TYPE`, `DEFAULT_TICK_BATCH`,
  `AUDIT_ACTION_SCHEDULE_TICK`.
- `tests/test_schedule_tick.py` — new. Always-on SQLite logic tests + disposable-Postgres concurrency
  tests (skip without `DCA_TEST_DATABASE_URL`).
- `scripts/manual_check.py` — registered the `DCA-108` scenario.

## Verification

```text
# focused (always-on SQLite layer)
.venv/Scripts/python.exe -m pytest tests/test_schedule_tick.py -q
# -> 8 passed, 2 skipped (the Postgres concurrency tests)

# focused incl. real Postgres concurrency (exactly-once under FOR UPDATE SKIP LOCKED)
docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55498:5432 postgres:16-alpine
DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55498/dca" \
  .venv/Scripts/python.exe -m pytest tests/test_schedule_tick.py -q
# -> 10 passed

# full suite -> 1561 passed, 121 skipped (Postgres-gated), exit 0
.venv/Scripts/python.exe -m pytest -q

# inspectable scenario (prints real rows/counts, not a bare PASS)
.venv/Scripts/python.exe scripts/manual_check.py DCA-108
```

The manual check seeds isolated synthetic Businesses A/B and shows: a due schedule enqueues **exactly one**
`QUEUED` job and advances `next_run_at` by one interval; a repeat tick at the same instant enqueues
**nothing more**; replaying the **same** cycle boundary collapses onto the one job (`created=False`); a
3.5-interval-overdue schedule fires **once** with `missedIntervals=3` and jumps strictly past `now`
(bounded catch-up); a not-yet-due and a paused schedule are skipped unchanged; and business B's tick never
touches A.

## Boundary

Python DCA only. No `effortlesspromo-ai`, Web, Admin, host/.NET or platform-DB changes. No network, no AI,
no paid action, no send. Future execution of enqueued cycles (and run-time entitlement/budget re-checks at
fire time) stay disabled.
