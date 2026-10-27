# DCA-040 — Confirm one ICP version

**Status:** implemented. Adds an explicit **confirmation** operation that selects which DCA-039
`IcpDraft` version is the business's **active** Ideal Customer Profile, recording **who** confirmed it
and **when** — without granting any campaign/sending authority. Backend-only (Python DCA); no provider
SDKs/keys, no model routing, no second AI gateway, no writes to another service's database, and **no AI
call** (confirmation is a deterministic selection + audit, like the DCA-039 draft store).

## What it does

A business user can confirm one ICP draft version as the one the agent should treat as its active
targeting profile. Editing an ICP still creates a new draft (DCA-039) and does **not** change the
active version — the previously-confirmed version stays active until a new version is itself confirmed.

| Behaviour | How |
|---|---|
| **One active version per business** | `app/db/models.py:IcpConfirmation` — a tenant-owned, RLS-protected pointer row (`business_id` unique). Confirming re-points this single row (an upsert); there is never a second active version. |
| **Only a valid, material draft becomes active** | `app/services/icp_confirmation.py:IcpConfirmationStore.confirm` validates FIRST: the version must exist for **this** business (unknown/other-tenant → `icp_draft_version_not_found`, 404) and carry **material targeting** (`has_material_targeting`: ≥1 of industries / size bands / roles / geography — exclusions alone never establish a target → `no_material_targeting`, 422). A refusal stages no row and no audit. |
| **Records actor + time** | The confirmation row stores `actor_id` / `actor_type` (opaque subject id, supplied by the caller — the `X-Service-Auth` gate authenticates the service, not the user, per DCA-001 B3) and `confirmed_at`. A caller-supplied actor id carrying contact/secret data is refused by the audit sink (`actor_id_not_opaque`, 422). |
| **Atomic audit** | A `icp.confirm` `AuditEvent` is staged into the caller's session, so it commits in the SAME unit of work as the pointer change (SEC-001/BR-122 discipline); references are opaque (`version`, `icpDraftId`). |
| **Editing never changes the active version** | Drafts are insert-only (DCA-039); saving a new version never touches the pointer. Confirming the new version is a separate, explicit act. |
| **No campaign authority** | Confirmation selects the active targeting version only. The DCA has no send path; acquisition/consent/suppression stay platform-gated. The response carries `grantsCampaignAuthority: false`. |
| **Idempotent & tenant-isolated** | The confirm route is DCA-035 `Idempotency-Key`-safe; every read/write is `business_id`-scoped (B cannot see or confirm A's version). |

Confirming an ICP version is unrelated to recipient consent; this story touches no sending/consent
path and enrolls no one.

## Where it lives

- **Model** — `app/db/models.py`: `IcpConfirmation` (table `icp_confirmations`, `uq_icp_confirmation_business`).
- **Store** — `app/services/icp_confirmation.py`: `IcpConfirmationStore` (`confirm` / `active` /
  `active_version`), `has_material_targeting`, `IcpConfirmationError`, the stable reason codes
  (`REASON_DRAFT_VERSION_NOT_FOUND`, `REASON_NO_MATERIAL_TARGETING`) and `AUDIT_ACTION_CONFIRM`.
- **Routes** — `app/api/routes/icp.py`: `POST /v1/tenants/{business_id}/icp/confirm` and
  `GET /v1/tenants/{business_id}/icp/active`; the draft list/get views now carry an `active` flag.
- **Migration** — `migrations/versions/0013_icp_confirmations.py` (dual-path create + RLS policy, same
  guard style as 0005..0012). A fresh DB gets the table from the 0001 metadata baseline and its RLS
  policy from 0002 (it is a `TenantOwned` table), so 0013 is a no-op there.

**Design note (interpretation):** "confirmed material targeting" is read as *at least one positive
targeting dimension* (industry / size band / role / geography). An empty draft, or one listing only
exclusions, is not a target and cannot be confirmed — that is the headline denial case.

## Tests

- `tests/test_icp_confirmation.py` — service unit tests (`has_material_targeting` matrix; confirm
  selects the active version and writes one atomic audit; unknown version / no-material-targeting are
  refused staging nothing; editing keeps the prior version active and re-confirming re-points the
  single row; per-business scope; the store never commits) and HTTP tests (the headline confirm-v1 →
  edit-to-v2 → v1-stays-active-until-v2-confirmed flow; 404 / 422 / bad-actor / unsupported-field
  denials with no state change; idempotent retry; tenant isolation).
- `tests/test_rls.py` — `icp_confirmations` added to the RLS-covered tenant-owned set.
- `tests/test_migrations.py` — `icp_confirmations` added to `DCA_TABLES`; `test_0013_...` exercises the
  0013 create branch (table + unique constraint + RLS policy) on a pre-existing database.
- `tests/test_manual_check.py` — asserts the `DCA-040` scenario is registered and proves the confirm /
  edit-keeps-active / denials / tenant-isolation outcomes on local synthetic SQLite.

`ruff check` / `ruff format` / `pytest` pass (508 passed, 40 Postgres-gated skips on SQLite). The
RLS/migration suites were run against a **disposable PostgreSQL** (`DCA_TEST_DATABASE_URL`) — 30 passed
— exercising locking/RLS/transactions as required, not SQLite. (`mypy` on the changed modules is clean;
a pre-existing environment issue makes the bare `mypy` run choke on a global numpy stub.)

## Developer manual test

```bash
# Backend, inspectable scenario (local synthetic SQLite, AI disabled, auto-cleaned):
python scripts/manual_check.py DCA-040
```

Using synthetic Businesses A/B, it: confirms A's ICP v1 (shows it ACTIVE with actor + time and
`grantsCampaignAuthority=false`); edits to v2 and shows v1 is **still** active until v2 is confirmed;
then confirms v2. It reproduces the unknown-version (404), no-material-targeting (422) and
contact-data-actor-id (422) denials, proving the active version is unchanged after each; and proves B
has nothing active and cannot confirm A's version (404). Every call's real body is printed so the
outcomes can be compared against the acceptance criteria. No production/paid resources.

To run the Postgres-backed RLS/migration checks for this table:

```bash
docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55471:5432 postgres:16-alpine
export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55471/dca"
pytest tests/test_migrations.py tests/test_rls.py
```
