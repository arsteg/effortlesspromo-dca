# DCA-050 — Revoke a source version

Stage: Source rights and policy foundation · key: `source-revoke`

## What this adds

Operator revocation of an approved source-terms version, with **immediate** read/use denial and a
**queued** invalidation of the observations that source produced. Two layers, the first load-bearing:

1. **Operator revocation (immediate, authoritative).** `app/services/source_revocation.py` →
   `SourceRevocationStore.revoke` is the single sanctioned writer that withdraws a previously-approved
   source version: it moves the ACTIVE `SourceTermsApproval` to the terminal `REVOKED` status (stamping
   `revoked_by` / `revoked_at` / `revocation_reason` on the row — the durable who/when/why) and demotes
   the `SourceTerms` version it had promoted (ACTIVE → REVOKED). Because
   `SourceApprovalStore.is_in_force` already treats any non-ACTIVE approval as out of force (DCA-046),
   the right is pulled the **instant** the status flips — the DCA-048 preflight and the DCA-047
   entitlement ceiling both stop granting it at the very next read/use check, with **no sweep
   required**. This is the acceptance criterion: an already-running task that captured the old
   (now-revoked) source version is denied when it re-checks before using/committing what it fetched.

2. **Queued invalidation (durable housekeeping).** `RevocationInvalidationSweep` is the scheduled
   cleanup the worker runs each cycle: it finds the observations/recommendations (`ProspectCandidate`)
   acquired from a source whose rights are no longer in force (it has a REVOKED approval and **no**
   surviving in-force approval at `now`) and flips their still-`current` score to `stale` — queued for a
   future, disabled re-review/re-acquire capability (the same marker DCA-043 uses). It recomputes and
   removes nothing; it only *marks*, so it can never grant or widen anything, and it is idempotent (an
   already-`stale` observation is not re-marked).

### The guarantees (acceptance criteria)

- **Revoked rights cannot be used by an already-running task with an old source version.** A revocation
  flips the approval status; `is_in_force` keys on the status, so the moment an operator revokes, the
  preflight denies (`source_not_platform_approved`) and the version-specific in-force check
  (`approval_for(...) + is_in_force`) returns `False` — at the SAME clock, before any sweep runs. A task
  that authorized earlier and is paused mid-fetch therefore commits nothing on resume.
- **Reproduce the result and relevant denial without unintended state changes.** The preflight re-check
  is a pure, side-effect-free read. A refused revocation (unauthorized caller, unknown approval, missing
  reason, or an already-revoked/expired approval) raises **before** the session is mutated, so a
  rejection leaves the approval exactly as it was (still ACTIVE and usable where applicable). The
  invalidation sweep only ever flips a score flag MORE restrictive, so it never re-enables anything.
- **Authority is scoped — Admin role alone is insufficient.** Revoking requires the platform-plane
  scope `Scope.SOURCE_TERMS_REVOKE` (a *distinct* authority from approving). A tenant administrator
  holds only business-plane roles, so their roles yield no platform scope and the revocation is refused
  before any state is read — the shared catalog is control-plane data, not a tenant resource.
- **Future capabilities stay disabled.** This story enables no source and ships no live adapter.
  Revocation only ever makes a right more restrictive. In the running service every production preflight
  still denies at the source-status gate (no source is ever ENABLED).

## Components touched (DCA only)

- **Models** (`app/db/models.py`): add `REVOKED` to `SourceTermsStatus`; add the revocation record
  columns `revoked_at` / `revoked_by` / `revocation_reason` to `SourceTermsApproval`. (The
  `SourceApprovalStatus.REVOKED` value already existed as a reserved future state; DCA-050 is the story
  that writes it.)
- **Permissions** (`app/identity/permissions.py`): add the platform-plane scope `SOURCE_TERMS_REVOKE`
  and grant it to the `platform_operator` role (the disjoint business/platform scope invariant holds).
- **Service** (`app/services/source_revocation.py`): `RevokerPrincipal`, `SourceRevocationStore`
  (immediate revocation; session-only, stage-not-commit), `RevocationResult`,
  `RevocationInvalidationSweep` + `RevocationInvalidationResult` (the queued invalidation), and
  `run_revocation_invalidation(...)` (the worker-facing entry point that owns its own commit).
- **Worker** (`app/workers/scheduler.py`): `tick_maintenance` also runs the revocation-invalidation
  sweep each cycle (after the DCA-049 expiry sweep); the loop stays OFF by default.
- **Migration** (`migrations/versions/0020_source_approval_revocation.py`): the add-column branch for
  the three revocation record columns on the control-plane `source_term_approvals` table (no RLS). On a
  fresh DB the metadata baseline already has the columns, so it is a no-op there.
- **Manual check** (`scripts/manual_check.py`): the `DCA-050` scenario.

No new HTTP surface, Web/Admin UI or AI-service change is in scope.

## Tests

- `tests/test_source_revocation.py` — the behaviour + failure cases: a running task holding the old
  source version is denied at its re-check after revocation (same clock, no sweep); the revocation is a
  durable attributed record (approval + terms REVOKED with who/when/why); a tenant administrator is
  refused and changes nothing; an unknown approval / missing reason / double revocation are each refused
  and stage nothing; the invalidation sweep marks affected observations stale (idempotent), leaves a
  still-in-force source's observations untouched, and `run_revocation_invalidation` commits.
- `tests/test_worker.py` — the scheduled maintenance tick also invalidates observations from a revoked
  source (committing its own unit of work), alongside the DCA-049 expiry assertion.
- `tests/test_migrations.py` — the 0020 add-column branch on a pre-existing Postgres database
  (columns re-added, backfilled NULL, no RLS policy). Runs on disposable PostgreSQL only.

## Developer manual test

```text
python scripts/manual_check.py DCA-050
```

Runs on an ephemeral synthetic SQLite DB (AI disabled, auto-cleaned) with isolated Businesses A/B and a
controllable clock. It approves + (test-only) enables a source, entitles A and seeds one observation A
acquired from it, then:

- a running task authorizes against the in-force source version (ALLOW) and is *paused* mid-fetch
  (holding candidates it has not committed);
- a platform operator revokes that version — a tenant administrator is refused first (Admin role alone
  is insufficient);
- the task resumes and re-checks before committing — at the SAME clock — and is DENIED (preflight +
  version-specific in-force check), so it commits nothing;
- the scheduled invalidation sweep flips A's observation from the revoked source `current` → `stale`
  (idempotent on a second run); Business B (never entitled) is unaffected.

For RLS/transaction behaviour of the `source_term_approvals` table on disposable PostgreSQL, see
`tests/test_migrations.py` (the 0020 branch) and `tests/test_rls.py` (unchanged by this story).

## Traceability

Source sections: 5, 6; key: `source-revoke`. This story's stop rule overrides the reference prompt's
broad continue-working instructions.
