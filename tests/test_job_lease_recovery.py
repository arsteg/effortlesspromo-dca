"""DCA-106 — recover abandoned job leases.

DCA-101 leased a job to a worker with a fencing token; a worker that crashes or stalls never commits,
renews or fails its job, so the lease simply lapses. DCA-106 adds the RECOVERY SWEEP that finds those
abandoned (``RUNNING`` + lapsed-lease) rows and safely makes the work retryable again under a NEW fence —
so a crash never permanently loses the work AND the crashed process can never later commit.

Two layers, matching the DCA-101/102 suites:

- **Always-on logic tests** (in-memory SQLite) drive the recovery contract serially: an abandoned lease
  is recovered to ``QUEUED`` under a bumped fence (the crashed holder fenced off); a LIVE lease and
  non-running rows are never swept (an empty/no-op sweep changes nothing); a job whose attempt budget the
  crash exhausted is DEAD-LETTERED, not resurrected; the sweep is tenant-scoped; and — the headline — a
  recovered job is claimed + completed by ANOTHER worker while the crashed worker's commit is refused as a
  stale lease.
- **The real crash → recover → commit flow on a disposable PostgreSQL** (``DCA_TEST_DATABASE_URL``)
  re-runs the headline acceptance against real commits/rollbacks, and exercises two concurrent sweeps
  racing one abandoned row (``FOR UPDATE SKIP LOCKED``) recovering it exactly once. These skip without a
  URL, because the row-lock isolation is a PostgreSQL feature (SQLite omits ``FOR UPDATE``).

Spin up a throwaway Postgres, e.g.:
    docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55471:5432 postgres:16-alpine
    export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55471/dca"
"""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.base import Base, make_engine, make_session_factory
from app.db.models import DurableJobRequest, JobRequestStatus
from app.jobs.claim import JobClaimStore
from app.jobs.complete import JobCompletionOutcome, JobCompletionStore
from app.jobs.recovery import (
    RECOVERY_EXHAUSTED_NOTE,
    RECOVERY_NOTE,
    JobLeaseRecoveryStore,
    LeaseRecoveryOutcome,
)
from app.jobs.store import JobEnqueueRequest, JobRequestStore

BIZ_A = "11111111-1111-1111-1111-11111111c106"
BIZ_B = "22222222-2222-2222-2222-22222222c106"
NOW = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)


def _enqueue(factory, business_id: str, key: str, *, max_attempts: int = 5) -> str:
    with factory() as session:
        row, _ = JobRequestStore(session, business_id).enqueue(
            JobEnqueueRequest(
                job_type="prospect.research",
                idempotency_key=key,
                payload_ref=f"dca-payload://synthetic/{key}",
                max_attempts=max_attempts,
            ),
            now=NOW,
        )
        session.commit()
        return row.id


def _naive(value: datetime | None) -> datetime | None:
    """Compare tz-naively: SQLite reads datetimes back naive, Postgres aware."""
    return value.replace(tzinfo=None) if value is not None else None


@pytest.fixture
def factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


# --------------------------------------------------------------------------------------------------
# An abandoned lease is recovered to QUEUED under a NEW fence (the crashed holder fenced off).
# --------------------------------------------------------------------------------------------------


