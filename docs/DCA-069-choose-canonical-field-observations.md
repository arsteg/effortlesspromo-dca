# DCA-069 — Choose canonical field observations

Stage: Evidence and identity resolution · key `canonical` · source sections 6, 7.

## Prerequisite

DCA-068 (`app/matching/contact_match.py`) and the earlier sequence are implemented. This builds on the
DCA-061 append-only field-observation history (`app/services/field_observation.py`,
`FieldObservation`), the DCA-062 evidence model, and reuses the DCA-044/045 source catalog + terms and
DCA-059 org observations only as fixtures/provenance. Verified present before starting.

## What this adds

A pure, deterministic, network-free **canonical-selection** layer — the selection sibling of the
DCA-067/068 matchers. For ONE subject + ONE `field_key` it reads the observations recorded about that
field and decides which value is **canonical**, and — crucially — **why**, using **evidence
reliability, verified identity, freshness and rights**, NOT a rigid source-tier rule. It is a
*conclusion only*: it reads only what it is handed, and it **records / mutates / dedups / deletes
nothing**, so every losing value stays untouched in history.

- `app/canonical/field_selection.py` — the selector:
  - `select_canonical(candidates, *, now=None) -> CanonicalSelection` (one field_key) and
    `select_canonical_by_field(candidates, *, now=None) -> dict[str, CanonicalSelection]`.
  - `CanonicalCandidate` (a value object) + `candidates_from_observations(observations, *,
    verified_observation_ids=(), source_rights_in_force=None)` — the read-only adapter over stored
    `FieldObservation` rows (caller supplies the tenant-scoped observations, so scope never widens).
  - `CanonicalSelection` (winner + `winner_reasons` + per-candidate `verdicts` + `preserved_observation_ids`
    + `mutated_state=False`), `CandidateVerdict`, `Reliability` (`verified`/`observed`/`inferred`).
  - Stable reason codes — winners (`REASON_*`) and rights denials (`DENIAL_*`).
- `app/canonical/__init__.py` — the package surface.

No DB table, no migration, no HTTP route, no AI capability, no acquisition/send/promote/merge path.
**No .NET / UI / AI-service changes. No new dependency.**

## The selection model (why it is not a rigid source-tier rule)

A rigid tier rule (official > commercial > web) would let a **stale official registry** value win on
provenance alone, even when a newer, independently **verified** fact exists. This ranks by *evidence
quality* instead, with a hard **rights gate** on top:

| Dimension | Role |
|---|---|
| **Reliability** — `verified` > `observed` > `inferred` | Primary. A verified fact outranks a merely observed (registry) one, which outranks an inferred one. |
| **Freshness** — `observed_at` | Decides **within** a reliability tier (freshest wins); never lets a lower tier overtake a higher one. |
| **Rights gate** — basis / retention / source-in-force | A value with no rights basis, a lapsed retention window, or source terms no longer in force is **ineligible** (a denial), however fresh/reliable. It stays in history; it just cannot win. |
| **Tiebreaks** | source-in-force, then locally recorded time, then a stable observation-id — so the choice is fully reproducible. |

Deliberately **not** a ranking input: the source's *kind* / *trust tier*. Provenance is carried for the
explanation only.

### Acceptance mapping

- **Stale registry data cannot blindly replace a newer verified fact; losing values remain in
  history.** Reliability is primary, so a newer `verified` value is chosen over a stale `observed`
  registry value; `winner_reasons` records `higher_reliability` + `verified_over_stale_registry`. The
  losing registry value — and every other non-winner — is listed in `preserved_observation_ids` and is
  never deleted/mutated (the selector is pure).
- **Reproduce the result and relevant denial without unintended state changes or later-story work.**
  The selection is a pure function of its inputs at a given `now`; the "relevant denial" is a
  rights-ineligible candidate (e.g. a lapsed retention window → `retention_lapsed`) that is excluded
  from winning yet preserved. Both are perfectly reproducible with no state to change. Promoting a
  canonical value into any shared/host record stays a disabled future capability.

## Tests

`tests/test_canonical_field_selection.py`:

- **Stale registry vs newer verified** — the verified fact wins (order-independent), reasons record it,
  loser preserved.
- **Freshness within a tier**, **inferred-fresh never outranks observed-stale** (reliability primary).
- **Rights gate** — lapsed retention / out-of-force source terms / missing basis are denied with stable
  codes; a lesser eligible value wins when the best is denied; all values stay preserved.
- **Reproducibility**, field-key grouping, and mixed-field-key programming-error guard.
- **Read-only adapter** over stored observations on in-memory SQLite (verified-id mapping; source-rights
  predicate).
- **Durable path on disposable PostgreSQL** (`DCA_TEST_DATABASE_URL`, skipped when unset) — records a
  stale + a newer-verified observation over a real transaction, selects, and asserts the verified fact
  wins while **both** observations remain in history (nothing deleted). Never SQLite for the durable
  path.

## Manual check

```
python scripts/manual_check.py DCA-069
```

Records four conflicting fixture values of one field (stale official-registry `observed`, newer
`verified`, fresh `inferred`, retention-`lapsed`) into synthetic Business A, runs the pure selector,
and prints each candidate's verdict, the winner + reasons, the preserved losers, and the row count
(unchanged). Local synthetic only; no AI/paid/network path; the ephemeral DB is cleaned up.
