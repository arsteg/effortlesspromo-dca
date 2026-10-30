"""DCA-109 — run the Python worker process.

The earlier Stage-09 stories built the durable-job mechanics (DCA-099 store, DCA-101 claim/lease, DCA-104
retry/dead-letter, DCA-106 recovery, DCA-108 schedule tick). DCA-109 adds the WORKER PROCESS that drives
them in one supervised loop with graceful shutdown — and keeps handler EXECUTION a disabled future
capability (:data:`app.jobs.worker.EXECUTION_ENABLED` is ``False``): the worker claims and drives the
lifecycle but never runs a handler, does I/O or sends.

Two layers, matching the DCA-101/106 suites:

- **Always-on logic tests** (in-memory SQLite) drive the worker serially: a claimed job is parked
  ``DEAD_LETTER`` safely (never run, never sent); a registered handler's ``run`` is NEVER invoked; a
  shutdown stops new claims and relinquishes held work back to the queue; the maintenance ticks (recovery
  + schedule) run; the worker is tenant-scoped and skips disabled businesses; and a bounded run
  terminates (no crash-loop on an unrunnable job).
- **The real claim → park flow on a disposable PostgreSQL** (``DCA_TEST_DATABASE_URL``) re-runs the drive
  against real commits. It skips without a URL (``FOR UPDATE SKIP LOCKED`` is PostgreSQL-only).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from app.core.config import Settings
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import (
    DurableJobRequest,
    JobRequestStatus,
    RecurringSchedule,
    ScheduleStatus,
    TenantAgent,
)
from app.jobs.claim import JobClaimStore
from app.jobs.handlers import JobHandler, JobHandlerRegistry
from app.jobs.store import JobEnqueueRequest, JobRequestStore
from app.jobs.worker import (
    PARK_EXECUTION_DISABLED,
    PARK_NO_HANDLER,
    ClaimDisposition,
    DurableJobWorker,
)

BIZ_A = "11111111-1111-1111-1111-11111111c109"
BIZ_B = "22222222-2222-2222-2222-22222222c109"
NOW = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)


def _settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="testing",
        database_url="sqlite+pysqlite:///:memory:",
        service_auth_token="test-service-token",
        worker_interval_seconds=1,
    )


@pytest.fixture
def factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


def _enable(factory, business_id: str) -> None:
    with factory() as session:
        session.add(TenantAgent(business_id=business_id, country="IN", enabled=True))
        session.commit()


def _enqueue(factory, business_id: str, key: str, *, job_type: str = "prospect.research") -> str:
    with factory() as session:
        row, _ = JobRequestStore(session, business_id).enqueue(
            JobEnqueueRequest(
                job_type=job_type,
                idempotency_key=key,
                payload_ref=f"dca-payload://synthetic/{key}",
            ),
            now=NOW,
        )
        session.commit()
        return row.id


def _status(factory, job_id: str) -> str:
    with factory() as session:
        return session.get(DurableJobRequest, job_id).status


def _worker(factory, **kwargs) -> DurableJobWorker:
    return DurableJobWorker(factory, _settings(), clock=lambda: NOW, **kwargs)


# --------------------------------------------------------------------------------------------------
# A claimed job is PARKED safely — never run, never sent (the headline: execution stays disabled).
# --------------------------------------------------------------------------------------------------


def test_run_pass_parks_claimed_jobs_without_running_or_sending(factory):
    _enable(factory, BIZ_A)
    job_id = _enqueue(factory, BIZ_A, "job-1")

    report = _worker(factory).run_pass(now=NOW)

    assert report.parked_count == 1 and report.relinquished_count == 0
    handled = report.handled[0]
    assert handled.job_id == job_id
    assert handled.disposition is ClaimDisposition.PARKED_NO_HANDLER
    assert handled.status == JobRequestStatus.DEAD_LETTER.value

    with factory() as session:
        row = session.get(DurableJobRequest, job_id)
        assert row.status == JobRequestStatus.DEAD_LETTER.value  # parked, not executed
        assert row.last_error == PARK_NO_HANDLER
        assert row.lease_owner is None and row.lease_expires_at is None  # lease cleared
        assert row.result_ref is None and row.completed_at is None  # nothing was produced / sent
        assert row.attempt_count == 1  # the claim consumed one attempt


def test_bounded_run_terminates_and_does_not_crash_loop(factory):
    """A parked job is terminal (no longer claimable), so a second pass claims nothing — the worker
    never crash-loops on an unrunnable job. A bounded run returns rather than hanging."""
    _enable(factory, BIZ_A)
    _enqueue(factory, BIZ_A, "job-1")

    worker = _worker(factory)
    passes = worker.run(max_passes=2, idle_sleep_seconds=0)

    assert passes == 2
    with factory() as session:
        statuses = session.scalars(
            select(DurableJobRequest.status).where(DurableJobRequest.business_id == BIZ_A)
        ).all()
    # Only the one job, parked once; never resurrected / re-parked into a spin.
    assert statuses == [JobRequestStatus.DEAD_LETTER.value]


# --------------------------------------------------------------------------------------------------
# A registered handler is recognised but its run() is NEVER invoked (execution disabled).
# --------------------------------------------------------------------------------------------------


class _RecordingPayload(BaseModel):
    pass


class _RecordingHandler(JobHandler):
    job_type = "prospect.research"
    schema_version = 1
    payload_model = _RecordingPayload

    def __init__(self) -> None:
        self.invoked = False

    def run(self, payload: BaseModel) -> None:
        self.invoked = True  # if the worker ever executes, this flips — it must not


def test_registered_handler_is_parked_execution_disabled_and_run_never_invoked(factory):
    _enable(factory, BIZ_A)
    job_id = _enqueue(factory, BIZ_A, "job-1", job_type="prospect.research")

    handler = _RecordingHandler()
    registry = JobHandlerRegistry()
    registry.register(handler)

    report = _worker(factory, registry=registry).run_pass(now=NOW)

    assert report.handled[0].disposition is ClaimDisposition.PARKED_EXECUTION_DISABLED
    assert handler.invoked is False  # the worker never ran the handler body
    with factory() as session:
        row = session.get(DurableJobRequest, job_id)
        assert row.status == JobRequestStatus.DEAD_LETTER.value
        assert row.last_error == PARK_EXECUTION_DISABLED


# --------------------------------------------------------------------------------------------------
# Graceful shutdown: stops NEW claims AND relinquishes active work safely (the shutdown/failure case).
# --------------------------------------------------------------------------------------------------


def test_shutdown_stops_new_claims(factory):
    _enable(factory, BIZ_A)
    job_id = _enqueue(factory, BIZ_A, "job-1")

    worker = _worker(factory)
    worker.request_shutdown()  # stop requested BEFORE the pass
    report = worker.run_pass(now=NOW)

    assert worker.shutting_down is True
    assert report.handled == ()  # claimed nothing
    assert _status(factory, job_id) == JobRequestStatus.QUEUED.value  # left untouched


def test_relinquish_returns_a_held_claim_to_the_queue(factory):
    _enable(factory, BIZ_A)
    job_id = _enqueue(factory, BIZ_A, "job-1")

    worker = _worker(factory)
    claim = worker.claim_one(BIZ_A, now=NOW)
    assert claim is not None
    assert _status(factory, job_id) == JobRequestStatus.RUNNING.value  # claimed/leased

    worker.request_shutdown()
    assert worker.relinquish(claim, now=NOW) is True

    with factory() as session:
        row = session.get(DurableJobRequest, job_id)
        assert row.status == JobRequestStatus.QUEUED.value  # preserved, claimable again
        assert row.lease_owner is None and row.lease_expires_at is None


def test_shutdown_racing_a_claim_relinquishes_it_inside_the_drain(factory):
    """If a shutdown lands AFTER a job is claimed but before it is handled, the drain relinquishes the
    held job back to the queue rather than wedging it RUNNING — the acceptance's 'relinquish active
    work safely'. Driven deterministically by latching shutdown at the moment of the claim."""
    _enable(factory, BIZ_A)
    job_id = _enqueue(factory, BIZ_A, "job-1")

    class _RacingWorker(DurableJobWorker):
        def claim_one(self, business_id, *, now):
            claim = super().claim_one(business_id, now=now)
            if claim is not None:
                self.request_shutdown()  # the shutdown races in right after the claim
            return claim

    worker = _RacingWorker(factory, _settings(), clock=lambda: NOW)
    handled = worker.drain_business(BIZ_A, now=NOW)

    assert len(handled) == 1
    assert handled[0].disposition is ClaimDisposition.RELINQUISHED
    with factory() as session:
        row = session.get(DurableJobRequest, job_id)
        assert row.status == JobRequestStatus.QUEUED.value  # relinquished, not executed
        assert row.lease_owner is None and row.result_ref is None  # nothing produced / sent


