"""DCA-050 — revoke a source version (operator revocation + queued invalidation).

Service unit tests (in-memory SQLite) pin the acceptance behaviour:

- **Revoked rights cannot be used by an already-running task with an old source version.** A task
  authorizes at the start (ALLOW) and captures the in-force source version; an operator then revokes
  that version; when the task re-checks before using/committing what it fetched — at the SAME ``now``,
  so nothing is time-driven — the right is gone (the preflight denies and the version-specific
  ``is_in_force`` check is ``False``). No sweep has to run: the revocation flips the status and
  ``is_in_force`` keys on it immediately.
- **The revocation is a durable, attributed record.** The approval moves ACTIVE → REVOKED (stamping
  ``revoked_by`` / ``revoked_at`` / ``revocation_reason``) and the terms version it promoted is demoted
  ACTIVE → REVOKED.
- **Queued invalidation.** A scheduled sweep flips the still-``current`` score of observations acquired
  from a now-revoked (no-longer-in-force) source to ``stale`` — queued for a future, disabled re-review.
  Idempotent; it never touches a source still in force.
- **Failure / negative cases.** A tenant administrator cannot revoke (refused before any state is read,
  the approval stays ACTIVE and usable); revoking an unknown approval, with no reason, or twice is
  refused with a stable code and stages nothing.

The platform ceiling, the entitlement narrowing and the preflight themselves are covered by
``tests/test_source_approval.py`` / ``test_source_entitlement.py`` / ``test_source_preflight.py``;
this file pins the revocation that sits across them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.db.base import Base, make_engine, make_session_factory
from app.db.models import (
    DiscoveryRun,
    ProspectCandidate,
    ScoreStatus,
    Source,
    SourceApprovalStatus,
    SourceStatus,
    SourceTerms,
    SourceTermsApproval,
    SourceTermsStatus,
)
from app.services.source_approval import (
    ReviewerPrincipal,
    SourceApprovalReview,
    SourceApprovalStore,
)
from app.services.source_catalog import SourceCatalogStore, SourceDraftInput
from app.services.source_entitlement import (
    BusinessEntitler,
    BusinessSourceEntitlementInput,
    BusinessSourceEntitlementStore,
)
from app.services.source_preflight import (
    REASON_SOURCE_NOT_PLATFORM_APPROVED,
    PreflightRequest,
    SourcePreflight,
)
from app.services.source_revocation import (
    REASON_ALREADY_REVOKED,
    REASON_APPROVAL_NOT_FOUND,
    REASON_MISSING_REASON,
    REASON_NOT_PLATFORM_OPERATOR,
    RevocationInvalidationSweep,
    RevokerPrincipal,
    SourceRevocationAuthorizationError,
    SourceRevocationError,
    SourceRevocationStore,
    run_revocation_invalidation,
)
from app.services.source_terms import SourceTermsInput, SourceTermsStore

SOURCE_KEY = "mca_master_data"
BIZ_A = "11111111-1111-1111-1111-111111111111"
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
EXPIRES_AT = NOW + timedelta(days=365)

ADMIN = BusinessEntitler(subject="00000000-0000-4000-9000-0000000a0050", roles=("owner", "admin"))
OPERATOR = ReviewerPrincipal(subject="op-001", roles=("platform_operator",))
REVOKER = RevokerPrincipal(subject="op-001", roles=("platform_operator",))
# A business administrator holds only business-plane roles — never the platform revoke scope.
TENANT_ADMIN = RevokerPrincipal(subject="tenant-admin-1", roles=("owner", "admin"))


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def _seed_approved_enabled_entitled(session, *, key: str = SOURCE_KEY) -> None:
    """Catalogue + record terms + APPROVE + (test-only) ENABLE + entitle A, so the DCA-048 preflight
    ALLOWS. Mirrors the DCA-048/049 seed; the approval window is wide so only a revocation can pull it."""
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
            reviewed_at=NOW - timedelta(days=1),
            expires_at=EXPIRES_AT,
        ),
        OPERATOR,
    )
    source = session.query(Source).filter(Source.key == key).one()
    source.status = SourceStatus.ENABLED.value
    source.enabled = True
    BusinessSourceEntitlementStore(session, BIZ_A).set_entitlement(
        BusinessSourceEntitlementInput(
            source_key=key,
            enabled=True,
            operations={"collect": True, "display": True},
            purposes=["prospecting"],
        ),
        ADMIN,
        now=NOW,
    )


def _seed_candidate(
    session,
    *,
    source_key: str = SOURCE_KEY,
    business_id: str = BIZ_A,
    org_identifier: str = "CIN-SYNTH-1",
) -> str:
    """One prospect observation (recommendation) acquired from ``source_key``, score ``current``."""
    run = DiscoveryRun(business_id=business_id, source_keys=[source_key], status="completed")
    session.add(run)
    session.flush()
    candidate = ProspectCandidate(
        business_id=business_id,
        run_id=run.id,
        org_name="Synthetic Textiles Pvt Ltd",
        org_identifier=org_identifier,
        contact={},
        source_key=source_key,
        source_ref="synthetic://record/1",
        trust_tier="official_registry",
        match_score=1.0,
        rationale="matches ICP",
    )
    session.add(candidate)
    session.flush()
    return candidate.id


def _preflight() -> PreflightRequest:
    return PreflightRequest(source_key=SOURCE_KEY, operation="collect", purpose="prospecting")


# -- the acceptance criterion: a running task with the old version is denied after revocation --------


def test_running_task_with_old_source_version_is_denied_after_revocation(session_factory):
    """A task authorizes (ALLOW) and captures the in-force version; an operator revokes it; the task's
    re-check before committing — at the SAME ``now`` — is DENIED. Nothing is time-driven: the revocation
    pulled the right, and ``is_in_force`` keys on the status, so no sweep is required."""
    with session_factory() as session:
        _seed_approved_enabled_entitled(session)
        session.commit()

    # 1) The task starts: it authorizes and captures the in-force source (terms) version — "the old
    #    source version" it will operate against.
    with session_factory() as session:
        gate = SourcePreflight(session, BIZ_A)
        start = gate.authorize(_preflight(), now=NOW)
        captured_version = SourceApprovalStore(session).approvals(SOURCE_KEY)[0].terms_version
    assert start.allowed is True
    assert captured_version == 1

    # 2) While the task is "paused" (mid-fetch), a platform operator revokes that version.
    with session_factory() as session:
        result = SourceRevocationStore(session).revoke(
            SOURCE_KEY, captured_version, REVOKER, reason="rights_withdrawn_by_licensor", now=NOW
        )
        session.commit()
    assert result.demoted_terms is True

    # 3) The task resumes and re-checks BEFORE using/committing — at the SAME clock. Both the preflight
    #    and the version-specific in-force check deny: the old source version can no longer be used.
    with session_factory() as session:
        gate = SourcePreflight(session, BIZ_A)
        resume = gate.authorize(_preflight(), now=NOW)
        approval = SourceApprovalStore(session).approval_for(SOURCE_KEY, captured_version)
        version_in_force = SourceApprovalStore.is_in_force(approval, now=NOW)

    assert (resume.allowed, resume.reason) == (False, REASON_SOURCE_NOT_PLATFORM_APPROVED)
    assert version_in_force is False


def test_revocation_is_a_durable_attributed_record(session_factory):
    with session_factory() as session:
        _seed_approved_enabled_entitled(session)
        session.commit()

    with session_factory() as session:
        SourceRevocationStore(session).revoke(
            SOURCE_KEY, 1, REVOKER, reason="licensor_terminated", now=NOW
        )
        session.commit()

    with session_factory() as session:
        approval = (
            session.query(SourceTermsApproval)
            .filter(SourceTermsApproval.source_key == SOURCE_KEY)
            .one()
        )
        terms = session.query(SourceTerms).filter(SourceTerms.source_key == SOURCE_KEY).one()

    assert approval.status == SourceApprovalStatus.REVOKED.value
    assert approval.revoked_by == "op-001"
    assert approval.revocation_reason == "licensor_terminated"
    assert approval.revoked_at is not None
    assert terms.status == SourceTermsStatus.REVOKED.value


# -- authorization + negative cases (each refuses before staging) ------------------------------------


def test_tenant_administrator_cannot_revoke_and_changes_nothing(session_factory):
    with session_factory() as session:
        _seed_approved_enabled_entitled(session)
        session.commit()

    with session_factory() as session:
        with pytest.raises(SourceRevocationAuthorizationError) as exc:
            SourceRevocationStore(session).revoke(
                SOURCE_KEY, 1, TENANT_ADMIN, reason="trying_to_revoke", now=NOW
            )
        assert exc.value.code == REASON_NOT_PLATFORM_OPERATOR
        session.rollback()

    # The approval is untouched and the preflight still ALLOWS — the refusal staged nothing.
    with session_factory() as session:
        approval = (
            session.query(SourceTermsApproval)
            .filter(SourceTermsApproval.source_key == SOURCE_KEY)
            .one()
        )
        assert approval.status == SourceApprovalStatus.ACTIVE.value
        assert approval.revoked_at is None
        decision = SourcePreflight(session, BIZ_A).authorize(_preflight(), now=NOW)
        assert decision.allowed is True


def test_revoking_an_unknown_approval_is_refused(session_factory):
    with session_factory() as session:
        with pytest.raises(SourceRevocationError) as exc:
            SourceRevocationStore(session).revoke("no_such_source", 1, REVOKER, reason="x", now=NOW)
        assert exc.value.code == REASON_APPROVAL_NOT_FOUND


def test_revocation_requires_a_reason(session_factory):
    with session_factory() as session:
        _seed_approved_enabled_entitled(session)
        session.commit()
    with session_factory() as session:
        with pytest.raises(SourceRevocationError) as exc:
            SourceRevocationStore(session).revoke(SOURCE_KEY, 1, REVOKER, reason="   ", now=NOW)
        assert exc.value.code == REASON_MISSING_REASON
        session.rollback()
    # Still ACTIVE — the refusal changed nothing.
    with session_factory() as session:
        approval = (
            session.query(SourceTermsApproval)
            .filter(SourceTermsApproval.source_key == SOURCE_KEY)
            .one()
        )
        assert approval.status == SourceApprovalStatus.ACTIVE.value


def test_revoking_twice_is_refused_idempotently(session_factory):
    with session_factory() as session:
        _seed_approved_enabled_entitled(session)
        session.commit()
    with session_factory() as session:
        SourceRevocationStore(session).revoke(SOURCE_KEY, 1, REVOKER, reason="first", now=NOW)
        session.commit()
    with session_factory() as session:
        with pytest.raises(SourceRevocationError) as exc:
            SourceRevocationStore(session).revoke(SOURCE_KEY, 1, REVOKER, reason="second", now=NOW)
        assert exc.value.code == REASON_ALREADY_REVOKED
        session.rollback()
    # The original revocation reason is preserved — the second attempt overwrote nothing.
    with session_factory() as session:
        approval = (
            session.query(SourceTermsApproval)
            .filter(SourceTermsApproval.source_key == SOURCE_KEY)
            .one()
        )
        assert approval.revocation_reason == "first"


# -- queued invalidation of affected observations/recommendations ------------------------------------


def test_invalidation_sweep_marks_observations_from_a_revoked_source_stale(session_factory):
    with session_factory() as session:
        _seed_approved_enabled_entitled(session)
        candidate_id = _seed_candidate(session)
        session.commit()

    # Before revocation the sweep finds nothing (the source is still in force).
    with session_factory() as session:
        pre = RevocationInvalidationSweep(session).sweep(now=NOW)
        session.commit()
    assert pre.invalidated == 0

    # Revoke, then the sweep flips the affected observation current -> stale.
    with session_factory() as session:
        SourceRevocationStore(session).revoke(SOURCE_KEY, 1, REVOKER, reason="pulled", now=NOW)
        session.commit()

    with session_factory() as session:
        first = RevocationInvalidationSweep(session).sweep(now=NOW)
        session.commit()
    with session_factory() as session:
        second = RevocationInvalidationSweep(session).sweep(now=NOW)
        session.commit()

    assert first.revoked_source_keys == [SOURCE_KEY]
    assert first.invalidated_candidate_ids == [candidate_id]
    assert second.invalidated == 0  # idempotent — the candidate is already stale

    with session_factory() as session:
        candidate = session.get(ProspectCandidate, candidate_id)
        assert candidate.score_status == ScoreStatus.STALE.value
        assert candidate.status == "suggested"  # score-only: approval/acceptance untouched


def test_invalidation_leaves_observations_from_a_still_in_force_source_untouched(session_factory):
    """A candidate from a DIFFERENT, non-revoked source is not affected by a revocation elsewhere."""
    other_key = "another_registry"
    with session_factory() as session:
        _seed_approved_enabled_entitled(session)
        _seed_approved_enabled_entitled(session, key=other_key)
        revoked_cand = _seed_candidate(session)
        safe_cand = _seed_candidate(session, source_key=other_key, org_identifier="CIN-SYNTH-2")
        session.commit()

    with session_factory() as session:
        SourceRevocationStore(session).revoke(SOURCE_KEY, 1, REVOKER, reason="pulled", now=NOW)
        session.commit()

    result = run_revocation_invalidation(session_factory, now=NOW)
    assert result.invalidated_candidate_ids == [revoked_cand]

    with session_factory() as session:
        assert session.get(ProspectCandidate, revoked_cand).score_status == ScoreStatus.STALE.value
        assert session.get(ProspectCandidate, safe_cand).score_status == ScoreStatus.CURRENT.value


def test_worker_entrypoint_commits_the_invalidation(session_factory):
    with session_factory() as session:
        _seed_approved_enabled_entitled(session)
        candidate_id = _seed_candidate(session)
        SourceRevocationStore(session).revoke(SOURCE_KEY, 1, REVOKER, reason="pulled", now=NOW)
        session.commit()

    result = run_revocation_invalidation(session_factory, now=NOW)
    assert result.invalidated == 1

    with session_factory() as session:
        assert session.get(ProspectCandidate, candidate_id).score_status == ScoreStatus.STALE.value
