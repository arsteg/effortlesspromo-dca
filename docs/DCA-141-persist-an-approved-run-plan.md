# DCA-141 — Persist an approved run plan

Stage: Discovery pipeline in small steps · Source sections 4, 8 · key `plan-stage`

## What this adds

DCA-141 is the first pipeline step that fills the DCA-140 run *envelope*: it connects the already-accepted
DCA-135 planner to a single run stage. An **authorized business administrator** approves a reviewed
`DiscoveryPlan` version onto a `pending` `DiscoveryRunRequest`, which **pins** the exact plan version,
**freezes** that plan's bounded tasks onto the run as an immutable checkpoint, and moves the run
`pending` → `planned`.

Approving a plan **executes nothing**: the run only advances one state and records the pinned snapshot —
no task is run, no job enqueued, no prospect suggested, no message sent and nothing is spent. Executing a
frozen plan's tasks is the later, still-disabled DCA-142+ pipeline. A plan cannot execute directly.

## Files

DCA (Python service) only — no AI service, Web, Admin or .NET changes:

- `app/db/models.py` — five new columns on `DiscoveryRunRequest` (`plan_version`, `plan_snapshot`,
  `approved_by`, `approved_by_type`, `approved_at`): the pinned/frozen approved plan + who/when.
- `app/identity/permissions.py` — new business-plane administrator scope `run.plan.approve`
  (`Scope.RUN_PLAN_APPROVE`), added to the admin/owner role set. An ordinary user, a viewer and a
  platform operator (no membership) do not hold it.
- `app/services/run_plan_approval.py` — `approve_run_plan(...)` + `RunPlanApprover` +
  `RunPlanApprovalResult` and the stable refusal / per-task violation codes. Authority-first, atomic
  DCA-026 audit, idempotent, caller owns the unit of work.
- `app/api/routes/discovery_runs.py` — new `POST /v1/tenants/{id}/discovery/runs/{run_id}/plan`
  (approve, 200, retry-safe via `Idempotency-Key`), gated by the `run.plan.approve` scope on top of the
  router's service-auth gate; `_run_view` now surfaces the plan-binding fields.
- `migrations/versions/0060_run_plan_approval.py` — dual-path add-column branch (next after 0059) for
  the five columns; `approved_by` / `approved_by_type` carry a server default so pre-existing PENDING
  rows backfill to "not yet approved". No RLS change (`discovery_run_requests` is already tenant-owned).
- `tests/test_run_plan_approval.py` — focused service + HTTP + Postgres-backed tests.
- `tests/test_migrations.py` — the `test_0060_...` add-column branch test.
- `tests/test_rls.py` — a `discovery_run_requests` tenant-isolation + write-check test.
- `scripts/manual_check.py` — `DCA-141` inspectable scenario.

## Guarantees (acceptance criteria)

Every guard is fail-closed and checked BEFORE the run is mutated, so a denial leaves the run and plan
unchanged (reproducible denial), and rejected steps carry an auditable reason:

- **Authority first (DCA-019).** The actor must hold the business-plane `run.plan.approve` scope for THIS
  business. An ordinary user, a viewer and a platform operator are refused (`run_plan_not_authorized`)
  before any state is read.
- **Both exist, within the tenant.** An unknown / cross-tenant run (`discovery_run_not_found`) or plan
  version (`discovery_plan_version_not_found`) is refused and changes nothing.
- **The run is still open.** Only a `pending` run can take a plan; a run already planned with a different
  plan, or running/terminal, is refused (`discovery_run_not_pending`).
- **Same grounding.** The plan's confirmed `icp_version` must match the run's (`plan_icp_version_mismatch`).
- **Bounded by the run's envelope.** Every plan task must stay inside the run's `source_scope` allowlist
  (source + permitted fields), its per-task `query_cap` and its `task_cap`. An over-bounds plan is refused
  WHOLE — never silently trimmed — (`plan_over_run_bounds`) with per-task violations. Over-scope can never
  widen a run.
- **Idempotent.** Re-approving the SAME plan onto an already-`planned` run returns the existing binding
  (no second transition, no duplicate audit).
- **Nothing executes.** The snapshot pins `executed` / `sent` / `spent` / `autoApproved` all `False`; no
  job is enqueued and no candidate suggested.

Tenant-owned (DCA-017/018 RLS): one business can never see, approve a plan onto, or enumerate another's
runs.

## Tests / results

- Focused: `.venv/Scripts/python.exe -m pytest tests/test_run_plan_approval.py -q`.
- Full suite: `.venv/Scripts/python.exe -m pytest -q`.
- RLS / migration behaviour is verified on a disposable PostgreSQL (`DCA_TEST_DATABASE_URL`), per the
  pack rule; those tests skip when no Postgres URL is supplied.

## Manual test

```
.venv/Scripts/python.exe scripts/manual_check.py DCA-141
```

Local synthetic, isolated Business A, ephemeral SQLite, AI/paid/executing disabled. Prints the run BEFORE
approval (pending), the two headline refusals (an unauthorized viewer; the over-bounds plan v2 — each
leaving the run untouched), then the APPROVED run (planned, plan version pinned, tasks frozen,
`executed=False`) and the idempotent re-approval, with `jobs_enqueued=0` / `candidates_suggested=0`.
