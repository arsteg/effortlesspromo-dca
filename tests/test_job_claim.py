"""DCA-101 — claim durable jobs safely across workers.

Two layers, matching the DCA-099/100 suites:

- **Always-on logic tests** (in-memory SQLite) drive the claim contract serially: a claim leases a
  queued job (RUNNING + owner + fencing token, attempt incremented) without executing it; a live lease
  blocks a second claim; an expired lease lets another worker RECLAIM the job (fencing token bumped);
  a stale/foreign fencing token is refused by ``renew``/``release`` and changes nothing; and the store
  is tenant-scoped so one business can never claim or touch another's job.
- **The real cross-worker safety on a disposable PostgreSQL** (``DCA_TEST_DATABASE_URL``) exercises
  ``SELECT ... FOR UPDATE SKIP LOCKED``: two concurrent workers racing one queued job yield exactly one
  claim; racing two jobs yield two distinct claims (SKIP LOCKED never blocks); and the row lock is
  released at the short claim's COMMIT — proven by locking the claimed row ``FOR UPDATE NOWAIT`` from a
  separate connection while the "handler" is still running. These skip when no URL is supplied, because
  ``FOR UPDATE SKIP LOCKED`` is a PostgreSQL feature (SQLite omits it).

Spin up a throwaway Postgres, e.g.:
    docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55471:5432 postgres:16-alpine
    export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55471/dca"
"""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from app.db.base import Base, make_engine, make_session_factory
from app.db.models import DurableJobRequest, JobRequestStatus
from app.jobs.claim import DEFAULT_LEASE_SECONDS, JobClaimStore
from app.jobs.store import JobEnqueueRequest, JobRequestStore

BIZ_A = "11111111-1111-1111-1111-11111111c101"
BIZ_B = "22222222-2222-2222-2222-22222222c101"
NOW = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)


def _enqueue(factory, business_id: str, key: str, *, now: datetime = NOW) -> str:
    with factory() as session:
        row, _ = JobRequestStore(session, business_id).enqueue(
            JobEnqueueRequest(
                job_type="prospect.research",
                idempotency_key=key,
                payload_ref=f"dca-payload://synthetic/{key}",
            ),
            now=now,
        )
        session.commit()
        return row.id


@pytest.fixture
def factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


# --------------------------------------------------------------------------------------------------
# Claim leases a queued job without executing it.
# --------------------------------------------------------------------------------------------------


def test_claim_leases_a_queued_job_and_runs_nothing(factory):
    job_id = _enqueue(factory, BIZ_A, "job-1")

    with factory() as session:
        claim = JobClaimStore(session, BIZ_A).claim_next("worker-1", now=NOW)

    assert claim is not None
    assert claim.job_id == job_id
    assert claim.worker_id == "worker-1"
    assert claim.fence_token == 1  # first claim bumps the fencing token from 0 to 1
    assert claim.attempt_count == 1  # the claim records one attempt
    assert claim.lease_expires_at == NOW + timedelta(seconds=DEFAULT_LEASE_SECONDS)

    with factory() as session:
        row = session.get(DurableJobRequest, job_id)
        assert row.status == JobRequestStatus.RUNNING.value  # leased, not executed
        assert row.lease_owner == "worker-1"
        assert row.lease_version == 1


def test_claim_on_empty_queue_returns_none(factory):
    with factory() as session:
        assert JobClaimStore(session, BIZ_A).claim_next("worker-1", now=NOW) is None


def test_claim_skips_a_job_not_yet_available(factory):
    # available_at in the future -> not claimable yet.
    _enqueue(factory, BIZ_A, "future", now=NOW + timedelta(hours=1))
    with factory() as session:
        assert JobClaimStore(session, BIZ_A).claim_next("worker-1", now=NOW) is None


# --------------------------------------------------------------------------------------------------
# A live lease blocks a second claim; two workers never hold the same current claim (serial form).
# --------------------------------------------------------------------------------------------------


def test_live_lease_blocks_a_second_claim(factory):
    _enqueue(factory, BIZ_A, "job-1")

    with factory() as session:
        first = JobClaimStore(session, BIZ_A).claim_next("worker-1", now=NOW)
    assert first is not None

    # A second worker, while the lease is still live, finds nothing to claim — no double-claim.
    with factory() as session:
        second = JobClaimStore(session, BIZ_A).claim_next(
            "worker-2", now=NOW + timedelta(seconds=1)
        )
    assert second is None


