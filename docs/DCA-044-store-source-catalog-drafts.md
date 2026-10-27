# DCA-044 — Store source catalog drafts

Stage: Source rights and policy foundation · key: `source-catalog`

## What this adds

The **platform source catalog** — a registry of the prospect data sources (official registries and
licensed commercial datasets) the Data Concierge could, once approved, acquire from. This story stores
**drafts only**; a source's rights details and the approve/enable capability are later, disabled
stories.

A catalogued `Source` records its declared shape:

- **kind** — `official` | `commercial` (descriptive provenance class).
- **capabilities** / **fields** — what the source offers and which fields it can return (string lists).
- **access method** — `api` | `bulk_download` | `portal_lookup` | `manual`.
- **trust tier** — the tier the acquisition rulepack screens on (e.g. `official_registry`).
- **India coverage** — the service is India-only, so coverage is `{"nationwide": bool, "states": [IN
  state/UT codes]}`; a coverage that names no recognized Indian state/UT (and is not nationwide), or an
  unrecognized state, is refused.
- **status / enabled / permissions** — the lifecycle: every source is created a **disabled `draft`**
  (`enabled=False`) with an **empty permission set**.

### The guarantees (acceptance criteria)

- **Disabled by default, no implicit permissions.** `SourceCatalogStore.create_draft` always writes
  `status='draft'`, `enabled=False`, `permissions=[]`, regardless of input — there is no argument to
  create an enabled source or attach permissions. Cataloguing an official **or** commercial source
  grants nothing on its own.
- **A draft cannot be used.** `is_usable()` is `False` for a draft; `usable()` (approved **and**
  enabled) is an empty list while every source is a draft; `usability_reason()` returns the stable
  code `source_not_enabled`. Nothing in this service promotes a draft to `enabled`.
- **Invalid drafts are refused before anything is staged** (stable codes): `missing_source_key`,
  `duplicate_source_key`, `unknown_source_kind`, `unknown_access_method`, `invalid_india_coverage`.

## Components touched (DCA only)

- **Model**: `app/db/models.py` → `Source` + `SourceKind` / `SourceAccessMethod` / `SourceStatus`.
  Platform **control-plane** data (a shared catalog), like `tenant_agents` / `entitlement_projections`
  — deliberately **outside tenant RLS** and never tenant-scoped.
- **Store**: `app/services/source_catalog.py` → `SourceCatalogStore`, `SourceDraftInput`, validation +
  stable reason codes, the `is_usable` / `usable` / `usability_reason` draft-denial guard.
- **Migration**: `migrations/versions/0016_sources.py` (dual-path, mirrors 0003's platform catalog;
  no RLS policy).
- **Manual check**: `scripts/manual_check.py` → `DCA-044` scenario.

No HTTP surface, Web/Admin UI, or AI-service change is in scope: this is the schema + store. A
platform operator surface to manage/approve sources is a later story (`source.approve` already exists
as the business-plane seam in `app/identity/permissions.py`), kept disabled here — "missing contracts
stay disabled".

## Tests

- `tests/test_source_catalog.py` — the behaviour + failure cases (both kinds disabled drafts, no
  permissions, draft-not-usable denial, coverage normalization, invalid/duplicate refusals stage
  nothing).
- `tests/test_migrations.py` — `sources` added to the DCA table set; `test_0016_creates_sources_on_a_preexisting_database`
  (disposable Postgres) proves the dual-path create + unique key constraint and **no** tenant RLS policy.
- `tests/test_rls.py` — asserts `sources` is excluded from the tenant-owned RLS set (control-plane).
- `tests/test_manual_check.py` — runs the `DCA-044` scenario end-to-end on synthetic SQLite.

## Developer manual test

```
python scripts/manual_check.py DCA-044
```

Expected: an official dataset entry (`mca_master_data`) and a commercial source (`acme_leads`) are
catalogued; each row prints `status=draft enabled=False permissions=[]` and `usable=False
usability_reason=source_not_enabled`; the usable set is empty; and an unknown kind, a non-India
coverage, and a duplicate key are each refused leaving the catalog at 2 rows. Local synthetic only;
no AI/paid action; nothing is approved or enabled.
