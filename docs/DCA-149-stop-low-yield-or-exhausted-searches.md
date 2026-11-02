# DCA-149 — Stop low-yield or exhausted searches

Stage: Discovery pipeline in small steps. Key: `yield-stop`. Source sections: 4, 8.

## What this adds

A durable, inert **no-new-result / marginal-yield stop-and-backoff state per `(business_id, search
partition)`**. After each bounded discovery round the DCA-149 recorder measures that round's YIELD and
advances a small state machine so a market that stops producing NEW results is **visibly paused / backed
off** rather than searched — and spent on — forever, or made to fabricate records.

It invents **no new fetch path**. It composes the already-accepted pipeline concepts (a round's yield is
derivable from a finalized DCA-146 `DiscoveryRunSummary`) and keeps acquisition a disabled future
capability. Recording a round reads already-recorded yield and performs **no fetch, model call, spend or
send** (`provenance` pins `sent`/`spent`/`invented` = False).

## The model

`SearchPartitionBackoff` (table `search_partition_backoffs`, tenant-owned, RLS-protected) — one row per
repeated-search partition of a business, advanced in place (`(business_id, partition_key)` unique):

- `partition_key` — the logical repeated-search unit (opaque, bounded; `partition_key_for(icp_version,
  source_keys)` builds a stable ICP+source-scope fingerprint, but the key is otherwise caller-supplied).
- `state` — `active | backing_off | exhausted` (`SearchPartitionState`).
- `rounds_recorded`, `consecutive_low_yield`, `backoff_level` — the counters the state machine advances.
- `last_new_count` / `last_total_count` / `last_low_yield` / `last_stop_reason` / `last_round_at` — the
  last round's yield and classification.
- `next_eligible_at` — when a `backing_off` partition may search again (NULL while `active`, and NULL when
  `exhausted` — an exhausted market has no scheduled retry).
- `last_run_id`, `schema_version`, `provenance`, `created_at`, `updated_at`.

It holds only aggregate counts, stable reason codes and opaque row references — never a contact value or
a secret.

## The service — `app/services/search_backoff.py`

`DiscoverySearchBackoff(session, business_id, settings)` — tenant-scoped; stages into the caller's session
and never commits.

- **`record_round(partition_key, RoundYield(new_results, total_results), actor, run_id=None, now=None)`**
  classifies the round and advances the state:
  - **Good yield** → resets the streak + backoff, recovers the partition to `active`.
  - **Low yield** (`no_new_results` when `new < search_low_yield_min_new_results`; `marginal_yield` when
    `new/total < search_marginal_yield_min_ratio`) → grows `consecutive_low_yield`; once it crosses
    `search_low_yield_rounds_before_backoff` the partition is `backing_off` for a bounded,
    exponentially-growing window (`search_backoff_base_seconds` doubled each further low-yield round,
    capped at `search_backoff_max_seconds`); once `backoff_level` reaches `search_exhaust_after_backoffs`
    it is `exhausted` (no retry).
  - Stages one atomic DCA-026 audit entry (`discovery_search.backoff.recorded`) in the caller's unit of
    work. `RoundYield.from_run_summary(summary)` derives a round's yield from a finalized run summary
    (NEW = suggestions, TOTAL = resolved subjects).
- **`check_eligibility(partition_key, now=None)`** — a **pure read** (mutates nothing): a never-recorded or
  `active` partition is eligible; a `backing_off` partition is refused until `next_eligible_at` passes
  (`search_backing_off`); an `exhausted` partition is refused with no retry (`search_exhausted`).

### Authority & tenancy

- Recording requires a business administrator holding `run.plan.approve` (the same chargeable-discovery
  authority as the DCA-141..146 chain) — a read-only viewer or a platform operator is refused
  (`SearchBackoffAuthorizationError`, 403) **before any state is read**. An empty partition key is refused
  (`SearchBackoffError`, `invalid_search_partition`) before any state is read.
- Every read/write is confined to the one business; a cross-tenant partition reads as never-recorded.
  Postgres RLS backs the app-level scope (defence-in-depth).

### Acceptance criteria → how they hold

- **An exhausted market is visibly paused / backed off, not repeatedly spent on or invented.** The durable
  state + `next_eligible_at` is what a future (still-disabled) scheduler consults before a round; recording
  stages only an inert state row + its audit entry and does no fetch/model/spend/send.
- **Reproduce the result and the relevant denial without unintended state changes.** Authority and
  partition validity are checked first; each refusal raises before the session is mutated. An eligibility
  check mutates nothing, so both an eligible result and a backing-off/exhausted denial reproduce
  deterministically.

## Config (all validated positive/bounded at boot; env prefix `EFFORTLESSPROMO_DCA_`)

| Setting | Default | Meaning |
|---|---|---|
| `search_low_yield_min_new_results` | `1` | fewer NEW results than this ⇒ `no_new_results` |
| `search_marginal_yield_min_ratio` | `0.1` | NEW/total below this (with some new) ⇒ `marginal_yield` (must be in `(0,1]`) |
| `search_low_yield_rounds_before_backoff` | `2` | consecutive low-yield rounds before backing off |
| `search_backoff_base_seconds` | `3600` | first backoff window (doubled each step) |
| `search_backoff_max_seconds` | `604800` | hard cap on any backoff window |
| `search_exhaust_after_backoffs` | `4` | backoff steps before the partition is `exhausted` |

## Migration

`migrations/versions/0066_search_partition_backoffs.py` (down_revision `0065_discovery_run_summaries`).
Dual-path safe, same guard style as 0049..0065: on a fresh DB the metadata baseline (0001) creates the
table and 0002 applies RLS (it is tenant-owned), so 0066 is a no-op; on a DB that predates the table, 0066
creates it + its tenant-isolation policy + the app-role grant. Downgrade is a deliberate no-op
(expand→contract, BR-201).

## Tests

- `tests/test_search_backoff.py` — in-memory SQLite service tests (classification, active/eligible,
  back-off + within-window refusal, exhaustion + refusal, recovery, `from_run_summary`,
  `partition_key_for`) and the failure/denial cases (unauthorized viewer, empty partition, cross-tenant
  isolation), plus a `DCA_TEST_DATABASE_URL`-gated Postgres test that advances a partition to exhausted
  and refuses it.
- `tests/test_rls.py` — `search_partition_backoffs` added to the tenant-owned set + tenant-isolation and
  foreign-owner write-check tests (Postgres-gated).
- `tests/test_migrations.py` — `search_partition_backoffs` added to `DCA_TABLES` + the 0066 create-branch
  test (Postgres-gated).

## Manual check

```
python scripts/manual_check.py DCA-149
```

Synthetic Businesses A/B, refuses production, scoped cleanup. Prints the actual per-round state (active →
backed off with next-eligible time → exhausted (refused) → recovered), the unauthorized-viewer denial with
no state change, and tenant isolation — ending with `checks: {...}` all `True`. Nothing is fetched, sent,
spent or invented.

## Boundary

Python DCA only. No UI, no AI-service internals, no `.NET` changes. The eligibility gate is exposed as a
structured contract for a later (still-disabled) search scheduler to consult; it is not wired into run
creation here.
