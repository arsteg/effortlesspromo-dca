"""DCA-107 — the per-business recurring-schedule store.

Two layers, matching the DCA-099 suite:

- **Always-on logic tests** (in-memory SQLite) drive the store's contract: a schedule is stored durably
  for a CONFIRMED + ENTITLED business, pinned to the confirmed ICP version; it survives a "restart"
  (a fresh engine/session over the same file DB re-reads the same persisted ``next_run_at``); storing it
  enqueues NOTHING; and each denial (unauthorized actor, unconfirmed business, unentitled business,
  unknown profile version, duplicate key, bad shape) refuses BEFORE any write so it changes nothing. The
  store is tenant-scoped, so one business can never see another's schedules.
- **The unique-constraint serialisation on a disposable PostgreSQL** (``DCA_TEST_DATABASE_URL``)
  exercises the real engine: two concurrent stores of the same ``(business, schedule_key)`` collapse to
  exactly one row. It skips when no URL is supplied.

Spin up a throwaway Postgres, e.g.:
    docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55497:5432 postgres:16-alpine
    export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55497/dca"
"""

from __future__ import annotations

import os
import tempfile
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.db.base import Base, make_engine, make_session_factory
from app.db.models import (
    BusinessProfileDraft,
    EntitlementProjection,
    IcpConfirmation,
    RecurringSchedule,
    ScheduleStatus,
)
from app.services.schedule_store import (
    REASON_INVALID_INTERVAL,
    REASON_NOT_CONFIRMED,
    REASON_NOT_ENTITLED,
    REASON_PROFILE_VERSION_NOT_FOUND,
    RecurringScheduleStore,
    ScheduleAuthorizationError,
    ScheduleConflict,
    ScheduleError,
    ScheduleInput,
    ScheduleManager,
)

BIZ_A = "11111111-1111-1111-1111-1111111111a7"
BIZ_B = "22222222-2222-2222-2222-2222222222b7"
NOW = datetime(2026, 10, 31, 9, 0, 0, tzinfo=UTC)
NEXT_RUN = datetime(2026, 10, 31, 10, 0, 0, tzinfo=UTC)

ADMIN = ScheduleManager(subject="op-admin", roles=("owner",), actor_type="user")
VIEWER = ScheduleManager(subject="op-viewer", roles=("viewer",), actor_type="user")


def _input(**overrides) -> ScheduleInput:
    base: dict = {
        "schedule_key": "prospect.discovery",
        "interval_seconds": 3600,
        "next_run_at": NEXT_RUN,
        "profile_version": None,
    }
    base.update(overrides)
    return ScheduleInput(**base)


def _confirm_icp(session, business_id: str, version: int = 1) -> None:
    """Seed the DCA-040 confirmed active ICP precondition for a business."""
    session.add(
        IcpConfirmation(
            business_id=business_id, version=version, actor_id="seed", actor_type="user"
        )
    )


def _entitle(session, business_id: str, *, expires_at: datetime | None = None) -> None:
    """Seed the DCA-023 active cached entitlement projection precondition for a business."""
    session.add(
        EntitlementProjection(
            business_id=business_id,
            status="active",
            plan="synthetic",
            capabilities=[],
            limits={},
            expires_at=expires_at,
            captured_at=NOW,
            version="v1",
        )
    )


@pytest.fixture
def db_path():
    # A file-backed SQLite DB so a "restart" (a fresh engine) can re-read persisted rows.
    path = Path(tempfile.gettempdir()) / f"dca-test-sched-{uuid.uuid4().hex[:8]}.db"
    yield path
    if path.exists():
        path.unlink()


@pytest.fixture
def factory(db_path):
    engine = make_engine(f"sqlite+pysqlite:///{db_path.as_posix()}")
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


def _count(factory, business_id: str) -> int:
    with factory() as session:
        return session.scalar(
            select(func.count())
            .select_from(RecurringSchedule)
            .where(RecurringSchedule.business_id == business_id)
        )


# --------------------------------------------------------------------------------------------------
# Happy path: a schedule is stored durably for a confirmed, entitled business — and runs nothing.
# --------------------------------------------------------------------------------------------------


def test_store_schedule_for_confirmed_entitled_business(factory):
    with factory() as session:
        _confirm_icp(session, BIZ_A, version=3)
        _entitle(session, BIZ_A)
        row = RecurringScheduleStore(session, BIZ_A).create(_input(), ADMIN, now=NOW)
        session.commit()
        schedule_id = row.id

    with factory() as session:
        stored = session.get(RecurringSchedule, schedule_id)
        assert stored.business_id == BIZ_A
        assert stored.status == ScheduleStatus.ACTIVE.value
        assert stored.schedule_key == "prospect.discovery"
        assert stored.interval_seconds == 3600
        # The confirmed active ICP version is snapshotted as the versioned profile reference.
        assert stored.icp_version == 3
        assert stored.profile_version is None
    assert _count(factory, BIZ_A) == 1