def test_expired_lease_lets_another_worker_reclaim_and_bumps_the_fence(factory):
    job_id = _enqueue(factory, BIZ_A, "job-1")

    with factory() as session:
        first = JobClaimStore(session, BIZ_A).claim_next("worker-1", now=NOW, lease_seconds=60)
    assert first is not None and first.fence_token == 1

    # Past the lease window: a crashed/stalled worker-1's job is reclaimable by worker-2.
    later = NOW + timedelta(seconds=120)
    with factory() as session:
        second = JobClaimStore(session, BIZ_A).claim_next("worker-2", now=later)
    assert second is not None
    assert second.job_id == job_id
    assert second.fence_token == 2  # the fencing token advanced on reclaim
    assert second.attempt_count == 2

    # worker-1's handed-out token is now stale; worker-2's is current — only ONE current claim exists.
    with factory() as session:
        store = JobClaimStore(session, BIZ_A)
        assert store.holds_current_lease(job_id, "worker-1", first.fence_token) is False
        assert store.holds_current_lease(job_id, "worker-2", second.fence_token) is True


# --------------------------------------------------------------------------------------------------
# Fencing: a stale/foreign token is refused and changes nothing (the relevant denial).
# --------------------------------------------------------------------------------------------------


def test_stale_fence_token_cannot_release_a_reclaimed_job(factory):
    job_id = _enqueue(factory, BIZ_A, "job-1")
    with factory() as session:
        first = JobClaimStore(session, BIZ_A).claim_next("worker-1", now=NOW, lease_seconds=60)
    later = NOW + timedelta(seconds=120)
    with factory() as session:
        second = JobClaimStore(session, BIZ_A).claim_next("worker-2", now=later)
    assert second is not None

    # worker-1 (lapsed, stale fence) tries to release the job it no longer holds -> refused, no change.
    with factory() as session:
        released = JobClaimStore(session, BIZ_A).release(
            job_id, "worker-1", first.fence_token, now=later
        )
    assert released is False

    with factory() as session:
        row = session.get(DurableJobRequest, job_id)
        assert row.status == JobRequestStatus.RUNNING.value  # still worker-2's live claim
        assert row.lease_owner == "worker-2"
        assert row.lease_version == 2  # unchanged by the stale-token release


def test_release_by_current_holder_returns_job_to_queue(factory):
    job_id = _enqueue(factory, BIZ_A, "job-1")
    with factory() as session:
        claim = JobClaimStore(session, BIZ_A).claim_next("worker-1", now=NOW)

    with factory() as session:
        released = JobClaimStore(session, BIZ_A).release(
            job_id, "worker-1", claim.fence_token, now=NOW + timedelta(seconds=5)
        )
    assert released is True

    with factory() as session:
        row = session.get(DurableJobRequest, job_id)
        assert row.status == JobRequestStatus.QUEUED.value  # claimable again
        assert row.lease_owner is None and row.lease_expires_at is None
        assert row.lease_version == 1  # fencing token is monotonic; a release never rewinds it

    # And it can be claimed again (fencing token keeps climbing).
    with factory() as session:
        again = JobClaimStore(session, BIZ_A).claim_next("worker-2", now=NOW + timedelta(seconds=6))
    assert again is not None and again.fence_token == 2


def test_renew_extends_only_the_current_holders_lease(factory):
    job_id = _enqueue(factory, BIZ_A, "job-1")
    with factory() as session:
        claim = JobClaimStore(session, BIZ_A).claim_next("worker-1", now=NOW, lease_seconds=60)

    # The current holder renews -> lease window pushed out, fencing token UNCHANGED (same claim).
    with factory() as session:
        renewed = JobClaimStore(session, BIZ_A).renew(
            job_id, "worker-1", claim.fence_token, now=NOW + timedelta(seconds=30), lease_seconds=60
        )
    assert renewed is not None
    assert renewed.fence_token == claim.fence_token
    assert renewed.lease_expires_at == NOW + timedelta(seconds=90)

    # A foreign worker (wrong owner) cannot renew -> refused, nothing changes.
    with factory() as session:
        bad = JobClaimStore(session, BIZ_A).renew(
            job_id, "worker-2", claim.fence_token, now=NOW + timedelta(seconds=35)
        )
    assert bad is None
    with factory() as session:
        row = session.get(DurableJobRequest, job_id)
        assert row.lease_owner == "worker-1"
        # Unchanged by the foreign renew (compare tz-naively: SQLite reads datetimes back naive).
        expected = (NOW + timedelta(seconds=90)).replace(tzinfo=None)
        assert row.lease_expires_at.replace(tzinfo=None) == expected


# --------------------------------------------------------------------------------------------------
# Tenant isolation: a worker never claims or touches another business's job.
# --------------------------------------------------------------------------------------------------


