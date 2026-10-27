# DCA-046 — Approve a source configuration

Stage: Source rights and policy foundation · key: `source-approval`

## What this adds

Platform-operator **review/approval** of a source configuration — the capability that lets a recorded
`SourceTerms` version (DCA-045) be accepted independently. A platform operator records a review
carrying the four essentials the story requires, and the endorsed terms version is promoted from
`draft` to `active`.

A `SourceTermsApproval` row (one approval of one `(source_key, terms_version)`) records:

- **review evidence** — `{reference, kind, uri}` the operator relied on; a non-empty `reference` is
  **required** (a bare approval is refused).
- **reviewer** — `reviewer_id` / `reviewer_type`: an opaque *platform operator* subject id (never a
  name/email). Required.
- **review date** — `reviewed_at`, when the operator reviewed. Required.
- **expiry** — `expires_at`, the approval's EXPLICIT validity (optionally delayed by `effective_from`).
  Required: an approval lapses rather than living forever.
- **rights snapshot** — `approved_operation_rights` / `approved_field_rights`: the field/use rights the
  version documented and this approval endorsed — never more than the version documents.

### The guarantees (acceptance criteria)

- **Approval cannot be performed by tenant admins and does not exceed documented field/use rights.**
  Authority is the **platform-plane** scope `platform.source.terms.approve`
  (`Scope.SOURCE_TERMS_APPROVE`), resolved from the caller's platform roles. A business
  administrator (`owner`/`admin`) holds only business-plane scopes, so their roles yield no platform
  scope and the approval is refused **before any state is read or written** — the shared catalog is
  control-plane data, not a tenant resource. An approval endorses exactly the rights the version
  documents; a request to approve a `(field, operation)` the version does not grant is refused
  (`approval_exceeds_rights`), and the stored snapshot is always the documented rights.
- **Reproduce the result and relevant denial without unintended state changes.** The complete
  operator approval promotes the version to `active`; the tenant-admin denial and every incomplete
  review are refused **before** any row is staged, leaving the version a `draft` and no approval row.
- **A version is approved at most once.** `(source_key, terms_version)` is unique; a double approval
  is refused (`already_approved`), defence-in-depth over the DB constraint.
- **Future capabilities stay disabled.** Promoting the terms version to `active` grants **no**
  acquisition right on its own: the source catalog entry must additionally be ENABLED (DCA-044),
  which stays a disabled future capability. No discovery path consults the catalog, so an approval
  never, by itself, authorizes acquisition.

## Components touched (DCA only)

- **Permissions**: `app/identity/permissions.py` → new platform-plane scope
  `Scope.SOURCE_TERMS_APPROVE`, granted to the `platform_operator` role (disjoint from the business
  plane, so a tenant admin can never hold it).
- **Model**: `app/db/models.py` → `SourceTermsApproval` + `SourceApprovalStatus`. Platform
  **control-plane** data (a shared registry), like `source_terms` (DCA-045) — deliberately **outside
  tenant RLS** and never tenant-scoped.
- **Store**: `app/services/source_approval.py` → `SourceApprovalStore`, `SourceApprovalReview`,
  `ReviewerPrincipal`, validation + stable reason codes, the `approve` write path and the
  `is_in_force` evaluator.
- **Migration**: `migrations/versions/0018_source_term_approvals.py` (dual-path, mirrors 0017; no RLS).
- **Manual check**: `scripts/manual_check.py` → `DCA-046` scenario.

No HTTP surface, Web/Admin UI, or AI-service change is in scope: this is the schema + store + the
platform-plane authority. A platform-operator UI to drive approvals is a later story, kept disabled
here ("missing contracts stay disabled").

## Stable codes

Authorization refusal (raised first, before any state is touched): `not_platform_operator`.
Incomplete/invalid-review refusals (raised by `approve` before any row is staged):
`terms_version_not_found`, `missing_reviewer`, `missing_review_evidence`, `missing_review_date`,
`missing_expiry`, `invalid_expiry`, `approval_exceeds_rights`, `already_approved`.

## Tests

- `tests/test_source_approval.py` — the behaviour + failure cases (operator approval records all four
  essentials and promotes the version; tenant admin cannot approve and changes nothing; each
  incomplete review is refused leaving no row; an approval cannot exceed the documented operation or
  field rights; a double approval is refused; an expired approval is not in force).
- `tests/test_migrations.py` — `source_term_approvals` added to the DCA table set;
  `test_0018_creates_source_term_approvals_on_a_preexisting_database` (disposable Postgres) proves the
  dual-path create + unique `(source_key, terms_version)` constraint and **no** tenant RLS policy.
- `tests/test_rls.py` — asserts `source_term_approvals` is excluded from the tenant-owned RLS set.
- `tests/test_manual_check.py` — runs the `DCA-046` scenario end-to-end on synthetic SQLite.

## Developer manual test

Isolated synthetic Businesses A/B are not needed here: a source configuration is platform
control-plane data (shared across tenants, outside RLS), so the scenario exercises the platform-plane
authority directly — a platform operator vs a Business-A administrator (`owner`/`admin`).

```
python scripts/manual_check.py DCA-046
```

Expected: a source (`mca_master_data`) is catalogued and a terms **version 1** recorded. A **tenant
administrator** (Business-A `owner`/`admin`) attempting the approval is **refused**
(`not_platform_operator`) — the version stays a `draft` and no approval row is created. An operator
review with **no evidence** is refused (`missing_review_evidence`), and an approval trying to endorse
`share` (which the version denies) is refused (`approval_exceeds_rights`) — both leave the registry at
**0 approvals**. A **platform operator**'s complete review (evidence + reviewer + review date +
expiry) is **approved**: the approval row records all four essentials, the terms version is promoted
`draft` → `active`, and the source catalog entry **stays a disabled draft** (approving the
configuration does not enable acquisition). Local synthetic only; no AI/paid action.

To exercise the DDL/RLS paths on disposable PostgreSQL (not SQLite):

```
docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55472:5432 postgres:16-alpine
export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55472/dca"
pytest tests/test_migrations.py tests/test_rls.py
```
