# DCA-146 — Finalize a discovery run summary

**Status:** Ready for manual review (uncommitted). **Stop rule honored:** DCA-147 not started.

## What this story does

Aggregate ONE discovery run's already-recorded outcomes — its **task counts**, the **held (never spent)
cost**, the **failure/denial reasons**, the resolved-batch **counts**, the **suggestion count** and a
durable checkpoint **cursor** — into ONE durable `DiscoveryRunSummary`, and **settle the run into a
terminal status** (`completed` / `failed`). The summary exposes **no raw private data**: only aggregate
counts, stable reason codes and opaque, checkpointable row references.

This is the terminal inert step of the still-disabled discovery pipeline, after DCA-145 suggests
prospects from a run's resolved batch:

```
DCA-140 run → DCA-141 approve plan → DCA-142 execute task → DCA-143 convert to observations
            → DCA-144 resolve batch → DCA-145 create suggestion → DCA-146 FINALIZE summary   ← this story
```

Finalizing is **inert**: it reads already-recorded pipeline rows and concludes — no network fetch, no
model call, no spend, no send. `provenance` pins `finalized = True`, `sent = False`, `spent = False`.

## Acceptance criteria → how they are met

1. **Partial failure is reported honestly; a run with no matches is not fabricated success.**
   - The counts mirror the underlying `DiscoveryTaskExecution` rows exactly — `task_count`,
     `done_count`, `failed_count`, `denied_count` — and `reason_counts` names *why* each non-DONE task
     stopped (its stable `reason_code`).
   - `final_status` is `COMPLETED` **only** when the run did genuine successful work (≥ 1 DONE task). A
     run whose tasks all failed / were denied, or that executed **nothing** (`empty = True`), settles to
     `FAILED` — never a fabricated success. `partial_failure` flags a `COMPLETED` run that nonetheless had
     failed / denied tasks.
   - The resolved `matched_count` is recorded **as-is**, so "no matches" reads as `0`, not as success.
2. **Reproduce the result and the relevant denial without unintended state changes.**
   Fail-closed guard order (every refusal is raised **before** the session is mutated):
   1. **Authority FIRST** — caller must hold `Scope.RUN_PLAN_APPROVE` (a business admin); a viewer /
      platform operator is refused (`RunFinalizationAuthorizationError`, `run_finalization_not_authorized`).
   2. **Run exists in THIS business** — else `discovery_run_not_found` (an unknown / cross-tenant run
      reads exactly like not-found).
   3. **Idempotent** — `(business_id, run_id)` is unique, so re-finalizing returns the existing summary
      and advances no state and writes no second audit.

## What gets aggregated

- **Task outcomes / counts** — over the run's `DiscoveryTaskExecution` records (reused via a tenant-scoped
  read): `done` / `failed` / `denied` out of `task_count`.
- **Reasons** — `reason_counts` = `{reason_code: count}` for every non-DONE task.
- **Cost** — `reserved_cost_minor` = the sum of the tasks' DCA-113 reservation **holds** (paise). A hold
  is **never a spend** — the DCA has no spend path — so this is the budget that *would* have been
  consumed; `per_run_budget_minor` echoes the run's spend ceiling.
- **Resolved batch** — `subject_count` / `matched_count` from the DCA-144 `ObservationBatchResolution`
  (0 when the run was never resolved — recorded as-is), plus `suggestion_count` (DCA-145 rows for the run).
- **Cursor** — a durable checkpoint: `executedTaskIndices`, `lastTaskIndex`, aggregate
  `importedCount` / `skippedCount` / `refusedCount`, the opaque `importedRowRefs`, and the resolved-batch
  breakdown. Never a contact value.

## Terminal-status rule (honest)

| Run state at finalize | `done_count` | result |
|---|---|---|
| ≥ 1 DONE task | > 0 | `completed` (`partial_failure=True` if any failed/denied) |
| executed, all failed/denied | 0 | `failed` |
| nothing executed (empty) | 0 | `failed` (`empty=True`) |

