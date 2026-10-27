"""DCA-053 — approve a policy version (platform-operator review of a compliance policy rulepack).

Service unit tests (in-memory SQLite) pin the store logic and the acceptance behaviour:

- a platform operator's COMPLETE review (references + reviewer + review date + review expiry) approves
  a recorded rulepack version, recording all four essentials and a content fingerprint, and promoting
  the version ``draft`` → ``active``;
- a DRAFT (unapproved) version and an EXPIRED approval never authorize production behavior
  (``evaluate`` denies with a stable reason) — only an approved, in-force version is in force;
- changing approved content requires a NEW version: the DCA-052 store is insert-only, so a modified
  pack is a fresh DRAFT (unapproved), and the prior approval's fingerprint still binds the exact rules
  it reviewed;
- approval CANNOT be performed by a tenant administrator (business roles yield no platform scope) — it
  is refused before any state is read or written, leaving the version a draft and no approval row;
- INCOMPLETE reviews (missing reviewer / references / review date / expiry, an incoherent expiry, an
  unknown version, a double approval) are each refused before anything is staged.

``compliance_policy_approvals`` is platform control-plane data (not tenant-scoped), so the behaviour is
app-level; the transaction/migration paths for the table are covered by ``tests/test_migrations.py``
(disposable Postgres) and its exclusion from RLS by ``tests/test_rls.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.compliance.policy_rulepacks import (
    CompliancePolicyRulepackStore,
    PolicyRuleInput,
    RulepackInput,
)
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import CompliancePolicyApproval, PolicyRulepackStatus
from app.services.policy_approval import (
    REASON_ALREADY_APPROVED,
    REASON_EXPIRED,
    REASON_INVALID_EXPIRY,
    REASON_MISSING_EXPIRY,
    REASON_MISSING_REFERENCES,
    REASON_MISSING_REVIEW_DATE,
    REASON_MISSING_REVIEWER,
    REASON_NOT_APPROVED,
    REASON_NOT_PLATFORM_OPERATOR,
    REASON_VERSION_NOT_FOUND,
    PolicyApprovalAuthorizationError,
    PolicyApprovalError,
    PolicyApprovalReview,
    PolicyApprovalStore,
    PolicyReviewerPrincipal,
    content_fingerprint,
)

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)

# A platform operator (the only principal allowed to approve) vs a tenant administrator (business
# roles: owner/admin — they hold business scopes only, never the platform approval scope).
OPERATOR = PolicyReviewerPrincipal(subject="op-001", roles=("platform_operator",))
TENANT_ADMIN = PolicyReviewerPrincipal(subject="user-owner-A", roles=("owner", "admin"))


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def _evidence(ref: str = "DPDP-2023-s7") -> dict:
    return {"reference": ref, "kind": "statute", "uri": "https://example.test/law"}


def _seed_rulepack(session, **overrides) -> None:
    """Record a DCA-052 India policy rulepack DRAFT version to approve."""
    data = {
        "jurisdiction": "IN",
        "title": "India baseline policy",
        "evidence": _evidence("POLICY-REVIEW-2026-11"),
        "rules": [
            PolicyRuleInput(
                effect="allow",
                action="contact",
                channel="email",
                authority="law",
                conditions={"consent": "required"},
                evidence=_evidence(),
            ),
        ],
    }
    data.update(overrides)
    CompliancePolicyRulepackStore(session).add_version(RulepackInput(**data))


def _review(**overrides) -> PolicyApprovalReview:
    data = {
        "reviewer_id": "op-001",
        "references": {"reference": "LEGAL-REVIEW-2026-11", "kind": "legal_review"},
        "reviewed_at": NOW,
        "expires_at": NOW + timedelta(days=365),
    }
    data.update(overrides)
    return PolicyApprovalReview(**data)


# -- a complete operator review approves and promotes ---------------------------------------------


def test_operator_complete_review_approves_and_promotes(session_factory):
    with session_factory() as session:
        _seed_rulepack(session)
        session.commit()

    with session_factory() as session:
        store = PolicyApprovalStore(session)
        approval = store.approve(1, _review(), OPERATOR)
        session.commit()
        approval_id = approval.id

    with session_factory() as session:
        approval = session.get(CompliancePolicyApproval, approval_id)
        assert approval is not None
        # All four review essentials recorded.
        assert approval.reviewer_id == "op-001"
        assert approval.reviewer_type == "platform_operator"
        assert approval.review_references["reference"] == "LEGAL-REVIEW-2026-11"
        assert approval.reviewed_at is not None
        assert approval.expires_at is not None
        assert approval.content_fingerprint  # bound to the exact rules reviewed
        # The endorsed version was promoted draft -> active.
        pack = CompliancePolicyRulepackStore(session).get_version(1)
        assert pack is not None
        assert pack.status == PolicyRulepackStatus.ACTIVE.value
        # The approval is in force inside its window, and the version evaluates as in force.
        assert PolicyApprovalStore.is_in_force(approval, now=NOW) is True
        decision = PolicyApprovalStore(session).evaluate(1, now=NOW)
        assert decision.in_force is True
        assert decision.reason is None
        # The fingerprint matches the pack's rules (immutable binding).
        assert approval.content_fingerprint == content_fingerprint(pack.rules)


# -- draft / expired packs never authorize production behavior ------------------------------------


def test_unapproved_draft_version_is_not_in_force(session_factory):
    with session_factory() as session:
        _seed_rulepack(session)
        session.commit()

    with session_factory() as session:
        decision = PolicyApprovalStore(session).evaluate(1, now=NOW)
        assert decision.in_force is False
        assert decision.reason == REASON_NOT_APPROVED  # a draft never authorizes


def test_expired_approval_is_not_in_force(session_factory):
    with session_factory() as session:
        _seed_rulepack(session)
        # Reviewed in the past, effective then, and since lapsed — a coherent, now-expired window.
        approval = PolicyApprovalStore(session).approve(
            1,
            _review(
                reviewed_at=NOW - timedelta(days=30),
                effective_from=NOW - timedelta(days=30),
                expires_at=NOW - timedelta(days=1),
            ),
            OPERATOR,
        )
        session.commit()
        # Before expiry: in force; after expiry: endorses nothing (explicit window).
        store = PolicyApprovalStore(session)
        assert store.is_in_force(approval, now=NOW - timedelta(days=10)) is True
        assert store.is_in_force(approval, now=NOW) is False
        assert store.evaluate(1, now=NOW - timedelta(days=10)).in_force is True
        expired = store.evaluate(1, now=NOW)
        assert expired.in_force is False
        assert expired.reason == REASON_EXPIRED


# -- changing approved content requires a new version ---------------------------------------------


def test_modifying_approved_content_is_a_new_unapproved_version(session_factory):
    with session_factory() as session:
        _seed_rulepack(session)  # version 1
        PolicyApprovalStore(session).approve(1, _review(), OPERATOR)
        session.commit()

    # Record a MODIFIED pack: an edit is a NEW version, not an in-place change.
    with session_factory() as session:
        _seed_rulepack(
            session,
            title="India baseline policy v2",
            rules=[
                PolicyRuleInput(
                    effect="deny",  # changed effect — different content
                    action="contact",
                    channel="email",
                    authority="law",
                    conditions={"consent": "required"},
                    evidence=_evidence(),
                ),
            ],
        )
        session.commit()

    with session_factory() as session:
        store = PolicyApprovalStore(session)
        # v1 is still approved and in force; v2 is a fresh DRAFT and authorizes nothing.
        assert store.evaluate(1, now=NOW).in_force is True
        v2 = store.evaluate(2, now=NOW)
        assert v2.in_force is False
        assert v2.reason == REASON_NOT_APPROVED
        rulepacks = CompliancePolicyRulepackStore(session)
        assert rulepacks.get_version(1).status == PolicyRulepackStatus.ACTIVE.value
        assert rulepacks.get_version(2).status == PolicyRulepackStatus.DRAFT.value
        # The v1 approval's fingerprint still binds v1's content, NOT v2's.
        approval = store.approval_for(1)
        assert approval.content_fingerprint == content_fingerprint(rulepacks.get_version(1).rules)
        assert approval.content_fingerprint != content_fingerprint(rulepacks.get_version(2).rules)


# -- approval cannot be performed by a tenant administrator ---------------------------------------


def test_tenant_admin_cannot_approve_and_changes_nothing(session_factory):
    with session_factory() as session:
        _seed_rulepack(session)
        session.commit()

    with session_factory() as session:
        store = PolicyApprovalStore(session)
        with pytest.raises(PolicyApprovalAuthorizationError) as exc:
            store.approve(1, _review(reviewer_id="user-owner-A"), TENANT_ADMIN)
        assert exc.value.code == REASON_NOT_PLATFORM_OPERATOR
        session.commit()

    with session_factory() as session:
        # Nothing staged, nothing promoted: the version is still a draft and there is no approval.
        pack = CompliancePolicyRulepackStore(session).get_version(1)
        assert pack is not None and pack.status == PolicyRulepackStatus.DRAFT.value
        assert PolicyApprovalStore(session).approval_for(1) is None
        assert session.query(CompliancePolicyApproval).count() == 0


def test_non_operator_principal_without_roles_cannot_approve(session_factory):
    with session_factory() as session:
        _seed_rulepack(session)
        session.commit()

    with session_factory() as session:
        store = PolicyApprovalStore(session)
        with pytest.raises(PolicyApprovalAuthorizationError):
            store.approve(1, _review(), PolicyReviewerPrincipal(subject="nobody"))


# -- incomplete reviews are rejected before anything is staged ------------------------------------


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"reviewer_id": "   "}, REASON_MISSING_REVIEWER),
        ({"references": {}}, REASON_MISSING_REFERENCES),
        ({"references": {"kind": "legal_review"}}, REASON_MISSING_REFERENCES),  # no reference
        ({"reviewed_at": None}, REASON_MISSING_REVIEW_DATE),
        ({"expires_at": None}, REASON_MISSING_EXPIRY),
        ({"expires_at": NOW}, REASON_INVALID_EXPIRY),  # not after the review date
        ({"expires_at": NOW - timedelta(days=1)}, REASON_INVALID_EXPIRY),
    ],
)
def test_incomplete_reviews_are_refused_and_stage_nothing(session_factory, overrides, code):
    with session_factory() as session:
        _seed_rulepack(session)
        session.commit()

    with session_factory() as session:
        store = PolicyApprovalStore(session)
        with pytest.raises(PolicyApprovalError) as exc:
            store.approve(1, _review(**overrides), OPERATOR)
        assert exc.value.code == code
        session.commit()

    with session_factory() as session:
        pack = CompliancePolicyRulepackStore(session).get_version(1)
        assert pack is not None and pack.status == PolicyRulepackStatus.DRAFT.value
        assert session.query(CompliancePolicyApproval).count() == 0


def test_unknown_version_is_refused(session_factory):
    with session_factory() as session:
        _seed_rulepack(session)
        session.commit()

    with session_factory() as session:
        store = PolicyApprovalStore(session)
        with pytest.raises(PolicyApprovalError) as exc:
            store.approve(99, _review(), OPERATOR)  # no such version
        assert exc.value.code == REASON_VERSION_NOT_FOUND
        session.commit()

    with session_factory() as session:
        assert session.query(CompliancePolicyApproval).count() == 0


def test_double_approval_is_refused(session_factory):
    with session_factory() as session:
        _seed_rulepack(session)
        PolicyApprovalStore(session).approve(1, _review(), OPERATOR)
        session.commit()

    with session_factory() as session:
        store = PolicyApprovalStore(session)
        with pytest.raises(PolicyApprovalError) as exc:
            store.approve(1, _review(), OPERATOR)
        assert exc.value.code == REASON_ALREADY_APPROVED
        session.commit()

    with session_factory() as session:
        assert len(PolicyApprovalStore(session).approvals("IN")) == 1
