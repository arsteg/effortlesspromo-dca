"""DCA-078 — store scoped processing restrictions (the restriction ledger).

Pins the load-bearing behaviour of :mod:`app.services.processing_restriction`:

1. **Local and platform-wide restrictions are distinguishable and require appropriate authority.** A
   LOCAL (tenant) restriction is tenant-owned, stamps ``authority_plane = business`` and needs a
   business user holding ``processing.restrict`` (a read-only viewer and a platform operator lack it).
   A PLATFORM-WIDE source removal is control-plane (no ``business_id``), stamps
   ``authority_plane = platform``, needs a platform operator holding ``platform.source.restrict`` (a
   tenant administrator cannot), and REQUIRES a justification.
2. **A denial changes nothing.** Every refusal (unauthorized caller, unknown channel, inconsistent
   retention, missing justification, unknown source) is raised BEFORE the session is mutated, so it
   stages NO row.
3. **Versioned, insert-only, tenant-isolated.** Re-restricting a scope inserts a new version; a
   cross-tenant id reads as not-found.

Store logic is unit-tested on in-memory SQLite; the RLS/migration paths for the tables are covered by
``tests/test_rls.py`` and ``tests/test_migrations.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.db.base import Base, make_engine, make_session_factory
from app.db.models import (
    AuditEvent,
    PlatformSourceRestriction,
    ProcessingRestriction,
    RestrictionAuthorityPlane,
    RestrictionRetentionMode,
)
from app.services.processing_restriction import (
    AUDIT_ACTION_RECORD_RESTRICTION,
    REASON_MISSING_JUSTIFICATION,
    REASON_MISSING_RETAIN_UNTIL,
    REASON_MISSING_SOURCE,
    REASON_NOT_AUTHORIZED,
    REASON_NOT_PLATFORM_OPERATOR,
    REASON_SOURCE_NOT_FOUND,
    REASON_UNEXPECTED_RETAIN_UNTIL,
    REASON_UNKNOWN_CHANNEL,
    REASON_UNKNOWN_RETENTION_MODE,
    PlatformRestrictionPrincipal,
    PlatformSourceRestrictionStore,
    ProcessingRestrictionAuthorizationError,
    ProcessingRestrictionError,
    ProcessingRestrictionInput,
    ProcessingRestrictionStore,
    RestrictionPrincipal,
)
from app.services.source_catalog import SourceCatalogStore, SourceDraftInput

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"
USER = "00000000-0000-4000-9000-0000000a0078"
OPERATOR = "00000000-0000-4000-9000-00000090e778"
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)

ACTING_USER = RestrictionPrincipal(subject=USER, roles=("user",))
VIEWER = RestrictionPrincipal(subject=USER, roles=("viewer",))
OPERATOR_PRINCIPAL = PlatformRestrictionPrincipal(subject=OPERATOR, roles=("platform_operator",))
TENANT_ADMIN_AS_PLATFORM = PlatformRestrictionPrincipal(subject=USER, roles=("owner", "admin"))


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _seed_source(session, key: str = "mca_master_data") -> None:
    catalog = SourceCatalogStore(session)
    if catalog.get(key) is None:
        catalog.create_draft(
            SourceDraftInput(
                key=key,
                kind="official",
                access_method="api",
                name="Synthetic Registry",
                india_coverage={"nationwide": True},
            )
        )


# --- LOCAL (tenant) restrictions ------------------------------------------------------------------


def test_authorized_user_records_a_local_restriction(session_factory):
    with session_factory() as session:
        store = ProcessingRestrictionStore(session, BIZ_A)
        row = store.restrict(
            ProcessingRestrictionInput(
                brand_key="AcmeNewsletter",
                channel="email",
                purpose="Marketing_Email",
                reason="subject_request",
            ),
            ACTING_USER,
            now=NOW,
        )
        session.commit()
        rid = row.id

    with session_factory() as session:
        row = ProcessingRestrictionStore(session, BIZ_A).get(rid)
        assert row is not None
        assert row.business_id == BIZ_A
        assert row.authority_plane == RestrictionAuthorityPlane.BUSINESS.value
        assert row.authority_scope == "processing.restrict"
        assert row.authorized_by == USER
        assert row.scope_brand == "acmenewsletter"  # normalized
        assert row.scope_channel == "email"
        assert row.scope_purpose == "marketing_email"
        assert row.version == 1
        assert row.retention_mode == RestrictionRetentionMode.UNTIL_LIFTED.value
        assert row.retain_until is None
        # atomic audit entry in the same unit of work, opaque references only.
        events = session.query(AuditEvent).filter_by(action=AUDIT_ACTION_RECORD_RESTRICTION).all()
        assert len(events) == 1
        assert events[0].references["restrictionId"] == rid
        assert events[0].references["scopeBrand"] == "acmenewsletter"


def test_tenant_wide_restriction_leaves_all_dimensions_empty(session_factory):
    with session_factory() as session:
        row = ProcessingRestrictionStore(session, BIZ_A).restrict(
            ProcessingRestrictionInput(), ACTING_USER, now=NOW
        )
        session.commit()
        assert row.scope_brand == "" and row.scope_channel == "" and row.scope_purpose == ""
        assert row.version == 1


def test_re_restricting_a_scope_inserts_a_new_version(session_factory):
    with session_factory() as session:
        store = ProcessingRestrictionStore(session, BIZ_A)
        store.restrict(ProcessingRestrictionInput(channel="email"), ACTING_USER, now=NOW)
        store.restrict(ProcessingRestrictionInput(channel="email"), ACTING_USER, now=NOW)
        session.commit()
        history = store.history_for_scope(channel="email")
        assert [r.version for r in history] == [1, 2]


def test_until_date_retention_persists_the_window(session_factory):
    until = datetime(2026, 12, 31, tzinfo=UTC)
    with session_factory() as session:
        row = ProcessingRestrictionStore(session, BIZ_A).restrict(
            ProcessingRestrictionInput(
                channel="mobile",
                retention_mode=RestrictionRetentionMode.UNTIL_DATE.value,
                retain_until=until,
            ),
            ACTING_USER,
            now=NOW,
        )
        session.commit()
        assert row.retention_mode == RestrictionRetentionMode.UNTIL_DATE.value
        assert row.retain_until == until


def test_viewer_is_denied_and_stages_no_row(session_factory):
    with session_factory() as session:
        store = ProcessingRestrictionStore(session, BIZ_A)
        with pytest.raises(ProcessingRestrictionAuthorizationError) as exc:
            store.restrict(ProcessingRestrictionInput(channel="email"), VIEWER, now=NOW)
        assert exc.value.code == REASON_NOT_AUTHORIZED
        session.rollback()
        assert session.query(ProcessingRestriction).count() == 0
        assert session.query(AuditEvent).count() == 0


def test_unknown_channel_is_refused(session_factory):
    with session_factory() as session:
        with pytest.raises(ProcessingRestrictionError) as exc:
            ProcessingRestrictionStore(session, BIZ_A).restrict(
                ProcessingRestrictionInput(channel="carrier-pigeon"), ACTING_USER, now=NOW
            )
        assert exc.value.code == REASON_UNKNOWN_CHANNEL
        session.rollback()
        assert session.query(ProcessingRestriction).count() == 0


def test_until_date_without_retain_until_is_refused(session_factory):
    with session_factory() as session:
        with pytest.raises(ProcessingRestrictionError) as exc:
            ProcessingRestrictionStore(session, BIZ_A).restrict(
                ProcessingRestrictionInput(
                    channel="email", retention_mode=RestrictionRetentionMode.UNTIL_DATE.value
                ),
                ACTING_USER,
                now=NOW,
            )
        assert exc.value.code == REASON_MISSING_RETAIN_UNTIL
        session.rollback()
        assert session.query(ProcessingRestriction).count() == 0


def test_until_lifted_with_retain_until_is_refused(session_factory):
    with session_factory() as session:
        with pytest.raises(ProcessingRestrictionError) as exc:
            ProcessingRestrictionStore(session, BIZ_A).restrict(
                ProcessingRestrictionInput(
                    channel="email",
                    retention_mode=RestrictionRetentionMode.UNTIL_LIFTED.value,
                    retain_until=datetime(2026, 12, 31, tzinfo=UTC),
                ),
                ACTING_USER,
                now=NOW,
            )
        assert exc.value.code == REASON_UNEXPECTED_RETAIN_UNTIL
        session.rollback()


def test_unknown_retention_mode_is_refused(session_factory):
    with session_factory() as session:
        with pytest.raises(ProcessingRestrictionError) as exc:
            ProcessingRestrictionStore(session, BIZ_A).restrict(
                ProcessingRestrictionInput(channel="email", retention_mode="forever-and-ever"),
                ACTING_USER,
                now=NOW,
            )
        assert exc.value.code == REASON_UNKNOWN_RETENTION_MODE
        session.rollback()


def test_a_local_restriction_is_not_readable_by_another_tenant(session_factory):
    with session_factory() as session:
        row = ProcessingRestrictionStore(session, BIZ_A).restrict(
            ProcessingRestrictionInput(channel="email"), ACTING_USER, now=NOW
        )
        session.commit()
        rid = row.id

    with session_factory() as session:
        assert ProcessingRestrictionStore(session, BIZ_A).get(rid) is not None
        # a cross-tenant id reads exactly like not-found.
        assert ProcessingRestrictionStore(session, BIZ_B).get(rid) is None
        assert ProcessingRestrictionStore(session, BIZ_B).restrictions() == []


# --- PLATFORM-WIDE source removal -----------------------------------------------------------------


def test_operator_records_a_platform_wide_source_removal(session_factory):
    with session_factory() as session:
        _seed_source(session)
        store = PlatformSourceRestrictionStore(session)
        row = store.restrict_source(
            "mca_master_data",
            OPERATOR_PRINCIPAL,
            justification="licence revoked by registry",
            now=NOW,
        )
        session.commit()
        rid = row.id

    with session_factory() as session:
        row = PlatformSourceRestrictionStore(session).get(rid)
        assert row is not None
        assert row.authority_plane == RestrictionAuthorityPlane.PLATFORM.value
        assert row.authority_scope == "platform.source.restrict"
        assert row.authorized_by == OPERATOR
        assert row.source_key == "mca_master_data"
        assert row.justification == "licence revoked by registry"
        assert row.version == 1
        # platform control-plane: no business_id column (not tenant-owned) and no tenant audit entry.
        assert not hasattr(row, "business_id")
        assert session.query(AuditEvent).count() == 0


def test_platform_removal_versions_per_source(session_factory):
    with session_factory() as session:
        _seed_source(session)
        store = PlatformSourceRestrictionStore(session)
        store.restrict_source("mca_master_data", OPERATOR_PRINCIPAL, justification="j1", now=NOW)
        store.restrict_source("mca_master_data", OPERATOR_PRINCIPAL, justification="j2", now=NOW)
        session.commit()
        assert [r.version for r in store.for_source("mca_master_data")] == [1, 2]


def test_tenant_admin_cannot_perform_a_platform_wide_removal(session_factory):
    with session_factory() as session:
        _seed_source(session)
        with pytest.raises(ProcessingRestrictionAuthorizationError) as exc:
            PlatformSourceRestrictionStore(session).restrict_source(
                "mca_master_data", TENANT_ADMIN_AS_PLATFORM, justification="j", now=NOW
            )
        assert exc.value.code == REASON_NOT_PLATFORM_OPERATOR
        session.rollback()
        assert session.query(PlatformSourceRestriction).count() == 0


def test_platform_removal_requires_a_justification(session_factory):
    with session_factory() as session:
        _seed_source(session)
        with pytest.raises(ProcessingRestrictionError) as exc:
            PlatformSourceRestrictionStore(session).restrict_source(
                "mca_master_data", OPERATOR_PRINCIPAL, justification="   ", now=NOW
            )
        assert exc.value.code == REASON_MISSING_JUSTIFICATION
        session.rollback()
        assert session.query(PlatformSourceRestriction).count() == 0


def test_platform_removal_requires_an_existing_source(session_factory):
    with session_factory() as session:
        with pytest.raises(ProcessingRestrictionError) as exc:
            PlatformSourceRestrictionStore(session).restrict_source(
                "no_such_source", OPERATOR_PRINCIPAL, justification="j", now=NOW
            )
        assert exc.value.code == REASON_SOURCE_NOT_FOUND
        session.rollback()


def test_platform_removal_requires_a_source_key(session_factory):
    with session_factory() as session:
        with pytest.raises(ProcessingRestrictionError) as exc:
            PlatformSourceRestrictionStore(session).restrict_source(
                "  ", OPERATOR_PRINCIPAL, justification="j", now=NOW
            )
        assert exc.value.code == REASON_MISSING_SOURCE
        session.rollback()


def test_local_and_platform_restrictions_are_distinguishable(session_factory):
    """Both kinds coexist in one restriction ledger yet are told apart by plane + table/ownership."""
    with session_factory() as session:
        _seed_source(session)
        ProcessingRestrictionStore(session, BIZ_A).restrict(
            ProcessingRestrictionInput(channel="email"), ACTING_USER, now=NOW
        )
        PlatformSourceRestrictionStore(session).restrict_source(
            "mca_master_data", OPERATOR_PRINCIPAL, justification="j", now=NOW
        )
        session.commit()

    with session_factory() as session:
        local = ProcessingRestrictionStore(session, BIZ_A).restrictions()
        platform = PlatformSourceRestrictionStore(session).restrictions()
        assert len(local) == 1 and len(platform) == 1
        assert local[0].authority_plane == RestrictionAuthorityPlane.BUSINESS.value
        assert platform[0].authority_plane == RestrictionAuthorityPlane.PLATFORM.value
        # local is tenant-owned (carries a business_id); platform is control-plane (does not).
        assert local[0].business_id == BIZ_A
        assert not hasattr(platform[0], "business_id")