# --------------------------------------------------------------------------------------------------
# The maintenance ticks run: DCA-108 schedule enqueue + DCA-106 lease recovery.
# --------------------------------------------------------------------------------------------------


def test_run_pass_fires_due_schedules_and_recovers_abandoned_leases(factory):
    _enable(factory, BIZ_A)
    # A DUE recurring schedule → the schedule tick enqueues one cycle job.
    with factory() as session:
        session.add(
            RecurringSchedule(
                business_id=BIZ_A,
                schedule_key="prospect.discovery",
                interval_seconds=3600,
                next_run_at=NOW,
                status=ScheduleStatus.ACTIVE.value,
                icp_version=1,
                profile_version=None,
                actor_id="seed",
                actor_type="user",
            )
        )
        session.commit()
    # An abandoned lease (a crashed worker): RUNNING with a lapsed lease → recovery heals it.
    crashed_id = _enqueue(factory, BIZ_A, "job-crashed")
    with factory() as session:
        JobClaimStore(session, BIZ_A).claim_next("dead-worker", now=NOW, lease_seconds=60)

    later = NOW + timedelta(seconds=120)  # past the abandoned lease
    worker = DurableJobWorker(factory, _settings(), clock=lambda: later)
    report = worker.run_pass(now=later)

    # The schedule tick enqueued a cycle job for A (then the same pass drained/parked it safely).
    assert report.scheduled[BIZ_A].created_count == 1
    # The recovery sweep healed the crashed worker's abandoned lease.
    assert report.recovered[BIZ_A].total == 1

    with factory() as session:
        crashed = session.get(DurableJobRequest, crashed_id)
        # Recovered to QUEUED (new fence), then claimed + parked by this same pass's drain.
        assert crashed.status == JobRequestStatus.DEAD_LETTER.value
        assert (
            crashed.lease_version >= 2
        )  # recovery bumped the fence, then the drain claim bumped again


