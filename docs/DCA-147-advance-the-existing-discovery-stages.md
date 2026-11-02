# DCA-147 — Advance the existing discovery stages

**Status:** Ready for manual review (uncommitted). **Stop rule honored:** DCA-148 not started.

## What this story does

Adds the one missing piece of the DCA-140..146 pipeline: an **orchestrator** that *advances* a run by
composing the already-accepted, single-purpose stages. It invents no new stage, makes **no provider call**
and calls **no model** — it reads the durable rows each stage wrote and drives the next stage off those
checkpoints.

```
DCA-140 run → DCA-141 approve plan → DCA-142 execute task (the extraction, the only provider access)
            → DCA-143 convert → DCA-144 resolve → DCA-145 suggest → DCA-146 finalize (terminal)
                                └──────────────── DCA-147 drives this inert tail ────────────────┘
```

`DiscoveryRunAdvancer.advance_run(run_id, actor)` drives the **post-extraction tail**
(`convert → resolve → suggest → finalize`) one stage-step at a time, re-deriving the next-incomplete stage
from the durable checkpoints after each step. It deliberately **cannot** run the two input-driven stages —
approving a plan (DCA-141) and executing a task (DCA-142, the only stage that dispatches a provider call
and needs caller-supplied / synthetic fixture inputs). A run that still needs either is reported *blocked*
(`awaiting_plan` / `awaiting_execution`) and advanced no further.

## Acceptance criteria → how they are met

