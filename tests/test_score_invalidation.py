"""DCA-043 — mark recommendation scores stale after a confirmed targeting change.

Three layers:

- **Service unit tests** (in-memory SQLite) pin :class:`~app.services.score_invalidation.ScoreInvalidator`:
  a confirmed targeting change flips only this business's *affected* live-recommendation scores from
  ``current`` to ``stale`` and stages exactly one atomic audit entry; an *accepted* recommendation keeps
  its approval (``status``) while its score goes stale; terminal (dismissed/expired) recommendations and
  another tenant's recommendations are never touched; a no-op (nothing affected) stages no row and no
  audit; and the invalidator stages (never commits) into the caller's session.
- **Confirm-integration tests** prove the DCA-040 ``confirm`` wires the invalidation in: confirming a
  *new* version invalidates affected scores (a second audit entry), re-confirming the SAME version
  invalidates nothing, and a confirm with no candidates writes only the ``icp.confirm`` audit (the
  DCA-040 single-audit guarantee is preserved).
- **HTTP tests** (``TestClient``) prove the behaviour over the real API following the developer manual
  test: discover recommendations for Business A, confirm an ICP version → A's scores go ``stale`` with
  approval untouched, while Business B's scores stay ``current``; and a *denied* confirm (unknown
  version → 404) changes no scores.

SQLite is sufficient (app-level per-tenant marking + audit); the transaction/RLS/migration paths are
covered by ``tests/test_rls.py`` and ``tests/test_migrations.py``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.audit.sink import ActorType, AuditActor
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import AuditEvent, ProspectCandidate, ScoreStatus
from app.main import create_app
from app.services.icp_confirmation import AUDIT_ACTION_CONFIRM, IcpConfirmationStore
from app.services.icp_store import IcpDraftInput, IcpStore
from app.services.score_invalidation import (
    AUDIT_ACTION_INVALIDATE_SCORES,
    ScoreInvalidator,
)
from tests.conftest import AUTH, make_settings

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"
ACTOR = AuditActor(id="00000000-0000-4000-9000-0000000a0043", type=ActorType.USER)


# --------------------------------------------------------------------------------------------------
# Service unit tests (in-memory SQLite)
# --------------------------------------------------------------------------------------------------


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
    score_status="current",
    scored_version=None,
    match_score=1.0,
) -> ProspectCandidate:
    candidate = ProspectCandidate(
        business_id=business_id,
        run_id="run-synthetic",
        org_name="Synthetic Co",
        org_identifier=org,
        contact={},
        source_key="synthetic.mca",
        source_ref=f"synthetic://mca/{org}",
        trust_tier="official_registry",
        match_score=match_score,
        status=status,
        score_status=score_status,
        scored_targeting_version=scored_version,
    )
    session.add(candidate)
    return candidate


def test_invalidate_marks_affected_scores_stale_and_audits_once(session_factory):
    with session_factory() as session:
        _add_candidate(session, BIZ_A, org="org-1")
        _add_candidate(session, BIZ_A, org="org-2")
        session.commit()

    with session_factory() as session:
        count = ScoreInvalidator(session, BIZ_A).invalidate_for_targeting_change(2, ACTOR)
        session.commit()
        assert count == 2

    with session_factory() as session:
        scores = {c.org_identifier: c.score_status for c in session.query(ProspectCandidate).all()}
        assert scores == {"org-1": ScoreStatus.STALE.value, "org-2": ScoreStatus.STALE.value}
        audits = session.query(AuditEvent).filter(AuditEvent.business_id == BIZ_A).all()
        assert [a.action for a in audits] == [AUDIT_ACTION_INVALIDATE_SCORES]
        assert audits[0].references["invalidatedCount"] == 2
        assert audits[0].references["newTargetingVersion"] == 2
        assert sorted(audits[0].references["candidateIds"])  # opaque row ids recorded


def test_invalidate_is_scoped_to_one_business(session_factory):
    with session_factory() as session:
        _add_candidate(session, BIZ_A, org="org-a")
        _add_candidate(session, BIZ_B, org="org-b")
        session.commit()

    with session_factory() as session:
        ScoreInvalidator(session, BIZ_A).invalidate_for_targeting_change(1, ACTOR)
        session.commit()

    with session_factory() as session:
        a = session.query(ProspectCandidate).filter_by(business_id=BIZ_A).one()
        b = session.query(ProspectCandidate).filter_by(business_id=BIZ_B).one()
        assert a.score_status == ScoreStatus.STALE.value  # A invalidated
        assert b.score_status == ScoreStatus.CURRENT.value  # B untouched (tenant isolation)
        # No audit landed under B's scope.
        assert session.query(AuditEvent).filter_by(business_id=BIZ_B).count() == 0


def test_accepted_recommendation_keeps_approval_when_score_invalidated(session_factory):
    # The headline acceptance criterion: send permission / approval is untouched.
    with session_factory() as session:
        _add_candidate(session, BIZ_A, org="accepted-1", status="accepted")
        session.commit()

    with session_factory() as session:
        count = ScoreInvalidator(session, BIZ_A).invalidate_for_targeting_change(2, ACTOR)
        session.commit()
        assert count == 1

    with session_factory() as session:
        c = session.query(ProspectCandidate).one()
        assert c.score_status == ScoreStatus.STALE.value  # score flagged for recomputation
        assert c.status == "accepted"  # approval preserved — invalidation is score-only
        assert c.match_score == 1.0  # the score value itself is left for the (disabled) rescore


def test_terminal_recommendations_are_not_affected(session_factory):
    with session_factory() as session:
        _add_candidate(session, BIZ_A, org="live", status="suggested")
        _add_candidate(session, BIZ_A, org="dropped", status="dismissed")
        _add_candidate(session, BIZ_A, org="gone", status="expired")
        session.commit()

    with session_factory() as session:
        count = ScoreInvalidator(session, BIZ_A).invalidate_for_targeting_change(1, ACTOR)
        session.commit()
        assert count == 1  # only the live (suggested) recommendation

    with session_factory() as session:
        states = {c.org_identifier: c.score_status for c in session.query(ProspectCandidate).all()}
        assert states == {
            "live": ScoreStatus.STALE.value,
            "dropped": ScoreStatus.CURRENT.value,
            "gone": ScoreStatus.CURRENT.value,
        }


def test_already_stale_is_a_no_op_with_no_audit(session_factory):
    with session_factory() as session:
        _add_candidate(session, BIZ_A, org="already", score_status=ScoreStatus.STALE.value)
        session.commit()

    with session_factory() as session:
        count = ScoreInvalidator(session, BIZ_A).invalidate_for_targeting_change(3, ACTOR)
        session.commit()
        assert count == 0  # nothing affected — not re-marked

    with session_factory() as session:
        # No unintended state: still stale, and no audit entry was written for the no-op.
        assert session.query(ProspectCandidate).one().score_status == ScoreStatus.STALE.value
        assert session.query(AuditEvent).count() == 0


def test_score_already_at_new_version_is_not_affected(session_factory):
    with session_factory() as session:
        # A score already computed against version 5 (current) is not affected by confirming 5.
        _add_candidate(session, BIZ_A, org="fresh", scored_version=5)
        _add_candidate(session, BIZ_A, org="old", scored_version=4)
        session.commit()

    with session_factory() as session:
        count = ScoreInvalidator(session, BIZ_A).invalidate_for_targeting_change(5, ACTOR)
        session.commit()
        assert count == 1  # only the one scored against the OLD version

    with session_factory() as session:
        states = {c.org_identifier: c.score_status for c in session.query(ProspectCandidate).all()}
        assert states == {"fresh": ScoreStatus.CURRENT.value, "old": ScoreStatus.STALE.value}


def test_invalidator_does_not_commit(session_factory):
    with session_factory() as session:
        _add_candidate(session, BIZ_A, org="org-1")
        session.commit()
    with session_factory() as session:
        ScoreInvalidator(session, BIZ_A).invalidate_for_targeting_change(1, ACTOR)
        session.rollback()  # the invalidator never commits — a rollback leaves nothing behind
    with session_factory() as session:
        assert session.query(ProspectCandidate).one().score_status == ScoreStatus.CURRENT.value
        assert session.query(AuditEvent).count() == 0


# --------------------------------------------------------------------------------------------------
# Confirm-integration tests — the DCA-040 confirm wires in the DCA-043 invalidation
# --------------------------------------------------------------------------------------------------


def _save_draft(factory, business_id, **kwargs) -> int:
    with factory() as session:
        draft = IcpStore(session, business_id).save_draft(IcpDraftInput(**kwargs))
        session.commit()
        return draft.version


def test_confirming_a_new_version_invalidates_scores(session_factory):
    _save_draft(session_factory, BIZ_A, industries=["textiles"])
    _save_draft(session_factory, BIZ_A, industries=["software"])  # v2 (a different target)
    with session_factory() as session:
        _add_candidate(session, BIZ_A, org="org-1")
        _add_candidate(session, BIZ_A, org="org-2")
        session.commit()

    # Confirm v1 — the first confirmed targeting; the placeholder scores are invalidated.
    with session_factory() as session:
        confirmation = IcpConfirmationStore(session, BIZ_A).confirm(1, ACTOR)
        session.commit()
        assert confirmation.invalidated_score_count == 2

    with session_factory() as session:
        assert all(
            c.score_status == ScoreStatus.STALE.value
            for c in session.query(ProspectCandidate).all()
        )
        actions = [a.action for a in session.query(AuditEvent).all()]
        # Both the confirm audit AND the score-invalidation audit landed in the same unit of work.
        assert AUDIT_ACTION_CONFIRM in actions
        assert AUDIT_ACTION_INVALIDATE_SCORES in actions


def test_reconfirming_same_version_invalidates_nothing(session_factory):
    _save_draft(session_factory, BIZ_A, industries=["textiles"])
    with session_factory() as session:
        _add_candidate(session, BIZ_A, org="org-1")
        IcpConfirmationStore(session, BIZ_A).confirm(1, ACTOR)  # first confirm invalidates
        session.commit()

    # Re-confirm the SAME version — not a targeting change, so nothing new is invalidated.
    with session_factory() as session:
        confirmation = IcpConfirmationStore(session, BIZ_A).confirm(1, ACTOR)
        session.commit()
        assert confirmation.invalidated_score_count == 0


def test_confirm_with_no_candidates_writes_only_the_confirm_audit(session_factory):
    # Guards the DCA-040 guarantee: a confirm that invalidates nothing stages exactly one audit entry.
    _save_draft(session_factory, BIZ_A, industries=["textiles"])
    with session_factory() as session:
        confirmation = IcpConfirmationStore(session, BIZ_A).confirm(1, ACTOR)
        session.commit()
        assert confirmation.invalidated_score_count == 0
    with session_factory() as session:
        assert [a.action for a in session.query(AuditEvent).all()] == [AUDIT_ACTION_CONFIRM]


# --------------------------------------------------------------------------------------------------
# HTTP tests (TestClient) — the real service API / developer manual-test flow
# --------------------------------------------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    app = create_app(make_settings())
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _discover(client, business_id) -> list[dict]:
    """Register + enable a tenant, set an (empty) ICP profile, run discovery, return its candidates."""
    client.post("/v1/tenants", json={"businessId": business_id}, headers=AUTH)
    client.post(f"/v1/tenants/{business_id}/enable", headers=AUTH)
    client.put(
        f"/v1/tenants/{business_id}/icp",
        json={"industries": [], "regions": [], "roles": []},
        headers=AUTH,
    )
    run = client.post(f"/v1/tenants/{business_id}/discovery/run", headers=AUTH)
    assert run.status_code == 200, run.text
    return client.get(f"/v1/tenants/{business_id}/candidates", headers=AUTH).json()


def _confirm(client, business_id, version, **overrides):
    body = {"version": version, "actorId": ACTOR.id}
    body.update(overrides)
    return client.post(f"/v1/tenants/{business_id}/icp/confirm", json=body, headers=AUTH)


def test_http_confirm_marks_scores_stale_without_touching_approval(client):
    candidates = _discover(client, BIZ_A)
    assert candidates and all(c["scoreStatus"] == "current" for c in candidates)
    assert all(c["scoredTargetingVersion"] is None for c in candidates)

    # Accept one recommendation first — its approval must survive the score invalidation.
    accepted_id = candidates[0]["id"]
    assert (
        client.post(
            f"/v1/tenants/{BIZ_A}/candidates/{accepted_id}/accept", headers=AUTH
        ).status_code
        == 200
    )

    # Confirm an ICP draft version — a confirmed targeting change. (Uses the DCA-039 draft route.)
    draft = client.post(
        f"/v1/tenants/{BIZ_A}/icp/drafts",
        json={
            "industries": ["textiles"],
            "sizeBands": [],
            "roles": [],
            "geography": [],
            "exclusions": [],
        },
        headers=AUTH,
    )
    assert draft.status_code == 201, draft.text
    resp = _confirm(client, BIZ_A, draft.json()["version"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["invalidatedScoreCount"] == len(candidates)
    assert resp.json()["grantsCampaignAuthority"] is False  # no sending authority granted

    after = client.get(f"/v1/tenants/{BIZ_A}/candidates", headers=AUTH).json()
    by_id = {c["id"]: c for c in after}
    assert all(c["scoreStatus"] == "stale" for c in after)  # every live score invalidated
    # The accepted recommendation keeps its approval; only its score is flagged stale.
    assert by_id[accepted_id]["status"] == "accepted"
    assert by_id[accepted_id]["scoreStatus"] == "stale"


def test_http_denied_confirm_changes_no_scores(client):
    candidates = _discover(client, BIZ_A)
    assert all(c["scoreStatus"] == "current" for c in candidates)

    # The denial: confirming an unknown version is a 404 and must invalidate nothing.
    denied = _confirm(client, BIZ_A, 99)
    assert denied.status_code == 404

    after = client.get(f"/v1/tenants/{BIZ_A}/candidates", headers=AUTH).json()
    assert all(c["scoreStatus"] == "current" for c in after)  # no unintended state change


def test_http_other_business_scores_are_untouched(client):
    a_candidates = _discover(client, BIZ_A)
    b_candidates = _discover(client, BIZ_B)
    assert a_candidates and b_candidates

    draft = client.post(
        f"/v1/tenants/{BIZ_A}/icp/drafts",
        json={
            "industries": ["textiles"],
            "sizeBands": [],
            "roles": [],
            "geography": [],
            "exclusions": [],
        },
        headers=AUTH,
    )
    assert _confirm(client, BIZ_A, draft.json()["version"]).status_code == 200

    a_after = client.get(f"/v1/tenants/{BIZ_A}/candidates", headers=AUTH).json()
    b_after = client.get(f"/v1/tenants/{BIZ_B}/candidates", headers=AUTH).json()
    assert all(c["scoreStatus"] == "stale" for c in a_after)  # A invalidated
    assert all(c["scoreStatus"] == "current" for c in b_after)  # B untouched (tenant isolation)
