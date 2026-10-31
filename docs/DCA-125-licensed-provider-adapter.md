# DCA-125 — Implement one licensed provider adapter

Stage: Approved source access · Key: `licensed-adapter` · Source sections 6, 8, 9

## What this adds

The third concrete source adapter behind the DCA-122 contract: an **organization-only** adapter that
enriches an organization observation a business already holds with company fields fetched from **one
paid/licensed B2B data provider**. Unlike the first two adapters (DCA-123 dataset import, DCA-124 website
extract — both free), this adapter's data comes from a **metered, chargeable** provider, so the
load-bearing addition is that the call flows through the cost machinery:

1. **Rights FIRST (DCA-048).** The fetch proceeds only when the source is approved AND the business is
   entitled to `collect` for the purpose. A rights denial reserves no budget, makes **no provider call**,
   and writes nothing.
2. **Reserve budget before the call (DCA-113).** The conservative up-front estimate is reserved against
   the business's caps. An over-budget request is **refused before the call** — no provider call, no
   spend, no state change.
3. **Record the provider-call lifecycle (DCA-115).** `PENDING → IN_FLIGHT → {SUCCEEDED | FAILED |
   UNCERTAIN}`, linked to the reservation.
4. **Reconcile the hold (DCA-114).** SUCCEEDED → **settle** at the reported actual cost (unused headroom
   released, any over-run clamped to the hold and flagged); FAILED → **release** the whole hold;
   UNCERTAIN → **preserve** the full hold (counts in full) awaiting the DCA-116 resolver.
5. **Record returned data as provenanced observations (DCA-061).** Each permitted company field becomes
   an observed `FieldObservation` citing the provider-call provenance. A personal/contact field the
   provider returns is **DROPPED** (counted, never recorded) — the organization-only guard (DCA-122).

## The live/paid call stays DISABLED

**No real/paid provider call is ever made.** The provider is reached only through the
`LicensedProviderClient` Protocol (`app/sources/licensed_provider_contract.py`). The **only**
implementation is the synthetic fake (`app/sources/licensed_provider_fake.py`), which performs no
network call, holds no provider SDK/key, and spends nothing. `build_licensed_provider_client(settings)`
**refuses in production** — the live HTTP adapter to a real provider is a later story (with its own
credential, rulepack and rights review), so the live egress stays a disabled future capability. This
mirrors the DCA-014 identity and DCA-096 delivery contract+fake pattern exactly.

## Files

| File | Change |
|---|---|
| `app/sources/licensed_provider_contract.py` | NEW — typed shapes + `LicensedProviderClient` Protocol + `LicensedProviderOutcome` |
| `app/sources/licensed_provider_fake.py` | NEW — `SyntheticLicensedProviderClient` + `build_licensed_provider_client` (refuses in prod) |
| `app/sources/licensed_provider.py` | NEW — `LicensedProviderAdapter` (the orchestrator) + capability descriptor, request/result, actor, errors |
| `app/db/models.py` | NEW `LicensedProviderFetch` run-level provenance table (tenant-owned, RLS) |
| `migrations/versions/0051_licensed_provider_fetches.py` | NEW — dual-path create + RLS, after 0050 |
| `tests/test_licensed_provider.py` | NEW — focused suite (SQLite unit + Postgres-gated path) |
| `tests/test_rls.py`, `tests/test_migrations.py` | add `licensed_provider_fetches` + the 0051 create-branch test |
| `scripts/manual_check.py` | register `DCA-125` |

## Provenance + idempotency

`LicensedProviderFetch` is the run-level source attribution: one row per logical fetch, recording the
subject observation, source/version, provider, `reservation_id`, `provider_call_id`, the provider
`outcome` + `call_status`, counts of fields recorded vs contact fragments dropped, the bounded spend
(`reserved_minor` / `settled_minor`) and a `needs_review` flag for an UNCERTAIN outcome.
`(business_id, source_key, subject_id, fetch_key)` is unique, so a re-fetch is a deterministic no-op — no
second reservation, no second provider call, no duplicate facts.

## Authority & tenancy

A paid fetch reserves and reconciles spend, so it requires a business administrator holding
`budget.manage` (an ordinary user / viewer / platform operator cannot). Every query is business-scoped;
a subject owned by another business reads as not-found (never a cross-tenant link). The adapter stages
into the caller's session and never commits — observations, ledger rows, their atomic audit and the
provenance row all commit together.

## Verification

- Focused suite (SQLite unit + Postgres-gated):
  `.venv/Scripts/python.exe -m pytest tests/test_licensed_provider.py -q`
- RLS / migration coverage for the new table: `tests/test_rls.py`, `tests/test_migrations.py` (the
  Postgres-gated paths run only with `DCA_TEST_DATABASE_URL` set).
- Inspectable scenario (real rows, no network, no paid call):
  `.venv/Scripts/python.exe scripts/manual_check.py DCA-125`

## Scope

Only this story. No live/paid provider call; no new source enabled in the running service (ENABLED stays
a disabled future capability — a real source gets enabled only via its own rulepack + rights-review
story); no execution/send path. Future capabilities stay disabled.
