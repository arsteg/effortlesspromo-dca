# DCA-109 — Run the Python worker process

Stage 09 (Durable Python execution) · key `worker-loop` · source sections 2, 3, 8.

## What this adds

A **worker process / entry point** that ties the already-shipped durable-job mechanics into one
supervised loop with graceful shutdown — the counterpart to the host job engine the DCA deliberately does
**not** depend on ("no host job engine is needed").

- `app/jobs/worker.py` — `DurableJobWorker`, the loop over the shipped stores.
- `effortlesspromo-dca worker` — a CLI subcommand that runs it with SIGINT/SIGTERM graceful shutdown
  (gated behind `worker_enabled`, OFF by default).

Each **pass** does, per enabled tenant, in order:

1. **Recovery tick** — `JobLeaseRecoveryStore.recover_abandoned` (DCA-106): heal a crashed worker's
   abandoned lease (re-queue under a new fence, or dead-letter if its attempts were exhausted).
2. **Schedule-maintenance tick** — `DueScheduleEnqueuer.tick` (DCA-108): turn due recurring schedules
   into enqueued durable jobs, exactly once logically.
3. **Claim + drive** — `JobClaimStore.claim_next` (DCA-101): claim each due job and drive it to a safe
   terminal state.

The loop (`run`) runs passes until shutdown (or a bounded `max_passes` for tests), sleeping on the
shutdown latch between passes so a stop wakes it immediately.

## The hard boundary — handler execution stays disabled

`EXECUTION_ENABLED = False` is the headline of this story. The worker claims a job and drives the
lifecycle **mechanics**, but it **never**:

- calls a handler body (`JobHandler.run` still raises `NotImplementedError`; **no path invokes it**),
- performs AI / network I/O, or **sends** anything (the DCA has no send path).

A claimed job is therefore **handled safely, not executed** — fail-closed, so the worker never
crash-loops and never sends:

- **no handler registered** for the job's `(job_type, schema_version)` (the production registry is
  deliberately empty, DCA-100) → PARKED `DEAD_LETTER`, note `worker_no_handler_registered`;
- **a handler is registered** but execution is disabled → the worker recognises the disabled capability
  and PARKS the job `DEAD_LETTER`, note `worker_execution_disabled` (it never calls `run`).

Parking goes through the DCA-104 `JobRetryScheduler` with a PERMANENT (`NOT_RETRYABLE`) category, so the
job reaches a terminal, inspectable state an operator can requeue via DCA-105 once the execution
capability ships — and it is no longer claimable, so the drain always terminates (no crash-loop). The
park branch is exactly where the future execution story wires the real `prepare` → `run` →
`JobCompletionStore` commit / retry path.

## Graceful shutdown (acceptance)

`request_shutdown()` (set by SIGINT/SIGTERM in the CLI entry point) latches a shutdown Event read at
every safe checkpoint:

- **Stops new claims** — the drain and pass loops check the latch before each claim and stop.
- **Relinquishes active work safely** — a job claimed just before a shutdown is released back to
  `QUEUED` (DCA-101 `release`, fenced on the claim) rather than left wedged `RUNNING` until its lease
  lapses. The work is preserved for another worker; nothing is executed or sent.

## Scope / boundary

Strictly within DCA-109. Explicitly **not** implemented (later stories): fair cross-business scheduling
(DCA-110) and hierarchical emergency stops (DCA-111). The worker iterates enabled tenants
(`TenantAgent.enabled`) in a simple stable order; a disabled business is never processed; every store is
tenant-scoped so a worker can never touch another tenant's job.

## Schema / migrations

**None.** The worker reuses the existing `durable_job_requests` lifecycle columns (lease/fence,
attempts, `last_error`, result pointer). No tenant-owned table was added, so `test_migrations.py` /
`test_rls.py` are unchanged.

## Verify

```bash
# focused
.venv/Scripts/python.exe -m pytest tests/test_job_worker.py -q
# full suite (exit code 0 is authoritative)
.venv/Scripts/python.exe -m pytest -q
# inspectable scenario (bounded run; prints real resulting rows, not PASS)
.venv/Scripts/python.exe scripts/manual_check.py DCA-109
```

The manual check, over isolated synthetic Businesses A (enabled) and C (disabled), shows: one bounded
pass fires a due schedule (enqueues a cycle job), recovers a crashed worker's abandoned lease, then
claims the due jobs and parks each `DEAD_LETTER` safely (no handler run, nothing sent); a shutdown stops
new claims and relinquishes a held claim back to `QUEUED`; and the disabled Business C is never touched.

A disposable-PostgreSQL test (`DCA_TEST_DATABASE_URL`) re-runs the real claim → park flow against real
commits; it skips without a URL (`FOR UPDATE SKIP LOCKED` is PostgreSQL-only).

## Run it (optional, local)

```bash
EFFORTLESSPROMO_DCA_WORKER_ENABLED=true effortlesspromo-dca worker
# Ctrl-C -> stops new claims, relinquishes held work, exits. Nothing is sent.
```
