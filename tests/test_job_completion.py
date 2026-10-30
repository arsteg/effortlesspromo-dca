"""DCA-103 — commit a fenced job result.

DCA-101 leased a job (fencing token); DCA-102 heartbeated a live lease. DCA-103 lets the worker that
holds the CURRENT lease COMMIT a result — flip the running row to ``SUCCEEDED``, clear its lease, record
an opaque result pointer — but only after re-validating, in the same short transaction as the commit:

- it still holds the current lease (fenced on ``(worker_id, lease_version)`` on a ``RUNNING`` row);
- the lease has not lapsed (``now < lease_expires_at`` — a reclaimable job may not be committed);
- every authorization revision it relied on (``(source_key, terms_version)``) is still in force.

The headline acceptance: a STALE worker cannot commit after another worker RECLAIMED the job, or after the
SOURCE RIGHTS it operated against CHANGED. Each refusal changes NOTHING (the relevant denial). Committing a
result records a terminal status; it EXECUTES nothing — execution stays a disabled future capability, the
DCA has no send path, and acceptance of a prospect is not recipient consent.

Two layers, matching the DCA-101/102 suites:

- **Always-on logic tests** (in-memory SQLite) drive the completion contract serially.
- **Disposable PostgreSQL** (``DCA_TEST_DATABASE_URL``) re-runs the commit — and, crucially, the
  commit-vs-reclaim race — against a real Postgres with real commits/rollbacks and the ``FOR UPDATE`` row
  lock, because the acceptance cases are about durable row state across short transactions (never SQLite).
  Skips without a URL.

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
from app.jobs.claim import JobClaimStore
from app.jobs.complete import (
    JobCompletionOutcome,
    JobCompletionStore,
    JobResultContainsPiiError,
    SourceAuthorizationRef,
    job_result_digest,
)
from app.jobs.store import JobEnqueueRequest, JobRequestStore
from app.services.source_approval import (
    ReviewerPrincipal,
    SourceApprovalReview,
    SourceApprovalStore,
)
from app.services.source_catalog import SourceCatalogStore, SourceDraftInput
from app.services.source_revocation import RevokerPrincipal, SourceRevocationStore
from app.services.source_terms import SourceTermsInput, SourceTermsStore

BIZ_A = "11111111-1111-1111-1111-11111111d103"
BIZ_B = "22222222-2222-2222-2222-22222222d103"
NOW = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)
SOURCE_KEY = "mca_master_data"
OPERATOR = ReviewerPrincipal(subject="op-001", roles=("platform_operator",))
REVOKER = RevokerPrincipal(subject="op-001", roles=("platform_operator",))
RESULT_REF = "dca-result://synthetic/observation-batch-1"


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


def _claim(factory, business_id: str, worker: str, *, now: datetime = NOW, lease_seconds: int = 60):
    with factory() as session:
        return JobClaimStore(session, business_id).claim_next(
            worker, now=now, lease_seconds=lease_seconds
        )


def _seed_in_force_approval(factory, *, key: str = SOURCE_KEY, now: datetime = NOW) -> int:
    """Catalogue a source, record a terms version and APPROVE it (platform operator), so the approval is
    in force. Returns the approved terms version. The source is NOT enabled/entitled — completion only
    re-checks the approval's ``is_in_force``, so that is all the authorization revision needs."""
    with factory() as session:
        SourceCatalogStore(session).create_draft(
            SourceDraftInput(
                key=key,
                kind="official",
                access_method="bulk_download",
                trust_tier="official_registry",
                india_coverage={"nationwide": True},
            )
        )
        SourceTermsStore(session).add_version(
            SourceTermsInput(
                source_key=key,
                operations={"collect": True, "display": True},
                purposes=["prospecting"],
                contract_evidence={"reference": "MCA-TOS-2026", "kind": "terms_of_use"},
            )
        )
        SourceApprovalStore(session).approve(
            key,
            1,
            SourceApprovalReview(
                reviewer_id="op-001",
                evidence={"reference": "RIGHTS-REVIEW-2026-11", "kind": "legal_review"},
                reviewed_at=now - timedelta(days=1),
                expires_at=now + timedelta(days=365),
            ),
            OPERATOR,
        )
        session.commit()
    return 1


def _revoke_approval(
    factory, *, key: str = SOURCE_KEY, version: int = 1, now: datetime = NOW
) -> None:
    with factory() as session:
        SourceRevocationStore(session).revoke(
            key, version, REVOKER, reason="rights_withdrawn_by_licensor", now=now
        )
        session.commit()


def _complete(factory, business_id: str, job_id, worker, fence, **kwargs):
    with factory() as session:
        return JobCompletionStore(session, business_id).complete(job_id, worker, fence, **kwargs)


