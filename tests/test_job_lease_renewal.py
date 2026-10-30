"""DCA-102 — renew a running job lease (bounded heartbeat).

DCA-101 gave a worker a lease + fencing token when it CLAIMS a job. DCA-102 lets a long-but-live job
keep that lease from lapsing via a bounded heartbeat (``JobClaimStore.renew``), and — crucially — makes
the bounds fail-closed so a lapsed/replaced/over-running worker can never hold a claim it no longer
legitimately holds:

- a renewal extends a LIVE lease without bumping the fencing token (the same claim continues);
- an **expired** worker is refused (a lapsed lease is reclaimable — it must not self-renew);
- a **replaced** worker (reclaimed job → bumped fencing token) is refused with its old fence;
- the lease can never be pushed past the row's absolute ``lease_deadline`` (a stuck/looping worker
  cannot heartbeat forever), and a single renewal window is capped at ``MAX_LEASE_SECONDS``.

Each refusal changes NOTHING (the relevant denial). Renewing keeps a claim alive; it still EXECUTES
nothing — execution stays a disabled future capability, the DCA has no send path, and acceptance of a
prospect is not recipient consent.

Two layers, matching the DCA-101 suite:

- **Always-on logic tests** (in-memory SQLite) drive the renewal contract serially.
- **Disposable PostgreSQL** (``DCA_TEST_DATABASE_URL``) re-runs the same bounded contract against a real
  Postgres with real commits/rollbacks and the real ``lease_deadline`` column, because the acceptance
  cases are about durable row state across short transactions (never SQLite). Skips without a URL.

Spin up a throwaway Postgres, e.g.:
    docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55471:5432 postgres:16-alpine
    export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55471/dca"
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from app.db.base import Base, make_engine, make_session_factory
from app.db.models import DurableJobRequest, JobRequestStatus
from app.jobs.claim import (
    DEFAULT_LEASE_SECONDS,
    MAX_LEASE_LIFETIME_SECONDS,
    MAX_LEASE_SECONDS,
    JobClaimStore,
)
from app.jobs.store import JobEnqueueRequest, JobRequestStore

BIZ_A = "11111111-1111-1111-1111-11111111c102"
BIZ_B = "22222222-2222-2222-2222-22222222c102"
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


def _naive(value: datetime | None) -> datetime | None:
    """Compare tz-naively: SQLite reads datetimes back naive, Postgres aware."""
    return value.replace(tzinfo=None) if value is not None else None


# --------------------------------------------------------------------------------------------------
# Always-on logic tests (SQLite). The real durable-row assertions also run on Postgres below.
# --------------------------------------------------------------------------------------------------


@pytest.fixture
def factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


def _run_contract(factory) -> None:
    """The DCA-102 bounded-renewal contract, driven serially (same on SQLite and Postgres)."""
    # --- Happy path: a live lease is extended, fencing token UNCHANGED, deadline carried. ---
    job_id = _enqueue(factory, BIZ_A, "job-live")
    with factory() as session:
        claim = JobClaimStore(session, BIZ_A).claim_next("worker-1", now=NOW, lease_seconds=60)
    assert claim is not None and claim.fence_token == 1
    # The claim carries an absolute lifetime ceiling stamped at claim time.
    assert _naive(claim.lease_deadline) == _naive(
        NOW + timedelta(seconds=MAX_LEASE_LIFETIME_SECONDS)
    )

    with factory() as session:
        renewed = JobClaimStore(session, BIZ_A).renew(
            job_id, "worker-1", claim.fence_token, now=NOW + timedelta(seconds=30), lease_seconds=60
        )
    assert renewed is not None
    assert renewed.fence_token == claim.fence_token  # same claim, not a reclaim
    assert _naive(renewed.lease_expires_at) == _naive(NOW + timedelta(seconds=90))
    with factory() as session:
        row = session.get(DurableJobRequest, job_id)
        assert row.lease_owner == "worker-1" and row.lease_version == 1
        assert _naive(row.lease_expires_at) == _naive(NOW + timedelta(seconds=90))


def test_bounded_renewal_contract_sqlite(factory):
    _run_contract(factory)


def test_renew_is_refused_once_the_lease_has_expired(factory):
    """The headline acceptance denial: an EXPIRED worker cannot renew (a lapsed lease is reclaimable)."""
    job_id = _enqueue(factory, BIZ_A, "job-expired")
    with factory() as session:
        claim = JobClaimStore(session, BIZ_A).claim_next("worker-1", now=NOW, lease_seconds=60)
    assert claim is not None

    # Past the lease window but BEFORE any other worker reclaimed it: renewal is still refused.
    expired_at = NOW + timedelta(seconds=120)
    with factory() as session:
        refused = JobClaimStore(session, BIZ_A).renew(
            job_id, "worker-1", claim.fence_token, now=expired_at, lease_seconds=60
        )
    assert refused is None  # the expired holder may not self-renew

    # Nothing changed: the lapsed lease still reads exactly as the original claim left it.
    with factory() as session:
        row = session.get(DurableJobRequest, job_id)
        assert row.status == JobRequestStatus.RUNNING.value
        assert row.lease_owner == "worker-1" and row.lease_version == 1
        assert _naive(row.lease_expires_at) == _naive(NOW + timedelta(seconds=60))


def test_replaced_worker_cannot_renew_with_an_old_fence(factory):
    """A worker whose job was RECLAIMED (fencing token bumped) cannot renew with its stale fence."""
    job_id = _enqueue(factory, BIZ_A, "job-reclaimed")
    with factory() as session:
        first = JobClaimStore(session, BIZ_A).claim_next("worker-1", now=NOW, lease_seconds=60)
    assert first is not None and first.fence_token == 1

    # worker-1 lapses; worker-2 reclaims -> fence advances to 2, worker-1's token is now stale.
    later = NOW + timedelta(seconds=120)
    with factory() as session:
        second = JobClaimStore(session, BIZ_A).claim_next("worker-2", now=later, lease_seconds=60)
    assert second is not None and second.fence_token == 2

    # worker-1 (replaced, stale fence) tries to renew -> refused, nothing changes.
    with factory() as session:
        refused = JobClaimStore(session, BIZ_A).renew(
            job_id, "worker-1", first.fence_token, now=later + timedelta(seconds=1)
        )
    assert refused is None
    with factory() as session:
        row = session.get(DurableJobRequest, job_id)
        assert row.lease_owner == "worker-2" and row.lease_version == 2  # worker-2's claim intact
        assert _naive(row.lease_expires_at) == _naive(later + timedelta(seconds=60))


def test_renewal_never_extends_past_the_absolute_deadline(factory):
    """Bounded extension: a long-but-live job that heartbeats repeatedly is clamped at
    ``lease_deadline`` and then refused once the ceiling is reached — no forever-renewal.

    The lease must be kept *live* across the whole runway (the live-only rule refuses a lapsed lease),
    so this marches a chain of in-window heartbeats up toward the ceiling — exactly how a real
    long-running worker stays leased — then asserts the clamp and the hard stop."""
    job_id = _enqueue(factory, BIZ_A, "job-ceiling")
    with factory() as session:
        claim = JobClaimStore(session, BIZ_A).claim_next(
            "worker-1", now=NOW, lease_seconds=MAX_LEASE_SECONDS
        )
    assert claim is not None
    deadline = NOW + timedelta(seconds=MAX_LEASE_LIFETIME_SECONDS)

    # Heartbeat repeatedly, each while the current lease is still live (step < the lease window so the
    # lease never lapses between beats), marching up toward the ceiling. Every renewal keeps the SAME
    # fencing token and never pushes ``lease_expires_at`` past ``lease_deadline``.
    step = timedelta(seconds=MAX_LEASE_SECONDS - 60)
    t = NOW + step
    while t < deadline:
        with factory() as session:
            renewed = JobClaimStore(session, BIZ_A).renew(
                job_id, "worker-1", claim.fence_token, now=t, lease_seconds=MAX_LEASE_SECONDS
            )
        assert renewed is not None
        assert renewed.fence_token == claim.fence_token  # same claim across every beat
        assert _naive(renewed.lease_expires_at) <= _naive(deadline)  # never past the ceiling
        t += step

    # A renewal close to the ceiling is clamped to exactly the deadline (not now + window).
    near = deadline - timedelta(seconds=30)
    with factory() as session:
        clamped = JobClaimStore(session, BIZ_A).renew(
            job_id, "worker-1", claim.fence_token, now=near, lease_seconds=MAX_LEASE_SECONDS
        )
    assert clamped is not None
    assert _naive(clamped.lease_expires_at) == _naive(deadline)  # clamped to the ceiling

    # At/after the deadline the heartbeat is refused outright — no forever-renewal.
    with factory() as session:
        refused = JobClaimStore(session, BIZ_A).renew(
            job_id, "worker-1", claim.fence_token, now=deadline, lease_seconds=60
        )
    assert refused is None
    with factory() as session:
        row = session.get(DurableJobRequest, job_id)
        assert _naive(row.lease_expires_at) == _naive(deadline)  # unchanged by the refused renewal


def test_single_renewal_window_is_capped_at_max_lease_seconds(factory):
    """A caller asking for an unbounded window gets at most ``MAX_LEASE_SECONDS`` (within the deadline)."""
    job_id = _enqueue(factory, BIZ_A, "job-cap")
    with factory() as session:
        claim = JobClaimStore(session, BIZ_A).claim_next("worker-1", now=NOW, lease_seconds=60)
    assert claim is not None

    with factory() as session:
        renewed = JobClaimStore(session, BIZ_A).renew(
            job_id,
            "worker-1",
            claim.fence_token,
            now=NOW + timedelta(seconds=10),
            lease_seconds=10_000_000,  # absurd request
        )
    assert renewed is not None
    # Capped at MAX_LEASE_SECONDS from the renewal instant (and still within the absolute deadline).
    assert _naive(renewed.lease_expires_at) == _naive(
        NOW + timedelta(seconds=10 + MAX_LEASE_SECONDS)
    )


def test_foreign_worker_cannot_renew_a_live_lease(factory):
    """Fenced on owner: a different worker id cannot renew someone else's live claim."""
    job_id = _enqueue(factory, BIZ_A, "job-foreign")
    with factory() as session:
        claim = JobClaimStore(session, BIZ_A).claim_next("worker-1", now=NOW, lease_seconds=60)
    assert claim is not None
    with factory() as session:
        refused = JobClaimStore(session, BIZ_A).renew(
            job_id, "worker-2", claim.fence_token, now=NOW + timedelta(seconds=5)
        )
    assert refused is None
    with factory() as session:
        row = session.get(DurableJobRequest, job_id)
        assert row.lease_owner == "worker-1"
        assert _naive(row.lease_expires_at) == _naive(NOW + timedelta(seconds=60))


