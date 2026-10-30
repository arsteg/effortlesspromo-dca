# DCA-105 — Inspect and requeue a dead-letter job

Stage: Durable Python execution · key `job-deadletter` · source sections 2, 3, 8.

## What this adds

The operator-facing recovery for the terminal state DCA-104 produces. When DCA-104 **dead-letters** a job
(a permanent policy/auth failure, or a transient failure that exhausted its attempt cap), the row is
parked `DEAD_LETTER` carrying a PII-safe `last_error`. `app/jobs/deadletter.py` lets an authorized platform
operator **inspect** that parked job's safe reason and, once the underlying cause is fixed out of band,
**requeue** it for another attempt.

Nothing is executed. Reviving a job only makes it *claimable* again; the handler body (`JobHandler.run`,
DCA-100) still raises `NotImplementedError`, there is no worker drain loop, and no path here re-dispatches
the revived `QUEUED` row. A requeue grants no send authority, the DCA has no send path, and acceptance of a
prospect is not recipient consent.

**No schema change / migration.** Requeue reuses the existing `durable_job_requests` columns: the
`DEAD_LETTER` / `QUEUED` statuses on `JobRequestStatus`, `attempt_count` (reset), `available_at` (set to
`now`), the lease columns (cleared) and `last_error` (kept as the inspectable record). The who/when/why is
recorded in the existing `audit_events` table (DCA-026). `tests/test_migrations.py` /`tests/test_rls.py`
are therefore unchanged.

## Design

`JobDeadLetterStore(session, business_id)` is tenant-scoped (bound to the owning tenant an operator names)
and stages into the caller's session without committing — the flip and its atomic DCA-026 audit entry
commit together in the caller's unit of work (the DCA-084 erasure discipline).

### `inspect(job_id, principal) -> DeadLetterDetail | None`

- **Authority first:** requires the platform-plane scope `Scope.PLATFORM_OPS_READ`; a tenant administrator
  (no platform scope) is refused with `JobRequeueAuthorizationError` before any job is read.
- Returns a **PII-safe** `DeadLetterDetail` — only opaque / non-identifying fields (row id, tenant scope,
  the `(job_type, idempotency_key)` dedup identity, the opaque `payload_ref` + one-way `payload_digest`,
  the attempt bookkeeping, and the PII-safe `last_error` "safe reason"). Never the payload or contact data.
- A cross-tenant / unknown id reads as `None` (never cross-tenant).

### `requeue(job_id, principal, *, reason, now) -> RequeueResult`

Ordered, fail-closed, each refusal changing nothing:

1. **Authority first** — requires the platform-plane scope `Scope.JOB_DEADLETTER_REQUEUE`, a **distinct**
   authority from the read scope (so reading platform ops never implies the right to *mutate* a job's
   lifecycle). A tenant administrator / any non-operator is refused with `JobRequeueAuthorizationError`
   (code `deadletter_requeue_not_authorized`) before any state is read or written.
2. **Reason required** — a non-empty, opaque `reason` (`JobRequeueError`, code `missing_requeue_reason`),
   screened like an audit reference (`assert_opaque_text`) so it carries no contact data — raised before
   the row is mutated.
3. **Only a parked job** — the row is locked `FOR UPDATE` within the tenant; a cross-tenant / unknown id
   returns `NOT_FOUND`, any non-`DEAD_LETTER` status returns `NOT_DEAD_LETTER` (idempotent: a job already
   revived to `QUEUED` is a no-op). The row is left untouched in both cases.
4. **Revive in place** — the **same row** flips `DEAD_LETTER` → `QUEUED`, **preserving its logical
   side-effect identity**: the row id, the `(business_id, job_type, idempotency_key)` dedup key and the
   `payload_ref` / `payload_digest` are untouched, so the revived job is the *same logical unit of work*
   (any downstream idempotency keyed on that identity still collapses a re-run to one job — not a
   duplicate). `attempt_count` is reset to `0` (a fresh DCA-104 budget), `available_at` is set to `now`
   (claimable again) and any residual lease is cleared. `last_error` is kept as the inspectable record.
5. **Atomic audit** — a DCA-026 entry (action `deadletter_job.requeued`, actor = the operator attributed as
   `PLATFORM_SUPPORT`, opaque references: job id, dedup identity, prior status/attempt counts, authority
   plane/scope) is staged in the SAME unit of work.

### Authority model

Requeue is a **platform-operations** decision over the DCA's own background-work machinery, so it lives on
the platform plane. A new scope `Scope.JOB_DEADLETTER_REQUEUE` (`platform.job.deadletter.requeue`) is added
to the `platform_operator` role in `app/identity/permissions.py`. The two scope planes stay disjoint, so a
business administrator can never hold it — a requeue in any tenant is refused for a tenant admin.

## Acceptance → evidence

| Criterion | Evidence |
|---|---|
| Only authorized operators requeue | `test_business_admin_cannot_requeue`, `test_roleless_caller_cannot_requeue`, `test_authority_is_checked_before_the_missing_reason`; manual `business_admin` denial |
| A corrected job retains its logical side-effect identity | `test_authorized_operator_requeues_preserving_logical_identity` (same id + dedup key + payload digest, one row, not a duplicate), `test_requeue_preserves_identity_postgres`; manual check step 2 |
| Reproduce the relevant denial without unintended state changes | `test_requeue_a_non_dead_letter_job_is_refused`, `test_second_requeue_is_idempotent_no_op`, `test_cross_tenant_requeue_is_not_found`, `test_missing_reason_is_refused_before_any_change`, `test_reason_with_raw_pii_is_refused_before_any_change` |
| No later-story work (execution stays disabled) | revive only flips status; `test_requeue_is_claimable_again_after_revival` leases but never runs the job |

## Tests

`tests/test_job_deadletter.py` — always-on SQLite logic tests (inspect, the happy-path revive with
identity preserved + atomic audit, and every denial) plus disposable-PostgreSQL re-runs
(`DCA_TEST_DATABASE_URL`) of the real `FOR UPDATE` flip and the no-double-revive case. The Postgres tests
skip without a URL.

## Manual check

```
python scripts/manual_check.py DCA-105
```

Dead-letters four isolated synthetic fixtures in Business A, inspects the primary one's safe reason, has an
authorized operator requeue it (printing the preserved identity, reset budget and the staged audit entry),
proves it is claimable again (leased, never executed), then reproduces every denial (tenant admin refused,
non-dead-letter / second-requeue no-op, cross-tenant not-found, missing reason) showing each row unchanged.
Local synthetic only; no AI/paid action, no network, no send.
```
