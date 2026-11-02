"""DCA-149 — stop low-yield or exhausted searches.

Two layers, matching the DCA-146/147 suites:

- **Service unit tests** (in-memory SQLite) pin
  :meth:`~app.services.search_backoff.DiscoverySearchBackoff.record_round` /
  :meth:`~app.services.search_backoff.DiscoverySearchBackoff.check_eligibility`: a good-yield round keeps
  a partition ACTIVE and eligible; a run of no-new-result (or marginal-yield) rounds grows the streak and,
  once it crosses the bound, BACKS the partition OFF for a bounded, exponentially-growing window (a fresh
  search is then refused until it elapses); another low-yield round steps the window up to EXHAUSTED (paused
  with no retry); and a later good-yield round RECOVERS it to ACTIVE. The two acceptance invariants are
  pinned: an exhausted market is visibly paused (never repeatedly searched / spent / invented — recording
  stages only an inert state row and its audit entry), and the relevant denials — an unauthorized caller,
  an empty partition, a cross-tenant partition — change no state. A round's yield can be derived from a
  finalized DCA-146 run summary.
- **Postgres-backed test** (``DCA_TEST_DATABASE_URL``) advances one partition to EXHAUSTED on a disposable
  PostgreSQL (real transactions, not SQLite): the backoff row + its atomic audit entries survive and the
  eligibility check refuses the exhausted partition.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import make_url

from app.db.base import Base, make_engine, make_session_factory
from app.db.models import (
    AuditEvent,
    DiscoveryRunSummary,
    SearchPartitionBackoff,
    SearchPartitionState,
)
from app.services.search_backoff import (
    AUDIT_ACTION_RECORD_ROUND,
    BACKOFF_SOURCE,
    DENY_BACKING_OFF,
    DENY_EXHAUSTED,
    REASON_INVALID_PARTITION,
    REASON_MARGINAL_YIELD,
    REASON_NO_NEW_RESULTS,
    REASON_NONE,
    DiscoverySearchBackoff,
    RoundYield,
    SearchBackoffActor,
    SearchBackoffError,
    SearchBackoffAuthorizationError,
    classify_round,
    partition_key_for,
)
from tests.conftest import make_settings

BIZ_A = "00000000-0000-4000-8000-00000000149a"
BIZ_B = "00000000-0000-4000-8000-00000000149b"
PARTITION = "icp:1:src:synthetic"

ADMIN = SearchBackoffActor(subject="00000000-0000-4000-9000-0000000a0149a", roles=("owner",))
VIEWER = SearchBackoffActor(subject="00000000-0000-4000-9000-0000000a0149b", roles=("viewer",))

# Small, deterministic thresholds: 2 low-yield rounds back a partition off; a 2nd backoff step exhausts it.
SETTINGS = make_settings(
    search_low_yield_min_new_results=1,
    search_marginal_yield_min_ratio=0.5,
    search_low_yield_rounds_before_backoff=2,
    search_backoff_base_seconds=3600,
    search_backoff_max_seconds=86400,
    search_exhaust_after_backoffs=2,
)

_NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def _backoff(session, business_id=BIZ_A) -> DiscoverySearchBackoff:
    return DiscoverySearchBackoff(session, business_id, SETTINGS)


# --- classification (pure) ------------------------------------------------------------------------


def test_classify_distinguishes_good_no_new_and_marginal():
    good = classify_round(SETTINGS, RoundYield(new_results=5, total_results=5))
    none = classify_round(SETTINGS, RoundYield(new_results=0, total_results=8))
    empty = classify_round(SETTINGS, RoundYield(new_results=0, total_results=0))
    marginal = classify_round(SETTINGS, RoundYield(new_results=1, total_results=10))
    assert (good.low_yield, good.reason) == (False, REASON_NONE)
    assert (none.low_yield, none.reason) == (True, REASON_NO_NEW_RESULTS)  # all duplicates
    assert (empty.low_yield, empty.reason) == (True, REASON_NO_NEW_RESULTS)  # nothing seen
    assert (marginal.low_yield, marginal.reason) == (True, REASON_MARGINAL_YIELD)  # 0.1 < 0.5 bar


# --- a yielding partition stays active and eligible -----------------------------------------------


def test_good_round_keeps_partition_active_and_eligible(session_factory):
    with session_factory() as session:
        result = _backoff(session).record_round(
            PARTITION, RoundYield(new_results=7, total_results=10), ADMIN, now=_NOW
        )
        session.flush()
        assert result.low_yield is False
        assert result.state == SearchPartitionState.ACTIVE.value
        assert result.next_eligible_at is None
        assert result.paused is False

        decision = _backoff(session).check_eligibility(PARTITION, now=_NOW)
        assert decision.eligible is True
        assert decision.state == SearchPartitionState.ACTIVE.value
        assert decision.reason is None

        # One row, one audit entry — the state is advanced in place with an atomic audit.
        assert session.query(SearchPartitionBackoff).filter_by(business_id=BIZ_A).count() == 1
        assert result.backoff.provenance["source"] == BACKOFF_SOURCE
        assert result.backoff.provenance["sent"] is False
        assert result.backoff.provenance["spent"] is False
        assert result.backoff.provenance["invented"] is False


# --- consecutive low-yield rounds back the partition off (the stop) -------------------------------


def test_consecutive_no_new_results_backs_off_and_check_denies(session_factory):
    with session_factory() as session:
        store = _backoff(session)
        # First low-yield round: streak grows but still ACTIVE (below the 2-round bound).
        first = store.record_round(
            PARTITION, RoundYield(new_results=0, total_results=6), ADMIN, now=_NOW
        )
        session.flush()
        assert first.low_yield is True
        assert first.reason == REASON_NO_NEW_RESULTS
        assert first.state == SearchPartitionState.ACTIVE.value
        assert first.consecutive_low_yield == 1
        assert store.check_eligibility(PARTITION, now=_NOW).eligible is True

        # Second consecutive low-yield round crosses the bound → BACKING_OFF for the base window.
        second = store.record_round(
            PARTITION, RoundYield(new_results=0, total_results=9), ADMIN, now=_NOW
        )
        session.flush()
        assert second.backed_off is True
        assert second.state == SearchPartitionState.BACKING_OFF.value
        assert second.backoff_level == 1
        assert second.next_eligible_at == _NOW + timedelta(seconds=3600)

        # Within the window the market is PAUSED — a fresh search is refused (the visible stop).
        denied = store.check_eligibility(PARTITION, now=_NOW + timedelta(seconds=60))
        assert denied.eligible is False
        assert denied.reason == DENY_BACKING_OFF
        assert denied.next_eligible_at == _NOW + timedelta(seconds=3600)

        # Once the window elapses, a retry is permitted again (still BACKING_OFF until the next round).
        after = store.check_eligibility(PARTITION, now=_NOW + timedelta(seconds=3601))
        assert after.eligible is True
        assert after.state == SearchPartitionState.BACKING_OFF.value


# --- a further low-yield round exhausts the market (paused, no retry) ------------------------------


def test_further_low_yield_exhausts_market_and_check_denies(session_factory):
    with session_factory() as session:
        store = _backoff(session)
        for _ in range(2):  # reach BACKING_OFF (backoff_level 1)
            store.record_round(PARTITION, RoundYield(0, 5), ADMIN, now=_NOW)
            session.flush()
        assert store.get(PARTITION).state == SearchPartitionState.BACKING_OFF.value

        # The next low-yield round steps the backoff up to the ceiling → EXHAUSTED, no scheduled retry.
        exhausted = store.record_round(PARTITION, RoundYield(0, 5), ADMIN, now=_NOW)
        session.flush()
        assert exhausted.exhausted is True
        assert exhausted.state == SearchPartitionState.EXHAUSTED.value
        assert exhausted.next_eligible_at is None

        # An exhausted market is refused with no scheduled retry, however far in the future we look.
        denied = store.check_eligibility(PARTITION, now=_NOW + timedelta(days=365))
        assert denied.eligible is False
        assert denied.reason == DENY_EXHAUSTED
        assert denied.next_eligible_at is None


# --- a good-yield round recovers an exhausted/backing-off partition -------------------------------


def test_good_round_recovers_paused_partition(session_factory):
    with session_factory() as session:
        store = _backoff(session)
        for _ in range(3):  # ACTIVE → BACKING_OFF → EXHAUSTED
            store.record_round(PARTITION, RoundYield(0, 5), ADMIN, now=_NOW)
            session.flush()
        assert store.get(PARTITION).state == SearchPartitionState.EXHAUSTED.value

        recovered = store.record_round(PARTITION, RoundYield(6, 8), ADMIN, now=_NOW)
        session.flush()
        assert recovered.recovered is True
        assert recovered.state == SearchPartitionState.ACTIVE.value
        assert recovered.consecutive_low_yield == 0
        assert recovered.backoff_level == 0
        assert store.check_eligibility(PARTITION, now=_NOW).eligible is True


# --- yield can be derived from a finalized run summary --------------------------------------------


def test_round_yield_from_run_summary():
    summary = DiscoveryRunSummary(
        business_id=BIZ_A,
        run_id="run-1",
        final_status="completed",
        subject_count=10,
        matched_count=9,
        suggestion_count=1,
    )
    y = RoundYield.from_run_summary(summary)
    assert (y.new, y.total) == (1, 10)
    # 1/10 = 0.1 < the 0.5 marginal bar → a marginal-yield (low) round.
    assert classify_round(SETTINGS, y).reason == REASON_MARGINAL_YIELD


def test_partition_key_for_is_stable_and_bounded():
    k1 = partition_key_for(1, ["b", "a"])
    k2 = partition_key_for(1, ["a", "b"])  # order-independent
    assert k1 == k2
    assert k1.startswith("icp:1:src:")
    assert len(k1) <= 64
    assert (
        partition_key_for(2, ["a", "b"]) != k1
    )  # a different ICP version is a different partition


# --- refusals (fail-closed, no state change) ------------------------------------------------------


def test_unauthorized_actor_refused_no_state(session_factory):
    with session_factory() as session:
        with pytest.raises(SearchBackoffAuthorizationError):
            _backoff(session).record_round(PARTITION, RoundYield(0, 5), VIEWER, now=_NOW)
        assert session.query(SearchPartitionBackoff).count() == 0
        assert session.query(AuditEvent).count() == 0


def test_empty_partition_refused_no_state(session_factory):
    with session_factory() as session:
        with pytest.raises(SearchBackoffError) as exc:
            _backoff(session).record_round("  ", RoundYield(0, 5), ADMIN, now=_NOW)
        assert exc.value.code == REASON_INVALID_PARTITION
        assert session.query(SearchPartitionBackoff).count() == 0


def test_cross_tenant_partition_is_isolated(session_factory):
    with session_factory() as session:
        # A backs a partition off.
        store_a = _backoff(session, BIZ_A)
        store_a.record_round(PARTITION, RoundYield(0, 5), ADMIN, now=_NOW)
        store_a.record_round(PARTITION, RoundYield(0, 5), ADMIN, now=_NOW)
        session.flush()
        assert store_a.get(PARTITION).state == SearchPartitionState.BACKING_OFF.value

        # B cannot see A's partition — it reads as never-recorded and is freely eligible.
        store_b = _backoff(session, BIZ_B)
        assert store_b.get(PARTITION) is None
        decision_b = store_b.check_eligibility(PARTITION, now=_NOW + timedelta(seconds=60))
        assert decision_b.eligible is True
        assert decision_b.exists is False

        # B recording its own round creates a SEPARATE row; A's backoff is untouched.
        store_b.record_round(PARTITION, RoundYield(0, 5), ADMIN, now=_NOW)
        session.flush()
        assert session.query(SearchPartitionBackoff).count() == 2
        assert store_a.get(PARTITION).state == SearchPartitionState.BACKING_OFF.value


# --------------------------------------------------------------------------------------------------
# Postgres-backed test — disposable PostgreSQL (real transactions, not SQLite)
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)
_PG_SCHEMA = "dca_149_test"


@pytest.fixture
def pg_session_factory():
    """An ORM session factory over a fresh schema in the disposable Postgres (dropped on teardown)."""
    engine = make_engine(PG_URL)

    @event.listens_for(engine, "connect")
    def _set_search_path(dbapi_conn, _record):  # pragma: no cover - exercised only with a PG URL
        cur = dbapi_conn.cursor()
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{_PG_SCHEMA}"')
        cur.execute(f'SET search_path TO "{_PG_SCHEMA}"')
        cur.close()

    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        with engine.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{_PG_SCHEMA}" CASCADE')
        engine.dispose()


@requires_pg
def test_pg_exhausts_partition_and_denies(pg_session_factory):
    """On real Postgres: three no-new-result rounds advance one partition to EXHAUSTED, the durable row +
    its atomic audit entries survive, and the eligibility check refuses the exhausted partition — nothing
    sent or spent."""
    assert make_url(PG_URL).get_backend_name() == "postgresql"
    with pg_session_factory() as session:
        store = DiscoverySearchBackoff(session, BIZ_A, SETTINGS)
        for _ in range(3):
            store.record_round(PARTITION, RoundYield(0, 5), ADMIN, now=_NOW)
        session.commit()

    with pg_session_factory() as session:
        row = session.scalars(
            select(SearchPartitionBackoff).where(SearchPartitionBackoff.business_id == BIZ_A)
        ).one()
        assert row.state == SearchPartitionState.EXHAUSTED.value
        assert row.rounds_recorded == 3
        audits = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.business_id == BIZ_A)
            .where(AuditEvent.action == AUDIT_ACTION_RECORD_ROUND)
        ).all()
        assert len(audits) == 3

        decision = DiscoverySearchBackoff(session, BIZ_A, SETTINGS).check_eligibility(
            PARTITION, now=_NOW + timedelta(days=30)
        )
        assert decision.eligible is False
        assert decision.reason == DENY_EXHAUSTED
