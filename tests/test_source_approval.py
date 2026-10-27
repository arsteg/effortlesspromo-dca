"""DCA-046 — approve a source configuration (platform-operator review of source terms).

Service unit tests (in-memory SQLite) pin the store logic and the acceptance behaviour:

- a platform operator's COMPLETE review (evidence + reviewer + review date + expiry) approves a
  recorded terms version, recording all four essentials and promoting the version ``draft`` → ``active``;
- approval CANNOT be performed by a tenant administrator (business roles yield no platform scope) — it
  is refused before any state is read or written, leaving the version a draft and no approval row;
- INCOMPLETE reviews (missing reviewer / evidence / review date / expiry, an incoherent expiry, an
  unknown terms version, a double approval) are each refused before anything is staged;
- an approval cannot EXCEED the documented field/use rights (DCA-045): approving a right the version
  does not document is refused.

``source_term_approvals`` is platform control-plane data (not tenant-scoped), so the behaviour is
app-level; the transaction/migration paths for the table are covered by ``tests/test_migrations.py``
(disposable Postgres) and its exclusion from RLS by ``tests/test_rls.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.db.base import Base, make_engine, make_session_factory
from app.db.models import SourceTermsApproval, SourceTermsStatus
from app.services.source_approval import (
    REASON_ALREADY_APPROVED,
    REASON_APPROVAL_EXCEEDS_RIGHTS,
    REASON_INVALID_EXPIRY,
    REASON_MISSING_EVIDENCE,
    REASON_MISSING_EXPIRY,
    REASON_MISSING_REVIEW_DATE,
    REASON_MISSING_REVIEWER,
    REASON_NOT_PLATFORM_OPERATOR,
    REASON_TERMS_NOT_FOUND,
    ReviewerPrincipal,
    SourceApprovalAuthorizationError,
    SourceApprovalError,
    SourceApprovalReview,
    SourceApprovalStore,
)
from app.services.source_catalog import SourceCatalogStore, SourceDraftInput
from app.services.source_terms import SourceTermsInput, SourceTermsStore

SOURCE_KEY = "mca_master_data"
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)

# A platform operator (the only principal allowed to approve) vs a tenant administrator (business
# roles: owner/admin — they hold business scopes only, never the platform approval scope).
OPERATOR = ReviewerPrincipal(subject="op-001", roles=("platform_operator",))
TENANT_ADMIN = ReviewerPrincipal(subject="user-owner-A", roles=("owner", "admin"))


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def _seed_terms(session) -> None:
    """Catalogue a source (DCA-044) and record a terms version (DCA-045) to approve."""
    SourceCatalogStore(session).create_draft(
        SourceDraftInput(
            key=SOURCE_KEY,
            kind="official",
            access_method="bulk_download",
            india_coverage={"nationwide": True},
        )
    )
    SourceTermsStore(session).add_version(
        SourceTermsInput(
            source_key=SOURCE_KEY,
            operations={"collect": True, "store": True, "display": True, "export": True},
            field_rights={"email": {"display": True, "export": False}},
            purposes=["prospecting"],
            contract_evidence={"reference": "MCA-TOS-2026", "kind": "terms_of_use"},
        )
    )


def _review(**overrides) -> SourceApprovalReview:
    data = {
        "reviewer_id": "op-001",
        "evidence": {"reference": "RIGHTS-REVIEW-2026-11", "kind": "legal_review"},
        "reviewed_at": NOW,
        "expires_at": NOW + timedelta(days=365),
    }
    data.update(overrides)
    return SourceApprovalReview(**data)


# -- a complete operator review approves and promotes ---------------------------------------------


def test_operator_complete_review_approves_and_promotes(session_factory):
    with session_factory() as session:
        _seed_terms(session)
        session.commit()

    with session_factory() as session:
        store = SourceApprovalStore(session)
        approval = store.approve(SOURCE_KEY, 1, _review(), OPERATOR)
        session.commit()
        approval_id = approval.id

    with session_factory() as session:
        approval = session.get(SourceTermsApproval, approval_id)
        assert approval is not None
        # All four review essentials recorded.
        assert approval.reviewer_id == "op-001"
        assert approval.reviewer_type == "platform_operator"
        assert approval.review_evidence["reference"] == "RIGHTS-REVIEW-2026-11"
        assert approval.reviewed_at is not None
        assert approval.expires_at is not None
        # The endorsed version was promoted draft -> active.
        terms = SourceTermsStore(session).get_version(SOURCE_KEY, 1)
        assert terms is not None
        assert terms.status == SourceTermsStatus.ACTIVE.value
        assert SourceTermsStore.is_active(terms) is True
        # The approval is in force inside its window.
        assert SourceApprovalStore.is_in_force(approval, now=NOW) is True
        # The snapshot records exactly the documented rights (never more).
        assert approval.approved_operation_rights["export"] is True
        assert approval.approved_field_rights["email"] == {"display": True, "export": False}


# -- approval cannot be performed by a tenant administrator ---------------------------------------


def test_tenant_admin_cannot_approve_and_changes_nothing(session_factory):
    with session_factory() as session:
        _seed_terms(session)
        session.commit()

    with session_factory() as session:
        store = SourceApprovalStore(session)
        with pytest.raises(SourceApprovalAuthorizationError) as exc:
            store.approve(SOURCE_KEY, 1, _review(reviewer_id="user-owner-A"), TENANT_ADMIN)
        assert exc.value.code == REASON_NOT_PLATFORM_OPERATOR
        session.commit()

    with session_factory() as session:
        # Nothing staged, nothing promoted: the version is still a draft and there is no approval.
        terms = SourceTermsStore(session).get_version(SOURCE_KEY, 1)
        assert terms is not None and terms.status == SourceTermsStatus.DRAFT.value
        assert SourceApprovalStore(session).approval_for(SOURCE_KEY, 1) is None
        assert session.query(SourceTermsApproval).count() == 0


def test_non_member_principal_without_roles_cannot_approve(session_factory):
    with session_factory() as session:
        _seed_terms(session)
        session.commit()

    with session_factory() as session:
        store = SourceApprovalStore(session)
        with pytest.raises(SourceApprovalAuthorizationError):
            store.approve(SOURCE_KEY, 1, _review(), ReviewerPrincipal(subject="nobody"))


# -- incomplete reviews are rejected before anything is staged ------------------------------------


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"reviewer_id": "   "}, REASON_MISSING_REVIEWER),
        ({"evidence": {}}, REASON_MISSING_EVIDENCE),
        ({"evidence": {"kind": "legal_review"}}, REASON_MISSING_EVIDENCE),  # no reference
        ({"reviewed_at": None}, REASON_MISSING_REVIEW_DATE),
        ({"expires_at": None}, REASON_MISSING_EXPIRY),
        ({"expires_at": NOW}, REASON_INVALID_EXPIRY),  # not after the review date
        ({"expires_at": NOW - timedelta(days=1)}, REASON_INVALID_EXPIRY),
    ],
)
def test_incomplete_reviews_are_refused_and_stage_nothing(session_factory, overrides, code):
    with session_factory() as session:
        _seed_terms(session)
        session.commit()

    with session_factory() as session:
        store = SourceApprovalStore(session)
        with pytest.raises(SourceApprovalError) as exc:
            store.approve(SOURCE_KEY, 1, _review(**overrides), OPERATOR)
        assert exc.value.code == code
        session.commit()

    with session_factory() as session:
        # The refusal left no approval and did not promote the version.
        terms = SourceTermsStore(session).get_version(SOURCE_KEY, 1)
        assert terms is not None and terms.status == SourceTermsStatus.DRAFT.value
        assert session.query(SourceTermsApproval).count() == 0


def test_unknown_terms_version_is_refused(session_factory):
    with session_factory() as session:
        _seed_terms(session)
        session.commit()

    with session_factory() as session:
        store = SourceApprovalStore(session)
        with pytest.raises(SourceApprovalError) as exc:
            store.approve(SOURCE_KEY, 99, _review(), OPERATOR)  # no such version
        assert exc.value.code == REASON_TERMS_NOT_FOUND
        session.commit()

    with session_factory() as session:
        assert session.query(SourceTermsApproval).count() == 0


# -- an approval cannot exceed the documented field/use rights ------------------------------------


def test_approval_cannot_exceed_documented_operation_rights(session_factory):
    with session_factory() as session:
        _seed_terms(session)  # 'share' is NOT documented as granted (deny-by-default)
        session.commit()

    with session_factory() as session:
        store = SourceApprovalStore(session)
        with pytest.raises(SourceApprovalError) as exc:
            store.approve(
                SOURCE_KEY,
                1,
                _review(
                    requested_operation_rights={"share": True}
                ),  # exceeds the documented rights
                OPERATOR,
            )
        assert exc.value.code == REASON_APPROVAL_EXCEEDS_RIGHTS
        session.commit()

    with session_factory() as session:
        terms = SourceTermsStore(session).get_version(SOURCE_KEY, 1)
        assert terms is not None and terms.status == SourceTermsStatus.DRAFT.value
        assert session.query(SourceTermsApproval).count() == 0


def test_approval_cannot_exceed_documented_field_rights(session_factory):
    with session_factory() as session:
        _seed_terms(session)  # 'email' is display-only: export is denied for that field
        session.commit()

    with session_factory() as session:
        store = SourceApprovalStore(session)
        with pytest.raises(SourceApprovalError) as exc:
            store.approve(
                SOURCE_KEY,
                1,
                _review(requested_field_rights={"email": {"export": True}}),  # version denies this
                OPERATOR,
            )
        assert exc.value.code == REASON_APPROVAL_EXCEEDS_RIGHTS


def test_approval_within_documented_field_rights_is_allowed(session_factory):
    with session_factory() as session:
        _seed_terms(session)
        session.commit()

    with session_factory() as session:
        store = SourceApprovalStore(session)
        # 'email' display IS documented; a field that falls back to the source-wide 'display' default
        # is within rights too. Both are a subset of the documented rights, so the approval succeeds.
        approval = store.approve(
            SOURCE_KEY,
            1,
            _review(
                requested_field_rights={"email": {"display": True}, "phone": {"display": True}}
            ),
            OPERATOR,
        )
        session.commit()
        assert approval.status == "active"


# -- a version is approved at most once -----------------------------------------------------------


def test_double_approval_is_refused(session_factory):
    with session_factory() as session:
        _seed_terms(session)
        SourceApprovalStore(session).approve(SOURCE_KEY, 1, _review(), OPERATOR)
        session.commit()

    with session_factory() as session:
        store = SourceApprovalStore(session)
        with pytest.raises(SourceApprovalError) as exc:
            store.approve(SOURCE_KEY, 1, _review(), OPERATOR)
        assert exc.value.code == REASON_ALREADY_APPROVED
        session.commit()

    with session_factory() as session:
        # Exactly one approval exists for the version.
        assert len(SourceApprovalStore(session).approvals(SOURCE_KEY)) == 1


# -- an expired approval is not in force ----------------------------------------------------------


def test_expired_approval_is_not_in_force(session_factory):
    with session_factory() as session:
        _seed_terms(session)
        # Reviewed in the past, took effect then, and has since lapsed — a coherent, now-expired window.
        approval = SourceApprovalStore(session).approve(
            SOURCE_KEY,
            1,
            _review(
                reviewed_at=NOW - timedelta(days=30),
                effective_from=NOW - timedelta(days=30),
                expires_at=NOW - timedelta(days=1),
            ),
            OPERATOR,
        )
        # Before expiry the approval is in force; after expiry it endorses nothing (explicit window).
        assert SourceApprovalStore.is_in_force(approval, now=NOW - timedelta(days=10)) is True
        assert SourceApprovalStore.is_in_force(approval, now=NOW) is False