## Files changed (all uncommitted, DCA repo only)

### New
- `app/services/discovery_run_finalization.py` — the single sanctioned writer `DiscoveryRunFinalizer`
  (+ `RunFinalizationActor` / `RunFinalizationResult`, stable reason codes, and the typed error classes).
- `migrations/versions/0065_discovery_run_summaries.py` — Alembic migration (dual-path create + RLS,
  mirroring 0064). Revision `0065_discovery_run_summaries`, down_revision `0064_prospect_suggestions`
  (revision id = 28 chars, ≤ 32).
- `tests/test_discovery_run_finalization.py` — SQLite unit tests + the opt-in Postgres-gated layer
  (`DCA_TEST_DATABASE_URL`, schema `dca_146_test`).
- `docs/DCA-146-finalize-a-discovery-run-summary.md` — this file.

### Edited
- `app/db/models.py` — new `DiscoveryRunSummary(TenantOwned, Base)` table (`discovery_run_summaries`).
- `scripts/manual_check.py` — `@register("DCA-146", …)` inspectable scenario.
- `tests/test_migrations.py` — added `discovery_run_summaries` to `DCA_TABLES` + a `0065`
  create-branch test.
- `tests/test_rls.py` — added `discovery_run_summaries` to the tenant-owned set + a tenant-isolation /
  write-check test pair.

## The `discovery_run_summaries` table

Tenant-owned (`business_id` + RLS). Columns: `run_id` (FK `discovery_run_requests.id`), `final_status`,
`task_count` / `done_count` / `failed_count` / `denied_count`, `reserved_cost_minor`,
`per_run_budget_minor`, `subject_count`, `matched_count`, `suggestion_count`, `partial_failure`, `empty`,
`reason_counts` (JSON), `cursor` (JSON), `schema_version`, `provenance` (invariants), `created_at`.
Unique `(business_id, run_id)` → `uq_discovery_run_summary_run` (a run finalizes once).

## Evidence

- Focused tests: `.venv/Scripts/python.exe -m pytest tests/test_discovery_run_finalization.py -q` →
  **8 passed, 1 skipped** (the skip is the Postgres-gated test; set `DCA_TEST_DATABASE_URL` to run it).
- Quality gates (new/edited files): `ruff check` clean, `ruff format --check` clean,
  `mypy app/services/discovery_run_finalization.py` clean, `mypy app/db/models.py` clean.
- Manual check: `.venv/Scripts/python.exe scripts/manual_check.py DCA-146` → all checks `True`:
  ```
  {'run1_completed_with_work': True, 'run1_not_partial_not_empty': True, 'run1_held_cost_recorded': True,
   'run1_no_matches_not_fabricated': True, 'run1_cursor_opaque_checkpoint': True,
   'run1_invariants_no_send_no_spend': True, 'run1_settled_terminal': True, 'refinalize_idempotent': True,
   'empty_run_failed_not_success': True, 'unauthorized_refused': True, 'cross_tenant_not_found': True}
  ```

## Developer manual test

```
.venv/Scripts/python.exe scripts/manual_check.py DCA-146
```

Drives Business A through the full pipeline then FINALIZES run #1 (prints the durable summary: terminal
status, honest outcome counts, held cost, resolved matches / suggestions, reason breakdown and the
checkpoint cursor), FINALIZES an **empty** run #2 → `failed` (NOT fabricated success), shows an idempotent
re-finalize, and reproduces the two refusals (unauthorized viewer, cross-tenant not-found) — each leaving
no state. Local synthetic only; no network/model/spend/send.

## Boundary confirmation

Python DCA only. No other service's DB touched. No AI capability added. No UI. No `.NET` change. Finalizing
reads already-recorded rows and aggregates — nothing fetched, sent, spent or auto-accepted; future
capabilities stay disabled.