def _row(factory, job_id) -> DurableJobRequest:
    with factory() as session:
        return session.get(DurableJobRequest, job_id)


# --------------------------------------------------------------------------------------------------
# Always-on logic tests (SQLite). The durable-row + race assertions also run on Postgres below.
# --------------------------------------------------------------------------------------------------


@pytest.fixture
def factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def test_current_holder_commits_a_fenced_result(factory):
    """Happy path: the current lease holder commits -> SUCCEEDED, lease cleared, result recorded."""
    job_id = _enqueue(factory, BIZ_A, "job-ok")
    claim = _claim(factory, BIZ_A, "worker-1")
    assert claim is not None and claim.fence_token == 1

    result = _complete(
        factory,
        BIZ_A,
        job_id,
        "worker-1",
        claim.fence_token,
        now=NOW + timedelta(seconds=5),
        result_ref=RESULT_REF,
    )
    assert result.committed is True
    assert result.outcome is JobCompletionOutcome.COMMITTED
    assert result.status == JobRequestStatus.SUCCEEDED.value
    assert result.result_ref == RESULT_REF
    assert result.result_digest == job_result_digest(RESULT_REF)

    row = _row(factory, job_id)
    assert row.status == JobRequestStatus.SUCCEEDED.value
    assert row.lease_owner is None and row.lease_expires_at is None
    assert row.lease_version == 1  # fencing token is not bumped by a commit
    assert row.result_ref == RESULT_REF
    assert row.result_digest == job_result_digest(RESULT_REF)
    assert row.completed_at is not None


def test_commit_without_a_result_ref_still_succeeds_but_stores_no_pointer(factory):
    """A job may complete with no output pointer: it goes SUCCEEDED, result columns stay NULL."""
    job_id = _enqueue(factory, BIZ_A, "job-noresult")
    claim = _claim(factory, BIZ_A, "worker-1")
    result = _complete(factory, BIZ_A, job_id, "worker-1", claim.fence_token, now=NOW)
    assert result.committed is True
    row = _row(factory, job_id)
    assert row.status == JobRequestStatus.SUCCEEDED.value
    assert row.result_ref is None and row.result_digest is None and row.completed_at is not None


def test_replaced_worker_cannot_commit_after_reclaim(factory):
    """ACCEPTANCE: a stale worker cannot commit after another worker RECLAIMED the job."""
    job_id = _enqueue(factory, BIZ_A, "job-reclaimed")
    first = _claim(factory, BIZ_A, "worker-1", lease_seconds=60)
    assert first is not None and first.fence_token == 1

    # worker-1 lapses; worker-2 reclaims -> fencing token advances to 2, worker-1's token is now stale.
    later = NOW + timedelta(seconds=120)
    second = _claim(factory, BIZ_A, "worker-2", now=later, lease_seconds=60)
    assert second is not None and second.fence_token == 2

    # worker-1 (replaced) tries to commit with its stale fence -> refused, NOTHING changes.
    result = _complete(
        factory,
        BIZ_A,
        job_id,
        "worker-1",
        first.fence_token,
        now=later + timedelta(seconds=1),
        result_ref=RESULT_REF,
    )
    assert result.committed is False
    assert result.outcome is JobCompletionOutcome.STALE_LEASE

    row = _row(factory, job_id)
    assert row.status == JobRequestStatus.RUNNING.value  # still worker-2's live claim
    assert row.lease_owner == "worker-2" and row.lease_version == 2
    assert row.result_ref is None and row.completed_at is None


def test_expired_holder_cannot_commit(factory):
    """A lapsed (reclaimable) lease may not be committed by its old holder, even before a reclaim."""
    job_id = _enqueue(factory, BIZ_A, "job-expired")
    claim = _claim(factory, BIZ_A, "worker-1", lease_seconds=60)
    assert claim is not None

    result = _complete(
        factory,
        BIZ_A,
        job_id,
        "worker-1",
        claim.fence_token,
        now=NOW + timedelta(seconds=120),
        result_ref=RESULT_REF,
    )
    assert result.committed is False
    assert result.outcome is JobCompletionOutcome.LEASE_EXPIRED

    row = _row(factory, job_id)
    assert row.status == JobRequestStatus.RUNNING.value  # untouched, still reclaimable
    assert row.lease_owner == "worker-1" and row.lease_version == 1
    assert row.result_ref is None and row.completed_at is None


