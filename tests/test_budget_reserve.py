"""DCA-113 — reserve spend atomically against the business budget caps.

Two layers, matching the DCA-042/107/111 suites:

- **Service unit tests** (in-memory SQLite) pin the store logic: authority is checked FIRST; a
  successful reserve decrements the remaining budget and stages an atomic audit entry; a reservation
  that would exceed ANY cap (per-run, daily, monthly, provider, platform ceiling) is refused with a
  stable code and changes nothing; the reservation is idempotent on its key; holds are scoped per
  business; windows are evaluated in the caps' India-local civil time; and the store stages (never
  commits) into the caller's session.
- **A PostgreSQL concurrency test** (disposable Postgres, same skip guard as ``tests/test_rls.py`` /
  ``tests/test_migrations.py``) proves the load-bearing property SQLite cannot: several reservations
  racing near a cap SERIALIZE on the ``FOR UPDATE`` lock, so the total reserved never overshoots the cap
  — exactly the DCA-113 acceptance "concurrent reservations cannot overshoot any cap".

SQLite is sufficient for the arithmetic / idempotency / audit / scoping; the transaction-safe
serialization is a PostgreSQL property, so it is exercised only on real Postgres. Reserving holds budget
and spends/sends nothing (there is no external call to make), so no "no external call" stub is needed.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from app.audit.sink import ActorType, AuditActor
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import AuditEvent, BudgetReservation
from app.services.budget_store import BudgetLimitInput, BudgetLimitStore
from app.cost.reserve import (
    AUDIT_ACTION_RESERVE,
    PLATFORM_PER_RESERVATION_CEILING_MINOR,
    REASON_DAILY_EXCEEDED,
    REASON_INVALID_AMOUNT,
    REASON_MONTHLY_EXCEEDED,
    REASON_NO_BUDGET,
    REASON_PER_RUN_EXCEEDED,
    REASON_PLATFORM_EXCEEDED,
    REASON_PROVIDER_EXCEEDED,
    BudgetReservationStore,
    ReservationActor,
    ReservationAuthorizationError,
    ReservationError,
    ReservationRequest,
)

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"
ADMIN = ReservationActor(subject="00000000-0000-4000-9000-0000000a0113", roles=("admin",))
VIEWER = ReservationActor(subject="00000000-0000-4000-9000-0000000v0113", roles=("viewer",))
BUDGET_ACTOR = AuditActor(id="00000000-0000-4000-9000-0000000b0113", type=ActorType.USER)

NOW = datetime(
    2026, 10, 31, 9, 0, 0, tzinfo=UTC
)  # 2026-10-31 14:30 IST → day 2026-10-31, month 2026-10


def _seed_limit(session, business_id, *, daily=5_000, monthly=1_000_000, per_run=5_000) -> None:
    """Seed one in-force BudgetLimit (version 1) for a business via the real DCA-042 store."""
    BudgetLimitStore(session, business_id).save(
        BudgetLimitInput(
            daily_cap_minor=daily, monthly_cap_minor=monthly, per_run_cap_minor=per_run
        ),
        BUDGET_ACTOR,
    )
    session.commit()


def _req(key: str, amount: int, **kw) -> ReservationRequest:
    return ReservationRequest(idempotency_key=key, amount_minor=amount, **kw)


def _reserve_audit_count(session, business_id) -> int:
    return (
        session.query(AuditEvent)
        .filter(AuditEvent.business_id == business_id, AuditEvent.action == AUDIT_ACTION_RESERVE)
        .count()
    )


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


# --------------------------------------------------------------------------------------------------
# Service unit tests (in-memory SQLite)
# --------------------------------------------------------------------------------------------------


def test_successful_reserve_decrements_remaining_and_audits(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000)
        store = BudgetReservationStore(session, BIZ_A)
        before = store.window(now=NOW)
        assert before.configured and before.daily_remaining_minor == 5_000

        resv = store.reserve(_req("run-1", 2_000), ADMIN, now=NOW)
        session.commit()

        assert resv.amount_minor == 2_000
        assert resv.currency == "INR"
        assert resv.provider == "platform"
        assert resv.budget_version == 1
        assert resv.day_key == "2026-10-31" and resv.month_key == "2026-10"
        assert resv.status == "active"

        after = store.window(now=NOW)
        assert after.day_reserved_minor == 2_000
        assert after.daily_remaining_minor == 3_000  # decremented by the hold
        assert _reserve_audit_count(session, BIZ_A) == 1


def test_over_budget_reservation_is_refused_and_changes_nothing(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000)
        store = BudgetReservationStore(session, BIZ_A)
        store.reserve(_req("run-1", 4_000), ADMIN, now=NOW)
        session.commit()

        # 4_000 already held; a 2_000 hold would make 6_000 > the 5_000 daily cap → refused.
        with pytest.raises(ReservationError) as exc:
            store.reserve(_req("run-2", 2_000), ADMIN, now=NOW)
        assert exc.value.code == REASON_DAILY_EXCEEDED
        session.rollback()

        # The refusal staged no second hold and no second audit; the first hold is intact.
        assert session.query(BudgetReservation).filter_by(business_id=BIZ_A).count() == 1
        assert _reserve_audit_count(session, BIZ_A) == 1
        assert store.window(now=NOW).day_reserved_minor == 4_000  # unchanged by the denial


def test_exact_fit_is_allowed_then_the_next_overshoots(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000)
        store = BudgetReservationStore(session, BIZ_A)
        store.reserve(_req("r1", 3_000), ADMIN, now=NOW)
        store.reserve(_req("r2", 2_000), ADMIN, now=NOW)  # exactly fills the 5_000 cap
        session.commit()
        assert store.window(now=NOW).daily_remaining_minor == 0
        with pytest.raises(ReservationError) as exc:
            store.reserve(_req("r3", 1), ADMIN, now=NOW)  # one paise over
        assert exc.value.code == REASON_DAILY_EXCEEDED
        session.rollback()


def test_per_run_cap_is_enforced(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=10_000, per_run=1_000)
        store = BudgetReservationStore(session, BIZ_A)
        with pytest.raises(ReservationError) as exc:
            store.reserve(_req("r1", 1_001), ADMIN, now=NOW)  # over per-run, under daily
        assert exc.value.code == REASON_PER_RUN_EXCEEDED
        session.rollback()


def test_monthly_cap_is_enforced(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000, monthly=5_000, per_run=5_000)
        store = BudgetReservationStore(session, BIZ_A)
        # Day 1: hold 4_000 (within the day cap).
        day1 = datetime(2026, 10, 10, 9, 0, 0, tzinfo=UTC)
        store.reserve(_req("r1", 4_000), ADMIN, now=day1)
        session.commit()
        # Day 2 of the SAME month: 4_000 is within the fresh day cap, but 4_000+4_000 > 5_000 monthly.
        day2 = datetime(2026, 10, 20, 9, 0, 0, tzinfo=UTC)
        with pytest.raises(ReservationError) as exc:
            store.reserve(_req("r2", 4_000), ADMIN, now=day2)
        assert exc.value.code == REASON_MONTHLY_EXCEEDED
        session.rollback()


def test_provider_cap_is_enforced_when_supplied(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=10_000)
        store = BudgetReservationStore(session, BIZ_A)
        store.reserve(
            _req("r1", 600, provider="vendor_x", provider_cap_minor=1_000), ADMIN, now=NOW
        )
        session.commit()
        # vendor_x already holds 600; another 600 would exceed its 1_000 provider cap (daily is fine).
        with pytest.raises(ReservationError) as exc:
            store.reserve(
                _req("r2", 600, provider="vendor_x", provider_cap_minor=1_000), ADMIN, now=NOW
            )
        assert exc.value.code == REASON_PROVIDER_EXCEEDED
        session.rollback()
        # A DIFFERENT provider is unaffected by vendor_x's bucket.
        other = store.reserve(
            _req("r3", 600, provider="vendor_y", provider_cap_minor=1_000), ADMIN, now=NOW
        )
        assert other.provider == "vendor_y"


def test_platform_per_reservation_ceiling_bounds_even_huge_caps(session_factory):
    # Even a (mis)configured giant BudgetLimit cannot authorize a reservation past the platform ceiling.
    giant = PLATFORM_PER_RESERVATION_CEILING_MINOR * 2
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=giant, monthly=giant, per_run=giant)
        store = BudgetReservationStore(session, BIZ_A)
        with pytest.raises(ReservationError) as exc:
            store.reserve(_req("r1", PLATFORM_PER_RESERVATION_CEILING_MINOR + 1), ADMIN, now=NOW)
        assert exc.value.code == REASON_PLATFORM_EXCEEDED
        session.rollback()


def test_reservation_is_idempotent_on_key(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000)
        store = BudgetReservationStore(session, BIZ_A)
        first = store.reserve(_req("same-key", 2_000), ADMIN, now=NOW)
        session.commit()
        # A replay with the SAME key returns the existing hold (even with a different amount) and stages
        # no second row / no second hold.
        replay = store.reserve(_req("same-key", 9_999), ADMIN, now=NOW)
        session.commit()
        assert replay.id == first.id
        assert replay.amount_minor == 2_000  # the original hold is authoritative
        assert session.query(BudgetReservation).filter_by(business_id=BIZ_A).count() == 1
        assert _reserve_audit_count(session, BIZ_A) == 1
        assert store.window(now=NOW).day_reserved_minor == 2_000


def test_no_budget_configured_is_refused(session_factory):
    with session_factory() as session:
        store = BudgetReservationStore(session, BIZ_A)  # no BudgetLimit seeded
        with pytest.raises(ReservationError) as exc:
            store.reserve(_req("r1", 1), ADMIN, now=NOW)
        assert exc.value.code == REASON_NO_BUDGET
        session.rollback()
        assert session.query(BudgetReservation).count() == 0


def test_invalid_amount_is_refused(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A)
        store = BudgetReservationStore(session, BIZ_A)
        for bad in (0, -5):
            with pytest.raises(ReservationError) as exc:
                store.reserve(_req("r1", bad), ADMIN, now=NOW)
            assert exc.value.code == REASON_INVALID_AMOUNT
        session.rollback()


def test_authority_is_checked_first_and_changes_nothing(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A)
        store = BudgetReservationStore(session, BIZ_A)
        # A read-only viewer lacks budget.manage → refused before anything is read/written.
        with pytest.raises(ReservationAuthorizationError):
            store.reserve(_req("r1", 1_000), VIEWER, now=NOW)
        session.rollback()
        assert session.query(BudgetReservation).count() == 0
        assert _reserve_audit_count(session, BIZ_A) == 0


def test_holds_are_scoped_per_business(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000)
        _seed_limit(session, BIZ_B, daily=5_000)
        BudgetReservationStore(session, BIZ_A).reserve(_req("r1", 2_000), ADMIN, now=NOW)
        session.commit()
        # B never sees A's hold; B has its own independent remaining budget.
        b_store = BudgetReservationStore(session, BIZ_B)
        assert b_store.active() == []
        assert b_store.window(now=NOW).day_reserved_minor == 0
        assert b_store.window(now=NOW).daily_remaining_minor == 5_000
        # A reuses the same idempotency key as it could in B without collision.
        b_resv = b_store.reserve(_req("r1", 1_000), ADMIN, now=NOW)
        assert b_resv.amount_minor == 1_000


def test_window_uses_india_local_civil_day(session_factory):
    # 2026-10-31 19:00 UTC == 2026-11-01 00:30 IST, so the hold counts in the IST day 2026-11-01.
    late_utc = datetime(2026, 10, 31, 19, 0, 0, tzinfo=UTC)
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000)
        store = BudgetReservationStore(session, BIZ_A)
        resv = store.reserve(_req("r1", 1_000), ADMIN, now=late_utc)
        session.commit()
        assert resv.day_key == "2026-11-01" and resv.month_key == "2026-11"
        # The same instant evaluated as a UTC day (2026-10-31) would show no hold — it is India-local.
        assert (
            store.window(now=datetime(2026, 11, 1, 1, 0, 0, tzinfo=UTC)).day_reserved_minor == 1_000
        )


def test_reserve_does_not_commit(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A)
        BudgetReservationStore(session, BIZ_A).reserve(_req("r1", 1_000), ADMIN, now=NOW)
        session.rollback()  # the store never commits — a rollback leaves nothing behind
    with session_factory() as session:
        assert session.query(BudgetReservation).count() == 0
        assert _reserve_audit_count(session, BIZ_A) == 0


# --------------------------------------------------------------------------------------------------
# PostgreSQL concurrency test — the transaction-safe serialization SQLite cannot show
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)
_PG_SCHEMA = "dca_reserve_test"


@pytest.fixture
def pg_factory():
    """A fresh schema with the DCA tables, a seeded in-force BudgetLimit, and a session factory whose
    connections all resolve to that schema (so threads share one physical datastore)."""
    from sqlalchemy import create_engine, text

    # Create the throwaway schema as the owner (bypasses RLS; the concurrency property under test is the
    # FOR UPDATE lock, not RLS, which tests/test_rls.py already covers on real Postgres).
    admin = create_engine(PG_URL)
    with admin.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{_PG_SCHEMA}" CASCADE'))
        conn.execute(text(f'CREATE SCHEMA "{_PG_SCHEMA}"'))
    admin.dispose()

    engine = create_engine(PG_URL, connect_args={"options": f"-csearch_path={_PG_SCHEMA}"})
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000, monthly=1_000_000, per_run=5_000)
    try:
        yield factory
    finally:
        engine.dispose()
        admin = create_engine(PG_URL)
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{_PG_SCHEMA}" CASCADE'))
        admin.dispose()


@requires_pg
def test_concurrent_reservations_cannot_overshoot_the_cap(pg_factory):
    """Several reservations racing near the daily cap SERIALIZE on the FOR UPDATE lock, so the total
    reserved never exceeds the cap — the DCA-113 concurrency acceptance, on real Postgres.

    Daily cap 5_000, each attempt holds 2_000: at most TWO can be admitted (4_000 ≤ 5_000; a third would
    be 6_000 > 5_000). Eight threads race; exactly two must win, the rest refused with the daily code,
    and the summed hold must be 4_000 — never 6_000+ (which an unserialized read-modify-write would allow).
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy import func, select

    n = 8
    amount = 2_000
    barrier = threading.Barrier(n)
    outcomes: list[str] = []
    lock = threading.Lock()

    def _attempt(i: int) -> None:
        barrier.wait()  # release all threads together to maximize contention on the lock
        session = pg_factory()
        try:
            BudgetReservationStore(session, BIZ_A).reserve(
                _req(f"race-{i}", amount), ADMIN, now=NOW
            )
            session.commit()
            result = "admitted"
        except ReservationError as exc:
            session.rollback()
            result = exc.code
        except Exception:  # pragma: no cover - defensive; surfaced via the outcomes assertion
            session.rollback()
            raise
        finally:
            session.close()
        with lock:
            outcomes.append(result)

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(_attempt, range(n)))

    admitted = [o for o in outcomes if o == "admitted"]
    refused = [o for o in outcomes if o != "admitted"]
    assert len(admitted) == 2, outcomes  # exactly the cap's worth were admitted
    assert all(o == REASON_DAILY_EXCEEDED for o in refused), outcomes

    with pg_factory() as session:
        total = session.scalar(
            select(func.coalesce(func.sum(BudgetReservation.amount_minor), 0)).where(
                BudgetReservation.business_id == BIZ_A
            )
        )
        rows = session.scalar(
            select(func.count())
            .select_from(BudgetReservation)
            .where(BudgetReservation.business_id == BIZ_A)
        )
    assert total == 4_000  # never overshot the 5_000 cap
    assert rows == 2
