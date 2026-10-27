# DCA-043 — Mark recommendations stale after targeting changes

Stage: Business profile and Indian targeting · key: `profile-invalidate`

## What this adds

When a business **confirms a new Ideal-Customer-Profile version** (DCA-040 — a confirmed targeting
revision change), the match scores the agent computed against the previous targeting no longer reflect
what the business is looking for. This story marks those **affected recommendation scores stale** so a
future (disabled) rescore capability can recompute them. It recomputes nothing — it only *flags*.

Two new columns on `prospect_candidates` (the recommendation table):

- **`score_status`** — `current` | `stale` (`NOT NULL`, defaults to `current`). Flipped to `stale` when
  a confirmed targeting change invalidates the score. Kept strictly separate from the existing
  `status` column (the recommendation's **acceptance/approval**).
- **`scored_targeting_version`** — the confirmed ICP version the score was computed against (nullable;
  `NULL` for a placeholder score produced before any targeting was confirmed — discovery's default).

### The invariants (acceptance criteria)

- **Only this business's affected scores are invalidated.** Every query is `business_id`-scoped (RLS
  backs it on Postgres), so another tenant's recommendations are never touched. *Affected* = a **live**
  recommendation (`suggested`/`accepted`) whose score is still `current` **and** whose
  `scored_targeting_version` differs from the newly-confirmed version. An already-`stale` score is not
  re-marked (idempotent); a dismissed/expired recommendation is not live and is skipped.
- **Approval / send permission is untouched.** Invalidation flips `score_status` only. It never changes
  a recommendation's `status` — an **accepted** recommendation stays accepted, its score merely flagged
  — and the DCA has no send path, so marking a score stale grants/revokes **no** sending authority.
  (Acceptance is a DCA-local status, not recipient consent.)
- **A real change is audited; a no-op stages nothing.** When ≥1 score is invalidated, a single atomic
  DCA-026 audit entry (`recommendation.scores.invalidated`, opaque references only) is staged in the
  **same unit of work** as the flipped rows. A confirmation that invalidates nothing writes no audit
  row and mutates nothing — so the DCA-040 single-audit guarantee is preserved.

## Components touched (DCA only)

- **Model**: `app/db/models.py` → `ScoreStatus` enum + `score_status` / `scored_targeting_version` on
  `ProspectCandidate`.
- **Service**: `app/services/score_invalidation.py` → `ScoreInvalidator` (the one write path; stages
  into the caller's session, never commits).
- **Wiring**: `app/services/icp_confirmation.py` → `IcpConfirmationStore.confirm` invalidates affected
  scores when the confirmed version actually changes (same unit of work); exposes
  `invalidated_score_count` on the returned row.
- **API**: `app/api/routes/icp.py` (confirm response now reports `invalidatedScoreCount`) and
  `app/api/routes/tenants.py` (the `/candidates` view now reports `scoreStatus` /
  `scoredTargetingVersion`).
- **Migration**: `migrations/versions/0015_prospect_score_staleness.py` (add-column, dual-path, mirrors
  the 0011 provenance-column migration).

## HTTP contract (for later Web/Admin consumers)

No new routes — the capability is driven by the existing DCA-040 confirm route and inspected through the
existing candidate-list route (both X-Service-Auth-gated):

| Method | Path | Effect |
|---|---|---|
| POST | `/v1/tenants/{business_id}/icp/confirm` | On a targeting **change**, marks affected scores stale in the same UoW. Response adds `invalidatedScoreCount` (0 when not a change / nothing affected). `grantsCampaignAuthority` stays `false`. |
| GET | `/v1/tenants/{business_id}/candidates` | Each row now carries `scoreStatus` (`current`/`stale`) and `scoredTargetingVersion`. `status` (approval) is unchanged by invalidation. |

## Tests

- `tests/test_score_invalidation.py` — service unit tests (SQLite): affected scores flip + single
  audit, accepted recommendation keeps approval while its score goes stale, terminal recommendations
  and another tenant untouched, already-stale no-op (no audit), score-already-at-new-version skipped,
  stages-not-commits. Confirm-integration tests: a new version invalidates (second audit), re-confirming
  the same version invalidates nothing, a candidate-less confirm writes only the `icp.confirm` audit.
  HTTP tests: discover → confirm → A's scores stale with approval preserved, a denied confirm (404)
  changes nothing, B's scores stay current.
- `tests/test_migrations.py` — dual-path `0015` add-column test (disposable PostgreSQL; skipped without
  `DCA_TEST_DATABASE_URL`): drops the columns, seeds a legacy row, re-runs `0015`, asserts the columns
  are re-added and the legacy row backfilled to `('current', None)`.
- `tests/test_manual_check.py` — asserts the registered `DCA-043` scenario.

## Developer manual check

```text
python scripts/manual_check.py DCA-043
```

Runs an inspectable A/B scenario (local synthetic SQLite, AI disabled): A discovers recommendations
(all `current`) and accepts one; a denied confirm (unknown version → 404) invalidates nothing; confirming
a real ICP draft version flips A's affected scores to `stale` while every recommendation keeps its
`status` (the accepted one stays accepted) and no sending authority is granted; and B's scores stay
`current` (tenant isolation). Prints real responses/rows — not a bare PASS.

For disposable-Postgres RLS/migration behaviour:

```text
docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55432:5432 postgres:16-alpine
export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55432/dca"
pytest tests/test_rls.py tests/test_migrations.py
```
