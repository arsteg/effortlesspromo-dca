"""DCA-081 — invalidate affected recommendations on restriction.

Service unit tests (in-memory SQLite) pin
:class:`~app.services.restriction_fanout.RestrictionFanout`:

- a LOCAL restriction fanout revokes ONLY the restricting business's affected live suggestions (scoped
  by channel; "" = all), leaves every other tenant untouched, honours the channel dimension, matches no
  raw suggestion when the restriction is narrowed to a specific brand/purpose, and stages exactly one
  propagation acknowledgment + one atomic audit entry;
- a PLATFORM-WIDE source removal fanout reaches EVERY tenant's live suggestions acquired from the removed
  source and stages one acknowledgment per affected tenant, while a tenant with no such suggestion is
  never touched;
- revocation is ``status``-only (``match_score`` / ``score_status`` untouched) and terminal suggestions
  (dismissed / expired / already revoked) are never affected, so a second fanout revokes nothing and
  writes no acknowledgment (idempotent, no unintended state);
- a DENIED fanout (a read-only viewer locally, a non-operator for a platform-wide removal) is refused
  BEFORE any state is touched — no revocation, no acknowledgment, no audit — and so is a plane mismatch.

The transaction / RLS behaviour of the new ``restriction_propagations`` table is exercised on a
disposable PostgreSQL in ``tests/test_rls.py`` (SQLite cannot enforce RLS); SQLite is sufficient here for
the app-level per-tenant revocation + acknowledgment + audit logic (same split as DCA-043).
"""

from __future__ import annotations

import pytest

from app.db.base import Base, make_engine, make_session_factory
from app.db.models import (
    AuditEvent,
    PlatformSourceRestriction,
    ProcessingRestriction,
    ProspectCandidate,
    RestrictionAuthorityPlane,
    RestrictionPropagation,
)
from app.services.processing_restriction import (
    PlatformRestrictionPrincipal,
    RestrictionPrincipal,
)
from app.services.restriction_fanout import (
    AUDIT_ACTION_REVOKE_ON_RESTRICTION,
    REASON_NOT_AUTHORIZED,
    REASON_NOT_PLATFORM_OPERATOR,
    REASON_PLANE_MISMATCH,
    REVOKED_STATUS,
    RestrictionFanout,
    RestrictionFanoutAuthorizationError,
    RestrictionFanoutError,
    run_restriction_fanout,
)

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"

OWNER_A = RestrictionPrincipal(subject="00000000-0000-4000-9000-0000000a0081", roles=("owner",))
VIEWER_A = RestrictionPrincipal(subject="00000000-0000-4000-9000-0000000a0082", roles=("viewer",))
OPERATOR = PlatformRestrictionPrincipal(
    subject="00000000-0000-4000-9000-0000000a0083", roles=("platform_operator",)
)
TENANT_ADMIN = PlatformRestrictionPrincipal(
    subject="00000000-0000-4000-9000-0000000a0084", roles=("admin",)
)


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def _add_candidate(
    session,
    business_id,
    *,
    org,
    status="suggested",
    source_key="synthetic.mca",
    contact=None,
) -> ProspectCandidate:
    candidate = ProspectCandidate(
        business_id=business_id,
        run_id="run-synthetic",
        org_name="Synthetic Co",
        org_identifier=org,
        contact=contact if contact is not None else {"email": f"{org}@synthetic.example"},
        source_key=source_key,
        source_ref=f"synthetic://mca/{org}",
        trust_tier="official_registry",
        match_score=1.0,
        status=status,
    )
    session.add(candidate)
    return candidate


def _local_restriction(
    business_id, *, brand="", channel="", purpose="", version=1
) -> ProcessingRestriction:
    return ProcessingRestriction(
        id=f"local-{business_id[-1]}-{channel or 'all'}-{version}",
        business_id=business_id,
        scope_brand=brand,
        scope_channel=channel,
        scope_purpose=purpose,
        version=version,
        authority_plane=RestrictionAuthorityPlane.BUSINESS.value,
        authority_scope="processing.restrict",
        authorized_by=OWNER_A.subject,
    )


