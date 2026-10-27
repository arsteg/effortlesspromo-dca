"""DCA-047 — restrict source access per business (narrow platform-approved rights).

Service unit tests (in-memory SQLite) pin the store logic and the acceptance behaviour:

- a business administrator can entitle a platform-approved source to a SUBSET of the approved rights
  (the narrowing succeeds, is versioned, and is audited);
- a business CANNOT enable a source with no in-force platform approval, nor grant a source-wide
  operation, a per-field right, or a purpose the platform approval does not document — each attempt to
  broaden through the business's own configuration is refused BEFORE anything is staged;
- authority is the business administrator scope: an ordinary user cannot configure an entitlement;
- entitlements are per-business (one business's entitlement never touches another's) and the narrowed
  ``permits`` read is the intersection of the platform ceiling and the business grant.

``business_source_entitlements`` is tenant-owned; the behaviour is app-level (per-tenant versioning +
narrowing + audit + authorization). The transaction/RLS/migration paths for the table are covered by
``tests/test_rls.py`` (it is pinned into the RLS set) and ``tests/test_migrations.py`` (its dual-path
migration on disposable Postgres).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.db.base import Base, make_engine, make_session_factory
from app.db.models import AuditEvent, BusinessSourceEntitlement
from app.services.source_approval import (
    ReviewerPrincipal,
    SourceApprovalReview,
    SourceApprovalStore,
)
from app.services.source_catalog import SourceCatalogStore, SourceDraftInput
from app.services.source_entitlement import (
    AUDIT_ACTION_SET_ENTITLEMENT,
    REASON_FIELD_RIGHT_DENIED_AT_PLATFORM,
    REASON_NOT_BUSINESS_ADMINISTRATOR,
    REASON_OPERATION_DENIED_AT_PLATFORM,
    REASON_OPERATION_NOT_ENTITLED,
    REASON_PURPOSE_DENIED_AT_PLATFORM,
    REASON_SOURCE_NOT_ENTITLED,
    REASON_SOURCE_NOT_PLATFORM_APPROVED,
    BusinessEntitler,
    BusinessSourceEntitlementAuthorizationError,
    BusinessSourceEntitlementError,
    BusinessSourceEntitlementInput,
    BusinessSourceEntitlementStore,
)
from app.services.source_terms import SourceTermsInput, SourceTermsStore

SOURCE_KEY = "mca_master_data"
BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)

# A business administrator (holds source.approve) vs an ordinary business user (candidate.read only).
ADMIN = BusinessEntitler(subject="00000000-0000-4000-9000-0000000a0047", roles=("owner", "admin"))
ORDINARY_USER = BusinessEntitler(subject="00000000-0000-4000-9000-0000000a0048", roles=("viewer",))

OPERATOR = ReviewerPrincipal(subject="op-001", roles=("platform_operator",))


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def _seed_approved_source(session, *, source_key: str = SOURCE_KEY) -> None:
    """Catalogue a source (DCA-044), record a terms version (DCA-045), and approve it (DCA-046) so it
    is the in-force platform ceiling. The approved rights: collect/store/display/export source-wide
    ('share' DENIED), email display-only (export denied for that field), purpose 'prospecting'.
    """
    SourceCatalogStore(session).create_draft(
        SourceDraftInput(
            key=source_key,
            kind="official",
            access_method="bulk_download",
            india_coverage={"nationwide": True},
        )
    )
    SourceTermsStore(session).add_version(
        SourceTermsInput(
            source_key=source_key,
            operations={"collect": True, "store": True, "display": True, "export": True},
            field_rights={"email": {"display": True, "export": False}},
            purposes=["prospecting"],
            contract_evidence={"reference": "MCA-TOS-2026", "kind": "terms_of_use"},
        )
    )
    SourceApprovalStore(session).approve(
        source_key,
        1,
        SourceApprovalReview(
            reviewer_id="op-001",
            evidence={"reference": "RIGHTS-REVIEW-2026-11", "kind": "legal_review"},
            reviewed_at=NOW - timedelta(days=1),
            expires_at=NOW + timedelta(days=365),
        ),
        OPERATOR,
    )


# -- a business administrator can narrow an approved source ---------------------------------------


def test_admin_narrows_approved_source_and_audits(session_factory):
    with session_factory() as session:
        _seed_approved_source(session)
        session.commit()

    with session_factory() as session:
        store = BusinessSourceEntitlementStore(session, BIZ_A)
        entitlement = store.set_entitlement(
            BusinessSourceEntitlementInput(
                source_key=SOURCE_KEY,
                enabled=True,
                # A strict SUBSET of the approved rights: display only (not export/collect/store).
                operations={"display": True, "export": False},
                field_rights={"email": {"display": True}},
                purposes=["prospecting"],
            ),
            ADMIN,
        )
        session.commit()
        ent_id = entitlement.id

    with session_factory() as session:
        entitlement = session.get(BusinessSourceEntitlement, ent_id)
        assert entitlement is not None
        assert entitlement.business_id == BIZ_A
        assert entitlement.version == 1
        assert entitlement.enabled is True
        assert entitlement.operation_rights["display"] is True
        assert entitlement.operation_rights["export"] is False
        assert entitlement.approved_terms_version == 1
        # Audited atomically with the row.
        audits = session.query(AuditEvent).filter(AuditEvent.business_id == BIZ_A).all()
        assert [a.action for a in audits] == [AUDIT_ACTION_SET_ENTITLEMENT]
        assert audits[0].references["sourceKey"] == SOURCE_KEY
        # The narrowed `permits` read is the intersection of ceiling and grant.
        store = BusinessSourceEntitlementStore(session, BIZ_A)
        assert store.permits(SOURCE_KEY, "email", "display", now=NOW) == (True, None)
        # 'export' is approved at platform but NOT entitled by the business -> denied by narrowing.
        assert store.permits(SOURCE_KEY, "name", "export", now=NOW) == (
            False,
            REASON_OPERATION_NOT_ENTITLED,
        )


def test_each_set_inserts_a_new_version_and_preserves_prior(session_factory):
    with session_factory() as session:
        _seed_approved_source(session)
        BusinessSourceEntitlementStore(session, BIZ_A).set_entitlement(
            BusinessSourceEntitlementInput(source_key=SOURCE_KEY, enabled=True), ADMIN
        )
        session.commit()
    with session_factory() as session:
        v2 = BusinessSourceEntitlementStore(session, BIZ_A).set_entitlement(
            BusinessSourceEntitlementInput(
                source_key=SOURCE_KEY, enabled=True, operations={"display": True}
            ),
            ADMIN,
        )
        session.commit()
        assert v2.version == 2
    with session_factory() as session:
        store = BusinessSourceEntitlementStore(session, BIZ_A)
        assert [e.version for e in store.versions(SOURCE_KEY)] == [1, 2]
        assert store.latest(SOURCE_KEY).version == 2


# -- a business cannot enable a source denied at the platform level -------------------------------


def test_cannot_enable_source_without_platform_approval(session_factory):
    with session_factory() as session:
        # Catalogue + record terms, but DO NOT approve — so there is no in-force platform ceiling.
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
                operations={"collect": True},
                purposes=["prospecting"],
                contract_evidence={"reference": "MCA-TOS-2026", "kind": "terms_of_use"},
            )
        )
        session.commit()

    with session_factory() as session:
        store = BusinessSourceEntitlementStore(session, BIZ_A)
        with pytest.raises(BusinessSourceEntitlementError) as exc:
            store.set_entitlement(
                BusinessSourceEntitlementInput(source_key=SOURCE_KEY, enabled=True), ADMIN
            )
        assert exc.value.code == REASON_SOURCE_NOT_PLATFORM_APPROVED
        session.commit()

    with session_factory() as session:
        # Nothing staged.
        assert session.query(BusinessSourceEntitlement).count() == 0
        assert session.query(AuditEvent).count() == 0


def test_cannot_enable_source_whose_approval_has_lapsed(session_factory):
    with session_factory() as session:
        _seed_approved_source(session)
        session.commit()

    with session_factory() as session:
        store = BusinessSourceEntitlementStore(session, BIZ_A)
        # Evaluate FAR in the future, after the approval's one-year window has lapsed: the ceiling is
        # no longer in force, so the source counts as denied at the platform level.
        future = NOW + timedelta(days=800)
        with pytest.raises(BusinessSourceEntitlementError) as exc:
            store.set_entitlement(
                BusinessSourceEntitlementInput(source_key=SOURCE_KEY, enabled=True),
                ADMIN,
                now=future,
            )
        assert exc.value.code == REASON_SOURCE_NOT_PLATFORM_APPROVED
        session.commit()

    with session_factory() as session:
        assert session.query(BusinessSourceEntitlement).count() == 0


# -- a business cannot grant a right the platform denies ------------------------------------------


def test_cannot_grant_operation_denied_at_platform(session_factory):
    with session_factory() as session:
        _seed_approved_source(session)  # 'share' is NOT approved
        session.commit()

    with session_factory() as session:
        store = BusinessSourceEntitlementStore(session, BIZ_A)
        with pytest.raises(BusinessSourceEntitlementError) as exc:
            store.set_entitlement(
                BusinessSourceEntitlementInput(
                    source_key=SOURCE_KEY, enabled=True, operations={"share": True}
                ),
                ADMIN,
            )
        assert exc.value.code == REASON_OPERATION_DENIED_AT_PLATFORM
        assert exc.value.detail == "share"
        session.commit()

    with session_factory() as session:
        assert session.query(BusinessSourceEntitlement).count() == 0


def test_cannot_grant_field_right_denied_at_platform(session_factory):
    with session_factory() as session:
        _seed_approved_source(session)  # 'email' is display-only: export denied for that field
        session.commit()

    with session_factory() as session:
        store = BusinessSourceEntitlementStore(session, BIZ_A)
        with pytest.raises(BusinessSourceEntitlementError) as exc:
            store.set_entitlement(
                BusinessSourceEntitlementInput(
                    source_key=SOURCE_KEY,
                    enabled=True,
                    field_rights={"email": {"export": True}},
                ),
                ADMIN,
            )
        assert exc.value.code == REASON_FIELD_RIGHT_DENIED_AT_PLATFORM
        assert exc.value.detail == "email"

    with session_factory() as session:
        assert session.query(BusinessSourceEntitlement).count() == 0


def test_cannot_grant_purpose_denied_at_platform(session_factory):
    with session_factory() as session:
        _seed_approved_source(session)  # only 'prospecting' is documented
        session.commit()

    with session_factory() as session:
        store = BusinessSourceEntitlementStore(session, BIZ_A)
        with pytest.raises(BusinessSourceEntitlementError) as exc:
            store.set_entitlement(
                BusinessSourceEntitlementInput(
                    source_key=SOURCE_KEY, enabled=True, purposes=["resale"]
                ),
                ADMIN,
            )
        assert exc.value.code == REASON_PURPOSE_DENIED_AT_PLATFORM
        assert exc.value.detail == "resale"


# -- authority: only a business administrator may configure ---------------------------------------


def test_ordinary_user_cannot_configure_and_changes_nothing(session_factory):
    with session_factory() as session:
        _seed_approved_source(session)
        session.commit()

    with session_factory() as session:
        store = BusinessSourceEntitlementStore(session, BIZ_A)
        with pytest.raises(BusinessSourceEntitlementAuthorizationError) as exc:
            store.set_entitlement(
                BusinessSourceEntitlementInput(source_key=SOURCE_KEY, enabled=True), ORDINARY_USER
            )
        assert exc.value.code == REASON_NOT_BUSINESS_ADMINISTRATOR
        session.commit()

    with session_factory() as session:
        assert session.query(BusinessSourceEntitlement).count() == 0
        assert session.query(AuditEvent).count() == 0


# -- entitlements are per-business ----------------------------------------------------------------


def test_entitlements_are_scoped_per_business(session_factory):
    with session_factory() as session:
        _seed_approved_source(session)
        BusinessSourceEntitlementStore(session, BIZ_A).set_entitlement(
            BusinessSourceEntitlementInput(
                source_key=SOURCE_KEY, enabled=True, operations={"display": True}
            ),
            ADMIN,
        )
        session.commit()

    with session_factory() as session:
        # B has no entitlement for the same (platform-approved) source: its narrowed read denies.
        store_b = BusinessSourceEntitlementStore(session, BIZ_B)
        assert store_b.latest(SOURCE_KEY) is None
        assert store_b.permits(SOURCE_KEY, "email", "display", now=NOW) == (
            False,
            REASON_SOURCE_NOT_ENTITLED,
        )
        # A's entitlement is unaffected and still visible only to A.
        store_a = BusinessSourceEntitlementStore(session, BIZ_A)
        assert store_a.latest(SOURCE_KEY) is not None
