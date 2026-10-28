# DCA-078 — Store scoped processing restrictions

Stage: Consent and restriction authority · key `restriction-ledger` · source sections 5, 10, 11.

## Prerequisite note

The story names DCA-077 (and the earlier sequence) as the prerequisite. DCA-077
(`require fresh evidence for re-consent`) is the latest **committed** work (HEAD). The DCA-074–077
consent-receipt spine it builds on is present in the working tree (some of it as uncommitted
changes). DCA-078 adds a new, self-contained restriction ledger that depends only on the committed
tenancy/audit/source-catalog foundation (DCA-017/018 tenancy + RLS, DCA-026 audit sink, DCA-044
source catalog, DCA-019 permission planes); it does not depend on the consent-receipt stores, so it
is implemented without relying on any uncommitted consent work.

## What this adds

A scoped **processing-restriction ledger** across the two disjoint authority planes
(`app/services/processing_restriction.py`), with two append-only, versioned tables:

- **`processing_restrictions`** (tenant-owned, RLS-protected) — a **LOCAL** restriction a business
  places on the agent's processing for a scope it owns: any combination of a **brand**, a **channel**
  and a **purpose** (each optional; an empty dimension means "all", so a fully-empty scope is a
  tenant-wide restriction). Written by `ProcessingRestrictionStore`, which requires a business user
  holding the new business scope `Scope.PROCESSING_RESTRICT` (`processing.restrict`). A read-only
  viewer, a non-member, and a platform operator all lack it. Each successful record stages an atomic
  DCA-026 audit entry (opaque references only) in the same unit of work.

- **`platform_source_restrictions`** (platform control-plane, NOT tenant-owned, outside tenant RLS —
  like the DCA-044 source catalog) — a **justified, PLATFORM-WIDE source removal**: a platform
  operator removes a catalogued `Source` across every tenant. Written by
  `PlatformSourceRestrictionStore`, which requires a platform operator holding the new platform scope
  `Scope.PLATFORM_SOURCE_RESTRICT` (`platform.source.restrict`) **and** a non-empty `justification`,
  and validates the source exists. A tenant administrator's business roles never authorize it. The
  row itself is the durable who/when/why record (no tenant audit entry — a platform-wide action has no
  single business scope), mirroring the DCA-050 revocation.

Both rows carry the **authority, version and retention metadata** the story asks for:
`authority_plane` (`business` | `platform`) + `authority_scope` + `authorized_by`, a 1-based `version`
(unique per scope / per source), and `retention_mode` (`until_lifted` default / indefinite, or
`until_date` + a required `retain_until`).

## Acceptance criteria → evidence

- **Local and platform-wide restrictions are distinguishable and require appropriate authority.** The
  two kinds are distinct records on distinct planes. Local is tenant-owned (`business_id` set,
  `authority_plane=business`, needs `processing.restrict`); platform-wide is control-plane (no
  `business_id`, `authority_plane=platform`, needs `platform.source.restrict` + justification). The
  planes are disjoint (`app/identity/permissions.py` asserts it), so neither authority reaches the
  other. Covered by `test_local_and_platform_restrictions_are_distinguishable`,
  `test_authorized_user_records_a_local_restriction`,
  `test_operator_records_a_platform_wide_source_removal`.

- **Reproduce the result and the relevant denial without unintended state changes.** Each denial is
  raised before the session is mutated and stages no row: a viewer placing a local restriction
  (`test_viewer_is_denied_and_stages_no_row`), a tenant admin / a missing justification / an unknown
  source for a platform-wide removal (`test_tenant_admin_cannot_perform_a_platform_wide_removal`,
  `test_platform_removal_requires_a_justification`, `test_platform_removal_requires_an_existing_source`),
  plus unknown channel / inconsistent retention for the local write.

A restriction authorizes nothing and sends nothing: it only ever makes processing MORE restrictive,
and acting on it stays a disabled future capability behind the one send pipeline. No live source is
enabled and no AI/paid path is touched.

## Files

- `app/db/models.py` — `ProcessingRestriction`, `PlatformSourceRestriction`,
  `RestrictionRetentionMode`, `RestrictionAuthorityPlane`.
- `app/services/processing_restriction.py` — the two stores + principals + input/validation.
- `app/identity/permissions.py` — new scopes `PROCESSING_RESTRICT` (business) and
  `PLATFORM_SOURCE_RESTRICT` (platform), wired into the role maps.
- `migrations/versions/0030_processing_restrictions.py` — creates both tables; RLS + app-role grant
  for the tenant-owned one only.
- `tests/test_processing_restriction.py` — behaviour + every denial.
- `tests/test_rls.py`, `tests/test_migrations.py` — table-set parity (local in RLS set, platform
  excluded) + the 0030 create-branch Postgres test.
- `scripts/manual_check.py` — the `DCA-078` inspectable scenario.

## Manual check

```
python scripts/manual_check.py DCA-078
```

Creates a local restriction for synthetic Business A and a platform-wide source removal in a
disposable SQLite DB, shows the actual rows (ownership, plane, version, retention, audit entry) and
reproduces each denial (viewer / tenant-admin / missing justification) proving no row is staged. Local
synthetic only; AI disabled; the DB is deleted at the end.

For the real transaction / RLS / DDL path, run the Postgres-backed suites against a disposable
Postgres:

```
docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55432:5432 postgres:16-alpine
export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55432/dca"
pytest tests/test_migrations.py tests/test_rls.py
```
