# DCA-140 — Create a bounded discovery run

Stage: Discovery pipeline in small steps · Source sections 4, 8 · key `run-record`

## What this adds

DCA-140 starts the "discovery pipeline in small steps" stage. It creates a **bounded discovery run** —
the orchestration *envelope* for one acquisition cycle that the later DCA-141..149 steps progressively
fill (persist a plan, execute a task, convert to observations, resolve, suggest, finalise). This story
persists the run **request only**; it executes nothing.

A run is created in the not-yet-executing `pending` state, grounded in the business's **confirmed** India
ICP and bounded, at creation, to the sources it is already approved+entitled to and to explicit
scope / budget caps.

## Files

DCA (Python service):

- `app/db/models.py` — new `DiscoveryRunStatus` enum (`pending` / `planned` / `running` / `completed` /
  `failed` / `cancelled`, with `active_values()` / `terminal_values()`) and the new tenant-owned
  `DiscoveryRunRequest` model (table `discovery_run_requests`). This is a NEW table, distinct from the
  legacy `discovery_runs` (the old deterministic "run-it-all-now" cycle), which is untouched.
- `app/services/discovery_run.py` — `create_discovery_run(...)` + `DiscoveryRunStore` + clamp helpers
  and the stable refusal / clamp reason codes.
- `app/api/routes/discovery_runs.py` — service-auth HTTP contract: `POST /v1/tenants/{id}/discovery/runs`
  (create, 201, retry-safe via `Idempotency-Key`), `GET .../discovery/runs` (list),
  `GET .../discovery/runs/{run_id}` (fetch). Plural `runs` to sit alongside the legacy singular
  `discovery/run`.
- `app/main.py` — register the router.
- `migrations/versions/0059_discovery_run_requests.py` — dual-path create + RLS policy/grant (next after
  0058), plus a **Postgres-only partial unique index** `uq_discovery_run_active_scope` on
  `(business_id, icp_version) WHERE status IN ('pending','planned','running')` backing the overlap guard.
- `tests/test_discovery_run.py` — focused service + HTTP tests.
- `tests/test_rls.py`, `tests/test_migrations.py` — add `discovery_run_requests` to the expected
  tenant-owned / DCA table sets.
- `scripts/manual_check.py` — `DCA-140` inspectable scenario.

No AI service, Web, Admin or .NET changes.

## Guarantees (acceptance criteria)

Every guard is fail-closed and checked BEFORE any row is staged, so a denial leaves no state
(reproducible denial):

- **Feature / tenant gate (DCA-021).** Feature off, or tenant not enabled / out of country → refused.
- **Entitlement freshness (DCA-023).** When enforcement is on, an absent/expired/stale subscription →
  refused before anything is staged.
- **Confirmed targeting (DCA-040).** No confirmed ICP → an **unconfirmed profile cannot start a run**
  (`no_confirmed_icp`).
- **No overlapping logical runs.** Only ONE active run per `(business_id, icp_version)`; a second one is
  refused (`duplicate_active_run`) — **duplicate active scope cannot start redundant work**. A terminal
  run frees the scope. A Postgres partial unique index backs the app-level guard under concurrency.
- **Bounded by construction.** Source scope = the approved+entitled allowlist; scope/budget caps are
  clamped to absolute ceilings (`target_cap` ≤ 500, `task`/`query` share the DCA-135 ceilings, budget ≤
  the business's per-run cap). An over-scope / over-budget request is **clamped down** (recorded in
  `clamp_notes`), never widened.
- **Nothing executes.** Creating a run runs no task, enqueues no job, suggests no prospect, sends no
  message and spends nothing. `provenance` pins `executed` / `sent` / `spent` / `autoApproved` all
  `False`.

Tenant-owned (DCA-017/018 RLS): one business can never read or start another's runs.

## Tests / results

- Focused: `.venv/Scripts/python.exe -m pytest tests/test_discovery_run.py -q` → 16 passed.
- Quality: `ruff check` / `ruff format --check` clean on the changed files; `mypy` clean on the new
  app modules and the new manual-check block (manual_check.py / policy_rulepacks.py carry pre-existing
  baseline errors, unrelated to this story).
- RLS / migration behaviour is verified on a disposable PostgreSQL (`DCA_TEST_DATABASE_URL`), per the
  pack rule; those tests skip when no Postgres URL is supplied.

## Manual test

```
.venv/Scripts/python.exe scripts/manual_check.py DCA-140
```

Local synthetic, isolated Businesses A/B, ephemeral SQLite, AI/paid disabled. Prints:

- Business A — a valid create: the PENDING run row, its clamped `targetCap` (999999 → 500) and
  `perRunBudgetMinor` (999999 → 50000), the honest `clampNotes`, and `executed/sent/spent=False`.
- Business A — the duplicate-active-scope denial (`duplicate_active_run`, no second row).
- Business B — the unconfirmed-profile denial (`no_confirmed_icp`, no row).
- Stored runs `A=1 B=0`, `jobs_enqueued=0`, and all checks `True`.
