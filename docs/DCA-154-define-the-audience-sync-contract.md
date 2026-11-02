# DCA-154 — Define the audience-sync contract

Stage: User selection and EffortlessPromo sync · Key: `crm-port` · Source sections 7, 10, 12, 13.

## What this story adds

A typed, **contract-only** definition of how the DCA asks the EffortlessPromo platform to sync one
**accepted** prospect (a DCA-153 `AcceptanceIntent`) into the platform's CRM/audience — plus a
deterministic synthetic fake. **No live adapter, no schema change, no migration, no CRM write.** The DCA
owns no CRM and no audience; the platform is the system of record. The only shape the DCA may define is a
*request-sync* port — it hands the platform an accepted intent under a stable key and the platform's own
CRM re-gates and performs the write.

This mirrors the DCA-096 delivery and DCA-037 platform-profile contracts: typed shapes + a client
`Protocol` + a fail-closed decision, a synthetic fake alongside, and the **live HTTP adapter deferred to
a later story (blocked on DCA-001 B1)**. The contract stays **disabled in production** — `build_*` raises
there; the fake never stands in for the live adapter.

## Files

### DCA (`apps/effortlesspromo-dca`)
- `app/audience/__init__.py` — new package; re-exports the contract + fake.
- `app/audience/contract.py` — typed shapes + the client `Protocol` + the fail-closed decision + a
  builder from an `AcceptanceIntent`:
  - `AudienceFieldClass` (`ORGANIZATION` | `CONTACT`), `AudienceSyncPurpose` (`RESEARCH` | `MARKETING`),
    `AudienceSyncStatus` (`PENDING` | `ACKNOWLEDGED` | `DISABLED`).
  - `AudienceField` (a minimized, class-tagged field; `value_digest` only — never a raw contact value).
  - `AudienceSyncRequest` (**stable `request_key`**, `intent_id`, captured `visibility`, `purpose`,
    `fields`; validates visibility; `has_restricted_fields`/`confers_consent` properties).
  - `ExternalRecordRef` (**external IDs** the platform owns: `crm_record_id`, `audience_id`,
    `external_version`).
  - `AudienceSyncAck` (the **acknowledgment result**; a model-validator enforces that external IDs are
    present **iff** `ACKNOWLEDGED`; `is_pending`/`is_acknowledged`/`confers_consent`).
  - `AudienceSyncPort` (`Protocol`, one method `sync` — no create/upsert/promote path).
  - `authorize_audience_sync(request)` — fail-closed gate.
  - `audience_sync_request_from_intent(intent, …)` — reuses the DCA-153 `AcceptanceIntent` as the thing
    to be synced (captures business/subject/visibility unchanged; reuses its outbox event id as the key).
- `app/audience/fake.py` — `FakeEffortlessPromoAudienceSyncPort` (`acknowledges` knob → PENDING vs
  ACKNOWLEDGED; refusals come from the contract gate), `build_audience_sync_port` (**refuses production**),
  `AudienceSyncContractDisabled`, `SYNTHETIC_VERSION`.
- `tests/test_audience_sync_contract.py` — 20 focused tests (shapes, port surface, gate, pending vs
  acknowledged, the restricted-field **denial**, idempotency, the ack invariant, from-intent builder,
  production refusal, immutability).
- `scripts/manual_check.py` — registered `@register("DCA-154")` scenario.

### Documentation
- `docs/DCA-154-define-the-audience-sync-contract.md` (this file).

## Acceptance criteria → how they are met

- **Cannot silently promote restricted contact fields.** A `CONTACT`-class `AudienceField` under a
  `research_only` acceptance makes `authorize_audience_sync` return `DISABLED` with
  `restricted_contact_field_not_permitted` — the whole request is refused, never silently stripped or
  promoted. A `sendable` sync and a `marketing` purpose are likewise disabled (both need consent that no
  path produces — acceptance is **not** consent).
- **Pending/acknowledged outcomes differ.** A PENDING hand-off is `accepted=True` with **no** external
  IDs; an ACKNOWLEDGED write carries `ExternalRecordRef`. The ack model-validator makes external IDs
  present *exactly* when `ACKNOWLEDGED`, so "queued" can never masquerade as "written".
- **Reproduce the result and the relevant denial with no state change.** The gate and fake are pure
  functions — no DB, no network, no write. The manual check reproduces the allow (PENDING + ACKNOWLEDGED),
  the restricted-field denial, the marketing-purpose denial and the sendable denial deterministically.

## Tenancy / boundary

- Every request is scoped to one `business_id`; the request is built from a tenant-owned acceptance
  intent and captures its visibility **unchanged** (the sync can never widen what was accepted).
- Only minimized, class-tagged fields travel; contact fields are carried as a one-way `value_digest`
  only and are refused outright under a research-only acceptance.
- A sync is never recipient consent and never a send (`confers_consent` is always `False`).
- The live capability stays **disabled** (B1); production refuses the fake.

## Migration / config

- **None.** Contract + fake + types only. No new table, no RLS change, no migration.

## Verification

- Focused: `.venv/Scripts/python.exe -m pytest tests/test_audience_sync_contract.py -q` → 20 passed.
- Manual: `.venv/Scripts/python.exe scripts/manual_check.py DCA-154` →
  `checks: {'pending_handoff_has_no_external_ids': True, 'acknowledged_write_carries_external_ids': True,
  'pending_and_acknowledged_differ': True, 'sync_is_idempotent_in_key': True,
  'restricted_contact_field_not_promoted': True, 'marketing_purpose_disabled': True,
  'sendable_sync_disabled': True, 'no_create_or_promote_path': True, 'sync_confers_no_consent': True}`
- Quality: `ruff check` + `ruff format --check` + `mypy` clean on changed files; full `pytest` green.

**Status: Ready for manual review.** Work left uncommitted. Did not start DCA-155.
