"""DCA-099 — the durable job-request store.

Two layers, matching the DCA-095 suite:

- **Always-on logic tests** (in-memory SQLite) drive the store's contract: enqueuing the same logical
  work twice collapses to ONE job (idempotent by ``(business_id, job_type, idempotency_key)``); a reused
  key for a DIFFERENT payload is a conflict that writes nothing; raw contact data in the reference /
  metadata is refused; the row carries only a pointer + digest, never the payload; and the store is
  tenant-scoped so one business can never see another's jobs. Jobs are persisted ``QUEUED`` only —
  nothing executes them.
- **The unique-constraint serialisation on a disposable PostgreSQL** (``DCA_TEST_DATABASE_URL``)
  exercises the real engine: enqueue-twice dedups to one row, and two concurrent enqueues of the same
  intent (separate sessions/threads) still yield exactly one job. It skips when no URL is supplied.

Spin up a throwaway Postgres, e.g.:
    docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55499:5432 postgres:16-alpine
    export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55499/dca"
"""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.db.base import Base, make_engine, make_session_factory
from app.db.models import DurableJobRequest, JobRequestStatus
from app.jobs.store import (
    JobEnqueueRequest,
    JobRequestConflict,
    JobRequestStore,
    PayloadContainsPiiError,
    job_payload_digest,
)

BIZ_A = "11111111-1111-1111-1111-1111111111a9"
BIZ_B = "22222222-2222-2222-2222-2222222222b9"
NOW = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)


def _request(**overrides) -> JobEnqueueRequest:
    base: dict = {
        "job_type": "prospect.research",
        "idempotency_key": "job-key-1",
        "payload_ref": "dca-payload://synthetic/run-1",
        "schema_version": 1,
        "payload_metadata": {"priority": "normal"},
    }
    base.update(overrides)
    return JobEnqueueRequest(**base)


@pytest.fixture
def factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


def _count(factory, business_id: str) -> int:
    with factory() as session:
        return session.scalar(
            select(func.count())
            .select_from(DurableJobRequest)
            .where(DurableJobRequest.business_id == business_id)
        )


# --------------------------------------------------------------------------------------------------
# Idempotent enqueue: the same logical work creates exactly one job.
# --------------------------------------------------------------------------------------------------


def test_enqueue_persists_a_queued_job_with_scope_and_no_raw_payload(factory):
    with factory() as session:
        row, created = JobRequestStore(session, BIZ_A).enqueue(_request(), now=NOW)
        session.commit()
        job_id = row.id

    assert created is True
    with factory() as session:
        stored = session.get(DurableJobRequest, job_id)
        assert stored.business_id == BIZ_A
        assert stored.status == JobRequestStatus.QUEUED.value  # persisted, never executed
        assert stored.schema_version == 1
        assert stored.attempt_count == 0 and stored.max_attempts == 5
        assert stored.payload_ref == "dca-payload://synthetic/run-1"
        # Only a pointer + a one-way digest live on the row — never the payload itself.
        assert stored.payload_digest == job_payload_digest(
            stored.payload_ref, stored.schema_version, stored.payload_metadata
        )
    assert _count(factory, BIZ_A) == 1


def test_repeated_enqueue_of_same_work_creates_one_job(factory):
    with factory() as session:
        first, created_first = JobRequestStore(session, BIZ_A).enqueue(_request(), now=NOW)
        session.commit()
        first_id = first.id

    # A retry of the SAME logical work (same type + key + payload) returns the existing job, no new row.
    with factory() as session:
        second, created_second = JobRequestStore(session, BIZ_A).enqueue(_request(), now=NOW)
        session.commit()
        second_id = second.id

    assert created_first is True
    assert created_second is False
    assert first_id == second_id
    assert _count(factory, BIZ_A) == 1


def test_reused_key_for_different_work_is_a_conflict_and_writes_nothing(factory):
    with factory() as session:
        JobRequestStore(session, BIZ_A).enqueue(_request(), now=NOW)
        session.commit()

    # Same key, a DIFFERENT payload reference -> different logical work -> conflict, nothing written.
    with factory() as session:
        with pytest.raises(JobRequestConflict):
            JobRequestStore(session, BIZ_A).enqueue(
                _request(payload_ref="dca-payload://synthetic/run-2"), now=NOW
            )
        session.rollback()

    # The denial changed nothing: still exactly the one original job.
    assert _count(factory, BIZ_A) == 1
    with factory() as session:
        only = session.scalars(select(DurableJobRequest)).one()
        assert only.payload_ref == "dca-payload://synthetic/run-1"


