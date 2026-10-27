# DCA-037 — Import an authorized platform profile snapshot

**Status:** implemented. Adds a **typed platform-profile contract** (the fake today; no host-table
access) and an **importer** that fetches a business's own profile + Brand KB facts through it and
stages them as a new, **provenance-bearing** `BusinessProfileDraft` version. An imported draft retains
the snapshot's **source version** and **business scope** and stays a reviewable `draft`. Backend-only
(Python DCA); no provider SDKs/keys, no model routing, no second AI gateway, no writes to another
service's database, and no AI call (importing a typed snapshot is deterministic).

## What it does

The agent can seed a business-profile draft from the platform's own record of the business rather than
having it hand-typed. It asks the platform (through a typed contract) for the business profile + Brand
KB facts, then stages them as a DCA-036 draft version that records exactly where they came from.

| Behaviour | How |
|---|---|
| **Typed contract, fake first, no host-table access** | `app/profiles/contract.py` defines `PlatformProfileRequest` / `PlatformProfileSnapshot` / the `PlatformProfileClient` `Protocol`. The only implementation is `FakePlatformProfileClient` (`app/profiles/fake.py`) — deterministic synthetic fixtures for Business A/B, flagged `synthetic`, with a plainly-fake `source_version`. It reads no platform table. The live adapter is a later story (B1) and stays **disabled in production** (`build_platform_profile_client` refuses the fake there), mirroring the DCA-014 identity / DCA-022 entitlement contracts. |
| **Imported facts retain source version + business scope, stay drafts** | `import_profile_snapshot` (`app/services/profile_importer.py`) maps the snapshot into a `ProfileDraftInput` carrying `source` / `source_version` + a full `provenance` record, then stages it via the DCA-036 `ProfileStore`. The draft is always stored under the business it was fetched for (its scope) and is always `status='draft'`. The new `source` / `source_version` / `provenance` columns on `business_profile_drafts` hold the provenance. |
| **DCA-036 guarantees preserved** | The importer never inserts a row itself — it composes `ProfileStore.save_draft`, so a second import is a NEW version preserving prior ones, unknown aspects are retained, and no confirmed profile is ever created. |
| **Denials change no state** | A **disabled contract** (production / no client) → `503 profile_import_contract_disabled`; the platform reporting **no profile** for the business → `404 platform_profile_not_found`. Both refuse before any row is staged, so the draft list is unchanged. |
| **Retry-safe** | The import route honours the DCA-035 `Idempotency-Key` (a byte-identical retry replays the first result and creates no second version). Tenant-scoped + `X-Service-Auth` fail-closed like every `/v1/tenants` route; an unregistered tenant is `404`. |

Acceptance of a platform profile is unrelated to recipient consent; this story touches no sending/
consent path.

## Where it lives

- **Contract** — `app/profiles/contract.py`: `PlatformProfileRequest`, `PlatformProfileSnapshot`
  (carries `business_id` scope, `source`, `source_version`, `found`, the profile aspects + Brand KB
  `facts`, `as_of`, `synthetic`), and the `PlatformProfileClient` `Protocol`.
- **Fake** — `app/profiles/fake.py`: `FakePlatformProfileClient` (distinct synthetic A/B fixtures, a
  not-found empty snapshot otherwise), `PlatformProfileContractDisabled`, `build_platform_profile_client`.
- **Importer** — `app/services/profile_importer.py`: `import_profile_snapshot` (stages into the
  caller's session, never commits), `default_platform_profile_client`, `ProfileImportError` and the
  stable reason codes `profile_import_contract_disabled` / `platform_profile_not_found`.
- **Model** — `app/db/models.py`: `BusinessProfileDraft` gains `source` / `source_version`
  (`String(64)`, default `""`) and `provenance` (`JSON`, default `{}`). Still tenant-owned (DCA-018 RLS).
- **Store** — `app/services/profile_store.py`: `ProfileDraftInput` gains optional `source` /
  `source_version` / `provenance`; `save_draft` stamps them (empty for a manually-authored draft).
- **Route** — `app/api/routes/profiles.py`: `POST /v1/tenants/{business_id}/profile/import` (201,
  idempotent). `_draft_view` / `_draft_summary` now surface `source` / `sourceVersion` / `provenance`.
- **Wiring** — `app/main.py` sets `app.state.platform_profile_client` (None when the contract is
  disabled → the import route fails closed).
- **Migration** — `migrations/versions/0011_profile_import_provenance.py`: additive, dual-path (fresh
  DB gets the columns from the 0001 metadata baseline; a DB already at 0010 gets them added with a
  backfilling server default). Downgrade is a no-op (expand→contract, BR-201).

## Tests

- `tests/test_platform_profile_contract.py` — the fake/contract: distinct versioned A/B snapshots, a
  not-found empty snapshot, the `PlatformProfileClient` protocol, and production refusing the fake.
- `tests/test_profile_importer.py` — service unit tests (in-memory SQLite): import stages a
  provenance-bearing `draft` retaining source version + scope + facts; A/B distinct; a second import
  is a new version; the contract-disabled and nothing-to-import denials refuse and stage nothing; the
  importer doesn't commit. HTTP tests (`TestClient`): import A/B distinct; no confirmed profile
  created; idempotent retry; unregistered tenant 404; and the `503` / `404` denials leave the draft
  list unchanged.
- `tests/test_migrations.py` — adds a Postgres-backed 0011 add-column test (drop the three columns,
  stamp back to 0010, upgrade; the columns are re-added and a pre-existing row is backfilled). Verified
  on a disposable Postgres 16.

RLS/transaction/migration paths run on **disposable PostgreSQL** (`DCA_TEST_DATABASE_URL`); the
importer's app-level logic is covered on SQLite like the DCA-036 store.

## Manual check

```text
python scripts/manual_check.py DCA-037
```

Registers an inspectable scenario (local synthetic, ephemeral DB, AI disabled, fake contract — no
host-table access): registers A and B, imports distinct provenance-bearing drafts, shows no confirmed
profile was created, then reproduces the disabled-contract (`503`) and nothing-to-import (`404`)
denials and shows each changed no state.

## Boundaries honoured

- Python DCA only — no `.NET`/TypeScript, no UI, no AI-service internals, no AI call.
- Fake contract first; no host-table access, no cross-service database writes, no provider SDKs/keys,
  no model routing, no second AI gateway.
- Tenant isolation, idempotency and the disabled-future-capability discipline preserved; the confirm
  capability stays disabled (DCA-036). The live platform-profile adapter stays disabled in production.
