# DCA-148 — Route missing fields through a provider waterfall

Stage: Discovery pipeline in small steps · Source sections 4, 8 · key `waterfall`

## What this story adds

A single new service, `app/services/provider_waterfall.py`
(`ProviderWaterfallPlanner`), that decides — **per missing field** — the ordered
fallback of providers to try, and **stops as soon as the field's requirement is
met**. It is the field-level analogue of
`app.services.discovery_planner.resolve_allowed_sources`: a **pure, side-effect-free
planning decision**. It fetches nothing, stages nothing and commits nothing, so a
route (and its denial) is perfectly reproducible with no state change.

It is **not** a second gateway or a fetch path. It composes the already-accepted
seams and performs no provider/model/paid work:

- **rights** — the business must still be approved + entitled to `COLLECT` the field
  from a source for the purpose, decided only through the sanctioned DCA-048
  `SourcePreflight` gate (catalogued + ENABLED source + platform ceiling + business
  entitlement, evaluated at `now`). A denied provider is recorded
  (`denied_rights`, carrying the preflight reason) and is never selected.
- **coverage** — the field must be one the catalog source actually offers
  (`Source.fields`, DCA-044). A source that does not offer the field is not a
  candidate for it (`no_coverage`).
- **freshness** — a provider whose data is older than the field requirement's
  `max_age_days` is too stale to satisfy it (`stale`).
- **marginal cost** — the eligible providers are ordered **cheapest-first** (ties
  broken by freshest, then by source key) so the waterfall spends the least to meet
  the requirement.

### The two acceptance guarantees

- **No redundant next-provider call after success.** The walk stops the instant a
  field's requirement is met; every cheaper-ranked-after provider is recorded
  `not_reached` and is never selected. `FieldRoute.selected` /
  `FieldRoute.calls_planned` are exactly the providers the requirement needed.
- **Shared upstream sources do not count as independent corroboration.**
  Corroboration is counted by **distinct upstream origin** (`ProviderCandidate.upstream_key`,
  defaulting to the provider's own source key). A provider whose upstream is already
  represented is recorded `skipped_shared_upstream` and does not advance the
  independent-source count — two resellers of one registry count as one.

### Keeping future capabilities disabled

There is no fetch, no source-enable, and no spend anywhere in this service. In the
running service every source is a disabled DCA-044 draft, so the preflight denies
every provider and the waterfall routes **nothing** — acquisition stays a disabled
future capability. The test/manual-check happy paths reach the allow branch only by
a **test-only synthetic enable** of the catalog source (exactly as `tests/test_source_preflight.py` does).

## Public shape (`app/services/provider_waterfall.py`)

- `FieldRequirement(field, min_independent_sources=1, max_age_days=None)` — one
  missing field + its corroboration bar (distinct upstreams required, clamped to
  ≥ 1) and freshness bar.
- `ProviderCandidate(source_key, age_days=0, marginal_cost_minor=0, upstream_key="")`
  — a declarative provider descriptor (synthetic / caller-supplied; nothing here
  authorizes anything — the rights chain still decides). `origin` = the shared
  upstream, else the source key.
- `FieldConsideration(source_key, outcome, rank, origin, age_days, marginal_cost_minor, reason)`
  — the auditable per-candidate record. `outcome` ∈ `selected`,
  `skipped_shared_upstream`, `not_reached`, `denied_rights`, `no_coverage`, `stale`.
- `FieldRoute(field, satisfied, required_independent_sources, independent_sources,
  selected, considered, reason)` with helpers `calls_planned`,
  `providers_not_reached`.
- `WaterfallResult(business_id, purpose, routes)` with `fully_satisfied` and
  `route_for(field)`.
- `ProviderWaterfallPlanner(session, business_id).route_fields(requirements,
  providers, *, purpose=DEFAULT_PURPOSE, now=None) -> WaterfallResult` — tenant-scoped,
  pure.

Route-level denial reasons (reproducible, no state change): `no_candidate_provider`
(nothing covers the field), `no_entitled_provider` (covering providers exist but all
rights-denied), `all_candidates_stale`, `insufficient_independent_corroboration`.

`DEFAULT_PURPOSE` is `discovery_planner.PLAN_PURPOSE` (`prospect_discovery`), so
routing is bound to the same purpose-limited grant the discovery plan runs under.

## Why no migration / schema change

DCA-148 persists nothing — it is a pure read over existing catalog + entitlement
rows plus caller-supplied synthetic provider descriptors. The acceptance criterion
"reproduce the result and relevant denial without unintended state changes" is met
by construction. There is therefore **no new table**, **no Alembic migration**
(latest committed stays `0065_discovery_run_summaries`), and **no RLS /
`test_migrations` / `test_rls` change**. Tenant isolation is inherited from the
DCA-048 preflight, which is already bound to one verified business scope.

## Tests

`tests/test_provider_waterfall.py` (in-memory SQLite — the planner has no
locking/RLS/transaction to exercise, matching every sibling discovery-service unit
suite). Reuses DCA-048's catalogue → approve → (synthetic) enable → entitle setup.
Covers:

- first-provider success stops the waterfall (pricier provider `not_reached`, one
  call planned);
- routing is pure and reproducible (identical result, nothing staged);
- shared upstream is not independent corroboration (2-source requirement met by two
  distinct origins, the duplicate skipped);
- shared-upstream-only candidates cannot meet a corroboration requirement
  (`insufficient_independent_corroboration`);
- the denial: the only covering provider is rights-denied → unroutable,
  `no_entitled_provider`, no state change;
- no covering source (`no_candidate_provider`); all candidates too stale
  (`all_candidates_stale`); freshness bar prefers a fresh provider over a stale
  cheaper one;
- tenant isolation (Business B, unentitled, is denied the field A can route).

## Manual check

```
python scripts/manual_check.py DCA-148
```

Synthetic Businesses A/B on a disposable DB (auto-cleaned; refuses production unless
`--allow-production`). Prints the actual routes for: first-provider success (the
pricier provider not reached), a 2-independent-source field (shared upstream
skipped), a shared-upstream-only field (insufficient corroboration), the
rights-denied field (unroutable, no state change), and Business B denied the same
field. `checks: {...}` are all `True`. Nothing is fetched, sent or spent.

## Boundary confirmation

Work stayed entirely inside `apps/effortlesspromo-dca` (Python DCA): one new service,
one new test module, one new `manual_check` scenario, this doc. No AI-service, Web,
Admin or other-repo changes; no new AI capability; no live source/adapter; no
migration. Left uncommitted.