def test_schema_version_change_under_same_key_is_a_conflict(factory):
    with factory() as session:
        JobRequestStore(session, BIZ_A).enqueue(_request(schema_version=1), now=NOW)
        session.commit()
    with factory() as session:
        with pytest.raises(JobRequestConflict):
            JobRequestStore(session, BIZ_A).enqueue(_request(schema_version=2), now=NOW)
        session.rollback()
    assert _count(factory, BIZ_A) == 1


# --------------------------------------------------------------------------------------------------
# PII refusal + tenant isolation.
# --------------------------------------------------------------------------------------------------


def test_enqueue_refuses_raw_email_in_payload_ref(factory):
    with factory() as session:
        with pytest.raises(PayloadContainsPiiError):
            JobRequestStore(session, BIZ_A).enqueue(
                _request(payload_ref="mailto:person@synthetic.example"), now=NOW
            )
        session.rollback()
    assert _count(factory, BIZ_A) == 0


def test_enqueue_refuses_raw_phone_in_metadata(factory):
    with factory() as session:
        with pytest.raises(PayloadContainsPiiError):
            JobRequestStore(session, BIZ_A).enqueue(
                _request(payload_metadata={"note": "call +91 98765 43210"}), now=NOW
            )
        session.rollback()
    assert _count(factory, BIZ_A) == 0


def test_same_key_in_two_businesses_is_two_jobs(factory):
    with factory() as session:
        JobRequestStore(session, BIZ_A).enqueue(_request(), now=NOW)
        JobRequestStore(session, BIZ_B).enqueue(_request(), now=NOW)
        session.commit()
    assert _count(factory, BIZ_A) == 1
    assert _count(factory, BIZ_B) == 1


def test_store_cannot_find_another_businesss_job(factory):
    with factory() as session:
        JobRequestStore(session, BIZ_A).enqueue(_request(), now=NOW)
        session.commit()
    with factory() as session:
        # B looks up A's exact (type, key): business-scoped query resolves nothing.
        assert JobRequestStore(session, BIZ_B).find("prospect.research", "job-key-1") is None


# --------------------------------------------------------------------------------------------------
# Digest determinism (metadata order-independent).
# --------------------------------------------------------------------------------------------------


def test_payload_digest_is_order_independent_and_payload_sensitive():
    a = job_payload_digest("ref://x", 1, {"a": "1", "b": "2"})
    b = job_payload_digest("ref://x", 1, {"b": "2", "a": "1"})
    assert a == b  # metadata order does not change the logical identity
    assert a != job_payload_digest(
        "ref://y", 1, {"a": "1", "b": "2"}
    )  # payload change -> new digest
    assert a != job_payload_digest(
        "ref://x", 2, {"a": "1", "b": "2"}
    )  # schema change -> new digest


# --------------------------------------------------------------------------------------------------
# The same dedup behaviour on a disposable PostgreSQL (skips without a URL).
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
def test_enqueue_twice_dedups_to_one_row_on_real_postgres(pg_factory):
    with pg_factory() as session:
        first, created_first = JobRequestStore(session, BIZ_A).enqueue(_request(), now=NOW)
        session.commit()
        first_id = first.id
    with pg_factory() as session:
        second, created_second = JobRequestStore(session, BIZ_A).enqueue(_request(), now=NOW)
        session.commit()
        second_id = second.id
    assert created_first is True and created_second is False
    assert first_id == second_id
    assert _count(pg_factory, BIZ_A) == 1


@requires_pg
def test_reused_key_for_different_work_conflicts_on_real_postgres(pg_factory):
    with pg_factory() as session:
        JobRequestStore(session, BIZ_A).enqueue(_request(), now=NOW)
        session.commit()
    with pg_factory() as session:
        with pytest.raises(JobRequestConflict):
            JobRequestStore(session, BIZ_A).enqueue(
                _request(payload_ref="dca-payload://synthetic/run-2"), now=NOW
            )
        session.rollback()
    assert _count(pg_factory, BIZ_A) == 1


@requires_pg
def test_concurrent_enqueue_of_same_intent_yields_one_job(pg_factory):
    """Two threads enqueue the SAME logical work at once over separate sessions. The unique constraint
    serialises them: one inserts, the other either dedups on find or loses the flush race and recovers —
    exactly one job either way, and no :class:`JobRequestConflict` for identical work."""
    barrier = threading.Barrier(2)
    results: list[tuple[str, bool]] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def _worker():
        try:
            with pg_factory() as session:
                barrier.wait(timeout=10)
                row, created = JobRequestStore(session, BIZ_A).enqueue(_request(), now=NOW)
                session.commit()
                with lock:
                    results.append((row.id, created))
        except Exception as exc:  # pragma: no cover - only on a real failure
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert errors == []
    assert len(results) == 2
    assert results[0][0] == results[1][0]  # both resolved to the SAME job id
    assert sum(created for _, created in results) == 1  # exactly one actually created the row
    assert _count(pg_factory, BIZ_A) == 1