def test_sweep_recovers_an_abandoned_lease_under_a_new_fence(factory):
    job_id = _enqueue(factory, BIZ_A, "job-1")
    # worker-1 claims (fence 1, attempt 1) with a short lease, then "crashes".
    with factory() as session:
        claim = JobClaimStore(session, BIZ_A).claim_next("worker-1", now=NOW, lease_seconds=60)
    assert claim is not None and claim.fence_token == 1 and claim.attempt_count == 1

    # Time advances past the lease: the sweep recovers the abandoned lease.
    later = NOW + timedelta(seconds=120)
    with factory() as session:
        report = JobLeaseRecoveryStore(session, BIZ_A).recover_abandoned(now=later)

    assert report.changed is True
    assert report.total == 1 and report.requeued_count == 1 and report.dead_lettered_count == 0
    rec = report.recovered[0]
    assert rec.job_id == job_id
    assert rec.outcome is LeaseRecoveryOutcome.RECOVERED
    assert rec.prior_lease_owner == "worker-1" and rec.prior_fence_token == 1
    assert rec.fence_token == 2  # the fencing token advanced on recovery

    with factory() as session:
        row = session.get(DurableJobRequest, job_id)
        assert row.status == JobRequestStatus.QUEUED.value  # retryable again
        assert row.lease_owner is None and row.lease_expires_at is None
        assert row.lease_deadline is None
        assert row.lease_version == 2  # bumped — the crashed holder's token is stale
        assert row.attempt_count == 1  # unchanged; the next claim bumps it
        assert row.last_error == RECOVERY_NOTE
        assert _naive(row.available_at) == _naive(later)  # due now, claimable


# --------------------------------------------------------------------------------------------------
# The headline acceptance: a crash does not lose work, and the old process cannot later commit.
# --------------------------------------------------------------------------------------------------


def test_crashed_worker_cannot_commit_after_recovery_but_another_worker_can(factory):
    job_id = _enqueue(factory, BIZ_A, "job-1")
    with factory() as session:
        crashed = JobClaimStore(session, BIZ_A).claim_next("worker-1", now=NOW, lease_seconds=60)
    assert crashed is not None and crashed.fence_token == 1

    later = NOW + timedelta(seconds=120)
    with factory() as session:
        report = JobLeaseRecoveryStore(session, BIZ_A).recover_abandoned(now=later)
    assert report.total == 1

    # The crashed worker-1 comes back and tries to commit with its OLD lease -> refused, nothing changes.
    with factory() as session:
        stale = JobCompletionStore(session, BIZ_A).complete(
            job_id,
            "worker-1",
            crashed.fence_token,
            now=later + timedelta(seconds=1),
            result_ref="dca-result://synthetic/stale",
        )
    assert stale.outcome is JobCompletionOutcome.STALE_LEASE
    assert stale.committed is False
    with factory() as session:
        row = session.get(DurableJobRequest, job_id)
        assert row.status == JobRequestStatus.QUEUED.value  # still just recovered, not succeeded
        assert row.result_ref is None  # the crashed worker committed nothing

    # Another healthy worker claims the recovered job (fresh fence) and completes it successfully.
    with factory() as session:
        reclaim = JobClaimStore(session, BIZ_A).claim_next(
            "worker-2", now=later + timedelta(seconds=2), lease_seconds=300
        )
    assert reclaim is not None and reclaim.job_id == job_id
    assert (
        reclaim.fence_token == 3 and reclaim.attempt_count == 2
    )  # recovery bumped to 2, claim to 3

    with factory() as session:
        done = JobCompletionStore(session, BIZ_A).complete(
            job_id,
            "worker-2",
            reclaim.fence_token,
            now=later + timedelta(seconds=3),
            result_ref="dca-result://synthetic/done",
        )
    assert done.committed is True and done.outcome is JobCompletionOutcome.COMMITTED
    with factory() as session:
        row = session.get(DurableJobRequest, job_id)
        assert row.status == JobRequestStatus.SUCCEEDED.value  # the work was not lost
        assert row.result_ref == "dca-result://synthetic/done"


# --------------------------------------------------------------------------------------------------
# A LIVE lease and non-running rows are never swept (the no-op / no-unintended-change case).
# --------------------------------------------------------------------------------------------------


