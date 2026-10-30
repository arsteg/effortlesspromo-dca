"""DCA-111 — enforce hierarchical emergency stops.

Pins the load-bearing behaviour of :mod:`app.services.emergency_stop` and its enforcement by the
DCA-109 worker:

1. **Hierarchy + enforcement at the worker's checkpoints.** A PLATFORM-WIDE stop halts EVERY business's
   background work; a PER-BUSINESS stop halts only that business while an unrelated business keeps
   working; clearing resumes; and the platform tier DOMINATES (a platform stop blocks a business whose
   own latest state is ``LIFTED``).
2. **Authority-gated set/clear; a denial changes nothing.** The platform stop needs a platform operator
   holding ``platform.emergency.stop`` (a business admin cannot); a business stop needs a business
   administrator holding ``emergency.stop`` (a viewer / ordinary user cannot, and a platform operator
   holds no business scope). Every refusal is raised BEFORE the session is mutated, so it stages no row
   and leaves the current effective state untouched.
3. **Append-only + atomic audit.** A set/clear inserts a new version (history is never deleted); a
   business set/clear stages an atomic DCA-026 audit entry in the same unit of work.

Store logic is unit-tested on in-memory SQLite; the worker enforcement is driven serially on SQLite
exactly like the DCA-109 suite. The RLS/migration paths for the tables are covered by
``tests/test_rls.py`` and ``tests/test_migrations.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.db.base import Base, make_engine, make_session_factory
from app.db.models import (
    AuditEvent,
    BusinessEmergencyStop,
    DurableJobRequest,
    EmergencyStopState,
    JobRequestStatus,
    PlatformEmergencyStop,
    RestrictionAuthorityPlane,
    TenantAgent,
)
from app.jobs.store import JobEnqueueRequest, JobRequestStore
from app.jobs.worker import DurableJobWorker
from app.services.emergency_stop import (
    AUDIT_ACTION_CLEAR,
    AUDIT_ACTION_SET,
    REASON_MISSING_REASON,
    REASON_NOT_AUTHORIZED,
    REASON_NOT_PLATFORM_OPERATOR,
    BusinessEmergencyStopStore,
    BusinessStopPrincipal,
    EmergencyStopAuthorizationError,
    EmergencyStopError,
    EmergencyStopResolver,
    PlatformEmergencyStopStore,
    PlatformStopPrincipal,
)

BIZ_A = "11111111-1111-1111-1111-11111111c111"
BIZ_B = "22222222-2222-2222-2222-22222222c111"
ADMIN = "00000000-0000-4000-9000-0000000a0111"
OPERATOR = "00000000-0000-4000-9000-00000090e111"
NOW = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)

BIZ_ADMIN = BusinessStopPrincipal(subject=ADMIN, roles=("admin",))
BIZ_VIEWER = BusinessStopPrincipal(subject=ADMIN, roles=("viewer",))
BIZ_USER = BusinessStopPrincipal(subject=ADMIN, roles=("user",))
PLATFORM_OP = PlatformStopPrincipal(subject=OPERATOR, roles=("platform_operator",))
# A tenant admin's business roles never yield a PLATFORM scope.
TENANT_ADMIN_AS_PLATFORM = PlatformStopPrincipal(subject=ADMIN, roles=("owner", "admin"))


@pytest.fixture
def factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


def _settings():
    from app.core.config import Settings

    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="testing",
        database_url="sqlite+pysqlite:///:memory:",
        service_auth_token="test-service-token",
        worker_interval_seconds=1,
    )


def _enable(factory, business_id: str) -> None:
    with factory() as session:
        session.add(TenantAgent(business_id=business_id, country="IN", enabled=True))
        session.commit()


def _enqueue(factory, business_id: str, key: str) -> str:
    with factory() as session:
        row, _ = JobRequestStore(session, business_id).enqueue(
            JobEnqueueRequest(
                job_type="prospect.research",
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


def _worker(factory) -> DurableJobWorker:
    return DurableJobWorker(factory, _settings(), clock=lambda: NOW)


# --------------------------------------------------------------------------------------------------
# Store: set / clear / resolve (per-business and platform), append-only versioning.
# --------------------------------------------------------------------------------------------------


def test_business_set_then_clear_toggles_effective_state_append_only(factory):
    with factory() as session:
        store = BusinessEmergencyStopStore(session, BIZ_A)
        assert store.is_stopped() is False  # never set → not stopped
        store.set_stop(BIZ_ADMIN, reason="manual_pause", now=NOW)
        session.commit()
    with factory() as session:
        store = BusinessEmergencyStopStore(session, BIZ_A)
        assert store.is_stopped() is True
        assert store.current().version == 1
        assert store.current().state == EmergencyStopState.ACTIVE.value
        store.clear_stop(BIZ_ADMIN, reason="resume_after_review", now=NOW)
        session.commit()
    with factory() as session:
        store = BusinessEmergencyStopStore(session, BIZ_A)
        assert store.is_stopped() is False  # cleared → resumed
        # Append-only: both the set and the clear are preserved as versions (history never deleted).
        assert [r.version for r in store.history()] == [1, 2]
        assert [r.state for r in store.history()] == [
            EmergencyStopState.ACTIVE.value,
            EmergencyStopState.LIFTED.value,
        ]


def test_business_stop_is_tenant_isolated_in_the_store(factory):
    with factory() as session:
        BusinessEmergencyStopStore(session, BIZ_A).set_stop(BIZ_ADMIN, reason="pause_a", now=NOW)
        session.commit()
    with factory() as session:
        assert BusinessEmergencyStopStore(session, BIZ_A).is_stopped() is True
        assert BusinessEmergencyStopStore(session, BIZ_B).is_stopped() is False  # B unaffected


def test_platform_set_then_clear_toggles_effective_state(factory):
    with factory() as session:
        store = PlatformEmergencyStopStore(session)
        assert store.is_stopped() is False
        store.set_stop(PLATFORM_OP, reason="incident", now=NOW)
        session.commit()
    with factory() as session:
        store = PlatformEmergencyStopStore(session)
        assert store.is_stopped() is True
        assert store.current().authority_plane == RestrictionAuthorityPlane.PLATFORM.value
        store.clear_stop(PLATFORM_OP, reason="incident_resolved", now=NOW)
        session.commit()
    with factory() as session:
        assert PlatformEmergencyStopStore(session).is_stopped() is False
        assert [r.version for r in PlatformEmergencyStopStore(session).history()] == [1, 2]


def test_resolver_platform_dominates_a_business_whose_own_state_is_lifted(factory):
    with factory() as session:
        # Business A has explicitly LIFTED its own stop, but the platform-wide stop is active.
        BusinessEmergencyStopStore(session, BIZ_A).set_stop(BIZ_ADMIN, reason="p", now=NOW)
        BusinessEmergencyStopStore(session, BIZ_A).clear_stop(BIZ_ADMIN, reason="r", now=NOW)
        PlatformEmergencyStopStore(session).set_stop(PLATFORM_OP, reason="incident", now=NOW)
        session.commit()
    with factory() as session:
        resolver = EmergencyStopResolver(session)
        assert resolver.is_business_stopped(BIZ_A) is False  # A's OWN state is lifted
        assert resolver.is_platform_stopped() is True
        assert resolver.is_claiming_blocked(BIZ_A) is True  # platform DOMINATES → still blocked
        assert resolver.is_claiming_blocked(BIZ_B) is True  # every business blocked platform-wide


# --------------------------------------------------------------------------------------------------
# Authority: unauthorized set/clear refused, and the refusal changes nothing.
# --------------------------------------------------------------------------------------------------


def test_business_stop_refuses_viewer_and_ordinary_user_and_changes_nothing(factory):
    for principal in (BIZ_VIEWER, BIZ_USER):
        with factory() as session:
            store = BusinessEmergencyStopStore(session, BIZ_A)
            with pytest.raises(EmergencyStopAuthorizationError) as exc:
                store.set_stop(principal, reason="pause", now=NOW)
            assert exc.value.code == REASON_NOT_AUTHORIZED
            session.rollback()
        # No row staged, no audit → the effective state is untouched.
        with factory() as session:
            assert session.query(BusinessEmergencyStop).count() == 0
            assert session.query(AuditEvent).count() == 0
            assert BusinessEmergencyStopStore(session, BIZ_A).is_stopped() is False


def test_platform_stop_refuses_tenant_admin_and_changes_nothing(factory):
    with factory() as session:
        store = PlatformEmergencyStopStore(session)
        with pytest.raises(EmergencyStopAuthorizationError) as exc:
            store.set_stop(TENANT_ADMIN_AS_PLATFORM, reason="pause", now=NOW)
        assert exc.value.code == REASON_NOT_PLATFORM_OPERATOR
        session.rollback()
    with factory() as session:
        assert session.query(PlatformEmergencyStop).count() == 0
        assert PlatformEmergencyStopStore(session).is_stopped() is False


def test_set_requires_a_non_empty_reason_and_changes_nothing(factory):
    with factory() as session:
        with pytest.raises(EmergencyStopError) as exc:
            BusinessEmergencyStopStore(session, BIZ_A).set_stop(BIZ_ADMIN, reason="  ", now=NOW)
        assert exc.value.code == REASON_MISSING_REASON
        session.rollback()
    with factory() as session:
        assert session.query(BusinessEmergencyStop).count() == 0


def test_business_set_and_clear_write_atomic_audit_entries(factory):
    with factory() as session:
        BusinessEmergencyStopStore(session, BIZ_A).set_stop(BIZ_ADMIN, reason="pause", now=NOW)
        BusinessEmergencyStopStore(session, BIZ_A).clear_stop(BIZ_ADMIN, reason="resume", now=NOW)
        session.commit()
    with factory() as session:
        events = session.query(AuditEvent).order_by(AuditEvent.action).all()
        actions = {e.action for e in events}
        assert actions == {AUDIT_ACTION_SET, AUDIT_ACTION_CLEAR}
        for e in events:
            assert e.business_id == BIZ_A
            assert e.actor_id == ADMIN
            assert e.references["authorityScope"] == "emergency.stop"


# --------------------------------------------------------------------------------------------------
# Worker enforcement: platform halts ALL; business halts ONE; clearing resumes.
# --------------------------------------------------------------------------------------------------


def test_platform_stop_halts_all_businesses_at_the_worker(factory):
    _enable(factory, BIZ_A)
    _enable(factory, BIZ_B)
    a_job = _enqueue(factory, BIZ_A, "a-1")
    b_job = _enqueue(factory, BIZ_B, "b-1")
    with factory() as session:
        PlatformEmergencyStopStore(session).set_stop(PLATFORM_OP, reason="incident", now=NOW)
        session.commit()

    report = _worker(factory).run_pass(now=NOW)

    assert report.handled == ()  # nothing claimed for ANY business
    assert _status(factory, a_job) == JobRequestStatus.QUEUED.value  # A untouched
    assert _status(factory, b_job) == JobRequestStatus.QUEUED.value  # B untouched


def test_business_stop_halts_only_that_business(factory):
    _enable(factory, BIZ_A)
    _enable(factory, BIZ_B)
    a_job = _enqueue(factory, BIZ_A, "a-1")
    b_job = _enqueue(factory, BIZ_B, "b-1")
    with factory() as session:
        BusinessEmergencyStopStore(session, BIZ_A).set_stop(BIZ_ADMIN, reason="pause_a", now=NOW)
        session.commit()

    report = _worker(factory).run_pass(now=NOW)

    # Only B's job was driven; A (paused) was skipped entirely.
    assert {h.business_id for h in report.handled} == {BIZ_B}
    assert _status(factory, a_job) == JobRequestStatus.QUEUED.value  # A paused → untouched
    assert (
        _status(factory, b_job) == JobRequestStatus.DEAD_LETTER.value
    )  # B driven (parked, not run)


def test_clearing_a_business_stop_resumes_processing(factory):
    _enable(factory, BIZ_A)
    a_job = _enqueue(factory, BIZ_A, "a-1")
    with factory() as session:
        BusinessEmergencyStopStore(session, BIZ_A).set_stop(BIZ_ADMIN, reason="pause_a", now=NOW)
        session.commit()

    # Paused: the pass does nothing for A.
    assert _worker(factory).run_pass(now=NOW).handled == ()
    assert _status(factory, a_job) == JobRequestStatus.QUEUED.value

    # Clear the stop → the next pass resumes A and drives its job safely (parked, never run/sent).
    with factory() as session:
        BusinessEmergencyStopStore(session, BIZ_A).clear_stop(BIZ_ADMIN, reason="resume", now=NOW)
        session.commit()

    report = _worker(factory).run_pass(now=NOW)
    assert report.parked_count == 1
    assert _status(factory, a_job) == JobRequestStatus.DEAD_LETTER.value


def test_business_stop_also_suppresses_due_schedule_enqueue(factory):
    """A paused business does not even generate new work: its due-schedule maintenance tick is skipped
    (a relevant side effect), so no cycle job is enqueued while it is stopped."""
    from app.db.models import RecurringSchedule, ScheduleStatus

    _enable(factory, BIZ_A)
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
        BusinessEmergencyStopStore(session, BIZ_A).set_stop(BIZ_ADMIN, reason="pause_a", now=NOW)
        session.commit()

    report = _worker(factory).run_pass(now=NOW)

    assert BIZ_A not in report.scheduled  # the maintenance tick was skipped for the paused business
    with factory() as session:
        # No cycle job was enqueued (only the schedule row exists; nothing new was created).
        jobs = session.query(DurableJobRequest).filter_by(business_id=BIZ_A).count()
        assert jobs == 0
