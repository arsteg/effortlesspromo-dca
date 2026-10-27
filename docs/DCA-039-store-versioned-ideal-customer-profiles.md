# DCA-039 — Store versioned ideal-customer profiles

**Status:** implemented. Adds a **versioned, business-scoped Ideal Customer Profile (ICP) draft**:
structured targeting — `industries`, `size_bands` (a controlled vocabulary), `roles`, `geography`
(recognized Indian states/UTs, the service being India-only) and `exclusions` — optionally **linked to
a DCA-036 business-profile draft version**. Backend-only (Python DCA); no provider SDKs/keys, no model
routing, no second AI gateway, no writes to another service's database, and **no AI call** (storage and
validation are deterministic Python). This story only stores a reviewable draft — no future capability
(promotion to an active ICP, discovery, export, consent) is enabled.

## What it does

A business user can store a structured ICP as a **reviewable draft version**. Each save inserts a new
version (`(business_id, version)` unique), so prior versions are **preserved**, never overwritten —
exactly like the DCA-036 `BusinessProfileDraft`. Unsupported inputs are refused with a clear `422`
before anything is staged.

| Behaviour | How |
|---|---|
| **Business-scoped & versioned** | `IcpDraft` is a `TenantOwned` (RLS-protected) table. `IcpStore` is bound to one `business_id`; `save_draft` inserts `max(version)+1`; there is no update/delete path, so a new draft preserves every prior version. One tenant can never read another's versions (app-level scope + DCA-018 RLS). |
| **Controlled size bands** | `SIZE_BANDS = (micro, small, medium, large, enterprise)`. A value outside the set is `invalid_size_band` (422). Values are lower-cased and de-duplicated. |
| **India-only geography** | Each `geography` entry must normalize to a recognized ISO 3166-2:IN state/UT (reuses `app/geography/india.py`); anything else is `unsupported_geography` (422). Stored as ISO codes. |
| **Unsupported fields rejected** | The request body is `extra='forbid'` — a field this capability does not support (a typo, a future-capability field) is a clear `422`, not silently ignored. |
| **Linked profile version** | `profile_version` optionally links the ICP to the business-profile draft version it was authored against. When supplied it must name an existing `BusinessProfileDraft` version **of the same business**; an unknown version is `profile_version_not_found` (422). |
| **Validation stages nothing** | Every validation runs before the session is touched, so a denial leaves no row and no partial state — the denial is reproducible. |
| **Retry-safe** | Writes honour the DCA-035 `Idempotency-Key` (a replay returns the stored result and creates no second version). |
| **Future capabilities disabled** | Rows are always `status='draft'`; nothing promotes a draft to an active/accepted ICP, and no discovery/export/consent path is wired. |

Storing an ICP is unrelated to recipient consent; this story touches no sending/consent path and
enrolls no one. The early single-active `IcpProfile` record (`PUT /v1/tenants/{id}/icp`) is left
untouched — the versioned drafts are the DCA-039 artifact.

## Where it lives

- **Model** — `app/db/models.py`: `IcpDraft` (`icp_drafts`, tenant-owned, `uq_icp_draft_version`).
- **Store + validation** — `app/services/icp_store.py`: `IcpStore`, `IcpDraftInput`, `SIZE_BANDS`,
  `IcpValidationError`, and the stable codes (`invalid_size_band`, `unsupported_geography`,
  `profile_version_not_found`).
- **API** — `app/api/routes/icp.py` (registered in `app/main.py`):
  - `POST /v1/tenants/{business_id}/icp/drafts` — save a new ICP draft version (201).
  - `GET  /v1/tenants/{business_id}/icp/drafts` — list versions (summaries).
  - `GET  /v1/tenants/{business_id}/icp/drafts/{version}` — one version (404 if unknown/cross-tenant).
  - `GET  /v1/tenants/{business_id}/icp/reference` — the controlled vocabularies (size bands +
    India states/UTs).
- **Migration** — `migrations/versions/0012_icp_drafts.py` (dual-path: no-op on a fresh DB where the
  baseline + 0002 RLS already cover it; creates the table + RLS policy on a pre-existing DB).

## Tests

- `tests/test_icp_store.py` — service unit tests (per-business versioning; size-band/geography
  normalization + refusal; linked-profile-version validation incl. per-business scope; stage-nothing;
  no-commit) and HTTP tests (save/list/retrieve; invalid-band/unsupported-field/non-India-geography/
  bad-link 422s with no state change; cross-tenant 404; idempotent retry).
- `tests/test_rls.py`, `tests/test_migrations.py` — `icp_drafts` added to the covered-table sets; a
  `0012` pre-existing-DB create-branch test (Postgres-only; skips without `DCA_TEST_DATABASE_URL`).
- `tests/test_manual_check.py` — the DCA-039 scenario runs on synthetic SQLite and proves versioning,
  the denials, and tenant isolation.

## Manual check

```text
python scripts/manual_check.py DCA-039
```

Runs against an ephemeral synthetic SQLite DB (AI disabled), registers Businesses A/B, saves valid +
versioned ICP drafts for A, reproduces the four denials (invalid band / unsupported field / non-India
geography / unknown linked profile version — each a clear `422` with no state change), links an ICP to
an existing business-profile draft version, and shows that B cannot retrieve A's version. For RLS on a
real Postgres, see the `DCA-018` scenario and `tests/test_rls.py`.