def test_live_lease_is_not_swept_and_its_worker_can_still_commit(factory):
    job_id = _enqueue(factory, BIZ_A, "job-1")
    with factory() as session:
        claim = JobClaimStore(session, BIZ_A).claim_next("worker-1", now=NOW, lease_seconds=300)
    assert claim is not None

    # Sweep WHILE the lease is still live -> nothing is abandoned, so nothing changes.
    with factory() as session:
        report = JobLeaseRecoveryStore(session, BIZ_A).recover_abandoned(
            now=NOW + timedelta(seconds=10)
        )
    assert report.changed is False and report.total == 0

    with factory() as session:
        row = session.get(DurableJobRequest, job_id)
        assert row.status == JobRequestStatus.RUNNING.value  # untouched
        assert row.lease_owner == "worker-1" and row.lease_version == 1
        assert row.last_error is None

    # The live holder can still commit — the sweep did not fence it off.
    with factory() as session:
        done = JobCompletionStore(session, BIZ_A).complete(
            job_id, "worker-1", claim.fence_token, now=NOW + timedelta(seconds=20)
        )
    assert done.committed is True


def test_empty_sweep_and_queued_rows_change_nothing(factory):
    # A queued-but-unclaimed row is not a lease at all -> never swept.
    job_id = _enqueue(factory, BIZ_A, "job-queued")
    with factory() as session:
        report = JobLeaseRecoveryStore(session, BIZ_A).recover_abandoned(
            now=NOW + timedelta(hours=1)
        )
    assert report.changed is False and report.total == 0
    with factory() as session:
        row = session.get(DurableJobRequest, job_id)
        assert row.status == JobRequestStatus.QUEUED.value
        assert row.lease_version == 0 and row.lease_owner is None


# --------------------------------------------------------------------------------------------------
# Recovery is BOUNDED: a crash that exhausts the attempt budget is dead-lettered, not resurrected.
# --------------------------------------------------------------------------------------------------


def test_exhausted_attempts_are_dead_lettered_not_requeued(factory):
    # max_attempts=1: the single claim consumes the whole budget, so a crash cannot be retried.
    job_id = _enqueue(factory, BIZ_A, "job-1", max_attempts=1)
    with factory() as session:
        crashed = JobClaimStore(session, BIZ_A).claim_next("worker-1", now=NOW, lease_seconds=60)
    assert crashed is not None and crashed.attempt_count == 1

    later = NOW + timedelta(seconds=120)
    with factory() as session:
        report = JobLeaseRecoveryStore(session, BIZ_A).recover_abandoned(now=later)

    assert report.total == 1 and report.dead_lettered_count == 1 and report.requeued_count == 0
    rec = report.recovered[0]
    assert rec.outcome is LeaseRecoveryOutcome.DEAD_LETTERED_EXHAUSTED
    assert rec.fence_token == 2  # still fenced off, even when parked

    with factory() as session:
        row = session.get(DurableJobRequest, job_id)
        assert row.status == JobRequestStatus.DEAD_LETTER.value  # parked, not resurrected
        assert row.lease_owner is None and row.lease_version == 2
        assert row.last_error == RECOVERY_EXHAUSTED_NOTE

    # The parked job is NOT claimable (only a QUEUED / lapsed-RUNNING row is), so it is never retried.
    with factory() as session:
        nothing = JobClaimStore(session, BIZ_A).claim_next(
            "worker-2", now=later + timedelta(seconds=1)
        )
    assert nothing is None

    # And the crashed worker still cannot commit the parked job.
    with factory() as session:
        stale = JobCompletionStore(session, BIZ_A).complete(
            job_id, "worker-1", crashed.fence_token, now=later + timedelta(seconds=2)
        )
    assert stale.outcome is JobCompletionOutcome.STALE_LEASE and stale.committed is False


# --------------------------------------------------------------------------------------------------
# Tenant isolation: a sweep never recovers or touches another business's abandoned lease.
# --------------------------------------------------------------------------------------------------


