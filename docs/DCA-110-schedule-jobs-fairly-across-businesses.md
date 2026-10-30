# DCA-110 — Schedule jobs fairly across businesses

Stage 09 (Durable Python execution) · key `tenant-fairness` · source sections 2, 3, 8.

## What this adds

Bounded per-business concurrency and **fair selection** in the DCA-109 worker, so one large tenant
cannot monopolize workers while another tenant's jobs wait (acceptance: *"B receives progress while A has
a large backlog; global and tenant limits are enforced"*).

DCA-109 drained each enabled tenant to its batch cap before the next tenant got a turn — a tenant with a
huge backlog could consume a whole pass. DCA-110 replaces the drain phase of a pass with a **round-robin
fair drain** under two caps:

- **Per-business cap (the TENANT limit)** — at most `worker_max_jobs_per_business_per_pass` jobs are
  claimed for any one tenant in a pass; the rest of its backlog stays `QUEUED` for a later pass.
- **Global cap (the GLOBAL limit)** — at most `worker_max_jobs_per_pass` jobs are claimed across **all**
  tenants in a pass, so the drain is always bounded.
- **Round-robin selection** — each round claims **one** job per still-active tenant (in a stable tenant
  order), so a tenant with a single queued job is serviced in the **same pass** as a tenant with a deep
  backlog.

A tenant drops out of the rotation once it hits its cap or has nothing more claimable; the drain ends
when every tenant has dropped out, the global cap is hit, a round makes no progress, or a shutdown lands.

## Files changed (DCA)

- `app/jobs/worker.py`
  - New `DurableJobWorker.drain_fair(business_ids, …, max_jobs_per_business, max_jobs_per_pass)` — the
    round-robin fair drain (the load-bearing guarantee is stated in its docstring and the module
    docstring).
  - Extracted `_claim_and_handle_one(...)` — the single shared claim→park/relinquish step reused by both
    `drain_business` (single-tenant, unchanged behaviour) and `drain_fair`.
  - `run_pass` now drives the drain phase through `drain_fair` instead of looping `drain_business`
    per tenant.
  - New constructor kwargs `max_jobs_per_business` / `max_jobs_per_pass` (default: the Settings values,
    then module defaults `DEFAULT_MAX_JOBS_PER_BUSINESS=25` / `DEFAULT_MAX_JOBS_PER_PASS=200`).
  - `WorkerPassReport.handled_by_business` — per-tenant handled counts (the fairness evidence).
- `app/core/config.py` — new settings `worker_max_jobs_per_business_per_pass` (default 25) and
  `worker_max_jobs_per_pass` (default 200), validated positive at boot (`validate_required`).
- `tests/test_job_fairness.py` — focused fairness tests (see below).
- `scripts/manual_check.py` — registered the `DCA-110` inspectable scenario.

## Preserves tenant isolation and the execution boundary

Fairness is **only a selection policy** layered on the DCA-101 claim — it changes *which* tenant's job is
claimed next, never *how* a job is claimed. Every claim still goes through the tenant-scoped
`JobClaimStore` (`FOR UPDATE SKIP LOCKED`, lease + fencing token), so tenant isolation / RLS and the
single-current-claim guarantee are fully preserved, and a disabled business is still never processed.

Handler **execution stays the same disabled future capability** (`EXECUTION_ENABLED = False`): a
fairly-claimed job is PARKED `DEAD_LETTER` exactly as in DCA-109 — never run, no AI / network I/O, nothing
sent. Emergency stops (DCA-111) are a later story and are **not** implemented here.

## Schema / migrations

**None.** This is pure worker scheduling logic over the existing `durable_job_requests` table. No
tenant-owned table was added or changed, so `test_migrations.py` / `test_rls.py` are unchanged.

## Verify

```bash
# focused
.venv/Scripts/python.exe -m pytest tests/test_job_fairness.py -q
# full suite (exit code 0 is authoritative)
.venv/Scripts/python.exe -m pytest -q
# inspectable scenario (prints real claim order + per-business counts, not PASS)
.venv/Scripts/python.exe scripts/manual_check.py DCA-110
```

The manual check, over isolated synthetic Businesses A (8-job backlog) and B (1 job) with tight caps
(per-business = 3, global = 5), prints the real claim **order** (`A → B → A → A` — B serviced in round 1,
not starved behind A's backlog), the per-business counts (A capped at 3, B = 1), and the resulting rows
(A: 3 `dead_letter` + 5 `queued` leftover for a later pass; B: 1 `dead_letter`) — every driven job parked
safely, nothing run or sent.

A disposable-PostgreSQL test (`DCA_TEST_DATABASE_URL`) re-runs the fair drain against real
`FOR UPDATE SKIP LOCKED` claim commits; it skips without a URL.

## Tuning (optional, local)

```bash
EFFORTLESSPROMO_DCA_WORKER_MAX_JOBS_PER_BUSINESS_PER_PASS=25   # TENANT limit per pass
EFFORTLESSPROMO_DCA_WORKER_MAX_JOBS_PER_PASS=200               # GLOBAL limit per pass
```