def test_worker_cannot_claim_another_businesss_job(factory):
    _enqueue(factory, BIZ_A, "job-1")
    # Business B has no jobs of its own -> B's claimer finds nothing, even though A has a queued job.
    with factory() as session:
        assert JobClaimStore(session, BIZ_B).claim_next("worker-b", now=NOW) is None
    # A's job is untouched.
    with factory() as session:
        row = session.scalars(
            select(DurableJobRequest).where(DurableJobRequest.business_id == BIZ_A)
        ).first()
    assert row.status == JobRequestStatus.QUEUED.value


def test_release_of_another_businesss_job_is_refused(factory):
    job_id = _enqueue(factory, BIZ_A, "job-1")
    with factory() as session:
        claim = JobClaimStore(session, BIZ_A).claim_next("worker-1", now=NOW)
    # B presents A's job id + a plausible token: cross-tenant lookup reads as not-found -> refused.
    with factory() as session:
        assert (
            JobClaimStore(session, BIZ_B).release(job_id, "worker-1", claim.fence_token, now=NOW)
            is False
        )
    with factory() as session:
        row = session.get(DurableJobRequest, job_id)
        assert row.status == JobRequestStatus.RUNNING.value and row.lease_owner == "worker-1"


# --------------------------------------------------------------------------------------------------
# The real cross-worker safety on a disposable PostgreSQL (skips without a URL).
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


def _concurrent_claims(pg_factory, worker_ids: list[str], now: datetime) -> list[object]:
    """Run ``claim_next`` from each worker concurrently (own session each), released on a barrier."""
    barrier = threading.Barrier(len(worker_ids))
    results: list[object] = [None] * len(worker_ids)
    lock = threading.Lock()

    def _worker(index: int, worker_id: str) -> None:
        with pg_factory() as session:
            barrier.wait(timeout=10)
            claim = JobClaimStore(session, BIZ_A).claim_next(worker_id, now=now)
        with lock:
            results[index] = claim

    threads = [threading.Thread(target=_worker, args=(i, wid)) for i, wid in enumerate(worker_ids)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    return results


@requires_pg
def test_two_workers_racing_one_job_yield_exactly_one_claim(pg_factory):
    """SKIP LOCKED + short transaction: two workers race a single queued job; exactly one gets it and
    the other gets nothing — two workers never hold the same current claim."""
    _enqueue(pg_factory, BIZ_A, "job-1")

    claims = _concurrent_claims(pg_factory, ["worker-1", "worker-2"], NOW)

    got = [c for c in claims if c is not None]
    assert len(got) == 1  # exactly one worker claimed it
    assert got[0].fence_token == 1
    # The one claim is RUNNING and owned by whichever worker won.
    with pg_factory() as session:
        row = session.scalars(
            select(DurableJobRequest).where(DurableJobRequest.business_id == BIZ_A)
        ).first()
    assert row.status == JobRequestStatus.RUNNING.value
    assert row.lease_owner == got[0].worker_id
    assert row.lease_version == 1


@requires_pg
def test_two_workers_racing_two_jobs_each_get_a_distinct_job(pg_factory):
    """SKIP LOCKED never blocks: with two queued jobs, two racing workers claim one each (not the
    same row, and neither waits on the other's lock)."""
    _enqueue(pg_factory, BIZ_A, "job-1")
    _enqueue(pg_factory, BIZ_A, "job-2")

    claims = _concurrent_claims(pg_factory, ["worker-1", "worker-2"], NOW)

    got = [c for c in claims if c is not None]
    assert len(got) == 2  # both workers claimed
    assert len({c.job_id for c in got}) == 2  # two DISTINCT jobs, no double-claim
    assert {c.worker_id for c in got} == {"worker-1", "worker-2"}


@requires_pg
def test_claim_releases_the_row_lock_before_handler_io(pg_engine, pg_factory):
    """The claim is a SHORT transaction: its ``FOR UPDATE`` row lock is released at COMMIT, before the
    worker does any handler I/O. Proven by locking the claimed row ``FOR UPDATE NOWAIT`` from a separate
    connection WHILE the worker's 'handler' is still running — it succeeds immediately instead of
    blocking/raising, so slow external work can never pin the row lock."""
    job_id = _enqueue(pg_factory, BIZ_A, "job-1")

    with pg_factory() as session:
        claim = JobClaimStore(session, BIZ_A).claim_next("worker-1", now=NOW)
    assert claim is not None

    # The worker is now doing slow external "handler I/O" (no open DB transaction). A separate
    # connection can immediately take the row's lock FOR UPDATE NOWAIT — it is NOT held by the worker.
    # (NOWAIT raises instead of blocking if the row were still locked, so success proves the release.)
    with pg_engine.connect() as probe:
        locked = probe.execute(
            text("SELECT id FROM durable_job_requests WHERE id = :id FOR UPDATE NOWAIT"),
            {"id": job_id},
        ).first()
        probe.rollback()
    assert locked is not None and locked[0] == job_id  # lock acquired without blocking
