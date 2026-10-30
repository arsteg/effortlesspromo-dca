"""DCA-105 — inspect and requeue a dead-letter job.

DCA-104 DEAD-LETTERED a job (a permanent policy/auth failure, or a transient failure that exhausted its
attempt cap). DCA-105 is the operator-facing recovery for that terminal state: a platform operator may
INSPECT a parked job's safe reason and REQUEUE it for another attempt.

The headline acceptance:
- only an authorized platform OPERATOR (holding ``platform.job.deadletter.requeue``) may requeue — a
  tenant administrator / any non-operator is refused BEFORE any state is read or written (nothing changes);
- a requeued job RETAINS its logical side-effect identity — the SAME row flips ``DEAD_LETTER`` → ``QUEUED``
  in place, preserving its ``(business_id, job_type, idempotency_key)`` dedup identity and payload
  pointer/digest (never a new job);
- every relevant denial (not-authorized, not-dead-letter, cross-tenant not-found, missing/PII reason) is
  reproduced with NO unintended state change;
- reviving a job makes it claimable; it EXECUTES nothing — execution stays a disabled future capability,
  the DCA has no send path, and acceptance of a prospect is not recipient consent.

Two layers, matching the DCA-101/102/103/104 suites:
- **Always-on logic tests** (in-memory SQLite) drive the inspect/requeue contract + every denial serially.
- **Disposable PostgreSQL** (``DCA_TEST_DATABASE_URL``) re-runs the flip — and the no-double-revive case
  across short transactions — against a real Postgres with the ``FOR UPDATE`` row lock. Skips without a URL.

Spin up a throwaway Postgres, e.g.:
    docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55471:5432 postgres:16-alpine
    export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55471/dca"
"""

from __future__ import annotations

import os
import random
from datetime import UTC, datetime, timedelta

import pytest

from app.audit.sink import ActorType, AuditPayloadError
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import AuditEvent, DurableJobRequest, JobRequestStatus
from app.identity.permissions import PLATFORM_OPERATOR_ROLE
from app.jobs.claim import JobClaimStore
from app.jobs.deadletter import (
    AUDIT_ACTION_REQUEUE,
    REASON_MISSING_REASON,
    REASON_NOT_AUTHORIZED,
    DeadLetterDetail,
    JobDeadLetterStore,
    JobRequeueAuthorizationError,
    JobRequeueError,
    RequeueOutcome,
    RequeuePrincipal,
)
from app.jobs.retry import FailureSignal, JobFailureCategory, JobRetryScheduler
from app.jobs.store import JobEnqueueRequest, JobRequestStore

BIZ_A = "11111111-1111-1111-1111-11111111d105"
BIZ_B = "22222222-2222-2222-2222-22222222d105"
NOW = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)

OPERATOR = RequeuePrincipal(subject="op-105-0001", roles=(PLATFORM_OPERATOR_ROLE,))
# A business administrator holds only business-plane scopes — never a platform scope.
BUSINESS_ADMIN = RequeuePrincipal(subject="admin-105-0001", roles=("admin", "owner"))
# A principal with NO roles at all (an unauthenticated / role-less caller).
NOBODY = RequeuePrincipal(subject="nobody-105-0001", roles=())


# --------------------------------------------------------------------------------------------------
# Fixtures + helpers
# --------------------------------------------------------------------------------------------------


@pytest.fixture
def factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def _enqueue(factory, business_id: str, key: str, *, max_attempts: int = 5) -> str:
    with factory() as session:
        row, _ = JobRequestStore(session, business_id).enqueue(
            JobEnqueueRequest(
                job_type="prospect.research",
                idempotency_key=key,
                payload_ref=f"dca-payload://synthetic/{key}",
                payload_metadata={"priority": "normal"},
                max_attempts=max_attempts,
            ),
            now=NOW,
        )
        session.commit()
        return row.id


def _dead_letter(factory, business_id: str, key: str) -> str:
    """Enqueue → claim → report a PERMANENT failure, leaving the job parked in ``DEAD_LETTER``."""
    job_id = _enqueue(factory, business_id, key, max_attempts=5)
    with factory() as session:
        claim = JobClaimStore(session, business_id).claim_next(
            "worker-1", now=NOW, lease_seconds=600
        )
    with factory() as session:
        JobRetryScheduler(session, business_id).record_failure(
            job_id,
            "worker-1",
            claim.fence_token,
            FailureSignal(JobFailureCategory.POLICY_DENIED, error_note="rulepack_denied"),
            now=NOW + timedelta(seconds=5),
            rng=random.Random(7),
        )
    return job_id


