# DCA-111 — Enforce hierarchical emergency stops

Stage 09 (Durable Python execution) · key `kill-switch` · source sections 2, 3, 8.

## One capability

A **hierarchical emergency stop** (kill switch) over the DCA's own background-work machinery, with two
tiers whose authority planes are disjoint and where the higher tier **dominates** the lower:

| Tier | Scope | Table | Plane / authority | Audit |
|---|---|---|---|---|
| **Platform-wide** (top) | ALL tenants | `platform_emergency_stops` (control-plane, **no** tenant RLS) | platform operator holding `platform.emergency.stop` | the row itself is the who/when/why record (no tenant audit — a platform action has no single business scope) |
| **Per-business** (lower) | ONE business | `business_emergency_stops` (tenant-owned, **RLS**) | business administrator holding `emergency.stop` | atomic DCA-026 `AuditEvent` in the same unit of work |

While a stop is **active** the DCA-109 worker claims/advances **no** affected durable job and enqueues
**no** due schedule for the covered business(es). The platform tier dominates: a platform-wide stop halts
every business even one whose own latest state is *lifted*. Clearing a stop resumes the scope on the next
pass (unless a higher scope still dominates).

This changes nothing about execution: handler execution stays a **disabled future capability**
(`EXECUTION_ENABLED = False`). A stop only ever makes the worker do **less** — it can never cause a job to
run or a message to be sent. The DCA has no send path, and acceptance of a prospect is not recipient
consent; a stop only tightens that.

## Acceptance criteria → evidence

- *A pause stops new relevant side effects at checkpoints while unrelated allowed work can continue.* —
  The worker consults `EmergencyStopResolver.is_claiming_blocked(business_id)` at its single shared
  claim checkpoint (`DurableJobWorker._claim_and_handle_one`) and in its maintenance loop (`run_pass`).
  A paused scope's jobs are never claimed and its due schedules never enqueued; an un-stopped business
  keeps working in the same pass. Tests:
  `test_platform_stop_halts_all_businesses_at_the_worker`,
  `test_business_stop_halts_only_that_business`,
  `test_business_stop_also_suppresses_due_schedule_enqueue`,
  `test_clearing_a_business_stop_resumes_processing`,
  `test_resolver_platform_dominates_a_business_whose_own_state_is_lifted`.
- *Reproduce the result and relevant denial without unintended state changes.* — Each writer checks
  authority **first** and requires a non-empty opaque reason before the session is mutated, so a refused
  set/clear stages no row and no audit and leaves the effective state untouched. Tests:
  `test_business_stop_refuses_viewer_and_ordinary_user_and_changes_nothing`,
  `test_platform_stop_refuses_tenant_admin_and_changes_nothing`,
  `test_set_requires_a_non_empty_reason_and_changes_nothing`.

## Design notes

- **Authority is checked FIRST**, exactly like the DCA-050 revocation, DCA-078 platform source removal
  and DCA-105 dead-letter requeue. The business and platform scope sets are structurally disjoint
  (`app/identity/permissions.py` asserts it), so a business admin can never set the platform stop and a
  platform operator (holding no membership) can never set a client's business stop.
- **Append-only ledgers.** Each set/clear inserts a new version at `max(version)+1` for its scope; the
  current effective state is the latest version's `state` (`active`/`lifted`). History is never deleted
  ("without deleting existing records").
- **The worker only READS** the effective state (`EmergencyStopResolver`). Setting/clearing lives in
  `app/services/emergency_stop.py`; the worker never mutates a stop.
- The business-stop table mirrors `ProcessingRestriction` (tenant-owned, RLS, atomic audit); the
  platform-stop table mirrors `PlatformSourceRestriction` (control-plane, outside tenant RLS, row-as-record).

## Changed / added files

- `app/db/models.py` — `EmergencyStopState` enum + `BusinessEmergencyStop` (tenant-owned) and
  `PlatformEmergencyStop` (control-plane) models.
- `app/identity/permissions.py` — business scope `emergency.stop` (admin-only) + platform scope
  `platform.emergency.stop` (operator).
- `app/services/emergency_stop.py` — the two authority-gated stores, their principals, and the
  `EmergencyStopResolver` the worker consults.
- `app/jobs/worker.py` — the DCA-111 checkpoint gate (`is_claiming_blocked`) in
  `_claim_and_handle_one` and the `run_pass` maintenance loop; docstrings updated.
- `migrations/versions/0043_emergency_stops.py` — dual-path create + RLS (tenant-owned table only).
- `tests/test_emergency_stop.py` — focused store + worker-enforcement tests.
- `tests/test_rls.py`, `tests/test_migrations.py` — coverage set updates + Postgres RLS / 0043
  create-branch tests.
- `scripts/manual_check.py` — `@register("DCA-111", ...)` inspectable scenario.

## Manual test

```
python scripts/manual_check.py DCA-111
```

Prints the real hierarchy end to end against an ephemeral synthetic DB: an unauthorized viewer's pause is
refused (no change); a per-business stop on A pauses only A while B is driven in the same pass; the
platform-wide stop then halts **both** (dominating even A's cleared business state); clearing the platform
stop resumes both and the worker drives each job safely (parked `dead_letter`, never run or sent). Local
synthetic only — no AI/paid action, no network, no send.

The RLS and migration behaviours require a disposable PostgreSQL (`DCA_TEST_DATABASE_URL`); those tests
skip when no URL is supplied.
