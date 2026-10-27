# DCA-042 — Store per-business spending limits

Stage: Business profile and Indian targeting · key: `budget-settings`

## What this adds

A business's **INR spending caps** are stored as **versioned settings** in the DCA-owned database:

- **Caps**: `dailyCapMinor`, `monthlyCapMinor`, `perRunCapMinor` — in INR **minor units (paise)**,
  stored as integers (`BIGINT`), so a limit is exact and never accrues binary-float rounding error.
- **Currency**: always `INR` (the service is India-only); any other value is refused.
- **Timezone semantics**: an IANA zone (`Asia/Kolkata`) whose civil calendar defines when the *daily*
  and *monthly* windows begin/end. The legacy `Asia/Calcutta` alias is normalized to `Asia/Kolkata`;
  any foreign/unknown zone is refused.
- **Versioned**: every save inserts a new version (`(business_id, version)` unique); prior versions are
  preserved, never overwritten — an immutable, auditable history of what the caps were and who set them.

Storing a limit grants **no spend**: the DCA has no send/charge path here. These caps are the ceiling a
*future* (disabled) spending capability will enforce.

## Components touched (DCA only)

- **Model**: `app/db/models.py` → `BudgetLimit` (tenant-owned, RLS-protected).
- **Store**: `app/services/budget_store.py` → `BudgetLimitStore`, `BudgetLimitInput`, validation +
  stable reason codes, atomic DCA-026 audit on each save.
- **Authorization**: `app/core/security.py` → `require_business_permission_on_path(scope)` (resolves
  the tenant scope from the route path and enforces a DCA-019 business scope).
- **API**: `app/api/routes/budget.py` (registered in `app/main.py`).
- **Migration**: `migrations/versions/0014_budget_limits.py` (dual-path, mirrors 0013).

## HTTP contract (for later Web/Admin consumers)

All routes are under `/v1/tenants/{business_id}/budget/...` and use the **end-user** auth model
(`Authorization: Bearer <jwt>`), not the coarse service-auth gate — this is what lets the service
distinguish *authorized* from *unauthorized users*.

| Method | Path | Scope | Notes |
|---|---|---|---|
| POST | `/v1/tenants/{business_id}/budget/limits` | `budget.manage` (admin) | Store a new version. 422 on invalid/negative/non-INR/non-India-tz. `Idempotency-Key` honoured. |
| GET | `/v1/tenants/{business_id}/budget/limits` | `candidate.read` (member) | The in-force (highest) version, or `{configured:false}`. |
| GET | `/v1/tenants/{business_id}/budget/limits/versions` | `candidate.read` | Full immutable history, oldest first. |
| GET | `/v1/tenants/{business_id}/budget/limits/versions/{version}` | `candidate.read` | One historical version (404 if unknown). |

Authorization outcomes (from the DCA-019 identity fake):

- **owner/admin of the business** → may set/raise caps (201), decision audited.
- **ordinary user (viewer)** → **403** on write (lacks `budget.manage`); may read.
- **platform operator** (no membership in the business) → **403** (no bypass, no impersonation).
- Membership is resolved against the **path** business, so an admin of B cannot set A's caps.

### Validation reason codes (422)

`unsupported_currency`, `negative_cap`, `per_run_exceeds_daily`, `daily_exceeds_monthly`,
`unsupported_timezone`. Each is raised **before** the session is mutated — a rejected write stages no
row and no audit.

## Tests

- `tests/test_budget_limits.py` — store unit tests (SQLite) + HTTP tests (auth wired): admin set +
  audit, ordinary-user/operator denial (nothing staged), negative/currency/timezone/ordering refusals,
  versioning/history, per-business isolation, read-not-write for a viewer, idempotent retry.
- `tests/test_rls.py` / `tests/test_migrations.py` — `budget_limits` added to the RLS set and a
  dual-path `0014` migration test (disposable PostgreSQL; skipped without `DCA_TEST_DATABASE_URL`).
- `tests/test_manual_check.py` — asserts the registered `DCA-042` scenario.

## Developer manual check

```text
python scripts/manual_check.py DCA-042
```

Runs an inspectable A/B scenario (local synthetic SQLite, AI disabled): an admin sets A's caps
(versioned, audited), raising them creates v2 with v1 preserved; an ordinary user and a platform
operator are denied (403) and a negative/non-INR value is rejected (422), each leaving A's caps
unchanged; and B's configuration stays independent. Prints real responses/rows — not a bare PASS.

For disposable-Postgres RLS/migration behaviour:

```text
docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55432:5432 postgres:16-alpine
export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55432/dca"
pytest tests/test_rls.py tests/test_migrations.py
```
