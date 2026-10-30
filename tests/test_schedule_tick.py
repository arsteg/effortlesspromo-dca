"""DCA-108 — enqueue due recurring schedules exactly once logically.

DCA-107 persisted per-business recurring schedules as durable rows (a ``next_run_at`` due time) and
deliberately ran nothing. DCA-108 adds the TICK that turns a DUE schedule into an enqueued durable job
(DCA-099) exactly once logically and advances the schedule's ``next_run_at`` — still executing nothing.

Two layers, matching the DCA-099/101/106 suites:

- **Always-on logic tests** (in-memory SQLite) drive the tick contract serially: a due schedule enqueues
  exactly one job and advances its ``next_run_at`` by one interval; a repeat tick enqueues nothing more
  (the schedule is no longer due); a tick replaying the SAME cycle boundary collapses onto the one job via
  the deterministic idempotency key (exactly once logically); a not-yet-due and a PAUSED schedule are each
  skipped untouched (the no-op / denial case); a long outage uses the documented bounded catch-up (fire
  once, collapse the missed intervals, jump strictly past now); and the tick is tenant-scoped.
- **The real concurrent-tick safety on a disposable PostgreSQL** (``DCA_TEST_DATABASE_URL``) runs two
  tickers racing the SAME due schedule under ``FOR UPDATE SKIP LOCKED`` and asserts exactly ONE job and a
  single advance — no duplicate logical tick. It skips without a URL (the row-lock isolation is a
  PostgreSQL feature; SQLite omits ``FOR UPDATE`` and the deterministic key still keeps it exactly-once).

Spin up a throwaway Postgres, e.g.:
    docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55498:5432 postgres:16-alpine
    export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55498/dca"
"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from app.db.base import Base, make_engine, make_session_factory
from app.db.models import (
    AuditEvent,
    DurableJobRequest,
    RecurringSchedule,
    ScheduleStatus,
)
from app.services.schedule_tick import (
    AUDIT_ACTION_SCHEDULE_TICK,
    SCHEDULE_CYCLE_JOB_TYPE,
    DueScheduleEnqueuer,
    schedule_cycle_key,
)

BIZ_A = "11111111-1111-1111-1111-11111111c108"
BIZ_B = "22222222-2222-2222-2222-22222222c108"
NOW = datetime(2026, 10, 31, 9, 0, 0, tzinfo=UTC)
INTERVAL = 3600


def _seed_schedule(
    factory,
    business_id: str,
    *,
    schedule_key: str = "prospect.discovery",
    next_run_at: datetime,
    interval_seconds: int = INTERVAL,
    status: str = ScheduleStatus.ACTIVE.value,
    icp_version: int = 3,
    profile_version: int | None = None,
) -> str:
    """Insert a DCA-107 recurring-schedule row directly (isolates the DCA-108 tick from DCA-107 gating)."""
    with factory() as session:
        row = RecurringSchedule(
            id=str(uuid.uuid4()),
            business_id=business_id,
            schedule_key=schedule_key,
            interval_seconds=interval_seconds,
            next_run_at=next_run_at,
            status=status,
            icp_version=icp_version,
            profile_version=profile_version,
            actor_id="seed",
            actor_type="user",
        )
        session.add(row)
        session.commit()
        return row.id


def _job_count(factory, business_id: str) -> int:
    with factory() as session:
        return session.scalar(
            select(func.count())
            .select_from(DurableJobRequest)
            .where(DurableJobRequest.business_id == business_id)
        )


def _next_run(factory, schedule_id: str) -> datetime:
    with factory() as session:
        value = session.get(RecurringSchedule, schedule_id).next_run_at
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value


@pytest.fixture
def factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


# --------------------------------------------------------------------------------------------------
# Happy path: a due schedule enqueues exactly one job and advances next_run_at by one interval.
# --------------------------------------------------------------------------------------------------


def test_due_schedule_enqueues_one_job_and_advances(factory):
    sched_id = _seed_schedule(factory, BIZ_A, next_run_at=NOW)

    with factory() as session:
        report = DueScheduleEnqueuer(session, BIZ_A).tick(now=NOW)

    assert report.changed is True
    assert report.total == 1 and report.created_count == 1
    cycle = report.enqueued[0]
    assert cycle.schedule_id == sched_id
    assert cycle.job_created is True
    assert cycle.missed_intervals == 0
    assert cycle.cycle_key == schedule_cycle_key(sched_id, NOW)
    assert cycle.new_next_run_at == NOW + timedelta(seconds=INTERVAL)

    # Exactly one durable job, keyed to this cycle, under the single schedule-cycle job type.
    assert _job_count(factory, BIZ_A) == 1
    with factory() as session:
        job = session.scalars(select(DurableJobRequest)).one()
        assert job.job_type == SCHEDULE_CYCLE_JOB_TYPE
        assert job.idempotency_key == schedule_cycle_key(sched_id, NOW)
        assert job.status == "queued"  # enqueued only — nothing claims or runs it
        assert job.payload_metadata["scheduleKey"] == "prospect.discovery"
        assert job.payload_metadata["icpVersion"] == 3

    # The clock moved forward by one interval.
    assert _next_run(factory, sched_id) == NOW + timedelta(seconds=INTERVAL)

    # An atomic audit entry was written for the fired cycle.
    with factory() as session:
        events = session.scalars(
            select(AuditEvent).where(AuditEvent.action == AUDIT_ACTION_SCHEDULE_TICK)
        ).all()
        assert len(events) == 1
        assert events[0].references["scheduleId"] == sched_id
        assert events[0].references["jobCreated"] is True


# --------------------------------------------------------------------------------------------------
# A repeat tick enqueues nothing more — the schedule is no longer due (no duplicate logical tick).
# --------------------------------------------------------------------------------------------------


def test_repeat_tick_at_same_time_enqueues_nothing_more(factory):
    sched_id = _seed_schedule(factory, BIZ_A, next_run_at=NOW)

    with factory() as session:
        first = DueScheduleEnqueuer(session, BIZ_A).tick(now=NOW)
    assert first.total == 1

    # Same instant again: next_run_at is now one interval in the FUTURE, so nothing is due.
    with factory() as session:
        second = DueScheduleEnqueuer(session, BIZ_A).tick(now=NOW)
    assert second.changed is False and second.total == 0

    assert _job_count(factory, BIZ_A) == 1  # still exactly one job
    assert _next_run(factory, sched_id) == NOW + timedelta(seconds=INTERVAL)  # unchanged


# --------------------------------------------------------------------------------------------------
# Exactly once logically: a tick replaying the SAME cycle boundary collapses onto the one job.
# --------------------------------------------------------------------------------------------------


def test_replaying_the_same_cycle_boundary_does_not_double_enqueue(factory):
    """Even if the schedule is (artificially) due again for the SAME boundary — e.g. a crashed ticker that
    advanced the clock but lost its job, or a double-tick race — the deterministic per-cycle idempotency
    key collapses the second enqueue onto the first job. One logical tick, one job."""
    sched_id = _seed_schedule(factory, BIZ_A, next_run_at=NOW)

    with factory() as session:
        DueScheduleEnqueuer(session, BIZ_A).tick(now=NOW)
    assert _job_count(factory, BIZ_A) == 1

    # Force the schedule back to the SAME fired boundary (simulating a replay of that exact cycle).
    with factory() as session:
        session.execute(
            update(RecurringSchedule)
            .where(RecurringSchedule.id == sched_id)
            .values(next_run_at=NOW)
        )
        session.commit()

    with factory() as session:
        report = DueScheduleEnqueuer(session, BIZ_A).tick(now=NOW)

    assert report.total == 1
    assert report.enqueued[0].job_created is False  # collapsed onto the existing job
    assert report.created_count == 0
    assert _job_count(factory, BIZ_A) == 1  # STILL exactly one job — no duplicate logical tick


# --------------------------------------------------------------------------------------------------
# The no-op / denial case: a not-yet-due schedule and a PAUSED schedule are skipped untouched.
# --------------------------------------------------------------------------------------------------


def test_not_yet_due_schedule_is_skipped_and_changes_nothing(factory):
    future = NOW + timedelta(minutes=30)
    sched_id = _seed_schedule(factory, BIZ_A, next_run_at=future)

    with factory() as session:
        report = DueScheduleEnqueuer(session, BIZ_A).tick(now=NOW)

    assert report.changed is False and report.total == 0
    assert _job_count(factory, BIZ_A) == 0
    assert _next_run(factory, sched_id) == future  # untouched


def test_paused_schedule_is_never_fired(factory):
    sched_id = _seed_schedule(factory, BIZ_A, next_run_at=NOW, status=ScheduleStatus.PAUSED.value)

    with factory() as session:
        report = DueScheduleEnqueuer(session, BIZ_A).tick(now=NOW)

    assert report.changed is False and report.total == 0
    assert _job_count(factory, BIZ_A) == 0
    assert _next_run(factory, sched_id) == NOW  # a paused schedule is not advanced either


def test_empty_tick_with_no_schedules_changes_nothing(factory):
    with factory() as session:
        report = DueScheduleEnqueuer(session, BIZ_A).tick(now=NOW)
    assert report.changed is False and report.total == 0
    assert _job_count(factory, BIZ_A) == 0


# --------------------------------------------------------------------------------------------------
# Bounded catch-up: a long outage fires ONCE and collapses the missed intervals (no backlog storm).
# --------------------------------------------------------------------------------------------------


def test_missed_intervals_use_bounded_catchup(factory):
    # The schedule was due 3.5 intervals ago (scheduler was down); now is well past several boundaries.
    fired = NOW - timedelta(seconds=INTERVAL * 3 + INTERVAL // 2)
    sched_id = _seed_schedule(factory, BIZ_A, next_run_at=fired)

    with factory() as session:
        report = DueScheduleEnqueuer(session, BIZ_A).tick(now=NOW)

    # Fired exactly ONCE — the missed boundaries are collapsed, NOT replayed as one job each.
    assert report.total == 1
    cycle = report.enqueued[0]
    assert cycle.missed_intervals == 3  # three whole boundaries between the fired one and now
    assert _job_count(factory, BIZ_A) == 1  # one job, not four

    # next_run_at jumped to the next boundary STRICTLY AFTER now, on the original cadence.
    new_next = _next_run(factory, sched_id)
    assert new_next > NOW
    assert new_next == fired + timedelta(seconds=INTERVAL) * 4
    # And it is no longer due, so an immediate repeat tick does nothing more.
    with factory() as session:
        again = DueScheduleEnqueuer(session, BIZ_A).tick(now=NOW)
    assert again.total == 0
    assert _job_count(factory, BIZ_A) == 1


# --------------------------------------------------------------------------------------------------
# Tenant isolation: a tick never fires or advances another business's due schedule.
# --------------------------------------------------------------------------------------------------


def test_tick_is_tenant_scoped(factory):
    a_id = _seed_schedule(factory, BIZ_A, next_run_at=NOW)
    b_id = _seed_schedule(factory, BIZ_B, next_run_at=NOW)

    # Business B ticks -> fires only B's schedule; A's is untouched.
    with factory() as session:
        report_b = DueScheduleEnqueuer(session, BIZ_B).tick(now=NOW)
    assert report_b.total == 1 and report_b.enqueued[0].schedule_id == b_id

    assert _job_count(factory, BIZ_A) == 0  # A's queue untouched by B's tick
    assert _job_count(factory, BIZ_B) == 1
    assert _next_run(factory, a_id) == NOW  # A's due time not advanced by B
    assert _next_run(factory, b_id) == NOW + timedelta(seconds=INTERVAL)


# --------------------------------------------------------------------------------------------------
# The real concurrent-tick safety on a disposable PostgreSQL (skips without a URL).
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)


@pytest.fixture
def pg_engine():
    engine = make_engine(PG_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def pg_factory(pg_engine):
    return make_session_factory(pg_engine)


@requires_pg
def test_two_concurrent_ticks_fire_a_schedule_exactly_once_postgres(pg_factory):
    """``FOR UPDATE SKIP LOCKED``: two tickers racing one DUE schedule fire it exactly once (the other
    skips the locked row), so there is exactly ONE job and a SINGLE advance — no duplicate logical tick."""
    sched_id = _seed_schedule(pg_factory, BIZ_A, next_run_at=NOW)

    reports: list[object] = [None, None]
    barrier = threading.Barrier(2)
    lock = threading.Lock()

    def _tick(index: int) -> None:
        with pg_factory() as session:
            enqueuer = DueScheduleEnqueuer(session, BIZ_A)
            barrier.wait(timeout=10)
            report = enqueuer.tick(now=NOW)
        with lock:
            reports[index] = report

    threads = [threading.Thread(target=_tick, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    total_fired = sum(r.total for r in reports)  # type: ignore[union-attr]
    assert (
        total_fired == 1
    )  # exactly one ticker fired the schedule; the other skipped the locked row

    assert _job_count(pg_factory, BIZ_A) == 1  # exactly one job regardless of the race
    assert _next_run(pg_factory, sched_id) == NOW + timedelta(seconds=INTERVAL)  # advanced once


@requires_pg
def test_same_cycle_key_collapses_to_one_job_postgres(pg_factory):
    """Belt-and-suspenders: even presented twice for the SAME boundary on real Postgres, the deterministic
    cycle idempotency key yields exactly one durable job (the DCA-099 unique-constraint serialisation)."""
    sched_id = _seed_schedule(pg_factory, BIZ_A, next_run_at=NOW)

    with pg_factory() as session:
        DueScheduleEnqueuer(session, BIZ_A).tick(now=NOW)
    # Reset to the same boundary and tick again — same cycle key, so no second job.
    with pg_factory() as session:
        session.execute(
            update(RecurringSchedule)
            .where(RecurringSchedule.id == sched_id)
            .values(next_run_at=NOW)
        )
        session.commit()
    with pg_factory() as session:
        report = DueScheduleEnqueuer(session, BIZ_A).tick(now=NOW)

    assert report.total == 1 and report.enqueued[0].job_created is False
    assert _job_count(pg_factory, BIZ_A) == 1