def _row(factory, job_id) -> DurableJobRequest:
    with factory() as session:
        return session.get(DurableJobRequest, job_id)


def _requeue(factory, business_id, job_id, principal, *, reason="corrected_and_retry", at=None):
    with factory() as session:
        result = JobDeadLetterStore(session, business_id).requeue(
            job_id, principal, reason=reason, now=at or (NOW + timedelta(hours=1))
        )
        session.commit()
        return result


# --------------------------------------------------------------------------------------------------
# Inspect
# --------------------------------------------------------------------------------------------------


def test_inspect_returns_pii_safe_detail_of_a_parked_job(factory):
    job_id = _dead_letter(factory, BIZ_A, "job-inspect")
    with factory() as session:
        detail = JobDeadLetterStore(session, BIZ_A).inspect(job_id, OPERATOR)
    assert isinstance(detail, DeadLetterDetail)
    assert detail.job_id == job_id and detail.business_id == BIZ_A
    assert detail.status == JobRequestStatus.DEAD_LETTER.value and detail.is_dead_letter is True
    assert detail.job_type == "prospect.research" and detail.idempotency_key == "job-inspect"
    assert detail.last_error == "rulepack_denied"  # the safe reason it was parked
    assert (
        detail.payload_ref == "dca-payload://synthetic/job-inspect"
    )  # opaque pointer, not payload
    assert detail.attempt_count == 1 and detail.max_attempts == 5


def test_inspect_requires_platform_read_scope(factory):
    """A tenant administrator (no platform scope) cannot even inspect — refused, learns nothing."""
    job_id = _dead_letter(factory, BIZ_A, "job-inspect-denied")
    with factory() as session:
        with pytest.raises(JobRequeueAuthorizationError):
            JobDeadLetterStore(session, BIZ_A).inspect(job_id, BUSINESS_ADMIN)


def test_inspect_cross_tenant_or_unknown_is_not_found(factory):
    job_id = _dead_letter(factory, BIZ_A, "job-inspect-tenant")
    with factory() as session:
        # Business B operator cannot see A's job (bound to B's scope -> not found).
        assert JobDeadLetterStore(session, BIZ_B).inspect(job_id, OPERATOR) is None
        assert JobDeadLetterStore(session, BIZ_A).inspect("no-such-id", OPERATOR) is None


# --------------------------------------------------------------------------------------------------
# Requeue — the happy path (ACCEPTANCE: authorized operator revives, logical identity preserved)
# --------------------------------------------------------------------------------------------------


def test_authorized_operator_requeues_preserving_logical_identity(factory):
    """ACCEPTANCE: an operator revives a parked job IN PLACE — same row id + dedup identity + payload."""
    job_id = _dead_letter(factory, BIZ_A, "job-requeue")
    before = _row(factory, job_id)
    identity_before = (before.id, before.job_type, before.idempotency_key, before.payload_digest)
    assert before.status == JobRequestStatus.DEAD_LETTER.value

    at = NOW + timedelta(hours=1)
    result = _requeue(factory, BIZ_A, job_id, OPERATOR, reason="fixed_payload_retry", at=at)

    assert result.changed is True and result.outcome is RequeueOutcome.REQUEUED
    assert result.status == JobRequestStatus.QUEUED.value
    assert result.prior_status == JobRequestStatus.DEAD_LETTER.value
    assert result.prior_attempt_count == 1 and result.attempt_count == 0

    row = _row(factory, job_id)
    # Same logical job — NOT a new one. Identity and payload pointer/digest are untouched.
    assert (row.id, row.job_type, row.idempotency_key, row.payload_digest) == identity_before
    assert row.payload_ref == before.payload_ref
    # Revived: QUEUED, fresh attempt budget, claimable again, lease cleared.
    assert row.status == JobRequestStatus.QUEUED.value
    assert row.attempt_count == 0 and row.max_attempts == 5
    assert row.lease_owner is None and row.lease_expires_at is None and row.lease_deadline is None
    assert row.available_at.replace(tzinfo=UTC) == at
    # The dead-letter reason is retained as the inspectable record (future attempt overwrites it).
    assert row.last_error == "rulepack_denied"
    # Exactly ONE job row for this logical intent (requeue reused the row, created no duplicate).
    with factory() as session:
        count = len(session.query(DurableJobRequest).filter_by(idempotency_key="job-requeue").all())
    assert count == 1