def test_renew_of_another_businesss_job_is_refused(factory):
    """Tenant isolation: a worker in business B cannot renew business A's job (by-id reads not-found)."""
    job_id = _enqueue(factory, BIZ_A, "job-x")
    with factory() as session:
        claim = JobClaimStore(session, BIZ_A).claim_next("worker-1", now=NOW, lease_seconds=60)
    assert claim is not None
    with factory() as session:
        refused = JobClaimStore(session, BIZ_B).renew(
            job_id, "worker-1", claim.fence_token, now=NOW + timedelta(seconds=5)
        )
    assert refused is None
    with factory() as session:
        row = session.get(DurableJobRequest, job_id)
        assert row.lease_owner == "worker-1" and row.lease_version == 1
        assert _naive(row.lease_expires_at) == _naive(NOW + timedelta(seconds=60))


def test_default_renewal_window_is_not_clamped(factory):
    """Sanity: the default lease window is well under the caps, so it is applied verbatim."""
    assert DEFAULT_LEASE_SECONDS <= MAX_LEASE_SECONDS
    job_id = _enqueue(factory, BIZ_A, "job-default")
    with factory() as session:
        claim = JobClaimStore(session, BIZ_A).claim_next("worker-1", now=NOW)
    assert claim is not None
    with factory() as session:
        renewed = JobClaimStore(session, BIZ_A).renew(
            job_id, "worker-1", claim.fence_token, now=NOW + timedelta(seconds=10)
        )
    assert renewed is not None
    assert _naive(renewed.lease_expires_at) == _naive(
        NOW + timedelta(seconds=10 + DEFAULT_LEASE_SECONDS)
    )


