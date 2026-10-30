# DCA-104 — Schedule bounded retries

Stage: Durable Python execution · key `job-retry` · source sections 2, 3, 8.

## What this adds

The FAILURE branch of the durable-job lifecycle, layered on the DCA-101/102/103 fenced lease. When the
worker holding a job's current lease reports a failed attempt, `app/jobs/retry.py` classifies the failure
and — fenced on that same lease — either **schedules a bounded retry** or **dead-letters** the job.
Nothing is executed and no row is re-dispatched; this story only decides *whether* and *when* a job may
next be attempted. The handler body (`JobHandler.run`, DCA-100) still raises `NotImplementedError`, there
is no worker drain loop, and no path re-picks a due `FAILED` row. A scheduled retry grants no send
authority, the DCA has no send path, and acceptance of a prospect is not recipient consent.

**No schema change / migration.** The existing `durable_job_requests` columns are sufficient:
`attempt_count` / `max_attempts` (attempt cap, already bumped at claim time by DCA-101), `available_at`
(the next-run time), `last_error` (the PII-safe note), and the `FAILED` / `DEAD_LETTER` statuses already
defined on `JobRequestStatus`. `tests/test_migrations.py` / `tests/test_rls.py` are therefore unchanged.

## Design — two layers (both deterministic / fail-closed)

### Pure decision layer (no DB)

- **`classify_failure(category) -> JobFailureClass`** — a total, pure map from a stable, PII-safe
  `JobFailureCategory` to `TRANSIENT` or `PERMANENT`. `PERMANENT` is exactly the policy/auth/validation
  categories (`POLICY_DENIED`, `AUTHORIZATION_REVOKED`, `AUTHENTICATION_FAILED`, `PAYLOAD_INVALID`,
  `NOT_RETRYABLE`); every other category — the transient infrastructure errors (`TIMEOUT`, `NETWORK`,
  `PROVIDER_UNAVAILABLE`, `RATE_LIMITED`) and the catch-all `UNKNOWN` — is `TRANSIENT`. Misreading a
  permanent failure as transient is the one outcome the acceptance forbids, so only clearly-permanent
  categories are permanent.
- **`compute_backoff_seconds(attempt, *, rng, retry_after_seconds=None)`** — capped exponential backoff
  (`BASE_BACKOFF_SECONDS * 2**(attempt-1)`, clamped at `MAX_BACKOFF_SECONDS`) with **equal jitter**
  (randomises `JITTER_RATIO` of the window, so simultaneous failures do not all retry on the same tick).
  A provider **`Retry-After`** is honoured as a **floor** (never retry sooner than the provider said),
  clamped to `MAX_RETRY_AFTER_SECONDS` so a hostile/absurd header cannot park a job forever. `rng` is
  injected (`random.Random`) so the schedule is deterministic in tests and the manual check.
  Constants: `BASE_BACKOFF_SECONDS=2.0`, `MAX_BACKOFF_SECONDS=600.0`, `JITTER_RATIO=0.5`,
  `MAX_RETRY_AFTER_SECONDS=3600.0`.
- **`plan_retry(signal, *, attempt_count, max_attempts, now, rng) -> RetryPlan`** — the ordered decision:
  1. **Permanent first** → `DEAD_LETTER_PERMANENT`, regardless of attempts remaining (never retried).
  2. **Attempt cap** → a transient failure at `attempt_count >= max_attempts` → `DEAD_LETTER_EXHAUSTED`.
  3. **Schedule** → otherwise `SCHEDULE_RETRY` at `now + compute_backoff_seconds(...)`.

### Fenced store (`JobRetryScheduler.record_failure`)

Tenant-scoped and owns its transaction, exactly like `JobCompletionStore` (DCA-103): it locks the row
`FOR UPDATE`, re-checks the lease fence in the same short transaction, then applies the plan and commits —
or rolls back. The applied/denial outcomes (`RetryScheduleOutcome`):

- Applied (row changed): `RETRY_SCHEDULED` (→ `FAILED`, `available_at` pushed out, lease cleared),
  `DEAD_LETTERED_PERMANENT` / `DEAD_LETTERED_EXHAUSTED` (→ `DEAD_LETTER`, lease cleared).
