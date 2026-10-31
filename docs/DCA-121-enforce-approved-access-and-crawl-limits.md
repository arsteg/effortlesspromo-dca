# DCA-121 — Enforce approved access and crawl limits

Stage: Approved source access · Key: `fetch-policy` · Source sections 6, 8, 9

## What this adds

A single gate that composes the **source-rights chain** (DCA-044..048) with the **crawl-etiquette
limits** (robots policy, per-host crawl-delay, per-host rate and concurrency caps) and decides, before
any web fetch, whether a request is both *approved* and *polite*. Authority and rights are checked
**FIRST**; crawl limits are consulted only once rights permit.

- `app/sources/crawl_limits.py` — the crawl half:
  - `evaluate_crawl_limits(policy, observation, *, path, now) -> CrawlDecision` — a **pure,
    deterministic, network-free** decider (same shape as `app/sources/destination_guard.py`): robots
    longest-match (RFC 9309) → crawl-delay → rate → concurrency, stop-at-first-failure. `CrawlVerdict`
    is `ALLOW | DENY | DEFER`; `CrawlLimitReason` names each bound.
  - `HostCrawlLimiter(session, business_id)` — the **durable** store. `reserve(source_key, host,
    policy, *, path, now)` takes a `FOR UPDATE` lock on the shared per-host crawl-state row (creating
    it on first use), rolls the rate window if elapsed, composes the pure decider against the **live
    committed** counters, and records the reservation **only on ALLOW**. `release(...)` frees an
    in-flight concurrency slot.
- `app/sources/fetch_policy.py` — the composition:
  - `FetchPolicyGate(session, business_id).authorize_fetch(request, *, now) -> FetchPolicyDecision`.
    Runs the DCA-048 `SourcePreflight.authorize` first; on a rights denial it returns immediately with
    `phase=RIGHTS` and **never touches the crawl limiter**; otherwise it reserves through the limiter
    and returns `phase=CRAWL`.
- `app/db/models.py` — `HostCrawlState` (tenant-owned, RLS): one row per `(business_id, source_key,
  host)` holding the rolling rate window (`window_started_at` / `window_seconds` / `request_count`),
  the `in_flight` concurrency counter and the `last_request_at` crawl-delay anchor. Unique
  `(business_id, source_key, host)`.
- Migration `0048_host_crawl_state` — dual-path (metadata baseline on a fresh DB, create-branch +
  RLS policy + app-role grant on a pre-existing DB), like `0044_budget_reservations`.

## The guarantees (acceptance criteria)

- **Robots allowance cannot override a license denial.** The rights chain is evaluated first by the
  unchanged DCA-048 preflight. A rights denial (no in-force platform approval, no business entitlement,
  operation/field/purpose not granted, draft/disabled/unknown source) is returned with the crawl
  limiter **never consulted** — a permissive `robots.txt` or an "official origin" hint (recorded for
  audit, ignored by the decision) can never, on its own, authorize a fetch the rights chain withholds.
  A rights denial changes no crawl state.
- **Rate limits apply across concurrent jobs.** The per-host rate / crawl-delay / concurrency limits
  are enforced against the **durable** `HostCrawlState` row under a `FOR UPDATE` lock, so two
  concurrent jobs reserving against the same host serialize and can never both be admitted past a cap
  (proven on real Postgres in `test_fetch_policy.py::test_concurrent_reserves_cannot_overshoot_the_rate_cap`).
- **A refused reserve changes nothing.** A `DENY`/`DEFER` stages no row and bumps no counter; only a
  fully-allowed reserve records the slot. Every decision is reproducible from its inputs.

## Crawl-limit order (fixed, stop at first failure)

1. **Robots allowed-path** — the path is matched against the policy's Allow/Disallow rules by RFC 9309
   longest-match (most specific rule wins; Allow wins a length tie). A Disallow → `robots_disallowed`
   (a hard `DENY`).
2. **Crawl-delay** — a reserve inside `crawl_delay_seconds` of the last request → `crawl_delay_not_elapsed` (`DEFER`).
3. **Rate cap** — the current window already holds `max_requests` reserved requests → `rate_limit_exceeded` (`DEFER`).
4. **Concurrency cap** — `max_concurrent` requests already in flight → `concurrency_limit_exceeded` (`DEFER`).

The `CrawlPolicy` (robots rules + caps) is **supplied by the caller** (derived, in a real deployment,
from the source's robots.txt and licensed rate terms) — the guard parses no robots.txt and resolves
nothing itself, exactly as DCA-118 takes a caller-supplied allowlist.

## Fetching stays disabled

The gate returns a decision and (on allow) a durable reservation; it performs **no** HTTP, DNS, socket
or fetch. Acting on an allow by performing the web fetch stays a disabled future capability — and
because rights are checked first and no source is ever `ENABLED` in the running service, the gate
denies every fetch at the rights phase in production.

## Tests / manual check

- `tests/test_fetch_policy.py` — the pure decider (robots longest-match, delay/rate/concurrency order),
  the durable limiter (records only on allow, denial changes nothing, rate holds across sessions/jobs,
  release frees a slot), the composed gate (allow; rights-denial never touches the limiter; robots
  cannot override a license denial; operation-not-entitled; robots-disallowed path; rate cap defers),
  plus a Postgres-gated true-concurrency serialization test.
- `tests/test_rls.py` — `host_crawl_state` tenant isolation + write-check on real Postgres.
- `tests/test_migrations.py` — `0048` create-branch + RLS policy on a pre-existing database.
- `python scripts/manual_check.py DCA-121` — prints the real ALLOW, the rights denials (robots not
  overriding a license denial), the crawl-limit denials (robots path + rate cap across jobs), and the
  durable per-host crawl-state rows.

## Boundary

Python DCA only; no AI, no network, no other service's database, no .NET. One migration, tests and this
doc. Future capabilities (actual fetching) stay disabled.
