# DCA-153 — Persist one acceptance intent

Stage: User selection and EffortlessPromo sync.

## What this adds

One capability: record a business user's **intent** to accept a DCA-145 `ProspectSuggestion` into their
audience. Recording the intent is **inert** — it persists the decision plus a durable sync *request*,
but performs **no** EffortlessPromo CRM sync / send, confers **no** recipient consent
(**acceptance is NOT consent**), and creates **no** audience membership. The actual sync and any billing
(DCA-157/158) stay disabled future capabilities.

A single accept stages, in **one unit of work** (atomic — all three commit or none do):

1. a `pending_sync` `AcceptanceIntent` row (tenant-owned), capturing the accepted suggestion's
   `visibility` and its **selection versions** (`scored_icp_version`, the suggestion's
   `schema_version`, the `match_score`) frozen at acceptance time;
2. its atomic DCA-026 `AuditEvent` (opaque references only — never a contact value); and
3. a durable DCA-027 `OutboxMessage` **sync request** (`event_type = audience.acceptance.pending_sync`,
   status `pending`, never delivered here).

## Invariants (the DCA-153 acceptance criteria)

- **Repeated submit creates ONE intent; a transaction failure commits neither the intent nor its
  audit/outbox.** `(business_id, suggestion_id)` is unique, so re-accepting returns the existing row
  and stages no duplicate / second audit / second outbox request. The recorder stages into the caller's
  session and never commits, so a rolled-back unit of work leaves none of the three rows.
- **A research-only suggestion never becomes sendable; no audience membership/send is created.** The
  suggestion's `visibility` is captured unchanged; the `provenance` pins
  `sendable` / `synced` / `sent` / `spent` / `consentConferred` / `membershipCreated` = `False`.
- **Reproduce the result and the relevant denials without unintended state changes.** Authority is
  checked first; then the suggestion must exist in this business (unknown / cross-tenant → not-found);
  then it must be an acceptable reviewable candidate (a `suggested`, unexpired, non-dismissed row).
  Every refusal is raised before the session is mutated.

## Tenancy & authorization

- Tenant-scoped: every query is confined to the path `business_id`; an unknown / cross-tenant
  suggestion id reads as not-found (404), never an oracle. `acceptance_intents` is `TenantOwned` →
  RLS-protected.
- New DCA-019 business-plane scope `candidate.accept` — held by acting users and administrators (the
  positive counterpart of `candidate.dismiss`), **not** a read-only viewer and **not** a platform
  operator. Denials are audited.

## Files changed

### DCA (apps/effortlesspromo-dca)

- `app/db/models.py` — new `AcceptanceIntentStatus` enum (`pending_sync`, future `synced`) and new
  tenant-owned `AcceptanceIntent` model (unique `(business_id, suggestion_id)`).
- `app/identity/permissions.py` — new `Scope.CANDIDATE_ACCEPT` (`candidate.accept`); added to the
  acting-user and administrator scope sets.
- `app/services/acceptance_intent.py` — **new.** `AcceptanceIntentRecorder` (the one accept path),
  `AcceptanceIntentActor`, `AcceptanceIntentResult`, errors + stable reason codes.
- `app/api/routes/acceptance_intent.py` — **new.** `POST /v1/tenants/{business_id}/prospect-suggestions/{suggestion_id}/accept`
  (idempotency-aware, `commit_idempotent`).
- `app/main.py` — register the new router.
- `migrations/versions/0068_acceptance_intents.py` — **new.** Create-branch + RLS for
  `acceptance_intents` (revision `0068_acceptance_intents`, down_revision `0067_suggestion_dismissal`).
- `tests/test_acceptance_intent.py` — **new.** Service + HTTP + PG-gated tests (behaviour + all denials
  + transaction-failure atomicity + idempotency + tenant privacy).
- `tests/test_rls.py` — added `acceptance_intents` to the covered-tables set + isolation / write-check
  tests.
- `tests/test_migrations.py` — added `acceptance_intents` to `DCA_TABLES` + the `0068` create-branch
  test.
- `scripts/manual_check.py` — registered `DCA-153`.

### Documentation

- `docs/DCA-153-persist-one-acceptance-intent.md` — this file.

## Migration / config notes

- New Alembic revision `0068_acceptance_intents` (22 chars, ≤ 32). On a fresh DB the metadata baseline
  (0001) creates the table and 0002 applies RLS automatically (it is `TenantOwned`); 0068 only handles a
  DB that reached 0067 before the table existed. No config changes. No new env vars.

## Verification

- Focused: `.venv/Scripts/python.exe -m pytest tests/test_acceptance_intent.py -q` — passes (PG-gated
  test skips without `DCA_TEST_DATABASE_URL`).
- Manual: `.venv/Scripts/python.exe scripts/manual_check.py DCA-153` — `checks` all `True`.
- Postgres-backed RLS / migration tests (`tests/test_rls.py`, `tests/test_migrations.py`) run when
  `DCA_TEST_DATABASE_URL` points at a disposable Postgres.

## Boundary

Python DCA only. No UI, no AI-service, no .NET, no other repo touched. The actual EffortlessPromo sync,
recipient consent, audience membership and billable acceptance all remain disabled future capabilities.