1. **Restart resumes the next incomplete stage, not all provider calls; no endless model loop.**
   - The advancer holds **no state of its own**. The next stage is recomputed purely from the stage rows
     the earlier stories wrote — each unique on `(business_id, run_id[, task_index|subject_id])` and
     idempotent. So a worker that crashes after extraction and restarts picks up at exactly the next
     unfinished stage: an already-converted task is skipped (no duplicate, no re-import), an
     already-resolved batch is not re-resolved, an already-suggested subject is not re-suggested.
   - None of the tail stages touches the AI gateway or dispatches a provider call, so advancing a run adds
     **zero** provider calls (the extraction's count is unchanged, `provider_calls_made == 0`) and there is
     no model loop. Every step makes monotonic progress (one more durable row) and the loop stops at the
     first terminal / blocked / cancelled / cap-reached state, so it always terminates (a defensive
     `MAX_ADVANCE_STEPS` ceiling backs this).
2. **Reproduce the result and the relevant denial without unintended state changes.** Fail-closed order
   (every refusal / stop is raised **before** any stage runs or leaves the run untouched):
   1. **Authority FIRST** — the caller must hold `Scope.RUN_PLAN_APPROVE` (the same administrator scope the
      whole chargeable-discovery chain requires); a viewer / platform operator is refused
      (`RunAdvancementAuthorizationError`, `run_advancement_not_authorized`).
   2. **Run exists in THIS business** — else `discovery_run_not_found` (an unknown / cross-tenant run reads
      exactly like not-found).
   3. **Cancelled / blocked** — a cancelled run is stopped cold and a run awaiting an input-driven stage is
      advanced no further; neither is finalized and the run is left exactly as it was.

## The stage state machine (derived, not persisted)

`progress(run_id)` returns a pure `RunProgress` snapshot whose `next_stage` the advancer acts on:

| `next_stage` | meaning | advancer action |
|---|---|---|
| `awaiting_plan` | PENDING / no frozen plan | **blocked** (needs DCA-141) |
| `awaiting_execution` | planned, extraction incomplete | **blocked** (needs DCA-142) |
| `convert` | a DONE task is unconverted | convert it (DCA-143) |
| `resolve` | the converted batch is unresolved | resolve it (DCA-144) |
| `suggest` | a resolved eligible subject is unsuggested, cap permitting | suggest it (DCA-145) |
| `finalize` | nothing left but the run is not settled | finalize it (DCA-146) |
| `done` | finalized / terminal | stop |
| `cancelled` | the run was cancelled | stop (never finalized) |

- **Extraction-complete gate.** The tail runs only once every task frozen into the run's plan has an
  execution record — the advancer never prematurely finalizes a run still waiting for a DCA-142 task.
- **Empty batch.** A run whose extraction produced no observations skips `resolve`/`suggest` straight to
  `finalize` (→ `failed`, honest, never fabricated success).
- **Cap bound.** Suggesting stops once the run's `target_cap` is reached (`cap_reached = True`); the run
  still finalizes.

## Files changed (all uncommitted, DCA repo only)

### New
- `app/services/discovery_run_advancement.py` — the orchestrator `DiscoveryRunAdvancer` (+ `RunAdvanceActor`
  / `RunProgress` / `RunAdvanceResult`, the stage labels, stable reason codes and the typed error classes).
  It composes the DCA-143 converter, DCA-144 resolver, DCA-145 suggestion creator and DCA-146 finalizer.
- `tests/test_discovery_run_advancement.py` — SQLite unit tests (full advance, restart-resume, idempotent
  re-advance, all-denied → failed, awaiting-plan / awaiting-execution blocks, cancellation, the `target_cap`
  cap, `progress()`, and the unauthorized / cross-tenant refusals) + the opt-in Postgres-gated layer
  (`DCA_TEST_DATABASE_URL`, schema `dca_147_test`: stop-after-extraction → restart → advance, no new
  provider calls).
- `docs/DCA-147-advance-the-existing-discovery-stages.md` — this file.

### Edited
- `scripts/manual_check.py` — `@register("DCA-147", …)` inspectable scenario.

**No migration / no schema change / no RLS change.** The advancer is stateless — it derives progress from
the existing durable checkpoint rows (DCA-142 executions, DCA-143 conversions, DCA-144 resolution, DCA-145
suggestions, DCA-146 summary), so there is no new table to migrate or protect. `tests/test_rls.py` /
`tests/test_migrations.py` are untouched.

## Evidence

- Focused tests: `.venv/Scripts/python.exe -m pytest tests/test_discovery_run_advancement.py -q` →
  **11 passed, 1 skipped** (the skip is the Postgres-gated test; set `DCA_TEST_DATABASE_URL` to run it).
- Quality gates (new/edited files): `ruff check` clean, `ruff format --check` clean,
  `mypy app/services/discovery_run_advancement.py` clean.
- Manual check: `.venv/Scripts/python.exe scripts/manual_check.py DCA-147` → all checks `True`:
  ```
  {'resumes_at_convert_after_extraction': True, 'advance_drives_full_tail': True,
   'run1_settled_completed': True, 'advance_adds_no_provider_call': True, 're_advance_is_noop': True,
   'awaiting_execution_blocked': True, 'cancelled_run_not_finalized': True, 'unauthorized_refused': True,
   'cross_tenant_not_found': True}
  ```

## Developer manual test

```
.venv/Scripts/python.exe scripts/manual_check.py DCA-147
```

Drives Business A: execute ONE task (the extraction, the only provider call) then STOP; a restart ADVANCEs
the inert tail to a terminal `completed` run (prints the resumed stage, the stages advanced, the settled
status/summary and the **unchanged** provider-call count). Shows a second advance is a no-op, a run awaiting
execution is blocked (nothing advanced), a cancelled run is stopped cold (not finalized), and reproduces the
two refusals (unauthorized viewer; Business B reading A's run as not-found) — each leaving no state. Local
synthetic only; no network/model/spend/send.

## Boundary confirmation

Python DCA only. No other service's DB touched. No AI capability added. No UI. No `.NET` change. Advancing
composes the accepted inert stages off durable checkpoints — nothing fetched, sent, spent, auto-accepted or
dispatched to a provider; future capabilities (plan approval, task execution, and anything executable) stay
outside this orchestrator and disabled.
