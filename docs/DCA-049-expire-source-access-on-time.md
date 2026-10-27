# DCA-049 — Expire source access on time

Stage: Source rights and policy foundation · key: `source-expiry`

## What this adds

Two layers that keep a lapsed source right from being used, and the first is **load-bearing**:

1. **Effective-date checks (immediate, authoritative — already in place).** A source right is in force
   only *inside* its explicit validity window. The platform ceiling's
   `SourceApprovalStore.is_in_force` (DCA-046) and the terms version's `SourceTermsStore.permits`
   (DCA-045) both evaluate `effective_from` / `expires_at` against the moment `now` the decision is
   made, so the DCA-048 preflight denies the instant an approval's review deadline (`expires_at`)
   passes or before an approval's `effective_from` arrives — **regardless of the stored status flag**.
   Nothing has to run first: an expired source is unusable the moment its window closes, even before
   any sweep catches up. This story verifies that guarantee end-to-end with a controllable clock.

2. **Scheduled expiry transition (new — durable housekeeping).** `app/services/source_expiry.py` →
   `SourceExpirySweep` is the scheduled cleanup the worker runs each cycle: it finds ACTIVE approvals
   whose review deadline has passed and ACTIVE terms versions whose own window has lapsed at `now`,
   and moves each to the terminal `EXPIRED` status (demoting the terms version a now-lapsed approval
   had promoted). This makes the lapse a *durable, auditable record* — a reader/query sees `EXPIRED`
   rather than a stale `ACTIVE` — but it is **never** what enforces the denial. The sweep only
   *records* what the clock already decided.

### The guarantees (acceptance criteria)

- **An expired source becomes unusable even before a cleanup sweep catches up.** The effective-date
  check is authoritative: the moment `now >= expires_at`, `is_in_force` returns `False`,
  `in_force_approval` yields `None`, and the preflight denies with `source_not_platform_approved` —
  while the approval row is still stored `active` because no sweep has run. The status flag never
  authorizes a fetch; the window does.
- **Reproduce the result and relevant denial without unintended state changes.** The effective-date
  check (`authorize`) is a pure, side-effect-free read — re-running it changes nothing. The sweep is a
  pure function of `now`: it transitions only rows genuinely past their window, so it is idempotent (a
  second run at the same `now` transitions nothing) and it can never promote, widen or re-enable
  anything — it only ever moves a lapsed right ACTIVE → EXPIRED. A not-yet-effective row (an approval
  whose `effective_from` is still in the future) is left `ACTIVE`: it is not lapsed, merely not yet in
  force, which the effective-date check already handles.
- **Future capabilities stay disabled.** This story enables no source and ships no live adapter. The
  sweep only makes a lapsed right *more* restrictive, so it is safe to run whenever the worker loop is
  active and is not gated behind any feature/source flag. In the running service every production
  preflight still denies at the source-status gate (no source is ever ENABLED).

## Components touched (DCA only)

- **Models** (`app/db/models.py`): add `EXPIRED` to `SourceTermsStatus` and `SourceApprovalStatus`
  (the terminal lapsed state the sweep writes). The `status` columns are `String(16)`, so **no
  migration is required** — a new enum string needs no DDL change.
- **Service** (`app/services/source_expiry.py`): `SourceExpirySweep` (the sweep, platform
  control-plane, session-only, stage-not-commit), `ExpirySweepResult`, and `run_expiry_sweep(...)`
  (the worker-facing entry point that owns its own commit).
- **Worker** (`app/workers/scheduler.py`): `tick_maintenance(...)` runs the sweep once per cycle; the
  `worker_loop` calls it before the per-tenant discovery ticks (the loop stays OFF by default).
- **Manual check** (`scripts/manual_check.py`): the `DCA-049` scenario.

No new HTTP surface, Web/Admin UI or AI-service change is in scope.

## Tests

- `tests/test_source_expiry.py` — the behaviour + failure cases: an expired source is denied by the
  preflight BEFORE the sweep (row still `active`); the sweep stamps approval + terms `EXPIRED` and is
  idempotent; `run_expiry_sweep` commits; an in-force approval is untouched and stays usable; a
  not-yet-effective approval is denied by the effective-date check yet is NOT expired by the sweep; a
  terms version whose own window lapsed is expired independently of its approval.
- `tests/test_worker.py` — the scheduled maintenance tick wiring (`tick_maintenance`) runs the sweep
  and commits, and is a safe idempotent no-op once rows are already EXPIRED.
- `tests/test_manual_check.py` — runs the `DCA-049` scenario end-to-end on synthetic SQLite and
  asserts the clock-crossing + durable-transition evidence.

## Developer manual test

```text
python scripts/manual_check.py DCA-049
```

Runs on an ephemeral synthetic SQLite DB (AI disabled, auto-cleaned) with isolated Businesses A/B and
a **controllable clock**. It approves a source with an explicit 30-day review deadline, then:

- authorizes the same preflight **before** expiry (ALLOW) and the instant **after** expiry (DENY) —
  showing the approval row is still stored `active`, i.e. the source became unusable **before** any
  sweep ran;
- runs the scheduled expiry sweep at the post-expiry clock and shows it durably stamped the approval
  and the terms it promoted `EXPIRED` (idempotent on a second run);
- shows a not-yet-effective approval is denied by the effective-date check yet not expired by the
  sweep, and that Business B (no entitlement) is denied.

For RLS/transaction behaviour of the `source_term_approvals` / `source_terms` tables on disposable
PostgreSQL, see `tests/test_migrations.py` and `tests/test_rls.py` (unchanged by this story).

## Traceability

Source sections: 5, 6; key: `source-expiry`. This story's stop rule overrides the reference prompt's
broad continue-working instructions.