def _platform_restriction(source_key, *, version=1) -> PlatformSourceRestriction:
    return PlatformSourceRestriction(
        id=f"plat-{source_key}-{version}",
        source_key=source_key,
        version=version,
        authority_plane=RestrictionAuthorityPlane.PLATFORM.value,
        authority_scope="platform.source.restrict",
        authorized_by=OPERATOR.subject,
        justification="synthetic removal",
    )


# --------------------------------------------------------------------------------------------------
# LOCAL fanout — affects only the restricting business's scope.
# --------------------------------------------------------------------------------------------------


def test_local_fanout_revokes_only_this_business_and_acknowledges(session_factory):
    with session_factory() as session:
        _add_candidate(session, BIZ_A, org="a-1")
        _add_candidate(session, BIZ_A, org="a-2", status="accepted")
        _add_candidate(session, BIZ_B, org="b-1")
        restriction = _local_restriction(BIZ_A)
        session.add(restriction)
        session.commit()

    with session_factory() as session:
        restriction = session.get(ProcessingRestriction, _local_restriction(BIZ_A).id)
        result = RestrictionFanout(session).fanout_local(restriction, OWNER_A)
        session.commit()
        assert result.revoked_count == 2
        assert result.affected_businesses == [BIZ_A]

    with session_factory() as session:
        states = {c.org_identifier: c.status for c in session.query(ProspectCandidate).all()}
        # A's live suggestions (suggested AND accepted) are revoked; B is untouched (scope boundary).
        assert states == {"a-1": REVOKED_STATUS, "a-2": REVOKED_STATUS, "b-1": "suggested"}

        acks = session.query(RestrictionPropagation).all()
        assert len(acks) == 1  # exactly one acknowledgment, for the restricting business
        assert acks[0].business_id == BIZ_A
        assert acks[0].revoked_count == 2
        assert acks[0].authority_plane == RestrictionAuthorityPlane.BUSINESS.value
        assert acks[0].acknowledged_by == OWNER_A.subject

        audits = session.query(AuditEvent).filter_by(business_id=BIZ_A).all()
        assert [a.action for a in audits] == [AUDIT_ACTION_REVOKE_ON_RESTRICTION]
        assert audits[0].references["revokedCount"] == 2
        # No acknowledgment and no audit landed under B's scope.
        assert session.query(RestrictionPropagation).filter_by(business_id=BIZ_B).count() == 0
        assert session.query(AuditEvent).filter_by(business_id=BIZ_B).count() == 0


def test_local_fanout_matches_only_the_named_channel(session_factory):
    with session_factory() as session:
        _add_candidate(session, BIZ_A, org="by-email", contact={"email": "x@synthetic.example"})
        _add_candidate(session, BIZ_A, org="by-phone", contact={"phone": "+911234567890"})
        session.add(_local_restriction(BIZ_A, channel="email"))
        session.commit()

    with session_factory() as session:
        restriction = session.get(
            ProcessingRestriction, _local_restriction(BIZ_A, channel="email").id
        )
        result = RestrictionFanout(session).fanout_local(restriction, OWNER_A)
        session.commit()
        assert result.revoked_count == 1

    with session_factory() as session:
        states = {c.org_identifier: c.status for c in session.query(ProspectCandidate).all()}
        assert states == {"by-email": REVOKED_STATUS, "by-phone": "suggested"}
        ack = session.query(RestrictionPropagation).one()
        assert ack.scope_channel == "email"


