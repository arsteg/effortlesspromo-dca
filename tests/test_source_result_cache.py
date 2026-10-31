"""DCA-127 — cache only entitled, fresh source results (the safe source-result cache).

Pins the load-bearing behaviours of :mod:`app.services.source_result_cache` — the cache that serves a
cached source-fetch result ONLY to the business that is still entitled to it, only while it is still
fresh, and only for the current in-force terms version:

1. **A fresh, entitled result is served from cache.** A put under an approved+entitled source is a HIT on
   a later get within the freshness window, returning the stored payload.
2. **A stale/expired entry is a miss, never served.** A get at/after ``fresh_until`` MISSES
   (``REASON_STALE``) and returns no entry — and a bounded negative entry expires the same way.
3. **A hit cannot exceed current entitlement or survive revocation.** Losing the entitlement, revoking
   the platform approval, or a NEWER approved terms version each turns a previously-cached result into a
   MISS — never another result.
4. **Cross-entitlement isolation.** One business can never read another's cached result (tenant scoping),
   even with the same cache_key.
5. **Writes respect retention.** A no-retention source caches only a reference + digest (no raw payload),
   mirroring DCA-062; the hit then serves only the reference.
6. **Idempotent put.** A re-put under the same key is a no-op (one row, one audit).

The cache logic is unit-tested on in-memory SQLite; the RLS/migration paths for the table are covered by
``tests/test_rls.py`` and ``tests/test_migrations.py``. A real-Postgres smoke of the put/get path runs
when ``DCA_TEST_DATABASE_URL`` is set.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine

from app.audit.sink import ActorType, AuditActor
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import (
    AuditEvent,
    EvidenceRetentionMode,
    Source,
    SourceResultCacheEntry,
    SourceStatus,
)
from app.services.source_approval import (
    ReviewerPrincipal,
    SourceApprovalReview,
    SourceApprovalStore,
)
from app.services.source_catalog import SourceCatalogStore, SourceDraftInput
from app.services.source_entitlement import (
    REASON_SOURCE_NOT_ENTITLED,
    BusinessEntitler,
    BusinessSourceEntitlementInput,
    BusinessSourceEntitlementStore,
)
from app.services.source_preflight import REASON_SOURCE_NOT_PLATFORM_APPROVED
from app.services.source_result_cache import (
    REASON_NOT_CACHED,
    REASON_SOURCE_NOT_FOUND,
    REASON_STALE,
    CacheResultInput,
    SourceResultCache,
    SourceResultCacheError,
)
from app.services.source_revocation import RevokerPrincipal, SourceRevocationStore
from app.services.source_terms import SourceTermsInput, SourceTermsStore

from tests.conftest import make_settings

SOURCE_KEY = "synthetic_cache_source"
BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"
NOW = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)
ACTOR = AuditActor(id="00000000-0000-4000-9000-0000000a0127", type=ActorType.USER)
ENTITLER = BusinessEntitler(
    subject="00000000-0000-4000-9000-0000000a0127", roles=("owner", "admin")
)
OPERATOR = ReviewerPrincipal(subject="op-001", roles=("platform_operator",))
REVOKER = RevokerPrincipal(subject="op-001", roles=("platform_operator",))

SETTINGS = make_settings(
    source_result_cache_default_fresh_seconds=86400,  # 24h
    source_result_cache_negative_ttl_seconds=3600,  # 1h
)

PAYLOAD = {"legal_name": "Synthetic Cache Target Pvt Ltd", "industry": "Information Technology"}


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def _catalogue(session, *, source_key: str = SOURCE_KEY) -> None:
    SourceCatalogStore(session).create_draft(
        SourceDraftInput(
            key=source_key,
            kind="commercial",
            access_method="api",
            trust_tier="licensed_provider",
            india_coverage={"nationwide": True},
        )
    )


def _add_terms(session, version_ops: dict, *, source_key: str = SOURCE_KEY) -> int:
    terms = SourceTermsStore(session).add_version(
        SourceTermsInput(
            source_key=source_key,
            operations=version_ops,
            purposes=["prospecting"],
            contract_evidence={"reference": "LICENSE-2026", "kind": "data_license"},
        )
    )
    session.flush()
    return terms.version


def _approve(session, version: int, *, source_key: str = SOURCE_KEY) -> None:
    SourceApprovalStore(session).approve(
        source_key,
        version,
        SourceApprovalReview(
            reviewer_id="op-001",
            evidence={"reference": "RIGHTS-REVIEW-2026-11", "kind": "legal_review"},
            reviewed_at=NOW - timedelta(days=1),
            expires_at=NOW + timedelta(days=365),
        ),
        OPERATOR,
    )


def _enable(session, *, source_key: str = SOURCE_KEY) -> None:
    src = session.query(Source).filter(Source.key == source_key).one()
    src.status = SourceStatus.ENABLED.value
    src.enabled = True


def _entitle(
    session, business_id: str, *, enabled: bool = True, source_key: str = SOURCE_KEY
) -> None:
    BusinessSourceEntitlementStore(session, business_id).set_entitlement(
        BusinessSourceEntitlementInput(
            source_key=source_key,
            enabled=enabled,
            operations={"collect": True},
            purposes=["prospecting"],
        ),
        ENTITLER,
        now=NOW,
    )


def _setup(session, *, store: bool = True) -> None:
    """Catalogue + terms(v1) + approve(v1) + ENABLE (test-only) + entitle A for collect/prospecting."""
    _catalogue(session)
    _add_terms(session, {"collect": True, "store": store})
    _approve(session, 1)
    _enable(session)
    _entitle(session, BIZ_A)


def _cache(session, business_id: str = BIZ_A) -> SourceResultCache:
    return SourceResultCache(session, business_id, settings=SETTINGS)


def _put_input(**overrides) -> CacheResultInput:
    base = {
        "source_key": SOURCE_KEY,
        "source_version": 1,
        "purpose": "prospecting",
        "cache_key": "req-1",
        "fetched_at": NOW,
        "operation": "collect",
        "payload": dict(PAYLOAD),
        "result_kind": "org_profile",
        "result_ref": "cache://synthetic/req-1",
    }
    base.update(overrides)
    return CacheResultInput(**base)


# --- 1) a fresh, entitled result is served from cache ---------------------------------------------


def test_fresh_entitled_result_is_a_hit(session_factory):
    with session_factory() as session:
        _setup(session)
        _cache(session).put(_put_input(), ACTOR, now=NOW)
        session.commit()

    with session_factory() as session:
        lookup = _cache(session).get(SOURCE_KEY, "collect", "prospecting", "req-1", now=NOW)
        assert lookup.hit is True
        assert lookup.negative is False
        assert lookup.payload == PAYLOAD  # the retained result is served
        assert lookup.retention_mode == EvidenceRetentionMode.RAW_RETAINED.value
        assert lookup.source_version == 1
        assert lookup.result_digest  # a fingerprint is always kept


# --- 2) a stale/expired entry is a miss, never served ---------------------------------------------


def test_stale_entry_is_a_miss_and_not_served(session_factory):
    with session_factory() as session:
        _setup(session)
        _cache(session).put(_put_input(), ACTOR, now=NOW)
        session.commit()

    with session_factory() as session:
        # Just inside the window: still a hit.
        fresh = _cache(session).get(
            SOURCE_KEY, "collect", "prospecting", "req-1", now=NOW + timedelta(hours=23)
        )
        assert fresh.hit is True
        # Past the 24h default freshness window: a MISS, and nothing is returned.
        stale = _cache(session).get(
            SOURCE_KEY, "collect", "prospecting", "req-1", now=NOW + timedelta(hours=25)
        )
        assert stale.hit is False
        assert stale.reason == REASON_STALE
        assert stale.phase == "freshness"
        assert stale.entry is None


def test_negative_entry_is_bounded_and_expires(session_factory):
    with session_factory() as session:
        _setup(session)
        _cache(session).put(
            _put_input(cache_key="neg-1", negative=True, payload=None, result_ref="cache://neg-1"),
            ACTOR,
            now=NOW,
        )
        session.commit()

    with session_factory() as session:
        # Inside the bounded 1h negative TTL: a HIT recording a cached "no result".
        within = _cache(session).get(
            SOURCE_KEY, "collect", "prospecting", "neg-1", now=NOW + timedelta(minutes=30)
        )
        assert within.hit is True and within.negative is True and within.payload is None
        # Past the bounded negative TTL: the negative result has expired (a miss).
        after = _cache(session).get(
            SOURCE_KEY, "collect", "prospecting", "neg-1", now=NOW + timedelta(hours=2)
        )
        assert after.hit is False and after.reason == REASON_STALE


# --- 3) a hit cannot exceed current entitlement or survive revocation -----------------------------


def test_lost_entitlement_turns_a_hit_into_a_miss(session_factory):
    with session_factory() as session:
        _setup(session)
        _cache(session).put(_put_input(), ACTOR, now=NOW)
        session.commit()

    with session_factory() as session:
        # The business disables its own entitlement (a new, disabled version).
        _entitle(session, BIZ_A, enabled=False)
        session.commit()

    with session_factory() as session:
        lookup = _cache(session).get(SOURCE_KEY, "collect", "prospecting", "req-1", now=NOW)
        assert lookup.hit is False
        assert lookup.phase == "rights"
        assert lookup.reason == REASON_SOURCE_NOT_ENTITLED
        assert lookup.entry is None


def test_revoked_approval_turns_a_hit_into_a_miss(session_factory):
    with session_factory() as session:
        _setup(session)
        _cache(session).put(_put_input(), ACTOR, now=NOW)
        session.commit()

    with session_factory() as session:
        SourceRevocationStore(session).revoke(
            SOURCE_KEY, 1, REVOKER, reason="rights_withdrawn", now=NOW
        )
        session.commit()

    with session_factory() as session:
        lookup = _cache(session).get(SOURCE_KEY, "collect", "prospecting", "req-1", now=NOW)
        assert lookup.hit is False
        assert lookup.phase == "rights"
        # The in-force approval is gone, so the rights gate denies at the platform ceiling.
        assert lookup.reason == REASON_SOURCE_NOT_PLATFORM_APPROVED


def test_newer_terms_version_makes_an_old_entry_unreachable(session_factory):
    with session_factory() as session:
        _setup(session)
        _cache(session).put(_put_input(), ACTOR, now=NOW)  # cached under terms v1
        session.commit()

    with session_factory() as session:
        # A newer approved terms version becomes the in-force ceiling.
        _add_terms(session, {"collect": True, "store": True})  # v2
        _approve(session, 2)
        session.commit()

    with session_factory() as session:
        lookup = _cache(session).get(SOURCE_KEY, "collect", "prospecting", "req-1", now=NOW)
        # Rights still ALLOW (entitled for collect/prospecting), but the entry is pinned to v1 while the
        # current in-force version is v2 — so it is unreachable: a miss, not another result.
        assert lookup.hit is False
        assert lookup.phase == "cache"
        assert lookup.reason == REASON_NOT_CACHED


# --- 4) cross-entitlement isolation ---------------------------------------------------------------


def test_one_business_cannot_read_anothers_cached_result(session_factory):
    with session_factory() as session:
        _setup(session)  # A is entitled
        _entitle(session, BIZ_B)  # B is ALSO entitled to the same source...
        _cache(session, BIZ_A).put(_put_input(), ACTOR, now=NOW)  # ...but only A caches a result
        session.commit()

    with session_factory() as session:
        # B, with the SAME cache_key, gets a miss: A's entry is invisible to B (tenant isolation).
        b_lookup = _cache(session, BIZ_B).get(
            SOURCE_KEY, "collect", "prospecting", "req-1", now=NOW
        )
        assert b_lookup.hit is False
        assert b_lookup.reason == REASON_NOT_CACHED
        # B sees none of A's cache rows at all.
        assert _cache(session, BIZ_B).entries_for(SOURCE_KEY) == []
        # A still hits its own entry.
        assert (
            _cache(session, BIZ_A).get(SOURCE_KEY, "collect", "prospecting", "req-1", now=NOW).hit
            is True
        )


# --- 5) writes respect retention (no-retention ⇒ reference + digest only) -------------------------


def test_no_retention_source_caches_only_a_reference(session_factory):
    with session_factory() as session:
        # store right WITHHELD → the retention screen forbids keeping the raw payload.
        _setup(session, store=False)
        entry = _cache(session).put(_put_input(), ACTOR, now=NOW)
        session.flush()
        assert entry.retention_mode == EvidenceRetentionMode.REFERENCE_ONLY.value
        assert entry.result_payload is None  # the raw payload is NEVER written
        assert entry.result_ref == "cache://synthetic/req-1"  # the locator is kept
        assert entry.result_digest  # and the non-reversible digest is kept
        session.commit()

    with session_factory() as session:
        lookup = _cache(session).get(SOURCE_KEY, "collect", "prospecting", "req-1", now=NOW)
        # A hit on a no-retention source serves only the reference + digest, never a raw copy.
        assert lookup.hit is True
        assert lookup.payload is None
        assert lookup.result_ref == "cache://synthetic/req-1"
        assert lookup.retention_mode == EvidenceRetentionMode.REFERENCE_ONLY.value


# --- 6) idempotent put ----------------------------------------------------------------------------


def test_put_is_idempotent(session_factory):
    with session_factory() as session:
        _setup(session)
        first = _cache(session).put(_put_input(), ACTOR, now=NOW)
        session.flush()
        first_id = first.id
        audits_after_first = session.query(AuditEvent).count()
        again = _cache(session).put(_put_input(), ACTOR, now=NOW)
        assert again.id == first_id  # the same row, not a duplicate
        # A replay stages no second audit.
        assert session.query(AuditEvent).count() == audits_after_first
        session.commit()

    with session_factory() as session:
        assert session.query(SourceResultCacheEntry).count() == 1


# --- request-shape refusals change nothing --------------------------------------------------------


def test_put_for_unknown_source_is_refused_and_changes_nothing(session_factory):
    with session_factory() as session:
        _setup(session)
        with pytest.raises(SourceResultCacheError) as exc:
            _cache(session).put(_put_input(source_key="does_not_exist"), ACTOR, now=NOW)
        assert exc.value.code == REASON_SOURCE_NOT_FOUND
        session.rollback()

    with session_factory() as session:
        assert session.query(SourceResultCacheEntry).count() == 0


def test_cache_never_commits(session_factory):
    with session_factory() as session:
        _setup(session)
        _cache(session).put(_put_input(), ACTOR, now=NOW)
        session.rollback()  # the cache never commits — a rollback undoes the whole put

    with session_factory() as session:
        assert session.query(SourceResultCacheEntry).count() == 0


# --- Postgres-backed smoke of the real transaction / RLS path -------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")


@pytest.mark.skipif(not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres")
def test_put_get_over_postgres():
    """Exercise the real put → hit → stale-miss path on a disposable PostgreSQL — never SQLite for the
    durable write path."""
    engine = create_engine(PG_URL, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        factory = make_session_factory(engine)
        with factory() as session:
            _setup(session)
            _cache(session).put(_put_input(), ACTOR, now=NOW)
            session.commit()

        with factory() as session:
            hit = _cache(session).get(SOURCE_KEY, "collect", "prospecting", "req-1", now=NOW)
            assert hit.hit is True and hit.payload == PAYLOAD
            stale = _cache(session).get(
                SOURCE_KEY, "collect", "prospecting", "req-1", now=NOW + timedelta(days=2)
            )
            assert stale.hit is False and stale.reason == REASON_STALE

        with factory() as session:
            assert session.query(SourceResultCacheEntry).count() == 1
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
