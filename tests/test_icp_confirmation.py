"""DCA-040 — confirm one ICP version (select the active ideal-customer-profile version).

Two layers, matching the DCA-039 ICP-draft suite:

- **Service unit tests** (in-memory SQLite) pin the store logic: only an existing draft version of the
  business that carries material targeting can be confirmed; confirming records actor + time and
  stages an atomic audit entry; there is exactly one confirmation row per business (confirming
  re-points it — an upsert); editing (a new draft) never changes the active version; a cross-tenant
  version cannot be confirmed; and the store stages (never commits) into the caller's session.
- **HTTP tests** (``TestClient``) prove the same over the real service API, following the developer
  manual-test steps: confirm version 1 (it becomes active), edit to version 2 and verify version 1
  stays active until version 2 is confirmed, and reproduce the unknown-version / no-material-targeting
  / bad-actor denials with no state change.

SQLite is sufficient here — the behaviour is app-level (per-tenant selection + validation + audit); the
transaction/RLS/migration paths are covered by ``tests/test_rls.py`` and ``tests/test_migrations.py``
(which pin ``icp_confirmations`` into the RLS set and its dual-path migration on disposable Postgres).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.audit.sink import ActorType, AuditActor
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import AuditEvent, IcpConfirmation, IcpDraft
from app.main import create_app
from app.services.icp_confirmation import (
    AUDIT_ACTION_CONFIRM,
    REASON_DRAFT_VERSION_NOT_FOUND,
    REASON_NO_MATERIAL_TARGETING,
    IcpConfirmationError,
    IcpConfirmationStore,
    has_material_targeting,
)
from app.services.icp_store import IcpDraftInput, IcpStore
from tests.conftest import AUTH, make_settings

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"
ACTOR = AuditActor(id="00000000-0000-4000-9000-0000000a0040", type=ActorType.USER)


# --------------------------------------------------------------------------------------------------
# has_material_targeting — the "material targeting" rule
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs,material",
    [
        ({"industries": ["retail"]}, True),
        ({"size_bands": ["small"]}, True),
        ({"roles": ["owner"]}, True),
        ({"geography": ["KA"]}, True),
        ({}, False),  # wholly empty — not a target
        (
            {"industries": [], "exclusions": ["competitors"]},
            False,
        ),  # exclusions never establish one
    ],
)
def test_has_material_targeting(kwargs, material):
    # Reads the list fields only; a transient (unpersisted) IcpDraft is enough.
    assert has_material_targeting(IcpDraft(**kwargs)) is material


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


def _save(factory, business_id, data: IcpDraftInput) -> int:
    with factory() as session:
        draft = IcpStore(session, business_id).save_draft(data)
        session.commit()
        return draft.version


def test_confirm_selects_the_active_version_and_audits(session_factory):
    _save(session_factory, BIZ_A, IcpDraftInput(industries=["retail"], geography=["KA"]))
    with session_factory() as session:
        confirmation = IcpConfirmationStore(session, BIZ_A).confirm(1, ACTOR)
        session.commit()
        assert confirmation.version == 1
        assert confirmation.actor_id == ACTOR.id
        assert confirmation.actor_type == "user"
        assert confirmation.confirmed_at is not None

    with session_factory() as session:
        assert IcpConfirmationStore(session, BIZ_A).active_version() == 1
        # An atomic audit entry was written in the same unit of work.
        audits = session.query(AuditEvent).filter(AuditEvent.business_id == BIZ_A).all()
        assert [a.action for a in audits] == [AUDIT_ACTION_CONFIRM]
        assert audits[0].references["version"] == 1
        assert audits[0].references["icpDraftId"]  # opaque draft row id recorded


def test_unknown_version_cannot_be_confirmed(session_factory):
    _save(session_factory, BIZ_A, IcpDraftInput(industries=["retail"]))
    with session_factory() as session:
        with pytest.raises(IcpConfirmationError) as exc:
            IcpConfirmationStore(session, BIZ_A).confirm(99, ACTOR)
        assert exc.value.code == REASON_DRAFT_VERSION_NOT_FOUND
        # Nothing staged: no confirmation, no audit.
        session.rollback()
    with session_factory() as session:
        assert IcpConfirmationStore(session, BIZ_A).active() is None
        assert session.query(AuditEvent).count() == 0


def test_draft_without_material_targeting_cannot_be_confirmed(session_factory):
    # An empty draft (saved for versioning purposes) is a valid row but not a target.
    _save(session_factory, BIZ_A, IcpDraftInput(exclusions=["x"]))
    with session_factory() as session:
        with pytest.raises(IcpConfirmationError) as exc:
            IcpConfirmationStore(session, BIZ_A).confirm(1, ACTOR)
        assert exc.value.code == REASON_NO_MATERIAL_TARGETING
        session.rollback()
    with session_factory() as session:
        assert IcpConfirmationStore(session, BIZ_A).active() is None


def test_editing_does_not_change_the_active_version(session_factory):
    _save(session_factory, BIZ_A, IcpDraftInput(industries=["retail"]))
    with session_factory() as session:
        IcpConfirmationStore(session, BIZ_A).confirm(1, ACTOR)
        session.commit()
    # Edit = a new draft version; the active pointer is untouched.
    _save(session_factory, BIZ_A, IcpDraftInput(industries=["retail", "wholesale"]))
    with session_factory() as session:
        assert IcpConfirmationStore(session, BIZ_A).active_version() == 1  # still v1
    # Confirming v2 re-points the SAME single row; no second confirmation is inserted.
    with session_factory() as session:
        IcpConfirmationStore(session, BIZ_A).confirm(2, ACTOR)
        session.commit()
    with session_factory() as session:
        assert IcpConfirmationStore(session, BIZ_A).active_version() == 2
        assert (
            session.query(IcpConfirmation).filter(IcpConfirmation.business_id == BIZ_A).count() == 1
        )


def test_confirmation_is_scoped_per_business(session_factory):
    _save(session_factory, BIZ_A, IcpDraftInput(industries=["a"]))
    with session_factory() as session:
        # B has no version 1 of its own — A's version is invisible, so it cannot be confirmed by B.
        with pytest.raises(IcpConfirmationError) as exc:
            IcpConfirmationStore(session, BIZ_B).confirm(1, ACTOR)
        assert exc.value.code == REASON_DRAFT_VERSION_NOT_FOUND


def test_confirm_does_not_commit(session_factory):
    _save(session_factory, BIZ_A, IcpDraftInput(industries=["retail"]))
    with session_factory() as session:
        IcpConfirmationStore(session, BIZ_A).confirm(1, ACTOR)
        session.rollback()  # the store never commits — a rollback leaves nothing behind
    with session_factory() as session:
        assert IcpConfirmationStore(session, BIZ_A).active() is None
        assert session.query(AuditEvent).count() == 0


# --------------------------------------------------------------------------------------------------
# HTTP tests (TestClient) — the real service API
# --------------------------------------------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    app = create_app(make_settings())
    with TestClient(app, raise_server_exceptions=False) as test_client:
        for biz in (BIZ_A, BIZ_B):
            test_client.post("/v1/tenants", json={"businessId": biz}, headers=AUTH)
        yield test_client


def _draft(client, business_id, **overrides) -> int:
    body = {
        "industries": [],
        "sizeBands": [],
        "roles": [],
        "geography": [],
        "exclusions": [],
    }
    body.update(overrides)
    resp = client.post(f"/v1/tenants/{business_id}/icp/drafts", json=body, headers=AUTH)
    assert resp.status_code == 201, resp.text
    return resp.json()["version"]


def _confirm(client, business_id, version, **overrides):
    body = {"version": version, "actorId": ACTOR.id}
    body.update(overrides)
    return client.post(f"/v1/tenants/{business_id}/icp/confirm", json=body, headers=AUTH)


def test_http_confirm_v1_edit_to_v2_v1_stays_active_until_v2_confirmed(client):
    # The headline developer manual-test flow.
    _draft(client, BIZ_A, industries=["retail"], geography=["KA"])  # v1
    resp = _confirm(client, BIZ_A, 1)
    assert resp.status_code == 200
    body = resp.json()
    assert body["active"] is True and body["version"] == 1
    assert body["grantsCampaignAuthority"] is False  # no campaign authority granted

    active = client.get(f"/v1/tenants/{BIZ_A}/icp/active", headers=AUTH).json()
    assert active["version"] == 1 and active["actorId"] == ACTOR.id

    # Edit = a new draft version (v2). The active version does NOT change.
    _draft(client, BIZ_A, industries=["retail", "wholesale"])  # v2
    assert client.get(f"/v1/tenants/{BIZ_A}/icp/active", headers=AUTH).json()["version"] == 1
    listed = client.get(f"/v1/tenants/{BIZ_A}/icp/drafts", headers=AUTH).json()
    assert {row["version"]: row["active"] for row in listed} == {1: True, 2: False}

    # Confirm v2 — now it is active; v1 is superseded.
    assert _confirm(client, BIZ_A, 2).status_code == 200
    assert client.get(f"/v1/tenants/{BIZ_A}/icp/active", headers=AUTH).json()["version"] == 2
    listed = client.get(f"/v1/tenants/{BIZ_A}/icp/drafts", headers=AUTH).json()
    assert {row["version"]: row["active"] for row in listed} == {1: False, 2: True}


def test_http_confirm_unknown_version_is_404_and_changes_nothing(client):
    resp = _confirm(client, BIZ_A, 99)
    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == REASON_DRAFT_VERSION_NOT_FOUND
    assert client.get(f"/v1/tenants/{BIZ_A}/icp/active", headers=AUTH).json()["active"] is False


def test_http_confirm_no_material_targeting_is_422_and_changes_nothing(client):
    _draft(client, BIZ_A, exclusions=["competitors"])  # a draft with no positive targeting
    resp = _confirm(client, BIZ_A, 1)
    assert resp.status_code == 422
    assert resp.json()["error"]["message"] == REASON_NO_MATERIAL_TARGETING
    assert client.get(f"/v1/tenants/{BIZ_A}/icp/active", headers=AUTH).json()["active"] is False


def test_http_unsupported_actor_type_is_422(client):
    _draft(client, BIZ_A, industries=["retail"])
    resp = _confirm(client, BIZ_A, 1, actorType="superadmin")
    assert resp.status_code == 422
    assert resp.json()["error"]["message"] == "unsupported_actor_type"
    assert client.get(f"/v1/tenants/{BIZ_A}/icp/active", headers=AUTH).json()["active"] is False


def test_http_unsupported_field_is_422(client):
    _draft(client, BIZ_A, industries=["retail"])
    resp = client.post(
        f"/v1/tenants/{BIZ_A}/icp/confirm",
        json={"version": 1, "actorId": ACTOR.id, "autoApprove": True},
        headers=AUTH,
    )
    assert resp.status_code == 422


def test_http_actor_id_carrying_contact_data_is_refused(client):
    _draft(client, BIZ_A, industries=["retail"])
    resp = _confirm(client, BIZ_A, 1, actorId="attacker@example.com")
    assert resp.status_code == 422
    assert resp.json()["error"]["message"] == "actor_id_not_opaque"
    assert client.get(f"/v1/tenants/{BIZ_A}/icp/active", headers=AUTH).json()["active"] is False


def test_http_confirm_is_idempotent_on_retry(client):
    from app.idempotency import IDEMPOTENCY_HEADER

    _draft(client, BIZ_A, industries=["retail"])
    keyed = {**AUTH, IDEMPOTENCY_HEADER: "icp-confirm-key-1"}
    first = client.post(
        f"/v1/tenants/{BIZ_A}/icp/confirm",
        json={"version": 1, "actorId": ACTOR.id},
        headers=keyed,
    )
    replay = client.post(
        f"/v1/tenants/{BIZ_A}/icp/confirm",
        json={"version": 1, "actorId": ACTOR.id},
        headers=keyed,
    )
    assert first.status_code == 200
    assert replay.json() == first.json()
    assert replay.headers.get("Idempotent-Replay") == "true"


def test_http_unregistered_tenant_is_404(client):
    resp = _confirm(client, "99999999-9999-9999-9999-999999999999", 1)
    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == "tenant agent not registered"


def test_http_confirmation_is_scoped_per_business(client):
    _draft(client, BIZ_A, industries=["retail"])
    _confirm(client, BIZ_A, 1)
    # B has nothing confirmed and cannot confirm A's version through its own scope.
    assert client.get(f"/v1/tenants/{BIZ_B}/icp/active", headers=AUTH).json()["active"] is False
    assert _confirm(client, BIZ_B, 1).status_code == 404