def test_local_fanout_brand_narrowed_restriction_matches_no_raw_suggestion(session_factory):
    # A raw suggestion carries no brand/purpose, so a brand-narrowed restriction touches none of them —
    # the "affects only its scope" guarantee (it never over-revokes the whole queue).
    with session_factory() as session:
        _add_candidate(session, BIZ_A, org="a-1")
        session.add(_local_restriction(BIZ_A, brand="acme"))
        session.commit()

    with session_factory() as session:
        restriction = session.get(ProcessingRestriction, _local_restriction(BIZ_A, brand="acme").id)
        result = RestrictionFanout(session).fanout_local(restriction, OWNER_A)
        session.commit()
        assert result.revoked_count == 0

    with session_factory() as session:
        assert session.query(ProspectCandidate).one().status == "suggested"
        assert session.query(RestrictionPropagation).count() == 0
        assert session.query(AuditEvent).count() == 0


def test_local_fanout_terminal_suggestions_untouched_and_idempotent(session_factory):
    with session_factory() as session:
        _add_candidate(session, BIZ_A, org="live")
        _add_candidate(session, BIZ_A, org="dropped", status="dismissed")
        _add_candidate(session, BIZ_A, org="gone", status="expired")
        session.add(_local_restriction(BIZ_A))
        session.commit()

    with session_factory() as session:
        restriction = session.get(ProcessingRestriction, _local_restriction(BIZ_A).id)
        count = RestrictionFanout(session).fanout_local(restriction, OWNER_A).revoked_count
        session.commit()
        assert count == 1  # only the live (suggested) one

    with session_factory() as session:
        # Idempotent re-run: nothing live remains, so nothing is revoked and no second acknowledgment.
        restriction = session.get(ProcessingRestriction, _local_restriction(BIZ_A).id)
        count = RestrictionFanout(session).fanout_local(restriction, OWNER_A).revoked_count
        session.commit()
        assert count == 0

    with session_factory() as session:
        states = {c.org_identifier: c.status for c in session.query(ProspectCandidate).all()}
        assert states == {"live": REVOKED_STATUS, "dropped": "dismissed", "gone": "expired"}
        assert session.query(RestrictionPropagation).count() == 1  # one ack, not two


def test_local_fanout_denied_for_viewer_changes_nothing(session_factory):
    with session_factory() as session:
        _add_candidate(session, BIZ_A, org="a-1")
        session.add(_local_restriction(BIZ_A))
        session.commit()

    with session_factory() as session:
        restriction = session.get(ProcessingRestriction, _local_restriction(BIZ_A).id)
        with pytest.raises(RestrictionFanoutAuthorizationError) as exc:
            RestrictionFanout(session).fanout_local(restriction, VIEWER_A)
        assert exc.value.code == REASON_NOT_AUTHORIZED
        session.commit()

    with session_factory() as session:
        # The relevant denial changed nothing: the suggestion is still live, no ack, no audit.
        assert session.query(ProspectCandidate).one().status == "suggested"
        assert session.query(RestrictionPropagation).count() == 0
        assert session.query(AuditEvent).count() == 0


def test_local_fanout_rejects_platform_plane_restriction(session_factory):
    with session_factory() as session:
        _add_candidate(session, BIZ_A, org="a-1")
        restriction = _local_restriction(BIZ_A)
        restriction.authority_plane = RestrictionAuthorityPlane.PLATFORM.value  # wrong plane
        session.add(restriction)
        session.commit()

    with session_factory() as session:
        restriction = session.get(ProcessingRestriction, _local_restriction(BIZ_A).id)
        with pytest.raises(RestrictionFanoutError) as exc:
            RestrictionFanout(session).fanout_local(restriction, OWNER_A)
        assert exc.value.code == REASON_PLANE_MISMATCH
        session.commit()

    with session_factory() as session:
        assert session.query(ProspectCandidate).one().status == "suggested"


# --------------------------------------------------------------------------------------------------
# PLATFORM-WIDE fanout — reaches every affected tenant's copies.
# --------------------------------------------------------------------------------------------------


