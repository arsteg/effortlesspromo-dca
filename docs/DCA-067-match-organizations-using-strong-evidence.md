# DCA-067 — Match organizations using strong evidence

Stage: Evidence and identity resolution · key `org-match` · source sections 6, 7.

## Prerequisite

DCA-066 (and the earlier sequence) is implemented: `app/normalization/phones.py`,
`org_identifiers.py` (DCA-065), `names.py` / `domains.py` (DCA-064) and the DCA-059
`OrgObservationStore` / `OrgObservation` + `OrgExternalId` are all present and were used as building
blocks here.

## What this adds

A pure, deterministic, network-free **identity-resolution decision** — the layer the DCA-064/065/066
normalizers each deliberately deferred ("resolving identity stays a later capability"). It compares two
bundles of already-normalized organization evidence and decides whether they are the **same**
organization using **strong evidence first**. It records nothing, mutates nothing, and **never merges**
two observations — a `MATCH` is an auditable conclusion, not an automatic collapse.

- `app/matching/org_match.py` — the matcher:
  - `match_organizations(a, b) -> OrgMatchResult`
  - `OrgMatchSubject` + `build_subject(...)` (from raw strings, via the existing normalizers) +
    `subject_from_observation(store, observation)` (read-only, tenant-scoped, from a stored DCA-059
    observation)
  - `MatchOutcome` (`match` / `no_match` / `review` / `insufficient`), `Confidence`
    (`high` / `medium` / `low` / `none`)
  - `MatchSignal` / `SignalKind` / `SignalStrength` / `SignalRelation`, and `OrgMatchDenial`
- `app/matching/__init__.py` — the package surface.

No DB table, no migration, no HTTP route, no AI capability, no acquisition/send/merge path. **No .NET /
UI / AI-service changes. No new dependency.** It reuses `app/normalization/*` (names/domains/typed
identifiers) and `app/geography/*` (India + location evidence).

## The evidence hierarchy

| Evidence | Strength | Effect |
|---|---|---|
| Same `cin` / `llpin` (legal-entity identifier) | strong | `MATCH` — same legal entity |
| Same `gstin` (establishment identifier) | strong | `MATCH` — same establishment |
| Different `cin` / `llpin` of the same scheme | strong | `NO_MATCH` — different registered companies (overrides any name/domain) |
| Different `gstin`s | strong | `DISTINCT` establishments — not a conflict, but never collapsed on name/domain |
| Shared company name (DCA-064 `match_key`) | weak | `REVIEW` candidate only |
| Shared corporate domain (DCA-064 host) | weak | `REVIEW` candidate only |
| Shared / conflicting location (state/UT, country) | weak | corroborates or contradicts a candidate; never decides alone |

`REVIEW` confidence: one weak anchor → `low`; name **and** domain → `medium`; a corroborating location
bumps `low`→`medium` (capped at `medium` — never promoted to a match on weak evidence); a conflicting
location knocks it back to `low`. A lone location signal (no name/domain anchor) is too weak to propose
a candidate → `INSUFFICIENT`.

Only `cin` / `llpin` / `gstin` participate as strong identifiers (the schemes DCA-065 can structurally
validate); other stored DCA-059 schemes (`pan` / `lei` / `tan` / `duns` / `platform_org_id`) are not
treated as proof here.

## Acceptance criteria → evidence

- **Different branches/groups are not collapsed from a shared name/domain alone.** A shared domain
  alone, or a shared name alone, is a `REVIEW` candidate — never a `MATCH`
  (`test_shared_domain_alone_is_a_review_candidate_not_a_match`,
  `test_shared_name_alone_is_a_review_candidate_not_a_match`). Two establishments of one group with the
  same name **and** the same corporate domain but different GSTINs stay a review candidate with a
  recorded `DISTINCT` establishment signal, never a match
  (`test_distinct_establishments_with_shared_name_and_domain_are_not_collapsed`). A conflicting
  legal-entity identifier yields `NO_MATCH` even when name **and** domain agree
  (`test_conflicting_legal_entity_identifier_overrides_name_and_domain`). Only a compatible typed
  identifier confirms a `MATCH` (`test_same_legal_entity_identifier_is_a_match`,
  `test_same_establishment_identifier_is_a_match`, `test_llpin_agreement_is_a_match`). Location
  corroborates/contradicts but never flips the outcome
  (`test_location_corroboration_promotes_low_to_medium`,
  `test_location_conflict_keeps_candidate_low_but_does_not_flip_outcome`,
  `test_location_alone_is_too_weak_to_propose_a_candidate`).

- **Reproduce the result and relevant denial without unintended state changes or later-story work.**
  The matcher is pure: the same inputs give the identical, symmetric result every time, and the
  relevant denial — `NO_MATCH` on a conflicting identifier, and the refusal to collapse branches to a
  mere `REVIEW` — is perfectly reproducible with no state to change
  (`test_result_is_reproducible_and_symmetric`). `auto_merged` is always `False`
  (asserted throughout). A malformed input is collected as an `OrgMatchDenial` and never fabricates a
  match (`test_build_subject_collects_denials_without_raising`,
  `test_malformed_identifier_does_not_fabricate_a_match`). The read-only observation adapter stays
  tenant-scoped (`test_subject_from_observation_matches_via_each_tenants_own_store`,
  `test_observation_adapter_reads_state_from_the_row`).

## Tests / quality

- `tests/test_org_match.py` — 18 focused tests (strong-identifier match, conflicting-identifier
  no-match, weak-only review, distinct-establishment non-collapse, location corroboration/conflict,
  insufficient, denial collection, reproducibility/symmetry, and the read-only observation adapter).
- The matcher is pure Python — there is no DB / RLS / transaction in the decision, so no PostgreSQL
  fixture is required for it (the disposable-Postgres rule applies to DB-touching behavior; the
  observation adapter's underlying `org_observations` / `org_external_ids` transaction + RLS path is
  already covered on disposable Postgres by `tests/test_org_observation.py` and `tests/test_rls.py`).
- Full suite re-run green: `917 passed, 60 skipped` (the skips are the Postgres-Docker-gated
  async/RLS suites; `DCA_TEST_DATABASE_URL` unset locally).
- Gate on the touched files: `ruff check app/matching tests/test_org_match.py scripts/manual_check.py`,
  `ruff format --check …`, `mypy app/matching` all green. (`mypy` over the whole repo surfaces 16
  **pre-existing** errors in `app/compliance/policy_rulepacks.py` and earlier `scripts/manual_check.py`
  scenarios — none in this story's code.)

## Manual check

```text
python scripts/manual_check.py DCA-067
```

Runs exact-ID, shared-domain and similar-name fixtures through the matcher and prints each pair's
outcome, confidence and contributing signals: a compatible typed identifier confirms a `MATCH`; a
conflicting legal-entity identifier yields `NO_MATCH` even with matching name + domain; a shared
name/domain alone is only a `REVIEW` candidate; two different GSTINs of one group stay distinct; and the
result + the relevant denial reproduce with no state change. Local synthetic only; no AI/paid/network/DB
write, and nothing is ever merged.