def test_stored_schedule_survives_a_process_restart(factory, db_path):
    """Create a schedule, then simulate a scheduler RESTART by opening a brand-new engine/session over
    the SAME file DB — the persisted next-run time is re-read unchanged (nothing in-memory)."""
    with factory() as session:
        _confirm_icp(session, BIZ_A)
        _entitle(session, BIZ_A)
        row = RecurringScheduleStore(session, BIZ_A).create(_input(), ADMIN, now=NOW)
        session.commit()
        schedule_id = row.id

    # "Restart": a fresh engine (new connection pool, no shared in-memory state) over the same file.
    restarted = make_engine(f"sqlite+pysqlite:///{db_path.as_posix()}")
    try:
        restarted_factory = make_session_factory(restarted)
        with restarted_factory() as session:
            after = RecurringScheduleStore(session, BIZ_A).by_key("prospect.discovery")
            assert after is not None
            assert after.id == schedule_id
            # The DUE time survived the restart unchanged.
            persisted = after.next_run_at
            if persisted.tzinfo is None:
                persisted = persisted.replace(tzinfo=UTC)
            assert persisted == NEXT_RUN
            # And it is visible as "due" at/after its next-run time — a read-only inspection.
            due = RecurringScheduleStore(session, BIZ_A).due(NEXT_RUN)
            assert [s.id for s in due] == [schedule_id]
    finally:
        restarted.dispose()


def test_storing_a_schedule_enqueues_no_job(factory):
    """Storing a schedule is durable DATA only — it never enqueues a durable job (DCA-108 is disabled)."""
    from app.db.models import DurableJobRequest

    with factory() as session:
        _confirm_icp(session, BIZ_A)
        _entitle(session, BIZ_A)
        RecurringScheduleStore(session, BIZ_A).create(_input(), ADMIN, now=NOW)
        session.commit()

    with factory() as session:
        jobs = session.scalar(select(func.count()).select_from(DurableJobRequest))
        assert jobs == 0  # nothing was enqueued; the schedule implies no running process


def test_optional_profile_version_is_pinned_when_it_exists(factory):
    with factory() as session:
        _confirm_icp(session, BIZ_A)
        _entitle(session, BIZ_A)
        session.add(BusinessProfileDraft(business_id=BIZ_A, version=2, products=[], languages=[]))
        session.flush()
        row = RecurringScheduleStore(session, BIZ_A).create(
            _input(profile_version=2), ADMIN, now=NOW
        )
        session.commit()
        assert row.profile_version == 2


# --------------------------------------------------------------------------------------------------
# Denials — each refuses BEFORE any write, so it changes nothing.
# --------------------------------------------------------------------------------------------------


def test_unauthorized_actor_is_refused_and_writes_nothing(factory):
    with factory() as session:
        _confirm_icp(session, BIZ_A)
        _entitle(session, BIZ_A)
        session.commit()
    with factory() as session:
        with pytest.raises(ScheduleAuthorizationError):
            RecurringScheduleStore(session, BIZ_A).create(_input(), VIEWER, now=NOW)
        session.rollback()
    assert _count(factory, BIZ_A) == 0


def test_unconfirmed_business_is_refused(factory):
    with factory() as session:
        _entitle(session, BIZ_A)  # entitled but NOT confirmed
        session.commit()
    with factory() as session:
        with pytest.raises(ScheduleError) as exc:
            RecurringScheduleStore(session, BIZ_A).create(_input(), ADMIN, now=NOW)
        assert exc.value.code == REASON_NOT_CONFIRMED
        session.rollback()
    assert _count(factory, BIZ_A) == 0


def test_unentitled_business_is_refused(factory):
    with factory() as session:
        _confirm_icp(session, BIZ_A)  # confirmed but NOT entitled
        session.commit()
    with factory() as session:
        with pytest.raises(ScheduleError) as exc:
            RecurringScheduleStore(session, BIZ_A).create(_input(), ADMIN, now=NOW)
        assert exc.value.code == REASON_NOT_ENTITLED
        session.rollback()
    assert _count(factory, BIZ_A) == 0


