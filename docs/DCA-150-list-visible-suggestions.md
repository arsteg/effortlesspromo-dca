# DCA-150 — List visible suggestions

Stage: User selection and EffortlessPromo sync · key `candidate-list`

## What this adds

A **tenant-scoped, read-only candidate list** over the DCA-145 `ProspectSuggestion` rows: one
authenticated HTTP capability (for a later Web/Admin consumer) that answers *which prospect suggestions
are currently VISIBLE for review* for one business — paginated, with safe optional filters and
rights-aware fields. It reads inert rows and **writes nothing, spends nothing, sends nothing**, so the
result reproduces with no state change.

No new table, migration or RLS change — the `prospect_suggestions` table (DCA-145) already exists and
is already RLS-protected; this is a pure read over it.

## The endpoint

```
GET /v1/tenants/{business_id}/prospect-suggestions
    ?limit=<1..200, default 50>
    &offset=<>=0, default 0>
    &runId=<optional: confine to one discovery run>
    &minScore=<optional 0.0..1.0: minimum ICP fit>
```

Response:

```json
{
  "businessId": "...",
  "generatedAt": "2026-11-02T12:00:00+00:00",
  "limit": 50, "offset": 0, "total": 2, "hasMore": false,
  "items": [
    {
      "id": "...", "runId": "...", "subjectType": "organization", "subjectId": "...",
      "visibility": "research_only", "sendable": false, "status": "suggested",
      "matchScore": 0.9, "rationale": "ICP fit 0.90", "scoredIcpVersion": 1,
      "schemaVersion": 1, "matchedDimensions": ["industry"],
      "createdAt": "...", "expiresAt": "..."
    }
  ]
}
```

## Authorization & tenancy

- **End-user permission model (DCA-019), not the service-auth gate** — like the sibling `spend/summary`
  read. Viewing suggestions is an ordinary-business-user action, so it requires the **`candidate.read`**
  scope on a membership-verified tenant context. A viewer, an acting user and an administrator all hold
  it; a **non-member — including a platform operator (no membership in any client business) — is denied
  (403, audited)** before any row is read. An unauthenticated request is 401.
- The path `business_id` is the **row scope**: the reader is bound to the verified business and every
  query is confined to it, so one tenant can never read (or page into) another's candidates.

## Visibility — only VISIBLE suggestions are listed

A suggestion is listable only when **all** hold (fail-closed predicate, enforced in SQL):

1. **status is the reviewable `suggested`** — DCA-145 writes only this; a `dismissed` / `accepted` /
   any-other-status row is omitted.
2. **visibility is `research_only`** — the disabled `sendable` future capability is **never surfaced**
   (making a prospect sendable requires recipient consent, which accepting a suggestion is not), so a
   prospect can never silently become a sendable recipient through a read.
3. **not expired** — `expires_at` is unset or in the future. An expired suggestion drops out of the
   list, so the DCA-145 expiry (source/policy invalidation) **affects reads**.

Each row carries only **rights-aware fields** — the subject id plus opaque scoring/targeting/expiry
metadata — and **never a contact value** (none is stored on a research-only organization suggestion).

## Pagination & filters

- `limit` is clamped to `[1, 200]` and `offset` floored at 0 (out-of-range values are rejected 422 at
  the HTTP boundary), so a read can never pull an unbounded or negative page.
- Ordering is deterministic — `match_score` desc, then `created_at` desc, then `id` — so paging is
  stable. `total` is the full count of matching visible suggestions; `hasMore` is derived from it.
- The `runId` and `minScore` filters only ever **narrow** the visible set; they can never widen it past
  the tenant scope or surface a non-visible row.

## Future capabilities stay disabled

Acceptance / dismissal of a suggestion, promotion to anything executable, and the `sendable` visibility
remain disabled future capabilities. This story only READS existing `suggested` research-only rows.
Acceptance is a human action and is **not recipient consent**; no audience/send surface is touched.

## Files changed

- `app/services/prospect_suggestion.py` — added the `ProspectSuggestionReader` (+ `SuggestionPage`,
  `SuggestionListItem`, `SuggestionListFilters`, visibility/pagination constants).
- `app/api/routes/prospect_suggestions.py` — new read-only route (`candidate.read`, path-scoped).
- `app/main.py` — registered the router.
- `tests/test_prospect_suggestions_api.py` — service-layer (SQLite) + HTTP (TestClient) + PG-gated
  tenant-isolation tests.
- `scripts/manual_check.py` — registered the `DCA-150` inspectable scenario.

## Verification

```bash
.venv/Scripts/python.exe -m pytest tests/test_prospect_suggestions_api.py -q
.venv/Scripts/python.exe scripts/manual_check.py DCA-150
```

The manual check prints the actual visible page and `checks: {...}` all `True`
(`only_visible_listed`, `sendable_never_surfaced`, `filters_only_narrow`,
`pagination_bounded_deterministic`, `tenant_isolated`, `read_only_no_state_change`).

The PG-gated test (`test_pg_list_is_tenant_scoped_and_visible_only`) runs against a disposable Postgres
when `DCA_TEST_DATABASE_URL` is set, proving tenant isolation and visible-only filtering over real RLS;
it skips otherwise.