def test_foreign_worker_cannot_commit_a_live_lease(factory):
    """Fenced on owner: a different worker id cannot commit someone else's live claim."""
    job_id = _enqueue(factory, BIZ_A, "job-foreign")
    claim = _claim(factory, BIZ_A, "worker-1")
    result = _complete(
        factory, BIZ_A, job_id, "worker-2", claim.fence_token, now=NOW + timedelta(seconds=5)
    )
    assert result.committed is False and result.outcome is JobCompletionOutcome.STALE_LEASE
    row = _row(factory, job_id)
    assert row.status == JobRequestStatus.RUNNING.value and row.lease_owner == "worker-1"


def test_source_rights_changed_refuses_commit(factory):
    """ACCEPTANCE: a worker cannot commit after the SOURCE RIGHTS it operated against CHANGED.

    The worker captures the in-force approval version, is "paused", a platform operator revokes that
    version, and the worker's commit — re-checking the authorization revision at the SAME ``now`` — is
    refused. Nothing is time-driven: the revocation pulled the right and ``is_in_force`` keys on it."""
    version = _seed_in_force_approval(factory)
    job_id = _enqueue(factory, BIZ_A, "job-rights")
    claim = _claim(factory, BIZ_A, "worker-1", lease_seconds=600)
    assert claim is not None
    auth = [SourceAuthorizationRef(SOURCE_KEY, version)]

    # Operator revokes the version the worker relies on (while the worker is mid-fetch).
    _revoke_approval(factory, version=version)

    # The worker's lease is still live, but the rights revision it operated against is gone -> refused.
    result = _complete(
        factory,
        BIZ_A,
        job_id,
        "worker-1",
        claim.fence_token,
        now=NOW + timedelta(seconds=30),
        result_ref=RESULT_REF,
        authorizations=auth,
    )
    assert result.committed is False
    assert result.outcome is JobCompletionOutcome.SOURCE_RIGHTS_CHANGED
    assert result.detail == f"{SOURCE_KEY}@v{version}"

    row = _row(factory, job_id)
    assert row.status == JobRequestStatus.RUNNING.value  # unchanged
    assert row.lease_owner == "worker-1" and row.lease_version == 1
    assert row.result_ref is None and row.completed_at is None


def test_commit_with_in_force_authorization_succeeds(factory):
    """The positive control for the rights check: an in-force revision lets the commit through."""
    version = _seed_in_force_approval(factory)
    job_id = _enqueue(factory, BIZ_A, "job-rights-ok")
    claim = _claim(factory, BIZ_A, "worker-1", lease_seconds=600)
    auth = [SourceAuthorizationRef(SOURCE_KEY, version)]
    result = _complete(
        factory,
        BIZ_A,
        job_id,
        "worker-1",
        claim.fence_token,
        now=NOW + timedelta(seconds=30),
        result_ref=RESULT_REF,
        authorizations=auth,
    )
    assert result.committed is True
    assert _row(factory, job_id).status == JobRequestStatus.SUCCEEDED.value


def test_commit_refused_when_a_relied_revision_was_never_approved(factory):
    """A revision with no approval at all (never approved / wrong version) is treated as not in force."""
    job_id = _enqueue(factory, BIZ_A, "job-noapproval")
    claim = _claim(factory, BIZ_A, "worker-1")
    result = _complete(
        factory,
        BIZ_A,
        job_id,
        "worker-1",
        claim.fence_token,
        now=NOW,
        authorizations=[SourceAuthorizationRef("never_catalogued", 7)],
    )
    assert result.committed is False
    assert result.outcome is JobCompletionOutcome.SOURCE_RIGHTS_CHANGED
    assert result.detail == "never_catalogued@v7"
    assert _row(factory, job_id).status == JobRequestStatus.RUNNING.value


def test_cross_tenant_job_cannot_be_completed(factory):
    """Tenant isolation: a worker in business B cannot complete business A's job (reads not-found)."""
    job_id = _enqueue(factory, BIZ_A, "job-tenant")
    claim = _claim(factory, BIZ_A, "worker-1")
    result = _complete(factory, BIZ_B, job_id, "worker-1", claim.fence_token, now=NOW)
    assert result.committed is False and result.outcome is JobCompletionOutcome.NOT_FOUND
    row = _row(factory, job_id)
    assert row.status == JobRequestStatus.RUNNING.value and row.lease_owner == "worker-1"


def test_result_ref_with_raw_pii_is_refused_before_any_change(factory):
    """A result pointer carrying raw contact data is refused before the row is touched (PII-safe)."""
    job_id = _enqueue(factory, BIZ_A, "job-pii")
    claim = _claim(factory, BIZ_A, "worker-1")
    with pytest.raises(JobResultContainsPiiError):
        _complete(
            factory,
            BIZ_A,
            job_id,
            "worker-1",
            claim.fence_token,
            now=NOW,
            result_ref="dca-result://leaks/person@example.com",
        )
    row = _row(factory, job_id)
    assert row.status == JobRequestStatus.RUNNING.value  # untouched
    assert row.result_ref is None and row.completed_at is None


