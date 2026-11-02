# DCA-152 — Preview an audience selection

Stage: User selection and EffortlessPromo sync · key `selection-preview`

## What this adds

A **tenant-scoped, read-only, non-mutating projection** over the DCA-145 `ProspectSuggestion` rows,
building on the DCA-150 candidate list: one authenticated HTTP capability (for a later Web/Admin
consumer) that answers *if I built an audience from THIS selection, what would it contain?* for one
business — how many records, how many are **activatable** vs merely **research-only**, which are
**excluded and why** (freshness / dismissed / not-reviewable / research-only rights / not-found), and
the **would-be cost** — **without creating any audience membership or incurring any billing**.

A preview is a pure read: it reads the business's own suggestion rows and **writes nothing, spends
nothing, sends nothing, and creates no audience membership**, so the result reproduces with no state
change.

**No new table, migration or RLS change** — the `prospect_suggestions` table (DCA-145) already exists
and is already RLS-protected; this is a pure read over it, classified with the same deterministic
visibility deciders the DCA-150 list uses (`VISIBLE_STATUS` / `DISMISSED_STATUS` /
`SuggestionVisibility` / expiry).

## The endpoint

```
POST /v1/tenants/{business_id}/audience-selection/preview
```

Body (POST only because the selection travels in the body; strictly non-mutating):

```json
{
  "suggestionIds": ["<id>", "..."],   // selected subset (from the DCA-150 candidate list), max 500
  "intendedUse": "Q4 outreach",        // optional, opaque, echoed back (never qualifies the result)
  "channel": "email"                   // optional, opaque
}
```

Response:

```json
{
  "businessId": "...", "generatedAt": "2026-11-02T12:00:00+00:00",
  "intendedUse": "Q4 outreach", "channel": "email",
  "selected": 6, "found": 4, "notFound": 2,
  "activatable": 1, "researchOnly": 1,
  "excluded": { "research_only": 1, "expired": 1, "dismissed": 1, "not_found": 2 },
  "estimatedChargeableContacts": 1,
  "billingCommitted": false, "membershipCreated": false,
  "note": "A preview is a read-only projection: it creates NO audience membership ...",
  "lines": [
    {
      "suggestionId": "...", "found": true, "activatable": false,
      "exclusionReason": "research_only", "subjectType": "organization", "subjectId": "...",
      "visibility": "research_only", "sendable": false, "status": "suggested",
      "matchScore": 0.6, "fresh": true, "expiresAt": "..."
    }
  ]
}
```

## Authorization & tenancy

- **End-user permission model (DCA-019), not the service-auth gate** — identical to the sibling DCA-150
  candidate list. Previewing is an ordinary-business-user review action, so it requires the
  **`candidate.read`** scope on a membership-verified tenant context. A viewer, an acting user and an
  administrator all hold it; a **non-member — including a platform operator (no membership in any client
  business) — is denied (403, audited)** before any row is read. An unauthenticated request is 401.
- The path `business_id` is the **row scope**: the previewer is bound to the verified business and every
  query is confined to it, so one tenant can never preview another's candidates. An unknown or
  cross-tenant suggestion id is simply absent from the scoped read and reported as a `not_found` line —
  **never an oracle for another tenant's row, never leaked**.

## Classification — research-only is separated from activatable

Each selected id lands in exactly one bucket (first match wins, deterministic):

1. `not_found` — unknown / cross-tenant / nonexistent id (the scoped read missed it).
2. `dismissed` — privately dismissed (DCA-151); no longer a reviewable candidate.
3. `not_reviewable` — some other non-`suggested` status (a suggestion never self-advances).
4. `expired` — **freshness**: past its expiry window; must be refreshed before use.
5. `research_only` — **rights**: a research-only organization carries no sendable contact and no
   recipient consent, so it can never be a message recipient — the key research-only vs activatable
   separation. Every suggestion the discovery pipeline produces today is this, so the **activatable set
   is empty in practice**.
6. _activatable_ — a visible, unexpired, reviewable **`sendable`** suggestion. `sendable` is a disabled
   future capability no path produces today; the classifier routes one here so the separation is
   explicit, but previewing it still creates **no membership, no send and no charge**.

Each line carries only **rights-aware fields** — the subject id plus opaque scoring/visibility/expiry
metadata — and **never a contact value** (none is stored on a research-only organization suggestion).

## Cost & non-mutation

- `estimatedChargeableContacts` equals the activatable count — a **would-be figure only**.
  `billingCommitted` and `membershipCreated` are **always `false`**.
- The selection is de-duplicated (order-preserving) and capped at **500** (`MAX_SELECTION_SIZE`); the
  HTTP boundary also rejects a longer body (422) and an unsupported field (`extra="forbid"` → 422).
- The preview **writes nothing** — no suggestion row, no audit entry, no audience/membership (no such
  table exists). Acting on the preview — building the audience, sending, or spending — stays a
  **disabled future capability**.

## Files changed

- `app/services/audience_selection_preview.py` — new `AudienceSelectionPreviewer` (+
  `AudienceSelectionPreview`, `AudiencePreviewLine`, exclusion-reason + cap constants).
- `app/api/routes/audience_preview.py` — new read-only, path-scoped route (`candidate.read`).
- `app/main.py` — registered the router.
- `tests/test_audience_selection_preview.py` — service-layer (SQLite) + HTTP (TestClient) + PG-gated
  tenant-isolation tests.
- `scripts/manual_check.py` — registered the `DCA-152` inspectable scenario.
- `docs/DCA-152-preview-an-audience-selection.md` — this document.

## Verification

```bash
.venv/Scripts/python.exe -m pytest tests/test_audience_selection_preview.py -q
.venv/Scripts/python.exe scripts/manual_check.py DCA-152
```

The manual check prints the actual projection and `checks: {...}` all `True`
(`research_only_separated_from_activatable`, `exclusions_tallied_by_reason`,
`cross_tenant_id_not_found_not_leaked`, `no_billing_or_membership_committed`,
`non_mutating_rows_and_audit_unchanged`), and shows the suggestion-row + audit counts unchanged.

The PG-gated test (`test_pg_preview_is_tenant_scoped`) runs against a disposable Postgres when
`DCA_TEST_DATABASE_URL` is set, proving tenant isolation over real RLS; it skips otherwise.
