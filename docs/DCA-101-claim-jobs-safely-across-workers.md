# DCA-101 — Claim jobs safely across workers

Stage: Durable Python execution · key `job-claim` · source sections 2, 3, 8.

## What this adds

A DCA-owned **safe cross-worker claim** for the DCA-099 durable job queue: a tenant-scoped claimer
(`app/jobs/claim.py`, `JobClaimStore`) plus three lease/fencing columns on `durable_job_requests`
(`lease_owner`, `lease_expires_at`, `lease_version`). A worker calls `claim_next(...)` to lease the
next available queued job; the claim is a **short PostgreSQL transaction** that selects one row with
`SELECT ... FOR UPDATE SKIP LOCKED`, flips it to `running` with a lease + an incremented fencing token,
and **commits** — releasing the row lock before the caller ever touches a handler.

This story claims (leases) jobs only. It does **not** execute them: the handler body
(`JobHandler.run`, DCA-100) still raises `NotImplementedError`, there is no worker loop that drains the
queue, and nothing advances a job to succeeded/failed/dead-letter. Claiming a job grants nothing and
sends nothing — the DCA has no send path, and acceptance of a prospect is not recipient consent.

## Design

- **Short transaction + `FOR UPDATE SKIP LOCKED`.** `claim_next` picks the oldest-due, oldest-created
  claimable row (`available_at <= now` and either `queued`, or `running` with a lapsed lease), locks
  exactly that row and **skips** (never blocks on) a row another worker is mid-claim on, flips it to
  `running`, and commits. The lock lives only for the tiny flip and is released at commit — *before*
  any handler I/O — so slow external work can never pin a database row lock.
- **Lease.** The commit stamps `lease_owner` (claiming worker) and `lease_expires_at` (when the claim
  lapses). While a lease is live the claim query excludes the row, so two workers never hold the same
  current claim. If a worker crashes/stalls past `lease_expires_at`, another worker may **reclaim** the
  row. `renew(...)` is the heartbeat a long-but-live job uses to push its lease out.
- **Fencing token.** `lease_version` is a monotonic counter bumped on every (re)claim and handed to the
  claimer as `JobClaim.fence_token`. A reclaim bumps it again, so the previous holder's token is now
  stale. `renew` / `release` are fenced on `(lease_owner, lease_version)`: a stale/foreign token changes
  nothing and is refused — a lapsed worker whose job was reclaimed cannot stomp it (`holds_current_lease`
  is the read-only check behind this).
- **Attempt bookkeeping.** A claim increments `attempt_count` (so a future worker can honour
  `max_attempts`); it never advances `status` beyond `running` or writes `last_error`.
- **Tenant-scoped.** The claimer is bound to one verified `business_id`; it only claims that business's
  jobs and a by-id guarded op on another tenant's job reads as not-found and is refused (same discipline
  as `JobRequestStore` / DCA-017, beneath DCA-018 RLS).
- **Owns its transaction.** Unlike the enqueue store (which stages into the caller's unit of work), the
  claimer commits the short claim itself — that commit is the point where the row lock is released.

## Schema

Migration `0038_durable_job_request_lease` adds `lease_owner` (nullable), `lease_expires_at` (nullable),
`lease_version` (NOT NULL, default `0`) to `durable_job_requests`. Dual-path safe, add-column branch
(same guard style as 0011/0015/0020): a no-op on a fresh DB (the 0001 metadata baseline already has the
columns), and the real add on a DB that reached 0037 before the columns existed (existing queued rows
backfill `lease_version` to `0` — "never claimed"). No RLS change: the table stays tenant-owned and the
0002/0037 tenant-isolation policy is untouched.

## Boundary

Python DCA only. No UI, no `effortlesspromo-ai` internals, no other service's database, no provider
SDKs/keys, no second AI gateway. Future execution (handler bodies, a worker drain loop, retry/dead-letter
transitions) stays a disabled future capability.

## Verification

- `tests/test_job_claim.py`
  - **Always-on (SQLite), serial logic:** a claim leases a queued job (`running` + owner + fence token
    `1` + one attempt) without executing it; an empty/not-yet-available queue claims nothing; a live
    lease blocks a second claim; an expired lease is reclaimed and the fence advances to `2`; a
    stale/foreign fence token cannot `release`/`renew` and changes nothing; `release` by the current
    holder returns the job to `queued`; tenant isolation (a worker never claims or releases another
    business's job).
  - **Disposable PostgreSQL (`DCA_TEST_DATABASE_URL`), real concurrency:** two workers racing one
    queued job yield exactly one claim; racing two jobs yield two distinct claims (SKIP LOCKED never
    blocks); and the claimed row's lock is released at the short claim's commit — proven by taking
    `SELECT ... FOR UPDATE NOWAIT` on it from a separate connection while the "handler" is still
    running (NOWAIT would raise if the lock were held).
- `tests/test_migrations.py::test_0038_adds_lease_columns_on_a_preexisting_database` — the add-column
  branch re-adds the three columns, backfills `lease_version` to `0`, and leaves the RLS policy intact.
- `tests/test_rls.py` — `durable_job_requests` stays tenant-isolated (unchanged).

Cross-worker safety (`FOR UPDATE SKIP LOCKED`) is a PostgreSQL feature, so those tests require a real
Postgres and skip without a URL; SQLite omits the `FOR UPDATE` clause, which is why only the
lease/fencing *logic* runs there.

## Manual check

```
python scripts/manual_check.py DCA-101
```

Enqueues two synthetic queued jobs in an isolated Business A, then drives the claim path serially
against the ephemeral SQLite DB and prints the real rows: worker-1 claims a job (leased, not run);
worker-2 cannot take that live claim and claims the other job; after worker-1's lease lapses worker-2
reclaims its job (fence `1→2`); worker-1's stale token is refused on release (no change). Local synthetic
only — no AI/paid action, no network, no send, and no job is executed. The true two-worker row-lock
isolation is exercised on disposable PostgreSQL in `tests/test_job_claim.py`.
