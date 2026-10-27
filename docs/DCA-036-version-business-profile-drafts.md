# DCA-036 — Version business profile drafts

**Status:** implemented. Adds a scoped, **versioned business-profile draft** model: each save creates a
new draft version of the tenant's own profile (products, business type, value proposition, languages,
confirmed facts) while **retaining unknown aspects explicitly**. A new draft preserves every prior
version and can never silently replace the confirmed profile — confirmation is a **disabled future
capability**. Backend-only (Python DCA); no provider SDKs/keys, no model routing, no second AI gateway,
no writes to another service's database.

## What it does

The agent records what it learns about a business as an immutable, versioned draft. Saving a draft is
insert-only and never touches a "confirmed" profile, so review is safe and reproducible.

| Behaviour | How |
|---|---|
| **A new draft preserves prior versions** | `ProfileStore.save_draft` inserts a NEW row at `max(version) + 1`; it never updates or deletes an existing version. `(business_id, version)` is unique, so a version can never overwrite another of the same tenant. Every prior draft stays readable via `versions()` / `get_version()`. |
| **Cannot silently replace the confirmed profile** | The store only ever stages `status='draft'` rows and never mutates an existing one. Promoting a draft to the accepted `CONFIRMED` profile is a **disabled future capability**: `ProfileStore.confirm()` / `POST .../confirm` refuses with `profile_confirmation_disabled` (403) and stages nothing — so no confirmed profile is ever created here. |
| **Unknowns retained explicitly** | Canonical aspects (`products`, `business_type`, `value_proposition`, `languages`, `confirmed_facts`) left empty are recorded in `unknowns` rather than silently dropped; a populated aspect is never listed unknown. The unknown set and the populated set are kept disjoint. |
| **Tenant-scoped** | Every read/write is confined to one `business_id`; a version id from another business reads as not-found (404), never surfaced. Tenant-owned, so RLS (DCA-018) protects it at the database too. |
| **Retry-safe writes** | The save route honours the DCA-035 `Idempotency-Key` (a byte-identical retry replays the first result and creates no second version). |

This is the tenant's **own** business content, not prospect contact data or secrets, so the fields are
NOT opaque-reference screened (unlike the audit/outbox/inbox/revision payloads). Acceptance of a
prospect is unrelated; this story touches no sending/consent path.

## Where it lives

- **Model** — `app/db/models.py`: `BusinessProfileDraft(TenantOwned)` with
  `tenant_unique("version", name="uq_profile_draft_version")` and a `ProfileStatus` enum
  (`draft` | `confirmed`). Tenant-owned, so it inherits the DCA-017 `business_id` column and the
  DCA-018 RLS policy like every other tenant data table.
- **Store (the one writer)** — `app/services/profile_store.py`: `ProfileStore` (tenant-scoped reader/
  writer that stages into the caller's session, never commits), `ProfileDraftInput`, the
  unknown-retention rule, and `ProfileError` / `REASON_CONFIRM_DISABLED` for the disabled confirm path.
- **Routes** — `app/api/routes/profiles.py` (prefix `/v1/tenants`, `X-Service-Auth` fail-closed):
  - `POST /{business_id}/profile/drafts` — save a new draft version (201, idempotent).
  - `GET /{business_id}/profile/drafts` — list versions (oldest first).
  - `GET /{business_id}/profile/drafts/{version}` — one version (404 if absent/cross-tenant).
  - `GET /{business_id}/profile/confirmed` — the confirmed profile (always none today).
  - `POST /{business_id}/profile/drafts/{version}/confirm` — the disabled capability → 403.
- **Migration** — `migrations/versions/0010_business_profile_drafts.py`: dual-path (metadata baseline
  on a fresh DB; create + RLS policy + grants on a DB migrated before the table existed), same guard
  style as 0005–0009. Downgrade is a no-op (teardown owned by 0001/0002; expand→contract, BR-201).
- **Registered** — `app/main.py` includes `profiles.router`.

## Tests

- `tests/test_profile_store.py` — service unit tests (in-memory SQLite): first draft is v1; a new
  draft preserves prior versions; unknowns retained/kept disjoint; per-business version scoping; save
  only ever stages `draft` status; confirm is disabled and stages nothing (no confirmed profile); the
  store does not commit. HTTP tests (`TestClient`): save two drafts + retrieve versions; saving creates
  no confirmed profile; confirm is denied (403) and changes nothing; unknown version 404; cross-tenant
  version not visible; unregistered tenant 404; save is idempotent on retry.
- `tests/test_migrations.py` — adds `business_profile_drafts` to `DCA_TABLES` and a Postgres-backed
  0010 create-branch test (recreates the table + its `uq_profile_draft_version` constraint + RLS
  policy on a pre-existing DB). Verified on a disposable Postgres 16.
- `tests/test_rls.py` — the exact-coverage assertion now includes `business_profile_drafts`.

## Manual check

```text
python scripts/manual_check.py DCA-036
```

Registers an inspectable scenario (local synthetic, ephemeral DB, AI disabled): registers Business A,
saves two draft versions and shows v1 is preserved and its unknowns retained, shows no confirmed
profile was created, then attempts to confirm v1 and shows the 403 denial with no state change.

## Boundaries honoured

- Python DCA only — no `.NET`/TypeScript, no UI, no AI-service internals, no AI call (the draft is
  deterministic, tenant-authored content; AI-assisted profile building is a separate capability story).
- No provider SDKs/keys, no model routing, no second AI gateway, no cross-service database writes.
- Tenant isolation, idempotency and the disabled-future-capability discipline preserved.
- Future capability (confirming/promoting a draft to the accepted profile) stays disabled.