def test_requeue_is_claimable_again_after_revival(factory):
    """A revived QUEUED job is picked up by the ordinary claim path again (it EXECUTES nothing)."""
    job_id = _dead_letter(factory, BIZ_A, "job-reclaimable")
    _requeue(factory, BIZ_A, job_id, OPERATOR, at=NOW + timedelta(hours=1))
    with factory() as session:
        claim = JobClaimStore(session, BIZ_A).claim_next(
            "worker-9", now=NOW + timedelta(hours=2), lease_seconds=600
        )
    assert claim is not None and claim.job_id == job_id
    row = _row(factory, job_id)
    assert row.status == JobRequestStatus.RUNNING.value  # leased, not run


def test_requeue_stages_an_atomic_audit_entry(factory):
    """The who/when/why is recorded as a DCA-026 audit entry with OPAQUE references (no payload)."""
    job_id = _dead_letter(factory, BIZ_A, "job-audit")
    _requeue(factory, BIZ_A, job_id, OPERATOR, reason="manual_recovery")
    with factory() as session:
        events = session.query(AuditEvent).filter_by(action=AUDIT_ACTION_REQUEUE).all()
    assert len(events) == 1
    event = events[0]
    assert event.business_id == BIZ_A
    assert event.actor_id == OPERATOR.subject
    assert event.actor_type == ActorType.PLATFORM_SUPPORT.value
    assert event.reason == "manual_recovery"
    assert event.references["jobId"] == job_id
    assert event.references["idempotencyKey"] == "job-audit"
    assert event.references["priorStatus"] == JobRequestStatus.DEAD_LETTER.value
    assert event.references["authorityScope"] == "platform.job.deadletter.requeue"


# --------------------------------------------------------------------------------------------------
# Requeue — the denials (each refused, NOTHING changes)
# --------------------------------------------------------------------------------------------------


def test_business_admin_cannot_requeue(factory):
    """ACCEPTANCE denial: a tenant administrator holds no platform scope — refused, nothing changes."""
    job_id = _dead_letter(factory, BIZ_A, "job-admin-denied")
    with factory() as session:
        with pytest.raises(JobRequeueAuthorizationError) as exc:
            JobDeadLetterStore(session, BIZ_A).requeue(
                job_id, BUSINESS_ADMIN, reason="trying", now=NOW + timedelta(hours=1)
            )
        session.rollback()
    assert exc.value.code == REASON_NOT_AUTHORIZED
    row = _row(factory, job_id)
    assert row.status == JobRequestStatus.DEAD_LETTER.value  # untouched
    assert row.attempt_count == 1
    with factory() as session:
        assert session.query(AuditEvent).filter_by(action=AUDIT_ACTION_REQUEUE).count() == 0


def test_roleless_caller_cannot_requeue(factory):
    job_id = _dead_letter(factory, BIZ_A, "job-nobody")
    with factory() as session:
        with pytest.raises(JobRequeueAuthorizationError):
            JobDeadLetterStore(session, BIZ_A).requeue(
                job_id, NOBODY, reason="trying", now=NOW + timedelta(hours=1)
            )
        session.rollback()
    assert _row(factory, job_id).status == JobRequestStatus.DEAD_LETTER.value


def test_requeue_a_non_dead_letter_job_is_refused(factory):
    """A QUEUED (never-parked) job is left untouched: requeue only revives a DEAD_LETTER job."""
    job_id = _enqueue(factory, BIZ_A, "job-still-queued", max_attempts=5)
    result = _requeue(factory, BIZ_A, job_id, OPERATOR)
    assert result.changed is False and result.outcome is RequeueOutcome.NOT_DEAD_LETTER
    assert result.prior_status == JobRequestStatus.QUEUED.value
    assert _row(factory, job_id).status == JobRequestStatus.QUEUED.value


def test_second_requeue_is_idempotent_no_op(factory):
    """ACCEPTANCE: a requeue of a job already revived to QUEUED changes nothing (not-dead-letter)."""
    job_id = _dead_letter(factory, BIZ_A, "job-double")
    first = _requeue(factory, BIZ_A, job_id, OPERATOR, at=NOW + timedelta(hours=1))
    assert first.changed is True
    second = _requeue(factory, BIZ_A, job_id, OPERATOR, at=NOW + timedelta(hours=2))
    assert second.changed is False and second.outcome is RequeueOutcome.NOT_DEAD_LETTER
    # Exactly one audit entry (only the real revival was audited).
    with factory() as session:
        assert session.query(AuditEvent).filter_by(action=AUDIT_ACTION_REQUEUE).count() == 1


