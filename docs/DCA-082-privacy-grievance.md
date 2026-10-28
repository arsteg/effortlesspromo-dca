# DCA-082 — Record an India privacy grievance

Stage: *Consent and restriction authority*. Scope: a **scoped grievance intake/status resource** with a
**proportionate verification state** and a **configurable, reviewed response deadline**. **No erasure
logic** — recording a grievance logs the request only; acting on it stays a disabled future capability.

## What was built (DCA only)

A tenant-owned `privacy_grievances` record + a single sanctioned store + a thin HTTP contract.

- **Model** `app/db/models.py::PrivacyGrievance` (+ enums `GrievanceCategory`,
  `GrievanceVerificationLevel`, `GrievanceVerificationState`, `GrievanceStatus`). Tenant-owned
  (`business_id`, RLS). Stores the requester's contact **only as a one-way digest** (never the raw
  value) and the requester's claim token **only as a SHA-256 hash**.
- **Store** `app/services/grievance_intake.py::PrivacyGrievanceStore` — `record()` (validate → derive
  proportionate level → mint one-time claim token → pin reviewed deadline → stage row + atomic audit),
  `get()` (operator by-id, tenant-scoped), `get_for_requester()` (constant-time claim-token match),
  `grievances()` (operator ledger). Refuses an unknown category/channel **before** touching the session.
- **Routes** `app/api/routes/grievances.py` (mounted in `app/main.py`):
  - `POST /v1/tenants/{business_id}/grievances` — **public intake** (service-auth only; the platform
    relays an anonymous public-form submission). Returns the one-time `statusToken`. Retry-safe with
    `Idempotency-Key`.
  - `GET /v1/tenants/{business_id}/grievances/{id}/status` — **requester status** (service-auth +
    `X-Grievance-Token` claim token). A wrong/absent token → **404** (never an existence oracle).
  - `GET /v1/tenants/{business_id}/grievances` and `.../{id}` — **operator reads** (end-user auth +
    the new `grievance.read` business scope). A viewer / platform operator (no membership) → **403**.
- **Permission** `app/identity/permissions.py::Scope.GRIEVANCE_READ` (`grievance.read`) — held by acting
  users + admins, **not** a read-only viewer; it is a business-plane scope, so a platform operator never
  holds it for a client business (delegated client support is a separate scoped/expiring grant).
- **Config** `app/core/config.py::Settings.grievance_response_days` (default **30**), validated to the
  reviewed window **1..90 days** (the India DPDP grievance-redressal statutory ceiling). Pinned onto
  each grievance at intake so the deadline is reproducible even if the policy later changes.
- **Migration** `migrations/versions/0032_privacy_grievances.py` — dual-path safe (no-op on a fresh DB;
  creates the table + tenant-isolation RLS on a DB that predated it), same shape as 0028/0030/0031.

### Acceptance criteria → where they hold

- *Requests expose no contact enumeration and remain visible only to authorized requester/operators* —
  the contact is stored only as a one-way digest and there is **no lookup-by-contact path**; status is
  readable only by the claim-token holder (`get_for_requester`, wrong/absent token → 404) or an
  authorized operator (`grievance.read`, membership-verified). Operator projections carry grievance
  **metadata only** — never the contact digest or the token hash.
- *Reproduce the result and relevant denial without unintended state changes or later-story work* — all
  reads are side-effect-free; the relevant denials (wrong token → 404, unauthorized operator → 403,
  cross-tenant id → 404) change nothing; intake does no erasure/send/model call and records only a
  `received` / `unverified` grievance (future lifecycle/verification/erasure stay disabled).

## Tests / quality

- `tests/test_grievance_intake.py` — service unit (SQLite) + HTTP (`TestClient`) incl. the failure
  cases (unknown category → 422 nothing staged; wrong/missing token → 404; viewer/operator → 403;
  isolation; configurable deadline; idempotent retry).
- `tests/test_rls.py` + `tests/test_migrations.py` — `privacy_grievances` added to the RLS set and the
  DCA table set, with a disposable-Postgres tenant-isolation test and a 0032 create-branch migration
  test (`DCA_TEST_DATABASE_URL`).
- Quality gate: `ruff check .` · `ruff format --check .` · `mypy` · `pytest`.

## Manual check

```text
python scripts/manual_check.py DCA-082
```

Submits a synthetic grievance for Business A, prints the recorded state (proportionate `elevated`
verification level for an `access` request, pinned reviewed deadline), then the safe status read with
the valid claim token, the not-found reads for a wrong token and a cross-tenant id, and the per-tenant
operator ledger sizes — local synthetic only, no AI/paid action, no erasure.

## Boundary

Python DCA only. No `.NET`, no AI-service, no Web/Admin changes. No new provider SDK/key, model routing
or second gateway. No live source/send/erasure path — all future capabilities stay disabled.