# --------------------------------------------------------------------------------------------------
# The same bounded contract on a disposable PostgreSQL (real commits/rollbacks + the real column).
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
def test_bounded_renewal_contract_postgres(pg_factory):
    """Happy-path renewal of a live lease, against a real Postgres with the real ``lease_deadline``."""
    _run_contract(pg_factory)


@requires_pg
def test_expired_worker_cannot_renew_postgres(pg_factory):
    """The acceptance denial on real Postgres: an expired worker's renewal is refused, row unchanged."""
    job_id = _enqueue(pg_factory, BIZ_A, "job-expired-pg")
    with pg_factory() as session:
        claim = JobClaimStore(session, BIZ_A).claim_next("worker-1", now=NOW, lease_seconds=60)
    assert claim is not None

    with pg_factory() as session:
        refused = JobClaimStore(session, BIZ_A).renew(
            job_id, "worker-1", claim.fence_token, now=NOW + timedelta(seconds=120)
        )
    assert refused is None
    with pg_factory() as session:
        row = session.get(DurableJobRequest, job_id)
        assert row.lease_owner == "worker-1" and row.lease_version == 1
        assert _naive(row.lease_expires_at) == _naive(NOW + timedelta(seconds=60))


@requires_pg
def test_renewal_bounded_by_deadline_postgres(pg_factory):
    """Bounded extension on real Postgres: renewal clamps to the deadline, then is refused past it."""
    job_id = _enqueue(pg_factory, BIZ_A, "job-ceiling-pg")
    with pg_factory() as session:
        claim = JobClaimStore(session, BIZ_A).claim_next(
            "worker-1", now=NOW, lease_seconds=MAX_LEASE_SECONDS
        )
    assert claim is not None
    deadline = NOW + timedelta(seconds=MAX_LEASE_LIFETIME_SECONDS)

    # Keep the lease live across the runway with a chain of in-window heartbeats (the live-only rule
    # refuses a lapsed lease), then assert the clamp at the ceiling.
    step = timedelta(seconds=MAX_LEASE_SECONDS - 60)
    t = NOW + step
    while t < deadline:
        with pg_factory() as session:
            renewed = JobClaimStore(session, BIZ_A).renew(
                job_id, "worker-1", claim.fence_token, now=t, lease_seconds=MAX_LEASE_SECONDS
            )
        assert renewed is not None and _naive(renewed.lease_expires_at) <= _naive(deadline)
        t += step

    with pg_factory() as session:
        clamped = JobClaimStore(session, BIZ_A).renew(
            job_id,
            "worker-1",
            claim.fence_token,
            now=deadline - timedelta(seconds=30),
            lease_seconds=MAX_LEASE_SECONDS,
        )
    assert clamped is not None and _naive(clamped.lease_expires_at) == _naive(deadline)

    with pg_factory() as session:
        refused = JobClaimStore(session, BIZ_A).renew(
            job_id, "worker-1", claim.fence_token, now=deadline, lease_seconds=60
        )
    assert refused is None
