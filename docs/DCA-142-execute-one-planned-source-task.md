# DCA-142 — Execute one planned source task

Stage: Discovery pipeline in small steps · Source sections 4, 8 · key `fetch-stage`

## What this adds

DCA-142 is the next pipeline step after a DCA-141 plan is frozen onto a run: it **executes exactly ONE**
of that run's approved, frozen tasks. It is the orchestration glue that connects, for one task, the four
mechanisms earlier stories already built — the DCA-048 source rights preflight, the DCA-113 cost
reservation, the DCA-115 provider-call ledger, and the concrete DCA-123 source adapter — storing only the
**permitted staged output / checkpoint** and advancing the run `planned` → `running`.

There is **no real network fetch and no recipient send.** The adapter runs deterministically against
caller-supplied / synthetic fixture inputs (exactly as the DCA-123 importer takes an already-approved
dataset); "execute" means run the adapter mechanics, bounded to one task. Acceptance of a prospect is not
recipient consent, and no send path exists.

Guards are checked **fail-closed, in order**: authority first, then the run must be executable
(`planned`/`running`) and the task must come from the run's frozen `plan_snapshot` (only approved tasks
run), then the **rights preflight**, then the **budget reservation**. Only once both guards permit is the
provider call dispatched and the adapter invoked. A revoked source or an exhausted budget records a
`denied` outcome and the adapter is never reached — the run is unchanged.

Execution is an internal pipeline step (invoked by the later worker), not a direct client action, so this
story adds **no HTTP route** — only the service seam the later pipeline / worker calls.

## Files

DCA (Python service) only — no AI service, Web, Admin or .NET changes:

- `app/db/models.py` — new `DiscoveryTaskStatus` enum (`done` / `failed` / `denied`) and the
  `DiscoveryTaskExecution` tenant-owned model (one row per executed task; `(business_id, run_id,
  task_index)` unique = "a task runs once"; holds the source/adapter keys, outcome + phase + reason, the
  reservation / provider-call ids it drew on, and the permitted `staged_result` checkpoint — opaque counts
  + row refs, no contact data).
- `app/services/discovery_task_execution.py` — `DiscoveryTaskExecutor.execute_task(...)` +
  `TaskExecutionActor` + `OrgDatasetTaskInput` + `TaskExecutionResult` and the stable refusal / phase
  codes. Authority-first, rights-then-budget-before-external-access, atomic DCA-026 audit, idempotent,
  caller owns the unit of work.
- `migrations/versions/0061_discovery_task_executions.py` — dual-path create branch (next after 0060) for
  `discovery_task_executions` with its tenant-isolation RLS policy and the per-(run, task) unique
  constraint. Revision id ≤ 32 chars.
- `tests/test_discovery_task_execution.py` — focused service + Postgres-backed tests.
- `tests/test_migrations.py` — the `test_0061_...` create-branch test; `DCA_TABLES` updated.
- `tests/test_rls.py` — a `discovery_task_executions` tenant-isolation + write-check test; the
  `tenant_owned_tables()` expectation updated.
- `scripts/manual_check.py` — `DCA-142` inspectable scenario.

Authority reuses the existing `Scope.RUN_PLAN_APPROVE` administrator scope (executing a run's approved
task governs the same chargeable discovery work the plan approval did) — **no new permission** is added.

## Guarantees (acceptance criteria)

- **A revoked source or exhausted budget stops the task before external access.** The rights preflight is
  evaluated before any reservation or adapter call; a revoked source records `denied` (phase `rights`)
  with no provider call and no budget hold. The reservation is taken before the adapter; an over-budget /
  no-budget refusal records `denied` (phase `budget`) and the adapter is never invoked. In both cases the
  run's state is unchanged.
- **Reproduce the result and the relevant denial without unintended state changes.** A task runs once
  (unique on `(business_id, run_id, task_index)`); re-executing is an idempotent no-op returning the
  existing record. A successful invocation stages the raw result and advances the run `planned` →
  `running` exactly once; a denial changes nothing but its own record + atomic audit entry.
- **Tenant isolation.** The executor is business-scoped and the table is RLS-protected; a cross-tenant run
  reads as not-found and one business can never execute or read another's tasks.
- **Future / paid capabilities stay disabled.** No real network fetch, no model call, no recipient send,
  and no spend (a reservation is a hold, not a charge). The DCA-124 website / DCA-125 licensed-provider /
  DCA-126 private-CSV adapters remain reachable through the same seam but are each their own wiring.

## Manual check

```text
python scripts/manual_check.py DCA-142
```

Runs three isolated synthetic businesses on a disposable DB: **A** executes a valid fixture task (`done` —
a staged result + a dispatched provider call, run advanced to `running`, then an idempotent re-run);
**B** has its source revoked after approval (`denied`, phase `rights`); **C** has a near-zero budget
(`denied`, phase `budget`). It prints each task before/after and a call-log comparison proving **only A**
reached external access.

## Tests

```text
.venv/Scripts/python.exe -m pytest tests/test_discovery_task_execution.py -q
```

SQLite service tests run everywhere; the Postgres-backed test (and the migration / RLS Postgres tests)
run only when `DCA_TEST_DATABASE_URL` points at a disposable PostgreSQL.