def test_lapsed_entitlement_is_not_entitled(factory):
    with factory() as session:
        _confirm_icp(session, BIZ_A)
        _entitle(session, BIZ_A, expires_at=NOW - timedelta(seconds=1))  # already expired
        session.commit()
    with factory() as session:
        with pytest.raises(ScheduleError) as exc:
            RecurringScheduleStore(session, BIZ_A).create(_input(), ADMIN, now=NOW)
        assert exc.value.code == REASON_NOT_ENTITLED
        session.rollback()
    assert _count(factory, BIZ_A) == 0


def test_unknown_profile_version_is_refused(factory):
    with factory() as session:
        _confirm_icp(session, BIZ_A)
        _entitle(session, BIZ_A)
        session.commit()
    with factory() as session:
        with pytest.raises(ScheduleError) as exc:
            RecurringScheduleStore(session, BIZ_A).create(
                _input(profile_version=99), ADMIN, now=NOW
            )
        assert exc.value.code == REASON_PROFILE_VERSION_NOT_FOUND
        session.rollback()
    assert _count(factory, BIZ_A) == 0


def test_invalid_interval_is_refused(factory):
    with factory() as session:
        _confirm_icp(session, BIZ_A)
        _entitle(session, BIZ_A)
        session.commit()
    with factory() as session:
        with pytest.raises(ScheduleError) as exc:
            RecurringScheduleStore(session, BIZ_A).create(
                _input(interval_seconds=0), ADMIN, now=NOW
            )
        assert exc.value.code == REASON_INVALID_INTERVAL
        session.rollback()
    assert _count(factory, BIZ_A) == 0


def test_duplicate_schedule_key_is_a_conflict_and_changes_nothing(factory):
    with factory() as session:
        _confirm_icp(session, BIZ_A)
        _entitle(session, BIZ_A)
        RecurringScheduleStore(session, BIZ_A).create(_input(), ADMIN, now=NOW)
        session.commit()
    with factory() as session:
        with pytest.raises(ScheduleConflict):
            RecurringScheduleStore(session, BIZ_A).create(
                _input(interval_seconds=7200), ADMIN, now=NOW
            )
        session.rollback()
    # Still exactly the one original schedule, with its original cadence.
    assert _count(factory, BIZ_A) == 1
    with factory() as session:
        only = session.scalars(select(RecurringSchedule)).one()
        assert only.interval_seconds == 3600


# --------------------------------------------------------------------------------------------------
# Tenant isolation.
# --------------------------------------------------------------------------------------------------


def test_same_key_in_two_businesses_is_two_schedules(factory):
    with factory() as session:
        for biz in (BIZ_A, BIZ_B):
            _confirm_icp(session, biz)
            _entitle(session, biz)
        RecurringScheduleStore(session, BIZ_A).create(_input(), ADMIN, now=NOW)
        RecurringScheduleStore(session, BIZ_B).create(_input(), ADMIN, now=NOW)
        session.commit()
    assert _count(factory, BIZ_A) == 1
    assert _count(factory, BIZ_B) == 1


def test_store_cannot_find_another_businesss_schedule(factory):
    with factory() as session:
        _confirm_icp(session, BIZ_A)
        _entitle(session, BIZ_A)
        RecurringScheduleStore(session, BIZ_A).create(_input(), ADMIN, now=NOW)
        session.commit()
    with factory() as session:
        # B looks up A's exact key: business-scoped query resolves nothing.
        assert RecurringScheduleStore(session, BIZ_B).by_key("prospect.discovery") is None


# --------------------------------------------------------------------------------------------------
# The unique-constraint serialisation on a disposable PostgreSQL (skips without a URL).
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)


@pytest.fixture
def pg_factory():
    engine = make_engine(PG_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    Base.metadata.drop_all(engine)
    engine.dispose()


@requires_pg
def test_concurrent_store_of_same_key_yields_one_schedule(pg_factory):
    """Two threads store the SAME ``(business, schedule_key)`` at once over separate sessions. The
    unique constraint serialises them: exactly one row survives; the loser raises on its flush and is
    rolled back, so the conflict leaves a single schedule — never two."""
    with pg_factory() as session:
        _confirm_icp(session, BIZ_A)
        _entitle(session, BIZ_A)
        session.commit()

    barrier = threading.Barrier(2)
    created: list[str] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def _worker():
        try:
            with pg_factory() as session:
                barrier.wait(timeout=10)
                try:
                    row = RecurringScheduleStore(session, BIZ_A).create(_input(), ADMIN, now=NOW)
                    session.commit()
                    with lock:
                        created.append(row.id)
                except (IntegrityError, ScheduleConflict):
                    session.rollback()
        except Exception as exc:  # pragma: no cover - only on a real failure
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert errors == []
    assert _count(pg_factory, BIZ_A) == 1  # exactly one schedule regardless of the race