def test_cross_tenant_requeue_is_not_found(factory):
    """Tenant isolation: a B operator cannot requeue A's job (reads not-found, changes nothing)."""
    job_id = _dead_letter(factory, BIZ_A, "job-cross")
    result = _requeue(factory, BIZ_B, job_id, OPERATOR)
    assert result.changed is False and result.outcome is RequeueOutcome.NOT_FOUND
    assert _row(factory, job_id).status == JobRequestStatus.DEAD_LETTER.value


def test_missing_reason_is_refused_before_any_change(factory):
    job_id = _dead_letter(factory, BIZ_A, "job-noreason")
    with factory() as session:
        with pytest.raises(JobRequeueError) as exc:
            JobDeadLetterStore(session, BIZ_A).requeue(
                job_id, OPERATOR, reason="   ", now=NOW + timedelta(hours=1)
            )
        session.rollback()
    assert exc.value.code == REASON_MISSING_REASON
    assert _row(factory, job_id).status == JobRequestStatus.DEAD_LETTER.value


def test_reason_with_raw_pii_is_refused_before_any_change(factory):
    """The reason is screened like an audit reference — raw contact data is refused before the flip."""
    job_id = _dead_letter(factory, BIZ_A, "job-pii-reason")
    with factory() as session:
        with pytest.raises(AuditPayloadError):
            JobDeadLetterStore(session, BIZ_A).requeue(
                job_id,
                OPERATOR,
                reason="retry after emailing person@example.com",
                now=NOW + timedelta(hours=1),
            )
        session.rollback()
    row = _row(factory, job_id)
    assert row.status == JobRequestStatus.DEAD_LETTER.value  # untouched
    with factory() as session:
        assert session.query(AuditEvent).filter_by(action=AUDIT_ACTION_REQUEUE).count() == 0


def test_authority_is_checked_before_the_missing_reason(factory):
    """Authority is the FIRST gate: an unauthorized caller is refused even with a bad reason."""
    job_id = _dead_letter(factory, BIZ_A, "job-order")
    with factory() as session:
        with pytest.raises(JobRequeueAuthorizationError):
            JobDeadLetterStore(session, BIZ_A).requeue(
                job_id, BUSINESS_ADMIN, reason="", now=NOW + timedelta(hours=1)
            )


# --------------------------------------------------------------------------------------------------
# The same contract on a disposable PostgreSQL (real FOR UPDATE / commits).
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
    try:
        yield make_session_factory(engine)
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@requires_pg
def test_requeue_preserves_identity_postgres(pg_factory):
    """ACCEPTANCE on real Postgres: the flip revives the SAME row (identity preserved) under FOR UPDATE."""
    job_id = _dead_letter(pg_factory, BIZ_A, "job-requeue-pg")
    before = _row(pg_factory, job_id)
    identity_before = (before.id, before.job_type, before.idempotency_key, before.payload_digest)
    result = _requeue(pg_factory, BIZ_A, job_id, OPERATOR, at=NOW + timedelta(hours=1))
    assert result.changed is True and result.outcome is RequeueOutcome.REQUEUED
    row = _row(pg_factory, job_id)
    assert (row.id, row.job_type, row.idempotency_key, row.payload_digest) == identity_before
    assert row.status == JobRequestStatus.QUEUED.value and row.attempt_count == 0


@requires_pg
def test_no_double_revive_postgres(pg_factory):
    """ACCEPTANCE on real Postgres: after a committed revive, a second requeue is a NOT_DEAD_LETTER no-op."""
    job_id = _dead_letter(pg_factory, BIZ_A, "job-double-pg")
    first = _requeue(pg_factory, BIZ_A, job_id, OPERATOR, at=NOW + timedelta(hours=1))
    assert first.changed is True
    second = _requeue(pg_factory, BIZ_A, job_id, OPERATOR, at=NOW + timedelta(hours=2))
    assert second.changed is False and second.outcome is RequeueOutcome.NOT_DEAD_LETTER


@requires_pg
def test_business_admin_cannot_requeue_postgres(pg_factory):
    """ACCEPTANCE on real Postgres: a tenant admin's requeue is refused and the row stays dead-lettered."""
    job_id = _dead_letter(pg_factory, BIZ_A, "job-admin-pg")
    with pg_factory() as session:
        with pytest.raises(JobRequeueAuthorizationError):
            JobDeadLetterStore(session, BIZ_A).requeue(
                job_id, BUSINESS_ADMIN, reason="trying", now=NOW + timedelta(hours=1)
            )
        session.rollback()
    assert _row(pg_factory, job_id).status == JobRequestStatus.DEAD_LETTER.value