def test_sweep_is_tenant_scoped(factory):
    job_id = _enqueue(factory, BIZ_A, "job-1")
    with factory() as session:
        JobClaimStore(session, BIZ_A).claim_next("worker-1", now=NOW, lease_seconds=60)

    later = NOW + timedelta(seconds=120)
    # Business B sweeps -> finds none of A's abandoned leases.
    with factory() as session:
        report_b = JobLeaseRecoveryStore(session, BIZ_B).recover_abandoned(now=later)
    assert report_b.changed is False and report_b.total == 0

    # A's abandoned lease is untouched by B's sweep.
    with factory() as session:
        row = session.get(DurableJobRequest, job_id)
        assert row.status == JobRequestStatus.RUNNING.value
        assert row.lease_owner == "worker-1" and row.lease_version == 1

    # A's own sweep recovers it.
    with factory() as session:
        report_a = JobLeaseRecoveryStore(session, BIZ_A).recover_abandoned(now=later)
    assert report_a.total == 1 and report_a.recovered[0].job_id == job_id


# --------------------------------------------------------------------------------------------------
# The real crash -> recover -> commit flow + concurrent-sweep safety on a disposable PostgreSQL.
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
def test_crash_recover_and_commit_with_another_worker_postgres(pg_factory):
    """The developer manual-test flow on real Postgres: a worker claims then is 'killed'; after the lease
    lapses the sweep recovers the job under a new fence; the crashed worker's commit is refused (stale
    lease), and a second worker claims and completes it — the work was not lost."""
    job_id = _enqueue(pg_factory, BIZ_A, "job-pg")
    with pg_factory() as session:
        crashed = JobClaimStore(session, BIZ_A).claim_next("worker-1", now=NOW, lease_seconds=60)
    assert crashed is not None and crashed.fence_token == 1

    later = NOW + timedelta(seconds=120)
    with pg_factory() as session:
        report = JobLeaseRecoveryStore(session, BIZ_A).recover_abandoned(now=later)
    assert report.total == 1 and report.recovered[0].fence_token == 2

    with pg_factory() as session:
        stale = JobCompletionStore(session, BIZ_A).complete(
            job_id,
            "worker-1",
            crashed.fence_token,
            now=later + timedelta(seconds=1),
            result_ref="dca-result://synthetic/stale",
        )
    assert stale.outcome is JobCompletionOutcome.STALE_LEASE

    with pg_factory() as session:
        reclaim = JobClaimStore(session, BIZ_A).claim_next(
            "worker-2", now=later + timedelta(seconds=2), lease_seconds=300
        )
    assert reclaim is not None and reclaim.fence_token == 3
    with pg_factory() as session:
        done = JobCompletionStore(session, BIZ_A).complete(
            job_id,
            "worker-2",
            reclaim.fence_token,
            now=later + timedelta(seconds=3),
            result_ref="dca-result://synthetic/done",
        )
    assert done.committed is True
    with pg_factory() as session:
        row = session.get(DurableJobRequest, job_id)
        assert row.status == JobRequestStatus.SUCCEEDED.value


@requires_pg
def test_two_concurrent_sweeps_recover_a_row_exactly_once_postgres(pg_factory):
    """``FOR UPDATE SKIP LOCKED``: two sweeps racing one abandoned lease recover it exactly once (the
    other skips the locked row), so a crashed job is never double-recovered / double-fenced."""
    _enqueue(pg_factory, BIZ_A, "job-race")
    with pg_factory() as session:
        JobClaimStore(session, BIZ_A).claim_next("worker-1", now=NOW, lease_seconds=60)

    later = NOW + timedelta(seconds=120)
    reports: list[object] = [None, None]
    barrier = threading.Barrier(2)
    lock = threading.Lock()

    def _sweep(index: int) -> None:
        with pg_factory() as session:
            store = JobLeaseRecoveryStore(session, BIZ_A)
            barrier.wait(timeout=10)
            report = store.recover_abandoned(now=later)
        with lock:
            reports[index] = report

    threads = [threading.Thread(target=_sweep, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    total_recovered = sum(r.total for r in reports)  # type: ignore[union-attr]
    assert total_recovered == 1  # exactly one sweep recovered the row; the other skipped it

    with pg_factory() as session:
        row = session.scalars(
            select(DurableJobRequest).where(DurableJobRequest.business_id == BIZ_A)
        ).first()
    assert row.status == JobRequestStatus.QUEUED.value
    assert row.lease_version == 2  # bumped exactly once, not twice
