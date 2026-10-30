# DCA-102 — Renew a running job lease

Stage: Durable Python execution · key `job-heartbeat` · source sections 2, 3, 8.

## What this adds

A DCA-owned **bounded heartbeat** on top of the DCA-101 claim: `JobClaimStore.renew(...)`
(`app/jobs/claim.py`) lets a long-but-live worker push its lease's `lease_expires_at` out so the claim
does not lapse while legitimate work is still running, plus one column on `durable_job_requests`
(`lease_deadline`) that caps how long a single claim can be heartbeated for. Renewing keeps a claim
alive; it still **executes nothing** — the handler body (`JobHandler.run`, DCA-100) still raises
`NotImplementedError`, there is no worker loop, and nothing advances a job to succeeded/failed/
dead-letter. A claim grants no send authority, the DCA has no send path, and acceptance of a prospect is
not recipient consent.

## Design — bounded on three axes (all fail-closed)

`renew(job_id, worker_id, fence_token, *, now, lease_seconds=DEFAULT_LEASE_SECONDS)` extends the lease
**only** when every bound below holds; any failure changes **nothing** and returns `None` (the relevant
denial), so a lapsed/replaced/over-running worker can never keep a claim it no longer legitimately holds:

1. **Fenced on the current lease.** The presented `(worker_id, fence_token)` must still be the live lease
   (`holds_current_lease`: row `running`, `lease_owner` and `lease_version` both match). A reclaim bumps
   `lease_version`, so a **replaced** worker's old fence is stale and refused — it cannot renew (or, in a
   future story, complete) with an old fence. This is the headline acceptance behaviour.
2. **Live-only.** The lease must not have already lapsed (`now < lease_expires_at`). An **expired** worker
   is refused even if no other worker has reclaimed the row yet — a lapsed lease is up-for-grabs, so
   extending it would risk two live claims. The expired worker must let the job be reclaimed, not
   self-renew.
3. **Within the absolute ceiling.** The lease may not be pushed past the row's `lease_deadline` (stamped
   at claim time as `now + MAX_LEASE_LIFETIME_SECONDS`, reset on every reclaim). Once `now >=
   lease_deadline` the heartbeat is refused, so a stuck/looping worker cannot heartbeat forever and starve
   a reclaim.

On success the window is `min(lease_seconds, MAX_LEASE_SECONDS)` from `now`, then clamped so it never
exceeds `lease_deadline` — bounded extension — and the **fencing token is not bumped** (the same claim
continues). Constants in `app/jobs/claim.py`: `DEFAULT_LEASE_SECONDS=300`, `MAX_LEASE_SECONDS=900`,
`MAX_LEASE_LIFETIME_SECONDS=3600` (all generous enough that normal heartbeats are never clamped).
`claim_next` stamps `lease_deadline` at claim time and clamps the first lease window to it, so the first
lease obeys the same ceiling as every later renewal.

Naive-vs-aware datetimes are normalised (`_as_utc`) before the live-only / deadline comparisons, because
SQLite reads timestamps back naive while PostgreSQL keeps them aware (same discipline as the tenants
accept route).

## Schema

Migration `0039_job_request_lease_deadline` adds `lease_deadline` (nullable `timestamptz`) to
`durable_job_requests` (the revision id stays ≤32 chars so it fits alembic's `version_num` column). Dual-path safe, add-column branch (same guard style as 0011/0015/0020/0038): a
no-op on a fresh DB (the 0001 metadata baseline already has the column), and the real add on a DB that
reached 0038 before it existed. The column is nullable, so existing rows (queued or already-running)
read `NULL` — `renew` treats a `NULL` deadline as "no DCA-102 ceiling recorded" and falls back to the
per-call bound; the absolute ceiling only applies to claims taken after the migration. No backfill
needed. No RLS change: `durable_job_requests` stays tenant-owned and the 0002/0037 isolation policy is
untouched.

## Boundary

Python DCA only. No UI, no `effortlesspromo-ai` internals, no other service's database, no provider
SDKs/keys, no second AI gateway, no .NET changes. Future execution (handler bodies, a worker drain loop,
retry/dead-letter transitions, a complete/fail path) stays a disabled future capability.

## Verification

- `tests/test_job_lease_renewal.py`
  - **Always-on (SQLite), serial logic:** a live lease is renewed and extended **without** bumping the
    fencing token (same claim); an **expired** worker's renewal is refused and the row is unchanged (the
    headline denial); a **replaced** worker (job reclaimed → fence bumped) cannot renew with its stale
    fence; a chain of in-window heartbeats clamps at `lease_deadline` and is then refused once the ceiling
    is reached (no forever-renewal); a single window is capped at `MAX_LEASE_SECONDS`; a foreign worker id
    cannot renew someone else's live lease; another business cannot renew this business's job (by-id reads
    not-found); and the default window is applied verbatim (well under the caps).
  - **Disposable PostgreSQL (`DCA_TEST_DATABASE_URL`), real commits/rollbacks + the real column:**
    re-runs the happy-path renewal, the expired-worker denial, and the clamp-then-stop-at-deadline
    contract against a real Postgres, because the acceptance cases are about durable row state across
    short transactions (never SQLite).
- `tests/test_migrations.py::test_0039_adds_lease_deadline_on_a_preexisting_database` — the add-column
  branch re-adds `lease_deadline` as nullable, leaves a pre-existing row's value `NULL` (no backfill), and
  leaves the RLS policy intact.
- `tests/test_job_claim.py` / `tests/test_rls.py` — the DCA-101 claim contract and tenant isolation stay
  green (unchanged).

The durable cross-transaction guarantees live on PostgreSQL, so those tests require a real Postgres and
skip without a URL; SQLite drives the lease/renewal *logic* serially.

## Manual check

```
python scripts/manual_check.py DCA-102
```

Claims a synthetic job in an isolated Business A, then drives the renewal path serially against the
ephemeral SQLite DB and prints the real rows: worker-1 claims a job (leased, not run); worker-1
**heartbeats** the live lease (extended `+60s→+90s`, fencing token unchanged); after the lease lapses the
same worker's renewal is **refused** (no change); worker-2 reclaims the lapsed job (fence `1→2`) and
worker-1's stale fence is **refused**; and a chain of heartbeats on a fresh claim clamps at the absolute
`lease_deadline` and is then **refused** at the ceiling. Local synthetic only — no AI/paid action, no
network, no send, and no job is executed. The durable-row renewal contract is exercised on disposable
PostgreSQL in `tests/test_job_lease_renewal.py`.
