# DCA-145 — Create one scoped prospect suggestion

**Status:** Ready for manual review (uncommitted). **Stop rule honored:** DCA-146 not started.

## What this story does

Convert ONE *eligible* resolved discovery observation into a business-scoped **prospect suggestion** —
a reviewable candidate carrying a deterministic **ICP score + rationale**, the **targeting version** it
was scored against, an **expiry**, and a **visibility constraint**.

This is the next inert step of the still-disabled discovery pipeline, after DCA-144 resolves a run's
converted observation batch:

```
DCA-140 run → DCA-141 approve plan → DCA-142 execute task → DCA-143 convert to observations
            → DCA-144 resolve batch → DCA-145 create ONE scoped suggestion   ← this story
```

Creating a suggestion is **inert**: no network fetch, no model call, no spend, no send, and it is
**never auto-accepted**. Scoring is **deterministic Python** (never the AI gateway).

## Acceptance criteria → how they are met

1. **Research-only organizations are distinguishable from sendable contacts; no automatic audience
   membership.**
   - Every suggestion this pipeline produces is stamped `visibility = "research_only"`
     (`SuggestionVisibility`). The DCA-143 converter records *organization-level facts only* (no contact
     point), so an organization suggestion is research-only *by construction* — it is never a sendable
     recipient.
   - Creating a suggestion writes **no** consent/recipient/audience state (the DCA has no audience or
     send surface). The invariants are recorded opaquely on `provenance`
     (`sendable=False`, `addedToAudience=False`, `autoAccepted=False`, `sent=False`, `spent=False`) and
     **tested**: `ConsentReceipt` count is unchanged, and the row is non-sendable.
   - **Accepting a suggestion is not recipient consent.** A `sendable` suggestion is a disabled future
     capability no path here produces.

2. **Reproduce the result and the relevant denial without unintended state changes.**
   Fail-closed guard order (every refusal is raised **before** the session is mutated):
   1. **Authority FIRST** — caller must hold `Scope.RUN_PLAN_APPROVE` (a business admin); a viewer /
      platform operator is refused (`ProspectSuggestionAuthorizationError`).
   2. **Run exists in THIS business** — else `discovery_run_not_found` (an unknown / cross-tenant run
      reads exactly like not-found).
   3. **Run has been resolved (DCA-144)** — else `discovery_run_not_resolved`.
   4. **Subject is eligible** — a readable `OrgObservation` in this business AND a member of the run's
      resolved batch; else `subject_not_eligible` (unknown / cross-tenant / off-batch → not-found).
   5. **Idempotent** — `(business_id, run_id, subject_id)` is unique, so re-suggesting returns the
      existing row and stages no duplicate and no second audit.

## ICP scoring (deterministic, no AI)

`score_against_icp(icp, canonical_fields, state_code)` is a pure function. It compares the subject's
**canonical field values** (via the DCA-069 selection over DCA-061 field observations) against the run's
**confirmed ICP version** (DCA-040) across the positive targeting dimensions the ICP actually sets:

- **industry** — canonical `industry` field ∈ ICP `industries`
- **geography** — subject `state_code` (or canonical `state`) ∈ ICP `geography`

`match_score` is the equal-weighted fraction of comparable dimensions matched (e.g. industry matched +
geography not → `0.5`); when the ICP sets none of them the score is the honest neutral `0.5`. The
`rationale` names which dimensions matched — dimension codes only, never a contact value.

## Files changed (all uncommitted, DCA repo only)

### New
- `app/services/prospect_suggestion.py` — the single sanctioned writer `ProspectSuggestionCreator`
  (+ `ProspectSuggestionActor` / `ProspectSuggestionResult`, the `score_against_icp` helper, stable
  reason codes, and the typed error classes).
- `migrations/versions/0064_prospect_suggestions.py` — Alembic migration (dual-path create + RLS,
  mirroring 0063). Revision `0064_prospect_suggestions`, down_revision `0063_observation_batch_resolutions`.
- `tests/test_prospect_suggestion.py` — SQLite unit tests + the opt-in Postgres-gated layer
  (`DCA_TEST_DATABASE_URL`, schema `dca_145_test`).
- `docs/DCA-145-create-one-scoped-prospect-suggestion.md` — this file.

### Edited
- `app/db/models.py` — new `SuggestionVisibility` enum + new `ProspectSuggestion(TenantOwned, Base)`
  table (`prospect_suggestions`).
- `scripts/manual_check.py` — `@register("DCA-145", …)` inspectable scenario.
- `tests/test_migrations.py` — added `prospect_suggestions` to `DCA_TABLES` + a `0064` create-branch test.
- `tests/test_rls.py` — added `prospect_suggestions` to the tenant-owned set + a tenant-isolation /
  write-check test pair.

## Why a new table (not a writer over `ProspectCandidate`)

`ProspectCandidate` is the **legacy** `run_discovery` pipeline's output: it FKs the legacy `discovery_runs`
table, inlines `contact` JSON (a sendability assumption), and lacks `version`, `visibility` and the
research-only distinction this story requires. DCA-145 belongs to the **new** DCA-140.. pipeline (over
`DiscoveryRunRequest` + resolved `OrgObservation`s), which is research-only by construction. Reusing
`ProspectCandidate` would mean changing its FK and conflating two pipelines. The accepted siblings
DCA-143 / DCA-144 each added their own tenant-owned table; DCA-145 follows that pattern with
`prospect_suggestions`.

## The `prospect_suggestions` table

Tenant-owned (`business_id` + RLS). Columns: `run_id` (FK `discovery_run_requests.id`), `subject_type`
(`organization`), `subject_id`, `visibility` (`research_only` | disabled `sendable`), `match_score`,
`rationale`, `scored_icp_version` (targeting version), `schema_version`, `status` (`suggested`-only),
`provenance` (invariants), `created_at`, `expires_at`. Unique
`(business_id, run_id, subject_id)` → `uq_prospect_suggestion_subject` (suggested once per run).

## Evidence

- Focused tests: `.venv/Scripts/python.exe -m pytest tests/test_prospect_suggestion.py -q` →
  **8 passed, 1 skipped** (the skip is the Postgres-gated test; set `DCA_TEST_DATABASE_URL` to run it).
- Quality gates (new/edited files): `ruff check` clean, `ruff format` applied,
  `mypy app/services/prospect_suggestion.py` clean, `mypy app/db/models.py` clean.
- Manual check: `.venv/Scripts/python.exe scripts/manual_check.py DCA-145` → all checks `True`:
  ```
  {'a_suggestion_created': True, 'a_scored_with_rationale_version': True, 'a_has_expiry': True,
   'a_research_only_not_sendable': True, 'a_no_audience_membership': True,
   'a_resuggest_idempotent': True, 'a_unauthorized_refused': True, 'b_cross_tenant_not_found': True}
  ```

## Developer manual test

```
.venv/Scripts/python.exe scripts/manual_check.py DCA-145
```

Prints the created suggestion's actual score/rationale/version/expiry/visibility, the research-only /
non-sendable flags, that the consent-receipt (audience) count is **unchanged**, an idempotent re-suggest,
and the two refusals (unauthorized viewer, cross-tenant not-found) — each leaving no state. Local
synthetic only; no network/model/spend/send.

## Boundary confirmation

Python DCA only. No other service's DB touched. No AI capability added. No UI. No `.NET` change. Suggested
only — nothing fetched, sent, spent or auto-accepted; acceptance is not recipient consent.
