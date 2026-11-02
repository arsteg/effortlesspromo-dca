# DCA-151 — Dismiss a suggestion privately

Stage: User selection and EffortlessPromo sync · key `candidate-dismiss`

## What this adds

A **per-business, private dismissal** of a DCA-145 `ProspectSuggestion`: one authorized business user
records a dismissal + relevance-feedback reason, which flips the suggestion's `status` to `dismissed` so
it **drops out of the DCA-150 VISIBLE candidate list** for that business. The dismissal:

- is **business-private** — it edits only the one `business_id`-scoped suggestion row; it changes **no
  global fact or policy** and never touches any shared/global master;
- **cannot remove another tenant's suggestion** — B's independent suggestion for the same subject is its
  own row, left untouched (a DCA-151 acceptance criterion);
- is **idempotent** — re-dismissing is a 200 no-op (no field re-written, no second audit), and a retried
  request carrying an `Idempotency-Key` replays verbatim;
- is **atomically audited** (DCA-026) in the same unit of work, with opaque references only;
- **spends and sends nothing** — future capabilities stay disabled.

## Schema change (migration `0067_suggestion_dismissal`)

Adds four columns to the existing (tenant-owned, RLS-protected) `prospect_suggestions` table:

| column | type | meaning |
|---|---|---|
| `dismissed_at` | `timestamptz` null | WHEN dismissed (NULL until dismissed) |
| `dismissal_reason` | `text` default `''` | opaque relevance-feedback note (bounded to 500 chars) |
| `dismissed_by` | `varchar(64)` default `''` | WHO dismissed it (opaque actor id) |
| `dismissed_by_type` | `varchar(16)` default `''` | actor kind |

Add-column branch, same guard style as `0011`/`0015`/`0020`/`0060`: a fresh DB gets the columns from the
metadata baseline (0001); a DB that reached 0066 first gets them added here with server defaults so
pre-existing rows backfill to "not dismissed". **No new table and no RLS change** — the table already
carries the 0002/0064 tenant-isolation policy, so the dismissal stays scoped to one `business_id`.

Alembic revision id is `0067_suggestion_dismissal` (25 chars, ≤ 32); `down_revision =
0066_search_partition_backoffs`.

## The endpoint

```
POST /v1/tenants/{business_id}/prospect-suggestions/{suggestion_id}/dismiss
Body: { "reason": "<optional, ≤500 chars>" }
Header (optional): Idempotency-Key: <key>
```

Response (200):

```json
{
  "id": "...", "businessId": "...", "runId": "...",
  "subjectType": "organization", "subjectId": "...",
  "visibility": "research_only", "status": "dismissed",
  "dismissed": true, "dismissedAt": "2026-11-02T12:00:00+00:00",
  "dismissalReason": "already a customer", "alreadyDismissed": false
}
```

## Authorization & tenancy

- **End-user permission model (DCA-019)**, like the sibling DCA-150 read — layered on top of
  membership-verified tenant resolution from the **path** `business_id`. Dismissing is a WRITE, so it
  requires the new **`candidate.dismiss`** scope: an acting user and an administrator hold it; a
  **read-only viewer does NOT** (viewing ≠ editing), and a non-member — including a platform operator
  (no membership in any client business) — is denied **(403, audited)** before anything is read or
  written. An unauthenticated request is 401.
- The path `business_id` is the **row scope**: the dismisser is bound to the verified business, so an
  unknown or cross-tenant `suggestion_id` reads as **not-found (404)** — a dismissal can never reach
  another tenant's row or any shared master.

## Idempotency

Two layers: (1) **semantic** — an already-dismissed suggestion returns `alreadyDismissed: true` with no
field re-written and no second audit; (2) **request-level** — an `Idempotency-Key` (DCA-035) replays the
first response verbatim (`Idempotent-Replay: true`).

## Future capabilities stay disabled

Dismissal only ever makes the agent do **less** (hide one of this business's own candidates). It
executes nothing, enqueues no job, spends nothing and sends nothing; acceptance / promotion to anything
executable and the `sendable` visibility remain disabled future capabilities.

## Files changed (DCA)

- `app/db/models.py` — four DCA-151 dismissal columns on `ProspectSuggestion`.
- `app/identity/permissions.py` — new business-plane scope `candidate.dismiss` (acting user + admin, not
  viewer).
- `app/services/prospect_suggestion.py` — `ProspectSuggestionDismisser` + `SuggestionDismisser` actor,
  `SuggestionDismissResult`, dismissal constants and error types.
- `app/api/routes/prospect_suggestions.py` — new path-scoped `POST .../dismiss` route (`candidate.dismiss`,
  idempotent).
- `migrations/versions/0067_suggestion_dismissal.py` — add-column migration.
- `tests/test_prospect_suggestion_dismiss.py` — service (SQLite) + HTTP (TestClient) + PG-gated tenant
  tests.
- `tests/test_migrations.py` — PG-gated 0067 add-column branch + backfill test.
- `scripts/manual_check.py` — registered the `DCA-151` inspectable scenario.

## Verification

```bash
.venv/Scripts/python.exe -m pytest tests/test_prospect_suggestion_dismiss.py -q
.venv/Scripts/python.exe scripts/manual_check.py DCA-151
```

The manual check prints the actual state and `checks: {...}` all `True`
(`dismissed_and_left_visible_list`, `idempotent_no_second_audit`, `viewer_refused_no_state_change`,
`cross_tenant_not_found`, `other_business_unaffected`).

The PG-gated tests (`test_pg_dismissal_is_tenant_scoped_and_private`,
`test_0067_adds_suggestion_dismissal_columns_on_a_preexisting_database`) run against a disposable
Postgres when `DCA_TEST_DATABASE_URL` is set; they skip otherwise.