def test_platform_fanout_reaches_all_affected_tenants(session_factory):
    with session_factory() as session:
        _add_candidate(session, BIZ_A, org="a-removed", source_key="src.gone")
        _add_candidate(session, BIZ_B, org="b-removed", source_key="src.gone")
        _add_candidate(session, BIZ_A, org="a-kept", source_key="src.other")
        session.add(_platform_restriction("src.gone"))
        session.commit()

    with session_factory() as session:
        restriction = session.get(PlatformSourceRestriction, _platform_restriction("src.gone").id)
        result = RestrictionFanout(session).fanout_platform_source(restriction, OPERATOR)
        session.commit()
        assert result.revoked_count == 2
        assert sorted(result.affected_businesses) == [BIZ_A, BIZ_B]

    with session_factory() as session:
        states = {c.org_identifier: c.status for c in session.query(ProspectCandidate).all()}
        # Every platform-controlled copy from the removed source is revoked, across BOTH tenants;
        # a copy from another source is untouched.
        assert states == {
            "a-removed": REVOKED_STATUS,
            "b-removed": REVOKED_STATUS,
            "a-kept": "suggested",
        }
        acks = {a.business_id: a for a in session.query(RestrictionPropagation).all()}
        assert set(acks) == {BIZ_A, BIZ_B}  # one acknowledgment per affected tenant
        assert all(a.source_key == "src.gone" for a in acks.values())
        assert all(
            a.authority_plane == RestrictionAuthorityPlane.PLATFORM.value for a in acks.values()
        )


def test_platform_fanout_denied_for_non_operator_changes_nothing(session_factory):
    with session_factory() as session:
        _add_candidate(session, BIZ_A, org="a-removed", source_key="src.gone")
        session.add(_platform_restriction("src.gone"))
        session.commit()

    with session_factory() as session:
        restriction = session.get(PlatformSourceRestriction, _platform_restriction("src.gone").id)
        with pytest.raises(RestrictionFanoutAuthorizationError) as exc:
            RestrictionFanout(session).fanout_platform_source(restriction, TENANT_ADMIN)
        assert exc.value.code == REASON_NOT_PLATFORM_OPERATOR
        session.commit()

    with session_factory() as session:
        assert session.query(ProspectCandidate).one().status == "suggested"
        assert session.query(RestrictionPropagation).count() == 0


def test_platform_fanout_is_idempotent(session_factory):
    with session_factory() as session:
        _add_candidate(session, BIZ_A, org="a-removed", source_key="src.gone")
        session.add(_platform_restriction("src.gone"))
        session.commit()

    with session_factory() as session:
        restriction = session.get(PlatformSourceRestriction, _platform_restriction("src.gone").id)
        RestrictionFanout(session).fanout_platform_source(restriction, OPERATOR)
        session.commit()

    with session_factory() as session:
        restriction = session.get(PlatformSourceRestriction, _platform_restriction("src.gone").id)
        result = RestrictionFanout(session).fanout_platform_source(restriction, OPERATOR)
        session.commit()
        assert result.revoked_count == 0  # already revoked — nothing to do

    with session_factory() as session:
        assert session.query(RestrictionPropagation).count() == 1  # no duplicate acknowledgment


def test_sweep_platform_fans_out_every_in_force_removal(session_factory):
    with session_factory() as session:
        _add_candidate(session, BIZ_A, org="a-removed", source_key="src.gone")
        _add_candidate(session, BIZ_B, org="b-removed", source_key="src.gone")
        session.add(_platform_restriction("src.gone"))
        session.commit()
        factory = session_factory

    results = run_restriction_fanout(factory)
    assert sum(r.revoked_count for r in results) == 2

    with session_factory() as session:
        states = {c.org_identifier: c.status for c in session.query(ProspectCandidate).all()}
        assert states == {"a-removed": REVOKED_STATUS, "b-removed": REVOKED_STATUS}
        assert session.query(RestrictionPropagation).count() == 2

    # A second sweep is a no-op (idempotent): nothing live remains.
    results = run_restriction_fanout(factory)
    assert sum(r.revoked_count for r in results) == 0
    with session_factory() as session:
        assert session.query(RestrictionPropagation).count() == 2
