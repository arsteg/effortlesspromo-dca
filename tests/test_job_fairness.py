"""DCA-110 — schedule jobs fairly across businesses.

DCA-109 ran the worker over enabled tenants in a simple order, draining each tenant to its batch cap
before the next got a turn — so one tenant with a huge backlog could monopolize a pass while a tenant
with a single queued job waited. DCA-110 makes the drain FAIR: jobs are claimed ROUND-ROBIN across
tenants under a per-business (TENANT) cap and a per-pass (GLOBAL) cap, so a busy tenant can never starve
another (acceptance: "B receives progress while A has a large backlog; global and tenant limits are
enforced").

Fairness is a selection policy on top of the DCA-101 tenant-scoped claim — it never weakens tenant
isolation — and it changes nothing about the headline boundary: a claimed job is PARKED ``DEAD_LETTER``
safely, never run and never sent (``EXECUTION_ENABLED`` stays ``False``).

Two layers, matching the DCA-101/109 suites:

- **Always-on logic tests** (in-memory SQLite) drive the worker serially and assert the fairness
  properties: a backlog-heavy Business A does not starve Business B; the per-business and global caps are
  enforced; claims interleave round-robin; and the no-op/edge cases change nothing. Round-robin ordering
  is a pure-Python selection property, so it is deterministic on SQLite.
- **The real fair claim flow on a disposable PostgreSQL** (``DCA_TEST_DATABASE_URL``) re-runs the
  fairness assertion against real ``FOR UPDATE SKIP LOCKED`` claim commits. It skips without a URL.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import DurableJobRequest, JobRequestStatus, TenantAgent
from app.jobs.store import JobEnqueueRequest, JobRequestStore
from app.jobs.worker import ClaimDisposition, DurableJobWorker

BIZ_A = "11111111-1111-1111-1111-11111111c110"  # the backlog-heavy tenant
BIZ_B = "22222222-2222-2222-2222-22222222c110"  # the small tenant that must still make progress
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


def _enqueue_many(factory, business_id: str, count: int, *, prefix: str) -> list[str]:
    ids: list[str] = []
    with factory() as session:
        store = JobRequestStore(session, business_id)
        for i in range(count):
            row, _ = store.enqueue(
                JobEnqueueRequest(
                    job_type="prospect.research",
                    idempotency_key=f"{prefix}-{i}",
                    payload_ref=f"dca-payload://synthetic/{prefix}-{i}",
                ),
                now=NOW,
            )
            ids.append(row.id)
        session.commit()
    return ids


def _queued_count(factory, business_id: str) -> int:
    with factory() as session:
        return len(
            session.scalars(
                select(DurableJobRequest.id).where(
                    DurableJobRequest.business_id == business_id,
                    DurableJobRequest.status == JobRequestStatus.QUEUED.value,
                )
            ).all()
        )


def _worker(factory, **kwargs) -> DurableJobWorker:
    return DurableJobWorker(factory, _settings(), clock=lambda: NOW, **kwargs)


# --------------------------------------------------------------------------------------------------
# The headline: a backlog-heavy Business A does NOT starve Business B — B makes progress in the SAME pass.
# --------------------------------------------------------------------------------------------------


def test_backlog_heavy_tenant_does_not_starve_another(factory):
    _enable(factory, BIZ_A)
    _enable(factory, BIZ_B)
    _enqueue_many(factory, BIZ_A, 50, prefix="a")  # a large backlog
    (b_job,) = _enqueue_many(factory, BIZ_B, 1, prefix="b")  # one small job

    # A tight global cap (3) that A alone could consume; fairness must still service B within it.
    report = _worker(factory, max_jobs_per_business=10, max_jobs_per_pass=3).run_pass(now=NOW)

    counts = report.handled_by_business
    assert (
        counts.get(BIZ_B, 0) == 1
    ), "B's single job must be handled in the same pass as A's backlog"
    assert len(report.handled) == 3, "global cap bounds the pass"
    # B's job was driven (parked, never run/sent); A still has plenty queued for later passes.
    with factory() as session:
        assert session.get(DurableJobRequest, b_job).status == JobRequestStatus.DEAD_LETTER.value
    assert _queued_count(factory, BIZ_A) == 48  # 50 - (3 total - 1 for B)


# --------------------------------------------------------------------------------------------------
# The per-business (TENANT) cap is enforced: one tenant cannot take more than its cap in a pass.
# --------------------------------------------------------------------------------------------------


def test_per_business_cap_bounds_one_tenant(factory):
    _enable(factory, BIZ_A)
    _enqueue_many(factory, BIZ_A, 20, prefix="a")

    report = _worker(factory, max_jobs_per_business=5, max_jobs_per_pass=100).run_pass(now=NOW)

    assert report.handled_by_business == {BIZ_A: 5}  # capped at the tenant limit
    assert _queued_count(factory, BIZ_A) == 15  # the rest stay QUEUED for a later pass


# --------------------------------------------------------------------------------------------------
# The global (per-pass) cap is enforced across all tenants.
# --------------------------------------------------------------------------------------------------


def test_global_cap_bounds_the_whole_pass(factory):
    _enable(factory, BIZ_A)
    _enable(factory, BIZ_B)
    _enqueue_many(factory, BIZ_A, 20, prefix="a")
    _enqueue_many(factory, BIZ_B, 20, prefix="b")

    report = _worker(factory, max_jobs_per_business=100, max_jobs_per_pass=6).run_pass(now=NOW)

    assert len(report.handled) == 6  # global cap, regardless of per-business headroom
    # Fair split under the global cap: round-robin gives each tenant an equal share (3 each).
    assert report.handled_by_business == {BIZ_A: 3, BIZ_B: 3}


# --------------------------------------------------------------------------------------------------
# Round-robin: claims INTERLEAVE across tenants rather than draining one fully first.
# --------------------------------------------------------------------------------------------------


def test_claims_interleave_round_robin_across_tenants(factory):
    _enable(factory, BIZ_A)
    _enable(factory, BIZ_B)
    _enqueue_many(factory, BIZ_A, 5, prefix="a")
    _enqueue_many(factory, BIZ_B, 5, prefix="b")

    handled = _worker(factory).drain_fair(
        (BIZ_A, BIZ_B), now=NOW, max_jobs_per_business=3, max_jobs_per_pass=100
    )

    order = [h.business_id for h in handled]
    # One per tenant per round → strict A,B,A,B,A,B interleave (not A,A,A,B,B,B).
    assert order == [BIZ_A, BIZ_B, BIZ_A, BIZ_B, BIZ_A, BIZ_B]


# --------------------------------------------------------------------------------------------------
# Fairness preserves the headline boundary: every claimed job is PARKED, never run or sent.
# --------------------------------------------------------------------------------------------------


def test_fairly_claimed_jobs_are_parked_not_executed(factory):
    _enable(factory, BIZ_A)
    _enable(factory, BIZ_B)
    _enqueue_many(factory, BIZ_A, 3, prefix="a")
    _enqueue_many(factory, BIZ_B, 3, prefix="b")

    report = _worker(factory).run_pass(now=NOW)

    assert report.parked_count == len(report.handled) == 6
    assert all(h.disposition is ClaimDisposition.PARKED_NO_HANDLER for h in report.handled)
    with factory() as session:
        rows = session.scalars(select(DurableJobRequest)).all()
        assert all(r.status == JobRequestStatus.DEAD_LETTER.value for r in rows)
        assert all(
            r.result_ref is None and r.completed_at is None for r in rows
        )  # nothing produced


# --------------------------------------------------------------------------------------------------
# No-op / edge cases: no enabled tenants, and tenants with nothing queued, change nothing.
# --------------------------------------------------------------------------------------------------


def test_fair_drain_no_tenants_is_noop(factory):
    assert _worker(factory).drain_fair((), now=NOW) == []


def test_fair_drain_nothing_queued_is_noop(factory):
    _enable(factory, BIZ_A)
    _enable(factory, BIZ_B)  # both enabled, but neither has any queued work

    report = _worker(factory).run_pass(now=NOW)

    assert report.handled == ()
    assert report.handled_by_business == {}


def test_one_tenant_exhausting_under_cap_lets_the_other_continue(factory):
    """B has fewer jobs than its cap; once B is exhausted the round-robin keeps serving A up to A's cap —
    draining B early never blocks A, and A's cap still bounds it."""
    _enable(factory, BIZ_A)
    _enable(factory, BIZ_B)
    _enqueue_many(factory, BIZ_A, 10, prefix="a")
    _enqueue_many(factory, BIZ_B, 2, prefix="b")

    report = _worker(factory, max_jobs_per_business=5, max_jobs_per_pass=100).run_pass(now=NOW)

    assert report.handled_by_business == {BIZ_A: 5, BIZ_B: 2}  # A capped at 5, B drained its 2
    assert _queued_count(factory, BIZ_A) == 5
    assert _queued_count(factory, BIZ_B) == 0


# --------------------------------------------------------------------------------------------------
# The real fair claim flow on a disposable PostgreSQL (FOR UPDATE SKIP LOCKED is PostgreSQL-only).
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
def test_fair_drain_does_not_starve_a_tenant_on_postgres(pg_factory):
    _enable(pg_factory, BIZ_A)
    _enable(pg_factory, BIZ_B)
    _enqueue_many(pg_factory, BIZ_A, 50, prefix="a")
    (b_job,) = _enqueue_many(pg_factory, BIZ_B, 1, prefix="b")

    report = DurableJobWorker(
        pg_factory, _settings(), clock=lambda: NOW, max_jobs_per_business=10, max_jobs_per_pass=3
    ).run_pass(now=NOW)

    assert report.handled_by_business.get(BIZ_B, 0) == 1  # B serviced despite A's backlog
    assert len(report.handled) == 3  # global cap honoured against real claim commits
    with pg_factory() as session:
        assert session.get(DurableJobRequest, b_job).status == JobRequestStatus.DEAD_LETTER.value
