# DCA-106 — Recover abandoned job leases

Stage: Durable Python execution · key `lease-recovery` · source sections 2, 3, 8.

## What this adds

A **recovery sweep** for abandoned job leases. DCA-101 leases a `QUEUED` job to a worker with a monotonic
fencing token; a worker that crashes or stalls never commits, renews or fails its job, so its lease simply
lapses and the row is left `RUNNING` forever. `app/jobs/recovery.py` adds `JobLeaseRecoveryStore`, whose
`recover_abandoned(now)` finds those abandoned (`RUNNING` + lapsed-lease) rows and safely makes the work
retryable again under a **new fence** — so a process crash never permanently loses the work, and the
crashed process can never later commit a result for a job that has moved on.

This is the automated, proactive counterpart to the opportunistic reclaim already inside
`JobClaimStore.claim_next` (which reclaims a lapsed lease only when some worker happens to pull the queue
next). The sweep heals abandoned leases on its own schedule, independently of whether a claimer is waiting,
and — crucially — keeps the recovery **bounded**: it never resurrects a job past its attempt cap.

Nothing is executed. Recovery only makes a job *claimable* again (or parks it); the handler body
(`JobHandler.run`, DCA-100) still raises `NotImplementedError`, there is no worker drain loop, and no path
here re-dispatches a recovered `QUEUED` row. A recovery grants no send authority, the DCA has no send path,
and acceptance of a prospect is not recipient consent.

**No schema change / migration.** The sweep reuses the existing `durable_job_requests` columns only:
`status` (`RUNNING` → `QUEUED`/`DEAD_LETTER`), `lease_version` (bumped — the new fence), the lease fields
`lease_owner` / `lease_expires_at` / `lease_deadline` (cleared), `attempt_count` / `max_attempts` (read for
the cap), `available_at` (set to `now` on a re-queue) and `last_error` (a PII-safe note). No tenant-owned
table is added, so `tests/test_migrations.py` / `tests/test_rls.py` are unchanged.

## Design

`JobLeaseRecoveryStore(session, business_id)` is **tenant-scoped** (bound to one verified business, every
read/write confined to it — the same discipline as `JobClaimStore`) and, like the claim store, **owns its
transaction**: `recover_abandoned` selects a bounded batch with `FOR UPDATE SKIP LOCKED`, applies the
recovery in place and **commits** the short transaction, releasing every row lock before returning.

It is automated infrastructure — the healing counterpart to `claim_next` — so, like claiming, it carries no
operator-authority gate (unlike the DCA-105 operator requeue, which is a human platform-ops decision over a
terminal state).

### `recover_abandoned(*, now, limit=100) -> LeaseRecoveryReport`

Selects up to `limit` abandoned rows for this business — `status == RUNNING` and `lease_expires_at <= now`
— oldest-lapsed first, with `FOR UPDATE SKIP LOCKED` (so it skips, never blocks on, a row a live
claim/complete currently holds, and two concurrent sweeps recover any one row exactly once). For each row:

1. **Bump the fence, clear the lease** — `lease_version += 1` (a new fencing token) and
   `lease_owner` / `lease_expires_at` / `lease_deadline` are cleared. The crashed worker still holds its
   **old** `(lease_owner, fence_token)`; because the row is no longer `RUNNING` at that token, its
   `complete` / `renew` / `record_failure` are all refused as a stale lease — *"its old process cannot
   later commit."* (The lapsed holder's commit was already refused by the DCA-103 live-only rule; the fence
   bump keeps the denial true even after a new holder takes the row.)
2. **Bounded outcome** —
   - attempts remain (`attempt_count < max_attempts`) → flip back to `QUEUED` and due (`available_at = now`)
     so a healthy worker can claim it (`RECOVERED`, note `lease_recovered_after_abandonment`);
   - budget spent (`attempt_count >= max_attempts`, because the crashed attempt already consumed one) →
     park `DEAD_LETTER` (`DEAD_LETTERED_EXHAUSTED`, note `lease_recovery_attempts_exhausted`) rather than
     resurrect a crash-looping job forever — the same attempt cap DCA-104 honours on a *reported* failure,
     applied to an *unreported* crash. A parked job is recoverable by an operator requeue (DCA-105).

A **live** lease (`lease_expires_at > now`), a `QUEUED` / `FAILED` / `SUCCEEDED` / `DEAD_LETTER` /
`CANCELLED` row, and any **other tenant's** row are never selected — a sweep that finds no abandoned lease
changes nothing. The returned `LeaseRecoveryReport` is inert (per-job `RecoveredLease` records + counts).

### Concurrency / portability

`FOR UPDATE SKIP LOCKED` is a PostgreSQL feature (the SQLite dialect omits it). The recovery *logic* runs
serially on SQLite for dev / the manual check; the real row-lock isolation (crash → recover → commit-denied,
and two concurrent sweeps recovering a row exactly once) is exercised on a disposable PostgreSQL.

## Acceptance → evidence

| Criterion | Evidence |
|---|---|
| A process crash does not permanently lose work | `test_sweep_recovers_an_abandoned_lease_under_a_new_fence`, `test_crashed_worker_cannot_commit_after_recovery_but_another_worker_can` (another worker claims the recovered job and completes it → `SUCCEEDED`), PG `test_crash_recover_and_commit_with_another_worker_postgres`; manual steps 1–3 |
| Its old process cannot later commit | Same tests: the crashed worker's `complete()` returns `STALE_LEASE` (committed `False`, row unchanged) because the fence was bumped and the row is no longer `RUNNING` at its token; manual step 2 |
| Reproduce the relevant denial without unintended state changes | `test_live_lease_is_not_swept_and_its_worker_can_still_commit`, `test_empty_sweep_and_queued_rows_change_nothing`, `test_sweep_is_tenant_scoped`, `test_exhausted_attempts_are_dead_lettered_not_requeued`; manual step 4 (live-lease / exhausted / cross-tenant / empty) |
| No later-story work (execution stays disabled) | Recovery only flips status + bumps the fence; recovered jobs are *claimed*, never run; a dead-lettered one is not even claimable |

## Tests

`tests/test_job_lease_recovery.py` — always-on SQLite logic tests (recover-under-new-fence; the headline
crash → recover → commit-denied-for-old-worker → completed-by-another-worker; live-lease / empty / queued
no-ops; exhausted → dead-letter; tenant isolation) plus disposable-PostgreSQL re-runs
(`DCA_TEST_DATABASE_URL`) of the real crash→recover→commit flow and two concurrent sweeps recovering a row
exactly once (`FOR UPDATE SKIP LOCKED`). The Postgres tests skip without a URL.

Spin up a throwaway Postgres, e.g.:

```
docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55471:5432 postgres:16-alpine
export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55471/dca"
```

## Manual check

```
python scripts/manual_check.py DCA-106
```

In an isolated synthetic Business A: claims a fixture, simulates a worker **crash** (lease left to lapse),
advances time and runs the recovery sweep — printing the real row before/after (status `running` →
`queued`, fence `1` → `2`, lease cleared, `last_error` note). Then shows the crashed worker's commit refused
as `stale_lease` (row unchanged), another worker claiming the recovered job and completing it to
`succeeded` (the work was not lost), and the relevant no-ops/denials (a live lease not swept — its holder
still commits; an exhausted-budget crash dead-lettered; a cross-tenant sweep recovering nothing; an empty
sweep changing nothing). Local synthetic only; no AI/paid action, no network, no send.
