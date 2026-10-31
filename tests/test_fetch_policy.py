"""DCA-121 — enforce approved access and crawl limits before fetching a web source.

Covers both halves of the story and their composition:

- the pure crawl-limit decider (:func:`app.sources.crawl_limits.evaluate_crawl_limits`) — robots
  longest-match, crawl-delay, rate and concurrency bounds, stop-at-first-failure;
- the durable per-host limiter (:class:`app.sources.crawl_limits.HostCrawlLimiter`) — a reservation is
  recorded only on ALLOW, a refused reserve changes NOTHING, the rate limit holds ACROSS jobs (a second
  session sees the first's committed count), and release frees a concurrency slot;
- the composed gate (:class:`app.sources.fetch_policy.FetchPolicyGate`) — rights FIRST, so a license
  denial is returned WITHOUT touching the crawl limiter (a permissive robots.txt / official origin can
  never override it), and only an approved+entitled+within-limits request is allowed.

Service unit tests use in-memory SQLite (existing style, like ``tests/test_source_preflight.py``); the
transaction-safe serialization that makes the rate limit hold across TRULY concurrent jobs is proven on
a disposable PostgreSQL behind the ``DCA_TEST_DATABASE_URL`` skip guard (RLS itself is covered by
``tests/test_rls.py``).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from app.db.base import Base, make_engine, make_session_factory
from app.db.models import HostCrawlState, Source, SourceStatus
from app.services.source_approval import (
    ReviewerPrincipal,
    SourceApprovalReview,
    SourceApprovalStore,
)
from app.services.source_catalog import SourceCatalogStore, SourceDraftInput
from app.services.source_entitlement import (
    BusinessEntitler,
    BusinessSourceEntitlementInput,
    BusinessSourceEntitlementStore,
)
from app.services.source_preflight import (
    REASON_OPERATION_NOT_ENTITLED,
    REASON_SOURCE_NOT_ENTITLED,
    REASON_SOURCE_NOT_PLATFORM_APPROVED,
    PreflightRequest,
)
from app.services.source_terms import SourceTermsInput, SourceTermsStore
from app.sources.crawl_limits import (
    CrawlLimitReason,
    CrawlPolicy,
    CrawlVerdict,
    HostCrawlLimiter,
    HostCrawlObservation,
    evaluate_crawl_limits,
)
from app.sources.fetch_policy import (
    FetchPhase,
    FetchPolicyGate,
    FetchPolicyRequest,
)

SOURCE_KEY = "mca_master_data"
HOST = "data.gov.in"
URL = f"https://{HOST}/mca/companies"
BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)

ADMIN = BusinessEntitler(subject="00000000-0000-4000-9000-0000000a0121", roles=("owner", "admin"))
OPERATOR = ReviewerPrincipal(subject="op-001", roles=("platform_operator",))

# "official origin" + "robots.txt allows crawling" — advisory hints the rights chain must IGNORE.
OFFICIAL_ROBOTS_OK = {"official_origin": True, "robots_allowed": True, "trust_tier": "official"}

# A generous crawl policy that admits a single well-behaved request (robots allows everything, a wide
# rate window, no crawl-delay, ample concurrency) — the allow-path default for the tests below.
GENEROUS = CrawlPolicy.of(
    crawl_delay_seconds=0.0, max_requests=5, window_seconds=3600, max_concurrent=5
)


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def _catalogue_and_approve(session, *, source_key: str = SOURCE_KEY) -> None:
    """Catalogue (DCA-044) + record terms (DCA-045) + approve (DCA-046): collect/store/display/export
    source-wide ('share' DENIED), purpose 'prospecting'. Catalog entry stays a DISABLED draft."""
    SourceCatalogStore(session).create_draft(
        SourceDraftInput(
            key=source_key,
            kind="official",
            access_method="bulk_download",
            trust_tier="official_registry",
            india_coverage={"nationwide": True},
        )
    )
    SourceTermsStore(session).add_version(
        SourceTermsInput(
            source_key=source_key,
            operations={"collect": True, "store": True, "display": True, "export": True},
            purposes=["prospecting"],
            contract_evidence={"reference": "MCA-TOS-2026", "kind": "terms_of_use"},
        )
    )
    SourceApprovalStore(session).approve(
        source_key,
        1,
        SourceApprovalReview(
            reviewer_id="op-001",
            evidence={"reference": "RIGHTS-REVIEW-2026-11", "kind": "legal_review"},
            reviewed_at=NOW - timedelta(days=1),
            expires_at=NOW + timedelta(days=365),
        ),
        OPERATOR,
    )


def _enable_source(session, *, source_key: str = SOURCE_KEY) -> None:
    """TEST-ONLY synthetic enable so the allow path is reachable (the service never flips ENABLED)."""
    source = session.query(Source).filter(Source.key == source_key).one()
    source.status = SourceStatus.ENABLED.value
    source.enabled = True


def _entitle(session, business_id: str, **kwargs) -> None:
    BusinessSourceEntitlementStore(session, business_id).set_entitlement(
        BusinessSourceEntitlementInput(source_key=SOURCE_KEY, enabled=True, **kwargs),
        ADMIN,
        now=NOW,
    )


def _fully_authorize(session, business_id: str = BIZ_A) -> None:
    _catalogue_and_approve(session)
    _enable_source(session)
    _entitle(session, business_id, operations={"collect": True}, purposes=["prospecting"])


def _req(url: str = URL, *, operation: str = "collect", policy: CrawlPolicy = GENEROUS):
    return FetchPolicyRequest(
        preflight=PreflightRequest(
            source_key=SOURCE_KEY,
            operation=operation,
            purpose="prospecting",
            origin_signals=OFFICIAL_ROBOTS_OK,
        ),
        url=url,
        policy=policy,
    )


# -- the pure crawl-limit decider (no DB) ---------------------------------------------------------


def test_decider_allows_within_every_bound():
    d = evaluate_crawl_limits(GENEROUS, HostCrawlObservation(), path="/mca/companies", now=NOW)
    assert d.verdict is CrawlVerdict.ALLOW and d.reason is None


def test_decider_robots_disallowed_is_a_hard_deny():
    policy = CrawlPolicy.of(
        max_requests=5, window_seconds=3600, max_concurrent=5, disallow_rules=("/private",)
    )
    d = evaluate_crawl_limits(policy, HostCrawlObservation(), path="/private/data", now=NOW)
    assert d.verdict is CrawlVerdict.DENY
    assert d.reason is CrawlLimitReason.ROBOTS_DISALLOWED


def test_decider_robots_longest_match_allow_overrides_disallow():
    # Disallow /private, but Allow the more specific /private/public → the longest rule (Allow) wins.
    policy = CrawlPolicy.of(
        max_requests=5,
        window_seconds=3600,
        max_concurrent=5,
        disallow_rules=("/private",),
        allow_rules=("/private/public",),
    )
    allowed = evaluate_crawl_limits(
        policy, HostCrawlObservation(), path="/private/public/x", now=NOW
    )
    denied = evaluate_crawl_limits(policy, HostCrawlObservation(), path="/private/secret", now=NOW)
    assert allowed.verdict is CrawlVerdict.ALLOW
    assert denied.verdict is CrawlVerdict.DENY


def test_decider_crawl_delay_defers():
    policy = CrawlPolicy.of(
        crawl_delay_seconds=60, max_requests=5, window_seconds=3600, max_concurrent=5
    )
    obs = HostCrawlObservation(request_count=1, last_request_at=NOW - timedelta(seconds=5))
    d = evaluate_crawl_limits(policy, obs, path="/x", now=NOW)
    assert d.verdict is CrawlVerdict.DEFER
    assert d.reason is CrawlLimitReason.CRAWL_DELAY_NOT_ELAPSED


def test_decider_rate_then_concurrency_in_order():
    policy = CrawlPolicy.of(max_requests=2, window_seconds=3600, max_concurrent=2)
    rate = evaluate_crawl_limits(
        policy, HostCrawlObservation(request_count=2, in_flight=0), path="/x", now=NOW
    )
    conc = evaluate_crawl_limits(
        policy, HostCrawlObservation(request_count=1, in_flight=2), path="/x", now=NOW
    )
    assert rate.reason is CrawlLimitReason.RATE_LIMIT_EXCEEDED
    assert conc.reason is CrawlLimitReason.CONCURRENCY_LIMIT_EXCEEDED


# -- the durable per-host limiter -----------------------------------------------------------------


def test_limiter_reserve_records_durably_and_denial_changes_nothing(session_factory):
    # Rate cap of 1: the first reserve is recorded, the second (a separate "job"/session) is deferred
    # and stages NOTHING — the rate limit holds across jobs via the shared durable row.
    policy = CrawlPolicy.of(max_requests=1, window_seconds=3600, max_concurrent=5)
    with session_factory() as session:
        first = HostCrawlLimiter(session, BIZ_A).reserve(SOURCE_KEY, HOST, policy, now=NOW)
        assert first.reserved is True and first.request_count == 1
        session.commit()

    with session_factory() as session:
        second = HostCrawlLimiter(session, BIZ_A).reserve(SOURCE_KEY, HOST, policy, now=NOW)
        assert second.reserved is False
        assert second.decision.reason is CrawlLimitReason.RATE_LIMIT_EXCEEDED
        session.commit()

    # Exactly one durable row with count 1 — the deferred second reserve left no trace.
    with session_factory() as session:
        rows = session.query(HostCrawlState).all()
        assert len(rows) == 1
        assert rows[0].request_count == 1 and rows[0].in_flight == 1


def test_limiter_robots_denial_creates_no_row(session_factory):
    policy = CrawlPolicy.of(
        max_requests=5, window_seconds=3600, max_concurrent=5, disallow_rules=("/private",)
    )
    with session_factory() as session:
        r = HostCrawlLimiter(session, BIZ_A).reserve(
            SOURCE_KEY, HOST, policy, path="/private/x", now=NOW
        )
        assert r.reserved is False
        assert r.decision.verdict is CrawlVerdict.DENY
        session.commit()
    with session_factory() as session:
        assert session.query(HostCrawlState).count() == 0  # a denial created nothing


def test_limiter_release_frees_a_concurrency_slot(session_factory):
    policy = CrawlPolicy.of(max_requests=10, window_seconds=3600, max_concurrent=1)
    with session_factory() as session:
        limiter = HostCrawlLimiter(session, BIZ_A)
        assert limiter.reserve(SOURCE_KEY, HOST, policy, now=NOW).reserved is True
        session.commit()
    with session_factory() as session:
        # A second concurrent job finds the one slot in flight → deferred on concurrency.
        blocked = HostCrawlLimiter(session, BIZ_A).reserve(SOURCE_KEY, HOST, policy, now=NOW)
        assert blocked.reserved is False
        assert blocked.decision.reason is CrawlLimitReason.CONCURRENCY_LIMIT_EXCEEDED
        session.commit()
    with session_factory() as session:
        HostCrawlLimiter(session, BIZ_A).release(SOURCE_KEY, HOST)
        session.commit()
    with session_factory() as session:
        # The freed slot now admits a reserve (rate count still rises; concurrency was the blocker).
        freed = HostCrawlLimiter(session, BIZ_A).reserve(SOURCE_KEY, HOST, policy, now=NOW)
        assert freed.reserved is True


# -- the composed gate: rights FIRST, then crawl limits -------------------------------------------


def test_gate_allows_when_approved_entitled_and_within_limits(session_factory):
    with session_factory() as session:
        _fully_authorize(session)
        session.commit()
    with session_factory() as session:
        decision = FetchPolicyGate(session, BIZ_A).authorize_fetch(_req(), now=NOW)
        assert decision.allowed is True
        assert decision.phase is FetchPhase.CRAWL
        assert decision.reason is None
        assert decision.reservation is not None and decision.reservation.reserved is True
        assert decision.host == HOST
        session.commit()
    with session_factory() as session:
        assert session.query(HostCrawlState).count() == 1  # the allowed reserve was recorded


def test_gate_rights_denial_never_touches_the_crawl_limiter(session_factory):
    # Approved at platform + enabled, but the tenant has NO entitlement → denied at the rights phase.
    with session_factory() as session:
        _catalogue_and_approve(session)
        _enable_source(session)
        session.commit()
    with session_factory() as session:
        decision = FetchPolicyGate(session, BIZ_A).authorize_fetch(_req(), now=NOW)
        assert decision.allowed is False
        assert decision.phase is FetchPhase.RIGHTS
        assert decision.reason == REASON_SOURCE_NOT_ENTITLED
        assert decision.reservation is None  # the limiter was never consulted
        session.commit()
    with session_factory() as session:
        assert (
            session.query(HostCrawlState).count() == 0
        )  # no crawl state touched by a rights denial


def test_robots_allowance_cannot_override_a_license_denial(session_factory):
    """The acceptance criterion: an official-origin source with a fully-permissive robots policy but NO
    platform approval is still DENIED at the rights phase — robots can never override a license denial,
    and the crawl limiter is never reached."""
    with session_factory() as session:
        # Catalogue + terms but DO NOT approve; enable so the ONLY thing missing is the rights chain.
        SourceCatalogStore(session).create_draft(
            SourceDraftInput(
                key=SOURCE_KEY,
                kind="official",
                access_method="bulk_download",
                trust_tier="official_registry",
                india_coverage={"nationwide": True},
            )
        )
        SourceTermsStore(session).add_version(
            SourceTermsInput(
                source_key=SOURCE_KEY,
                operations={"collect": True},
                purposes=["prospecting"],
                contract_evidence={"reference": "MCA-TOS-2026", "kind": "terms_of_use"},
            )
        )
        _enable_source(session)
        session.commit()
    with session_factory() as session:
        # A policy that robots-allows EVERYTHING, plus the "official origin + robots ok" hints.
        decision = FetchPolicyGate(session, BIZ_A).authorize_fetch(_req(policy=GENEROUS), now=NOW)
        assert decision.allowed is False
        assert decision.phase is FetchPhase.RIGHTS
        assert decision.reason == REASON_SOURCE_NOT_PLATFORM_APPROVED
        assert decision.reservation is None
        session.commit()
    with session_factory() as session:
        assert session.query(HostCrawlState).count() == 0


def test_gate_operation_not_entitled_is_denied_at_rights(session_factory):
    with session_factory() as session:
        _catalogue_and_approve(session)  # 'export' approved at platform
        _enable_source(session)
        _entitle(
            session, BIZ_A, operations={"display": True, "export": False}, purposes=["prospecting"]
        )  # business narrows 'export' out
        session.commit()
    with session_factory() as session:
        decision = FetchPolicyGate(session, BIZ_A).authorize_fetch(
            _req(operation="export"), now=NOW
        )
        assert decision.allowed is False
        assert decision.phase is FetchPhase.RIGHTS
        assert decision.reason == REASON_OPERATION_NOT_ENTITLED
        assert decision.reservation is None


def test_gate_robots_disallowed_path_is_denied_at_crawl(session_factory):
    with session_factory() as session:
        _fully_authorize(session)
        session.commit()
    with session_factory() as session:
        policy = CrawlPolicy.of(
            max_requests=5, window_seconds=3600, max_concurrent=5, disallow_rules=("/private",)
        )
        decision = FetchPolicyGate(session, BIZ_A).authorize_fetch(
            _req(url=f"https://{HOST}/private/companies", policy=policy), now=NOW
        )
        assert decision.allowed is False
        assert decision.phase is FetchPhase.CRAWL
        assert decision.reason == CrawlLimitReason.ROBOTS_DISALLOWED.value
        assert decision.reservation is not None and decision.reservation.reserved is False
        session.commit()
    with session_factory() as session:
        assert session.query(HostCrawlState).count() == 0  # a crawl denial recorded nothing


def test_gate_rate_cap_defers_across_jobs(session_factory):
    policy = CrawlPolicy.of(max_requests=1, window_seconds=3600, max_concurrent=5)
    with session_factory() as session:
        _fully_authorize(session)
        session.commit()
    with session_factory() as session:
        first = FetchPolicyGate(session, BIZ_A).authorize_fetch(_req(policy=policy), now=NOW)
        assert first.allowed is True
        session.commit()
    with session_factory() as session:
        # A second job: rights still pass, but the shared per-host rate window is full → deferred.
        second = FetchPolicyGate(session, BIZ_A).authorize_fetch(_req(policy=policy), now=NOW)
        assert second.allowed is False
        assert second.phase is FetchPhase.CRAWL
        assert second.reason == CrawlLimitReason.RATE_LIMIT_EXCEEDED.value
        session.commit()


# -- PostgreSQL concurrency: the rate limit holds across TRULY concurrent jobs (FOR UPDATE) --------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)
_PG_SCHEMA = "dca_crawl_test"


@pytest.fixture
def pg_factory():
    """A fresh schema with the DCA tables, a pre-seeded empty crawl-state row for the host, and a
    session factory whose connections all resolve to that schema (so threads share one datastore)."""
    from sqlalchemy import create_engine, text

    admin = create_engine(PG_URL)
    with admin.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{_PG_SCHEMA}" CASCADE'))
        conn.execute(text(f'CREATE SCHEMA "{_PG_SCHEMA}"'))
    admin.dispose()

    engine = create_engine(PG_URL, connect_args={"options": f"-csearch_path={_PG_SCHEMA}"})
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    # Pre-create the host row so every racing reserve locks the SAME existing row (FOR UPDATE) rather
    # than racing to INSERT — the serialization point under test is the rate-counter update.
    with factory() as session:
        session.add(
            HostCrawlState(
                business_id=BIZ_A,
                source_key=SOURCE_KEY,
                host=HOST,
                window_started_at=NOW,
                window_seconds=3600,
                request_count=0,
                in_flight=0,
            )
        )
        session.commit()
    try:
        yield factory
    finally:
        engine.dispose()
        admin = create_engine(PG_URL)
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{_PG_SCHEMA}" CASCADE'))
        admin.dispose()


@requires_pg
def test_concurrent_reserves_cannot_overshoot_the_rate_cap(pg_factory):
    """Several reserves racing against a rate cap of 2 SERIALIZE on the FOR UPDATE lock, so at most two
    are admitted and the durable count never exceeds the cap — the "rate limits apply across concurrent
    jobs" acceptance, on real Postgres."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy import select

    policy = CrawlPolicy.of(
        crawl_delay_seconds=0.0, max_requests=2, window_seconds=3600, max_concurrent=100
    )
    n = 8
    barrier = threading.Barrier(n)
    outcomes: list[bool] = []
    lock = threading.Lock()

    def _attempt(i: int) -> None:
        barrier.wait()
        session = pg_factory()
        try:
            r = HostCrawlLimiter(session, BIZ_A).reserve(SOURCE_KEY, HOST, policy, now=NOW)
            session.commit()
            reserved = r.reserved
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        with lock:
            outcomes.append(reserved)

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(_attempt, range(n)))

    assert sum(1 for o in outcomes if o) == 2, outcomes  # exactly the cap's worth admitted
    with pg_factory() as session:
        row = session.scalars(
            select(HostCrawlState).where(HostCrawlState.business_id == BIZ_A)
        ).one()
    assert row.request_count == 2  # never overshot the rate cap of 2