# --------------------------------------------------------------------------------------------------
# Tenant isolation: the worker only touches enabled businesses, never another tenant's or a disabled one.
# --------------------------------------------------------------------------------------------------


def test_worker_only_processes_enabled_businesses(factory):
    _enable(factory, BIZ_A)  # BIZ_B is NOT enabled
    a_job = _enqueue(factory, BIZ_A, "job-a")
    b_job = _enqueue(factory, BIZ_B, "job-b")

    worker = _worker(factory)
    assert worker.enabled_business_ids() == (BIZ_A,)
    worker.run_pass(now=NOW)

    assert _status(factory, a_job) == JobRequestStatus.DEAD_LETTER.value  # A processed
    assert _status(factory, b_job) == JobRequestStatus.QUEUED.value  # B untouched (disabled)


# --------------------------------------------------------------------------------------------------
# The real claim → park flow on a disposable PostgreSQL.
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
def test_worker_claims_and_parks_on_postgres(pg_factory):
    _enable(pg_factory, BIZ_A)
    job_id = _enqueue(pg_factory, BIZ_A, "job-pg")

    report = DurableJobWorker(pg_factory, _settings(), clock=lambda: NOW).run_pass(now=NOW)

    assert report.parked_count == 1
    with pg_factory() as session:
        row = session.get(DurableJobRequest, job_id)
        assert row.status == JobRequestStatus.DEAD_LETTER.value
        assert row.last_error == PARK_NO_HANDLER
        assert row.lease_owner is None
