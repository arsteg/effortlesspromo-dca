# DCA-127 — Cache only entitled, fresh source results

Stage 11 (Approved source access). Key: `source-cache`. Source sections 6, 8, 9.

## What this adds

A tenant-scoped **cache for source-fetch results** that is safe by construction: a cache **hit** can
never become a shortcut around the rights chain, the clock, or tenancy. It caches/serves only results the
requesting business is **entitled** to, only while they are still **fresh**, and only for the **current
in-force terms version** — so a hit never leaks data across an entitlement boundary and never serves a
stale/expired result.

The cache **stores and serves PROVIDED results only**; it performs **no fetch**. Paid/live acquisition
(and the live source adapters) stay disabled future capabilities — this story only records and returns
results a caller already holds.

### Components

- `app/services/source_result_cache.py` — `SourceResultCache` (the single sanctioned reader/writer),
  `CacheResultInput`, `CacheLookup`, `SourceResultCacheError`.
- `app/db/models.py` — `SourceResultCacheEntry` (table `source_result_cache`, `TenantOwned` → RLS).
- `migrations/versions/0053_source_result_cache.py` — dual-path create + RLS (no-op on a fresh DB; builds
  the table + tenant-isolation policy on a DB migrated before it existed), mirroring `0052`.
- `app/core/config.py` — three bounded freshness settings (validated positive at boot).

## The load-bearing guarantees (acceptance criteria)

1. **A hit cannot exceed current entitlement or survive source revocation.** Entitlement is rechecked on
   the **READ**, never trusted from the write. `SourceResultCache.get` re-runs the DCA-048
   `SourcePreflight` (source approved + ENABLED, in-force platform ceiling at `now`, the business's
   enabled entitlement, the purpose). A lost entitlement or a revoked/expired approval ⇒ **MISS**
   (`phase="rights"`), never another result. The entry is pinned to the terms `source_version` it was
   cached under; a read resolves the **current in-force** terms version and only an entry under that
   version is eligible, so a newer approved version makes an old entry unreachable (`not_cached`).
2. **A stale/expired entry is a miss, never served; negative results expire.** `fresh_until` is derived
   from the source's cited terms (its retention/freshness window), bounded by a configured maximum. A read
   at/after `fresh_until` is a **MISS** (`stale_or_expired`). A **negative** (no-result) entry carries a
   bounded negative TTL and expires the same way.
3. **Writes respect retention (no-retention ⇒ only a ref/digest).** The retention mode is derived from the
   cited terms at write time via the **same DCA-062 screen** (`decide_retention`). Where raw retention is
   prohibited the result payload is dropped (`result_payload` left `NULL`, never written) while the
   locator `result_ref` and the non-reversible SHA-256 `result_digest` are kept — so even a hit on a
   no-retention source serves only a reference, never a prohibited raw copy.
4. **Cross-entitlement isolation.** The cache is bound to one verified `business_id`; every read/write is
   confined to it (DCA-017 scoping + DCA-018 RLS). One business can never read another's cached result —
   the same `cache_key` recurs per tenant, each bound to that tenant's own entitlement. The rights recheck
   + terms-version pin are a second, in-tenant guard on top of RLS.
5. **Idempotent put.** `(business_id, source_key, source_version, cache_key)` is unique; a re-put is a
   deterministic no-op (one row, one audit). The write stages an atomic DCA-026 audit entry in the same
   unit of work (opaque references only — cache key / source key+version / operation / retention
   mode+reason / safe digest — the raw payload is never audited). The store never commits; the caller
   owns the unit of work.

## Configuration (bounded; validated positive at boot)

| Setting (env `EFFORTLESSPROMO_DCA_…`) | Default | Meaning |
|---|---|---|
| `SOURCE_RESULT_CACHE_DEFAULT_FRESH_SECONDS` | 86400 (24h) | positive freshness when the terms set no window |
| `SOURCE_RESULT_CACHE_MAX_FRESH_SECONDS` | 2592000 (30d) | hard cap clamping every positive entry |
| `SOURCE_RESULT_CACHE_NEGATIVE_TTL_SECONDS` | 3600 (1h) | bounded negative-result TTL |

A caller-requested `fresh_seconds` can only **narrow** the window, never widen it past the terms window or
the configured cap.

## Tests

- `tests/test_source_result_cache.py` — fresh entitled hit; stale miss (not served); negative bounded +
  expiry; lost-entitlement miss; revoked-approval miss; newer-terms-version (version-pin) miss;
  cross-tenant isolation; no-retention reference-only storage + hit; idempotent put; request-shape
  refusal; never-commits; plus a Postgres smoke of the real put/get path (skips without
  `DCA_TEST_DATABASE_URL`).
- `tests/test_migrations.py` — `test_0053_creates_source_result_cache_on_a_preexisting_database` and the
  `DCA_TABLES` set updated.
- `tests/test_rls.py` — `source_result_cache` added to the tenant-owned set; tenant-isolation +
  foreign-owner write-check tests (Postgres).

## Manual check

```
python scripts/manual_check.py DCA-127
```

Drives the real cache against a disposable DB with synthetic Businesses A/B and prints the REAL outcomes:
fresh hit → stale miss → cross-tenant miss → version-pin miss → revoked miss → no-retention reference-only
hit → negative-expiry → idempotent put. Local synthetic only: no network, no socket, no fetch, no
paid/live action.

## Boundary

Python DCA only. No UI, no AI-service internals, no .NET. No live source is enabled and no fetch is
performed — the cache stores/serves provided results, keeping paid/live acquisition disabled.
Ready for manual review.