def test_an_already_succeeded_job_cannot_be_committed_again(factory):
    """Idempotency of terminality: once SUCCEEDED the row is no longer RUNNING, so a re-commit is refused."""
    job_id = _enqueue(factory, BIZ_A, "job-twice")
    claim = _claim(factory, BIZ_A, "worker-1")
    first = _complete(
        factory, BIZ_A, job_id, "worker-1", claim.fence_token, now=NOW, result_ref=RESULT_REF
    )
    assert first.committed is True
    second = _complete(
        factory,
        BIZ_A,
        job_id,
        "worker-1",
        claim.fence_token,
        now=NOW + timedelta(seconds=1),
        result_ref="dca-result://synthetic/other",
    )
    assert second.committed is False and second.outcome is JobCompletionOutcome.STALE_LEASE
    row = _row(factory, job_id)
    assert row.result_ref == RESULT_REF  # the first committed result is intact


def test_empty_worker_id_is_rejected(factory):
    job_id = _enqueue(factory, BIZ_A, "job-noworker")
    claim = _claim(factory, BIZ_A, "worker-1")
    with pytest.raises(ValueError):
        _complete(factory, BIZ_A, job_id, "", claim.fence_token, now=NOW)


# --------------------------------------------------------------------------------------------------
# The same contract — and the commit-vs-reclaim race — on a disposable PostgreSQL.
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
def test_current_holder_commits_postgres(pg_factory):
    """Happy-path commit against real Postgres with the real result columns + FOR UPDATE lock."""
    job_id = _enqueue(pg_factory, BIZ_A, "job-ok-pg")
    claim = _claim(pg_factory, BIZ_A, "worker-1")
    result = _complete(
        pg_factory,
        BIZ_A,
        job_id,
        "worker-1",
        claim.fence_token,
        now=NOW + timedelta(seconds=5),
        result_ref=RESULT_REF,
    )
    assert result.committed is True
    row = _row(pg_factory, job_id)
    assert row.status == JobRequestStatus.SUCCEEDED.value
    assert row.lease_owner is None and row.result_ref == RESULT_REF


@requires_pg
def test_reclaimed_then_stale_commit_refused_postgres(pg_factory):
    """ACCEPTANCE on real Postgres: after a genuine reclaim (committed by worker-2), worker-1's stale
    commit is refused by the fence and nothing changes — the stale worker cannot stomp worker-2's claim."""
    job_id = _enqueue(pg_factory, BIZ_A, "job-reclaimed-pg")
    first = _claim(pg_factory, BIZ_A, "worker-1", lease_seconds=60)
    assert first is not None and first.fence_token == 1

    later = NOW + timedelta(seconds=120)
    second = _claim(pg_factory, BIZ_A, "worker-2", now=later, lease_seconds=60)
    assert second is not None and second.fence_token == 2

    result = _complete(
        pg_factory,
        BIZ_A,
        job_id,
        "worker-1",
        first.fence_token,
        now=later + timedelta(seconds=1),
        result_ref=RESULT_REF,
    )
    assert result.committed is False and result.outcome is JobCompletionOutcome.STALE_LEASE
    row = _row(pg_factory, job_id)
    assert row.status == JobRequestStatus.RUNNING.value
    assert row.lease_owner == "worker-2" and row.lease_version == 2
    assert row.result_ref is None


@requires_pg
def test_source_rights_changed_refuses_commit_postgres(pg_factory):
    """ACCEPTANCE on real Postgres: a revoked authorization revision refuses the commit, row unchanged."""
    version = _seed_in_force_approval(pg_factory)
    job_id = _enqueue(pg_factory, BIZ_A, "job-rights-pg")
    claim = _claim(pg_factory, BIZ_A, "worker-1", lease_seconds=600)
    _revoke_approval(pg_factory, version=version)
    result = _complete(
        pg_factory,
        BIZ_A,
        job_id,
        "worker-1",
        claim.fence_token,
        now=NOW + timedelta(seconds=30),
        result_ref=RESULT_REF,
        authorizations=[SourceAuthorizationRef(SOURCE_KEY, version)],
    )
    assert (
        result.committed is False and result.outcome is JobCompletionOutcome.SOURCE_RIGHTS_CHANGED
    )
    row = _row(pg_factory, job_id)
    assert row.status == JobRequestStatus.RUNNING.value and row.result_ref is None
