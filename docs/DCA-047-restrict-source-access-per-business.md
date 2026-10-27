# DCA-047 — Restrict source access per business

Stage: Source rights and policy foundation · key: `source-entitlement`

## What this adds

Per-business **entitlements** that NARROW the platform-approved source rights (DCA-046) down to the
field/operation/purpose rights a single business grants ITSELF — never widening them. This is the
capability that lets one business's source access be reviewed and accepted independently.

A `BusinessSourceEntitlement` row (one *version* of one business's decision for one source) records:

- **source** — the catalog `source_key` (DCA-044) the entitlement narrows.
- **enable intent** — `enabled`: whether the business has opted the source in for itself. Enabling
  grants NO acquisition right on its own (no discovery path consults this table); it only records the
  narrowed rights the business opted into.
- **narrowed operation rights** — `operation_rights` ({operation: bool}): the business's source-wide
  grant per operation, always a subset of the approval's `approved_operation_rights`.
- **narrowed field rights** — `field_rights` ({field: {operation: bool}}): per-field overrides, each
  a subset of the approval's effective (field-override-or-default) grant for that field.
- **narrowed purposes** — `purposes`: a subset of the approved terms' documented purposes.
- **ceiling reference** — `approved_terms_version`: the platform terms version (DCA-045/046) the
  entitlement was checked against, so a reader knows exactly which approved snapshot it is a subset of.

The entitlement is **versioned per `(business, source)`**: each save inserts a new version and
preserves prior ones (`(business_id, source_key, version)` unique, insert-only) — an immutable,
auditable history; the highest version is the one in force.

### The guarantees (acceptance criteria)

- **A business cannot enable a source or operation denied at platform level.** The platform ceiling
  is the in-force `SourceTermsApproval` (DCA-046; active AND inside its validity window). Before
  anything is staged, `BusinessSourceEntitlementStore.set_entitlement` refuses:
  - a source with **no in-force platform approval** (`source_not_platform_approved`) — including one
    whose approval has lapsed/been revoked — so a business can never enable a source the platform has
    not approved;
  - any **source-wide operation** the approval does not grant (`operation_denied_at_platform`);
  - any **per-field right** the approval does not grant for that field
    (`field_right_denied_at_platform`);
  - any **purpose** the approved terms version does not document (`purpose_denied_at_platform`).

  Each refusal is raised **before** the session is mutated, so a rejected broadening leaves no row and
  no partial state, and the stored row carries ONLY the validated (narrowed) rights.
- **Authority is a business administrator action.** The business-plane scope `Scope.SOURCE_APPROVE`
  ("a business-local intent to enable a data source") is required. An ordinary business user
  (`viewer`/`user`/`member`) holds only `candidate.read`, and a platform operator holds no business
  scope for the tenant, so neither can configure an entitlement — refused (`not_business_administrator`)
  before any state is read or written.
- **Tenant isolation.** The table is tenant-owned (`business_id`, RLS-protected): one business can
  never read or change another's entitlements, and one business's configuration never touches another.
- **Future capabilities stay disabled.** An entitlement authorizes no acquisition on its own — no
  discovery path consults `business_source_entitlements`, and the source catalog entry staying ENABLED
  (DCA-044) remains a disabled future capability. The narrowed `permits(...)` read is the INTERSECTION
  of the platform ceiling and the business grant, for a later (disabled) acquisition consumer.

## Components touched (DCA only)

- **Model**: `app/db/models.py` → `BusinessSourceEntitlement` (tenant-owned, `TenantOwned`), versioned
  per `(business, source)` via `tenant_unique("source_key", "version")`.
- **Store**: `app/services/source_entitlement.py` → `BusinessSourceEntitlementStore`,
  `BusinessSourceEntitlementInput`, `BusinessEntitler`, stable reason codes, the narrowing write path
  (authorize → resolve the in-force approval ceiling → narrow operations/fields/purposes → stage +
  atomic DCA-026 audit), and the `permits` read evaluator.
- **Migration**: `migrations/versions/0019_business_entitlements.py` (dual-path, tenant-owned, mirrors
  0014 — table + RLS policy + grants).
- **Manual check**: `scripts/manual_check.py` → `DCA-047` scenario.

No HTTP surface, Web/Admin UI, or AI-service change is in scope: this is the schema + store + the
narrowing/authority rules. A business-facing API/UI to drive entitlements is a later story, kept
disabled here ("missing contracts stay disabled").

## Stable codes

Authorization refusal (raised first, before any state is touched): `not_business_administrator`.
Narrowing refusals (raised by `set_entitlement` before any row is staged): `missing_source_key`,
`unknown_operation`, `source_not_platform_approved`, `operation_denied_at_platform`,
`field_right_denied_at_platform`, `purpose_denied_at_platform`.
Narrowed-read denials (from `permits`, not a staging error): `source_not_platform_approved`,
`operation_denied_at_platform`, `source_not_entitled`, `operation_not_entitled`.

## Tests

- `tests/test_source_entitlement.py` — the behaviour + failure cases (an administrator narrows an
  approved source to a subset and is audited; versions are insert-only; a business cannot enable an
  unapproved source or one whose approval has lapsed; cannot grant an operation / field right / purpose
  the platform denies; an ordinary user cannot configure; entitlements are per-business).
- `tests/test_migrations.py` — `business_source_entitlements` added to the DCA table set;
  `test_0019_creates_business_source_entitlements_on_a_preexisting_database` (disposable Postgres)
  proves the dual-path create + unique `(business_id, source_key, version)` constraint AND its tenant
  RLS policy.
- `tests/test_rls.py` — `business_source_entitlements` pinned into the tenant-owned RLS set.
- `tests/test_manual_check.py` — runs the `DCA-047` scenario end-to-end on synthetic SQLite.

## Developer manual test

Isolated synthetic Businesses A/B. A source is catalogued (DCA-044), a terms version recorded
(DCA-045) and approved (DCA-046) so it is the in-force platform ceiling (collect/store/display/export
source-wide — `share` DENIED; email display-only; purpose `prospecting`).

```
python scripts/manual_check.py DCA-047
```

Expected: a Business-A **administrator** narrows the source to a SUBSET (display only) → **allowed**
(version 1 row records exactly the narrowed rights, audited). A then attempts to **broaden** through
its own configuration — enable `share` (`operation_denied_at_platform`), grant email `export`
(`field_right_denied_at_platform`), a purpose `resale` (`purpose_denied_at_platform`), and enable a
source with **no** platform approval (`source_not_platform_approved`) — each **refused**, staging
nothing (total entitlement rows stays **1**). An **ordinary user** of A cannot configure an
entitlement at all (`not_business_administrator`). Business **B** is never touched (no entitlement),
and the narrowed `permits` read is the intersection (email/display allowed for A; `name`/export —
approved at platform but not entitled by A — denied `operation_not_entitled`). Local synthetic only;
no AI/paid action; entitling enables no acquisition.

To exercise the DDL/RLS paths on disposable PostgreSQL (not SQLite):

```
docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55477:5432 postgres:16-alpine
export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55477/dca"
pytest tests/test_migrations.py tests/test_rls.py
```