- Refused (nothing changes — same fenced-lease denials as DCA-103): `STALE_LEASE` (not the current
  `(worker_id, lease_version)` on a `RUNNING` row — the job was reclaimed), `LEASE_EXPIRED`
  (`now >= lease_expires_at` — a reclaimable job may not be failed by its old holder), `NOT_FOUND`
  (unknown / cross-tenant id).

`attempt_count` is **not** re-incremented here — DCA-101 already bumped it when it leased the attempt. The
PII-safe `error_note` is recorded to `last_error` and is screened for raw contact data (reused
`_contains_raw_pii`) before any state is read — a note carrying an email/phone raises
`JobErrorNoteContainsPiiError` and changes nothing.

### Why a scheduled retry uses `FAILED` (not `QUEUED`)

A scheduled retry sets status `FAILED` with `available_at` as the next-run time — matching the documented
`JobRequestStatus.FAILED` semantics ("a retriable attempt failed; retried while `attempt_count <
max_attempts`"). The current `claim_next` picks only `QUEUED` / lapsed-`RUNNING` rows, so a `FAILED` row is
**not** re-claimed by this story — re-dispatching a due `FAILED` row is the explicitly-disabled future
worker loop. DCA-104 records *when* the job may next run; it does not re-run it. (`naive`-vs-`aware`
datetimes are normalised with `_as_utc` before the live-only comparison, same discipline as
`app/jobs/claim.py` / `app/jobs/complete.py`.)

## Boundary

Python DCA only. No UI, no `effortlesspromo-ai` internals, no other service's database, no provider
SDKs/keys, no second AI gateway, no .NET changes, no migration. Future execution (handler bodies, a worker
drain loop that re-dispatches due `FAILED` rows, a send path) stays a disabled future capability.

## Verification

- `tests/test_job_retry.py`
  - **Pure layer (always-on, seeded RNG):** every transient category classifies `TRANSIENT` and every
    permanent category `PERMANENT`; backoff stays within `[capped*(1-JITTER_RATIO), capped]` and under the
    cap for attempts 1..14; with a max-jitter RNG the delay sequence is non-decreasing and saturates at
    `MAX_BACKOFF_SECONDS`; a `Retry-After` floors the delay; `plan_retry` dead-letters a permanent failure
    with attempts remaining and schedules transient retries until the cap then dead-letters (exhausted).
  - **Fenced apply (always-on, SQLite):** a transient failure schedules a retry (`FAILED`, next-run pushed
    out, lease cleared, `attempt_count` not re-bumped, note recorded); a permanent policy failure
    dead-letters with 4/5 attempts remaining (NOT retried); a transient failure at the cap
    (`max_attempts=1`) dead-letters; a `Retry-After` floors the scheduled `available_at`; a **replaced**
    worker (job reclaimed) cannot schedule a retry; an **expired** holder cannot; a **foreign** worker id
    cannot; a **cross-tenant** worker reads not-found; a PII-bearing `error_note` is refused before any
    change; an empty worker id is rejected; a dead-lettered row cannot be failed again (terminal).
  - **Disposable PostgreSQL (`DCA_TEST_DATABASE_URL`), real commits/rollbacks + the `FOR UPDATE` lock:**
    re-runs the scheduled retry, the permanent dead-letter, and the reclaim-then-stale-failure denial —
    the durable cross-transaction cases (skipped without a URL; SQLite drives the logic serially).

The durable cross-transaction guarantees live on PostgreSQL, so those tests require a real Postgres and
skip without a URL.

## Manual check

```
python scripts/manual_check.py DCA-104
```

In an isolated synthetic Business A, drives the scheduling path serially against the ephemeral SQLite DB
with a seeded RNG (so printed delays are reproducible) and prints the real rows: (1) a transient failure
→ `FAILED` with the next-run pushed out; (2) a permanent policy failure → `DEAD_LETTER`, not retried
despite 4/5 attempts remaining; (3) a rate-limit with `Retry-After=120s` → next-run floored at +120s;
(4) a transient failure at the `max_attempts=1` cap → `DEAD_LETTER` (exhausted); (5) the deterministic
backoff schedule for attempts 1..6; (6) a reclaim-then-stale-failure denial (`STALE_LEASE`, nothing
changes). Local synthetic only — no AI/paid action, no network, no send, and no job is executed. The
failure-vs-reclaim race across short transactions is exercised on disposable PostgreSQL in
`tests/test_job_retry.py`.
