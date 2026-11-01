# DCA-144 — Resolve one observation batch

Stage: Discovery pipeline in small steps. Status: implemented (ready for manual review).

## What it does

The next step of the still-disabled discovery pipeline after DCA-143 converts a run's DONE tasks into
provenanced observations. It **resolves one discovery run's observation batch** by connecting two
accepted, pure capabilities to the batch:

- the deterministic **DCA-067** identity matcher (`app/matching/org_match.py`), deciding for each batch
  subject whether it is the same as an existing identity in the business; and
- the **DCA-069/070** canonical selection (`app/canonical/field_selection.py` +
  `app/canonical/correction_overlay.py`), computing the canonical value per field through the
  user-correction overlay.

An **ambiguous** match (a deterministic `REVIEW` candidate — a shared name/domain with no confirming
typed identifier) is **routed to an existing DCA-137 `IdentityMatchSuggestion` review proposal**; it is
never merged. A strong `MATCH` is recorded as resolved-to-an-existing-entity, again without merging.
There is no network fetch, model call, spend or send — resolution reads already-collected,
already-permitted observations and concludes.

The batch of one run is exactly the distinct organization subjects whose DCA-143 field observations
carry that run's provenance prefix (`discovery_run:{run_id}/task:…`), so the batch is precisely what the
run converted, never wider; every read is tenant-scoped.

## Implementation

- `app/services/observation_resolution.py` — `ObservationBatchResolver.resolve_run(run_id, actor)`.
  Fail-closed guard order: **authority first** (`run.plan.approve`) → the run exists in this business
  (unknown / cross-tenant = not-found) → idempotent replay (a run resolves once) → the batch is
  non-empty (an empty batch is refused, not fabricated) → resolve. `BatchResolutionActor` /
  `BatchResolutionError` / `BatchResolutionAuthorizationError` / `BatchResolutionResult`.
- `app/db/models.py` — new tenant-owned `ObservationBatchResolution` (RLS), unique `(business_id,
  run_id)`. Holds opaque counts only (`subject_count`, `matched_count`, `ambiguous_count`,
  `distinct_count`, `review_suggestion_count`, `fields_canonicalized`, `corrections_preserved`) +
  provenance invariants (`merged`/`sent`/`spent` = `False`); never a personal/field value.
- `migrations/versions/0063_observation_batch_resolutions.py` — dual-path create + RLS (mirrors 0062).

## Acceptance criteria — how they hold

- **A batch cannot merge consent or overwrite protected corrections.** Resolution writes no
  `ConsentReceipt` / `ProcessingRestriction` and collapses no identities — an ambiguous pair becomes a
  review-only suggestion (the human-authorized DCA-071 merge is the only merge path, and it refuses an
  automation actor). Canonical values are computed through the pure DCA-070 overlay and written nowhere,
  so a protected user correction keeps its value as canonical over any fresher provider observation and
  the stored correction history is untouched (`corrections_preserved`).
- **Reproduce the result and the relevant denial without unintended state changes.** Each refusal
  (unauthorized, not-found, empty batch) is raised before the session is mutated; re-resolving returns
  the existing record and stages no duplicate suggestion and no second audit.

### Note on the MATCH path

A strong agreement needs a shared typed identifier (`cin`/`llpin`/`gstin`), but `org_external_ids` is
`UNIQUE (business_id, scheme, value)` — two distinct in-business identities can never share one
(duplicates are refused at import), so a confirmed within-business `MATCH` is unreachable by
construction. The reachable, acceptance-relevant outcome is an ambiguous shared-name candidate routed to
review. The `MATCH` handling remains as defensive, no-merge code.

## Evidence

- Focused: `pytest tests/test_observation_resolution.py -q` → 6 passed, 1 skipped (Postgres-gated).
- Manual check: `python scripts/manual_check.py DCA-144` → all 6 checks `True`. Printed state: one-subject
  batch resolves with `ambiguous=1`, `reviewSuggestionsRouted=1`, `identityMerges=0`,
  `correctionsPreserved=1` (protected `legal_name` untouched); re-resolve `already_resolved=True` (one
  resolution, one suggestion); Business B refused with `discovery_run_not_found`.
- `ruff` + `mypy` on the new service/test are clean.

Resolution stays strictly within DCA-144: it stages review proposals and the summary record, merges
nothing, promotes no value into a shared/host record, and performs no fetch, model call, spend or send.
