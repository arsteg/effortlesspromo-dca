# DCA-155 — Synchronize one accepted record

Stage: User selection and EffortlessPromo sync · key: `audience-sync` · source sections 7, 10, 12, 13.

## What this delivers

One capability: drive **one** DCA-153 `AcceptanceIntent` (a `pending_sync` record) through the DCA-154
`AudienceSyncPort` contract, and apply the platform's acknowledgment back onto the intent. The DCA owns
no CRM and no audience — it can only ever **request** a sync through EffortlessPromo; the platform's CRM
performs the write. The live HTTP adapter into the .NET API is still unbuilt (DCA-001 **B1**), so the
sync runs only against the **synthetic fake** today and **production refuses the contract outright**.

Syncing performs **no send**, confers **no recipient consent** (acceptance is NOT consent) and creates
**no audience membership**. A research-only organization stays research-only.

## The one task

> Deliver an acceptance intent through the CRM port with revalidation immediately before sync and
> persistent external mapping on acknowledgment.

Flow (`AcceptanceRecordSynchronizer.sync`, fail-closed, in order — a refusal changes nothing):

1. **Authority first.** The caller must hold `candidate.accept` (the positive counterpart of recording
   the acceptance). A read-only viewer / platform operator is refused (`AcceptanceSyncAuthorizationError`).
2. **The intent exists in this business.** An unknown / cross-tenant id reads as not-found
   (`acceptance_intent_not_found`).
3. **Idempotent replay.** An intent already `synced` (its external mapping persisted) short-circuits —
   no second port call, no duplicate sync, no second audit — returning the same external refs.
4. **Revalidate immediately before sync.** The request (built from the intent under its stable outbox
   request key) is re-gated by the contract's own `authorize_audience_sync`. A sendable sync, a
   restricted CONTACT field under a research-only acceptance, or a disallowed purpose is **refused with
   no port call and no state change** — the restricted field is never silently promoted.
5. **Request + apply.** The port is resolved via `build_audience_sync_port(settings)` (which **raises in
   production**) and asked to sync. The `AudienceSyncAck` is applied back onto the intent.

### Applying the ack

- **ACKNOWLEDGED** (external ids present): the intent advances `pending_sync -> synced`; the platform's
  external record mapping (`external_crm_record_id` / `external_audience_id` / `external_version`) and
  `synced_at` are persisted; `sync_status = acknowledged`; the `synced` provenance invariant flips to
  `True` (sent / spent / consentConferred / membershipCreated stay `False`).
- **PENDING** (queued, no external ids — exactly how a lost / delayed acknowledgment is modelled): the
  intent **stays `pending_sync`**; only `sync_status = pending` and `last_sync_attempt_at` are recorded.
  **No accepted status and no external ids before the required acknowledgment** (the DCA-155 acceptance
  criterion). The next pass retries under the same stable request key.

Every outcome stages an atomic DCA-026 audit entry in the **caller's** unit of work (the service never
commits), carrying only opaque references — never a contact value.

## Changed files

### DCA (`apps/effortlesspromo-dca`)
- `app/services/acceptance_sync.py` — **new.** `AcceptanceRecordSynchronizer`, `AcceptanceSyncActor`,
  `AcceptanceSyncResult`, error types, `AUDIT_ACTION_SYNC = "prospect.suggestion.synced"`.
- `app/db/models.py` — six sync-reconciliation columns on `AcceptanceIntent`: `sync_status`,
  `external_crm_record_id`, `external_audience_id`, `external_version`, `synced_at`,
  `last_sync_attempt_at`.
- `migrations/versions/0069_acceptance_intent_sync.py` — **new.** Add-column migration (dual-path safe,
  same guard style as 0067); no RLS change (`acceptance_intents` is tenant-owned already).
- `scripts/manual_check.py` — registered `@register("DCA-155")`.
- `tests/test_acceptance_sync.py` — **new.** Service unit tests (SQLite) + a PG-gated tenant/atomicity
  test.
- `tests/test_migrations.py` — `test_0069_adds_acceptance_sync_columns_on_a_preexisting_database`
  (PG-gated add-column branch test).

### Documentation
- `docs/DCA-155-synchronize-one-accepted-record.md` — this file.

No other service, Web, Admin or AI-service code was touched. No live capability was enabled; the fake is
dev/test-only and production refuses the contract.

## Migration / config notes

- Alembic `0069_acceptance_intent_sync` (down_revision `0068_acceptance_intents`); revision id is 27
  chars (≤32). Pure add-column on an existing tenant-owned table — no new table, no new RLS policy.
  String columns carry `server_default=''` so pre-existing rows backfill to "not yet synced"; timestamps
  default to NULL. `downgrade()` is a no-op (expand→contract, BR-201).
- No new config keys. The port is resolved through the existing DCA-154 `build_audience_sync_port`
  factory, which is disabled in production.

## Verification

- Focused: `python -m pytest tests/test_acceptance_sync.py -q` — **10 passed, 1 skipped** (the PG test
  skips without `DCA_TEST_DATABASE_URL`).
- Manual: `python scripts/manual_check.py DCA-155` — all `checks` `True`:
  `pending_handoff_stays_pending_no_external_ids`, `acknowledged_advances_to_synced_with_external_mapping`,
  `no_send_or_consent_side_effect`, `acknowledgment_wrote_exactly_one_more_audit`,
  `replay_idempotent_no_second_sync`, `restricted_contact_field_not_promoted`, `sendable_sync_disabled`,
  `production_refuses_the_contract`, `cross_tenant_not_found`.
- Quality gate: `ruff check` + `ruff format --check` clean on changed files.
- The Postgres-backed migration-branch and tenant/atomicity tests run when `DCA_TEST_DATABASE_URL` is
  set to a disposable Postgres (they skip otherwise, per the repo convention).

## Boundary

Stayed inside this story: Python DCA only. No live adapter, no provider SDKs/keys, no model routing, no
writes to another service's database, no Web/Admin/AI-service changes, no DCA-156 work. The contract
stays a disabled future capability in production.
