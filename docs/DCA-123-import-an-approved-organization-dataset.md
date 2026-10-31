# DCA-123 — Import an approved organization dataset

Stage: Approved source access · Key: `dataset-import` · Source sections 6, 8, 9

## What this adds

The **first concrete source adapter**: a manifest-driven importer that loads an APPROVED + entitled
ORGANIZATION dataset into the DCA-059 org-observation store, with durable source provenance. It
composes — and never bypasses — the already-built chain (DCA-048 rights, DCA-122 adapter contract,
DCA-059 observation store). There is **no network fetch**: the dataset is a caller-supplied / synthetic
`DatasetManifest` + `DatasetRow` list (an already-approved dataset); fetching stays a disabled future
capability.

- `app/sources/org_dataset_import.py`:
  - `ORG_DATASET_IMPORT_CAPABILITY` — an ORGANIZATION-ONLY DCA-122 `SourceAdapterCapability`
    (`handles_personal_contacts=False`, no `ENRICH_CONTACT`), validated at import time. It is the
    structural guarantee the importer can never write a contact field.
  - `DatasetManifest` — `dataset_id`, `source_key`, `source_version`, `license`, `purpose`
    (+ `schema_version`). All five are required; an incomplete manifest is rejected.
  - `DatasetRow` — `row_ref` (the checkpointable reference / idempotency key), `entity_type`
    (`legal_entity` | `establishment`), `display_name`, `parent_row_ref` (dataset-local), `state_code`,
    `external_ids` (CIN / PAN / GSTIN / …), and a `contact` guard (any personal field present ⇒ the row
    is refused).
  - `OrgDatasetImporter(session, business_id, *, max_rows=10_000)` — `import_dataset(manifest, rows,
    actor, now=...)` returns an `ImportResult` (`authorized`, `reason`, `imported`, `skipped`,
    `refused_rows`); `imports_for(dataset_id, source_key)` reads the provenance back.
- `app/db/models.py`: `OrgObservationImport` (table `org_observation_imports`) — tenant-owned, RLS, one
  provenance row per imported dataset row: `observation_id`, `source_key`, `dataset_id`,
  `source_version`, `dataset_license`, `source_row_ref`, `schema_version`. Unique
  `(business_id, source_key, dataset_id, source_row_ref)` — the idempotency key.
- `migrations/versions/0049_org_observation_imports.py` — dual-path create + RLS (same guard style as
  0048), next after 0048.

## How it works (fixed order; each stage imports nothing on failure)

1. **Manifest completeness + streaming bound** — a missing dataset id / source / version / license /
   purpose, or a dataset larger than `max_rows`, raises `OrgDatasetImportError` before any state is
   touched.
2. **Row schema** — every row needs a non-empty, unique `row_ref` (the checkpointable reference);
   otherwise `OrgDatasetImportError`.
3. **Rights FIRST (DCA-048)** — `SourcePreflight.authorize(collect, purpose)`. A denial returns an
   un-authorized `ImportResult` carrying the stable preflight reason, having written nothing.
4. **Import** — legal entities first (so an establishment's in-dataset parent exists), then
   establishments. Per row: an already-imported `(source, dataset, row_ref)` is **skipped** (its
   observation reused); a row carrying personal contact fields is **refused**; otherwise the identity is
   recorded through the DCA-059 `OrgObservationStore` (with its atomic audit) and an
   `OrgObservationImport` provenance row is staged. The importer never commits — the caller owns the
   unit of work.

## The guarantees (acceptance criteria)

- **An unapproved manifest is rejected and imports nothing.** Incomplete manifest ⇒ raises before any
  state; source not approved+entitled ⇒ un-authorized `ImportResult`, nothing written (no observation,
  external id, provenance or audit row).
- **Allowed rows retain source attribution + checkpointable row references.** Each imported observation
  gets an `OrgObservationImport` recording the source key / dataset id / version / license and the
  dataset `source_row_ref`.
- **A re-import does not duplicate.** The `(business_id, source_key, dataset_id, source_row_ref)` key
  skips already-imported rows; re-running the same import is a deterministic no-op.
- **Organization-only.** No personal contact field is ever imported; a row carrying one is refused
  (`personal_fields_not_importable`), never partially imported — the importer only ever writes through
  the org store, so it structurally cannot touch `person_observations` / `contact_points`.
- **Tenant isolation.** Two businesses importing the same dataset get independent, business-scoped
  observations and provenance; the same dataset row ref may recur across tenants, each mapping to its
  own observation. RLS (DCA-018) protects the new table beneath the app-level scoping.

## Tests / manual check

- `tests/test_org_dataset_import.py` — approved import records three org observations + provenance;
  an unentitled source imports nothing (reason `source_not_entitled`); an incomplete manifest is refused
  before any state; re-import is idempotent (0 imported / 3 skipped); a personal-field row is refused and
  no person/contact row is written; two businesses import the same dataset independently. Store logic on
  in-memory SQLite + a Postgres-guarded durable-path test (`DCA_TEST_DATABASE_URL`).
- `tests/test_migrations.py` / `tests/test_rls.py` — `org_observation_imports` added to the tenant-owned
  set; a 0049 create-branch test (recreates the table + its tenant-isolation RLS policy and the
  per-tenant unique constraint).
- `python scripts/manual_check.py DCA-123` — prints the real imported observations (name / type / state
  + source attribution), the idempotent re-import (0 imported / 3 skipped), the personal-field refusal,
  and the rights denial for an unentitled business (nothing written).

## Boundary

Python DCA only; no AI, no network fetch, no other service's database, no .NET. One migration (0049),
focused tests + this doc. The dataset is a provided approved fixture; crawling / live fetch and every
other concrete adapter (DCA-124..127) stay disabled future capabilities.
