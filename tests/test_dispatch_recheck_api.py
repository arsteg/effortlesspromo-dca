"""DCA-095 — the dispatch-recheck (consume/recheck) HTTP contract.

Two layers, matching the DCA-093/094 suites:

- **Always-on route tests** (in-memory SQLite) drive the real contract end-to-end. Two isolated
  synthetic businesses A and B each privately record an email contact point. A issues a DCA-094
  reference while clear, then re-checks it immediately before dispatch: a still-clear recheck grants
  and CONSUMES one slot; a retry under the same key replays the one grant; a withdrawal recorded since
  issuance (permission flip OR a new suppression entry) BLOCKS with no consumption; an unavailable
  authority is REVIEW and fails closed; a version-drifted / expired / tampered / wrong-tenant reference
  is rejected. With no signing secret the capability is disabled.
- **The same consume-once + withdrawal behaviour on a disposable PostgreSQL** (``DCA_TEST_DATABASE_URL``)
  exercises the real engine and the unique-constraint serialisation; it skips when no URL is supplied.

Spin up a throwaway Postgres, e.g.:
    docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55490:5432 postgres:16-alpine
    export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55490/dca"
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.audit.sink import ActorType, AuditActor
from app.db.base import make_engine, make_session_factory
from app.db.models import DispatchRecheckConsumption
from app.main import create_app
from app.services.contact_point import ContactPointInput, ContactPointStore, PersonInput
from tests.conftest import AUTH, make_settings

BIZ_A = "11111111-1111-1111-1111-1111111111a5"
BIZ_B = "22222222-2222-2222-2222-2222222222b5"
EMAIL_A = "owner@acme-synthetic.example"
EMAIL_B = "owner@globex-synthetic.example"
ACTOR = AuditActor(id="00000000-0000-4000-9000-0000000a0095", type=ActorType.USER)
SECRET = "dispatch-recheck-test-secret"


def _settings(**overrides):
    base = {"activation_reference_secret": SECRET}
    base.update(overrides)
    return make_settings(**base)


def _register_tenant(client: TestClient, business_id: str) -> None:
    assert (
        client.post("/v1/tenants", json={"businessId": business_id}, headers=AUTH).status_code
        == 201
    )


def _seed_contact(session, business_id: str, email: str) -> str:
    store = ContactPointStore(session, business_id)
    person = store.create_person(PersonInput(display_name="Synthetic Person"), actor=ACTOR)
    cp = store.add_contact_point(
        ContactPointInput(
            person_observation_id=person.id, channel="email", value=email, evidence="self_reported"
        ),
        actor=ACTOR,
    )
    return cp.id


def _context(**overrides) -> dict:
    """A fully-eligible gate context (shared by issue + recheck). ``provider`` is the eligibility RULE
    object; ``providerName`` is the provider IDENTITY the reference binds — two distinct things."""
    moment = overrides.pop("now", None) or datetime.now(UTC)
    consent_days_ago = overrides.pop("consent_days_ago", 7)
    collected = (
        None
        if consent_days_ago is None
        else (moment - timedelta(days=consent_days_ago)).isoformat()
    )
    return {
        "contactPoint": overrides.pop("contact_point", EMAIL_A),
        "purpose": "marketing_email",
        "basis": "consent",
        "permissionState": overrides.pop("permission_state", "granted"),
        "permissionScopeChannel": "email",
        "consentCollectedAt": collected,
        "sourceRights": {"permitsEmailContact": True},
        "suppression": {
            "availability": (
                "available" if overrides.pop("suppression_available", True) else "unavailable"
            ),
            "version": overrides.pop("suppression_version", "supp-v1"),
            "asOf": moment.isoformat(),
            "coveredChannels": ["email"],
            "suppressedContactPoints": list(overrides.pop("suppressed", ())),
        },
        "provider": {"allowed": True, "reason": ""},
        "providerName": overrides.pop("provider", "ses"),
        "campaignRevision": overrides.pop("campaign_revision", "rev-7"),
        "permissionVersion": overrides.pop("permission_version", "perm-v3"),
        "sourceVersion": overrides.pop("source_version", "src-v2"),
    }


def _issue(client, business_id, context=None, channel="email"):
    return client.post(
        f"/v1/tenants/{business_id}/activation-references/{channel}",
        json=context if context is not None else _context(),
        headers=AUTH,
    )


def _issued_reference(client, business_id=BIZ_A) -> str:
    resp = _issue(client, business_id)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["issued"] is True
    return body["reference"]


def _recheck(client, business_id, reference, idem_key, context=None, channel="email"):
    payload = dict(context if context is not None else _context())
    payload["reference"] = reference
    payload["idempotencyKey"] = idem_key
    return client.post(
        f"/v1/tenants/{business_id}/dispatch-recheck/{channel}", json=payload, headers=AUTH
    )


@pytest.fixture
def seeded_client():
    app = create_app(_settings())
    with TestClient(app) as client:
        _register_tenant(client, BIZ_A)
        _register_tenant(client, BIZ_B)
        with app.state.session_factory() as session:
            _seed_contact(session, BIZ_A, EMAIL_A)
            _seed_contact(session, BIZ_B, EMAIL_B)
            session.commit()
        yield client


def _consumption_count(app, business_id) -> int:
    with app.state.session_factory() as session:
        return session.scalar(
            select(func.count())
            .select_from(DispatchRecheckConsumption)
            .where(DispatchRecheckConsumption.business_id == business_id)
        )


# --------------------------------------------------------------------------------------------------
# Grant + idempotent consumption.
# --------------------------------------------------------------------------------------------------


def test_still_clear_recheck_grants_and_consumes_one_slot(seeded_client):
    reference = _issued_reference(seeded_client)
    resp = _recheck(seeded_client, BIZ_A, reference, "dispatch-1")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dispatch"] == {
        "allowed": True,
        "status": "allowed",
        "reason": "dispatch_recheck_cleared",
        "authorizesSend": False,
    }
    assert body["consumption"]["consumed"] is True
    assert body["consumption"]["replay"] is False
    assert body["recheck"]["referenceValid"] is True
    # A grant is never a send authorization and the recipient is bound as a digest, not the raw value.
    assert body["activation"]["authorizesSend"] is False
    assert EMAIL_A not in resp.text
    assert _consumption_count(seeded_client.app, BIZ_A) == 1


def test_retry_same_key_replays_the_one_grant(seeded_client):
    reference = _issued_reference(seeded_client)
    first = _recheck(seeded_client, BIZ_A, reference, "dispatch-1").json()
    second = _recheck(seeded_client, BIZ_A, reference, "dispatch-1").json()
    assert first["consumption"]["replay"] is False
    assert second["consumption"]["replay"] is True
    assert second["dispatch"]["allowed"] is True
    # Exactly-once: the retry re-serves the SAME consumption row, it does not record a second.
    assert first["consumption"]["consumptionId"] == second["consumption"]["consumptionId"]
    assert _consumption_count(seeded_client.app, BIZ_A) == 1


def test_reused_key_for_a_different_dispatch_is_a_conflict(seeded_client):
    ref1 = _issued_reference(seeded_client)
    assert _recheck(seeded_client, BIZ_A, ref1, "dispatch-1").status_code == 200
    # A different reference (a genuinely different dispatch) presented under the SAME key -> 409.
    ref2 = _issued_reference(seeded_client)
    # ref2 differs from ref1 (iat moves with the clock); if the clock is identical, force a difference.
    if ref2 == ref1:
        ref2 = _issue(seeded_client, BIZ_A, _context(campaign_revision="rev-8")).json()["reference"]
    resp = _recheck(seeded_client, BIZ_A, ref2, "dispatch-1")
    assert resp.status_code == 409
    assert resp.json()["error"]["message"] == "dispatch_recheck_idempotency_key_reused"


# --------------------------------------------------------------------------------------------------
# Acceptance: a withdrawal after issuance BLOCKS dispatch; unavailable authority fails closed.
# Each denial consumes nothing (no state change).
# --------------------------------------------------------------------------------------------------


def test_withdrawal_via_permission_blocks_dispatch(seeded_client):
    reference = _issued_reference(seeded_client)
    # The reference was minted while GRANTED; the subject withdraws before the host dispatches.
    resp = _recheck(
        seeded_client, BIZ_A, reference, "dispatch-1", _context(permission_state="withdrawn")
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dispatch"]["allowed"] is False
    assert body["dispatch"]["status"] == "blocked"
    assert body["recheck"]["referenceValid"] is True
    assert "consent_not_in_force" in body["recheck"]["reasons"]
    assert body["consumption"]["consumed"] is False
    # Fail-closed with NO state change: nothing was consumed.
    assert _consumption_count(seeded_client.app, BIZ_A) == 0


def test_withdrawal_via_suppression_blocks_dispatch(seeded_client):
    reference = _issued_reference(seeded_client)
    # An unsubscribe recorded since issuance now lists the contact in the authoritative snapshot.
    resp = _recheck(seeded_client, BIZ_A, reference, "dispatch-1", _context(suppressed=(EMAIL_A,)))
    body = resp.json()
    assert body["dispatch"]["allowed"] is False
    assert body["dispatch"]["status"] == "blocked"
    assert "suppression_blocked" in body["recheck"]["reasons"]
    assert _consumption_count(seeded_client.app, BIZ_A) == 0


def test_unavailable_authority_fails_closed_as_review(seeded_client):
    reference = _issued_reference(seeded_client)
    resp = _recheck(
        seeded_client, BIZ_A, reference, "dispatch-1", _context(suppression_available=False)
    )
    body = resp.json()
    assert body["dispatch"]["allowed"] is False
    assert body["dispatch"]["status"] == "review"
    assert _consumption_count(seeded_client.app, BIZ_A) == 0


def test_denial_does_not_consume_the_slot_so_a_fresh_recheck_still_grants(seeded_client):
    reference = _issued_reference(seeded_client)
    # First dispatch attempt is blocked by a withdrawal -> no slot consumed.
    blocked = _recheck(
        seeded_client, BIZ_A, reference, "dispatch-1", _context(permission_state="withdrawn")
    ).json()
    assert blocked["dispatch"]["allowed"] is False
    assert _consumption_count(seeded_client.app, BIZ_A) == 0
    # The withdrawal is lifted and the host re-checks (same key, since nothing was consumed): it grants.
    cleared = _recheck(seeded_client, BIZ_A, reference, "dispatch-1").json()
    assert cleared["dispatch"]["allowed"] is True
    assert cleared["consumption"]["replay"] is False
    assert _consumption_count(seeded_client.app, BIZ_A) == 1


# --------------------------------------------------------------------------------------------------
# Version-drift / tamper / expiry / tenant: the reference is rejected, nothing consumed.
# --------------------------------------------------------------------------------------------------


def test_version_drift_rejects_the_reference(seeded_client):
    reference = _issued_reference(seeded_client)  # bound to perm-v3
    # The host now holds a NEWER permission version than the reference was minted under -> stale.
    resp = _recheck(
        seeded_client, BIZ_A, reference, "dispatch-1", _context(permission_version="perm-v4")
    )
    body = resp.json()
    assert body["dispatch"]["allowed"] is False
    assert body["dispatch"]["status"] == "reference_rejected"
    assert body["recheck"]["referenceValid"] is False
    assert (
        body["recheck"]["referenceReason"]
        == "activation_reference_binding_mismatch:permission_version"
    )
    assert _consumption_count(seeded_client.app, BIZ_A) == 0


def test_source_version_drift_rejects_the_reference(seeded_client):
    reference = _issued_reference(seeded_client)
    resp = _recheck(
        seeded_client, BIZ_A, reference, "dispatch-1", _context(source_version="src-v9")
    )
    body = resp.json()
    assert body["recheck"]["referenceReason"] == (
        "activation_reference_binding_mismatch:source_version"
    )
    assert _consumption_count(seeded_client.app, BIZ_A) == 0


def test_expired_reference_is_rejected():
    app = create_app(_settings(activation_reference_ttl_seconds=1))
    with TestClient(app) as client:
        _register_tenant(client, BIZ_A)
        with app.state.session_factory() as session:
            _seed_contact(session, BIZ_A, EMAIL_A)
            session.commit()
        reference = _issued_reference(client)
        import time

        time.sleep(1.1)
        resp = _recheck(client, BIZ_A, reference, "dispatch-1")
        body = resp.json()
        assert body["dispatch"]["allowed"] is False
        assert body["recheck"]["referenceReason"] == "activation_reference_expired"
        assert _consumption_count(app, BIZ_A) == 0


def test_tampered_reference_fails_signature(seeded_client):
    reference = _issued_reference(seeded_client)
    version, payload_b64, sig = reference.split(".")
    tampered = f"{version}.{payload_b64[:-1] + ('A' if payload_b64[-1] != 'A' else 'B')}.{sig}"
    resp = _recheck(seeded_client, BIZ_A, tampered, "dispatch-1")
    body = resp.json()
    assert body["dispatch"]["allowed"] is False
    assert body["recheck"]["referenceReason"] == "activation_reference_bad_signature"
    assert _consumption_count(seeded_client.app, BIZ_A) == 0


def test_other_business_cannot_recheck_hidden_contact(seeded_client):
    reference = _issued_reference(seeded_client, BIZ_A)
    # B names A's exact address; it is private to A, so B's store resolves nothing -> 404.
    resp = _recheck(seeded_client, BIZ_B, reference, "dispatch-1", _context(contact_point=EMAIL_A))
    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == "contact_point_not_found"


def test_reference_for_another_tenant_is_rejected(seeded_client):
    # A reference minted in A, presented on B's recheck path for B's OWN contact -> tenant mismatch.
    reference = _issued_reference(seeded_client, BIZ_A)
    resp = _recheck(seeded_client, BIZ_B, reference, "dispatch-1", _context(contact_point=EMAIL_B))
    body = resp.json()
    assert body["dispatch"]["allowed"] is False
    assert body["recheck"]["referenceReason"] == "activation_reference_binding_mismatch:business_id"
    assert _consumption_count(seeded_client.app, BIZ_B) == 0


# --------------------------------------------------------------------------------------------------
# Auth / disabled / wiring.
# --------------------------------------------------------------------------------------------------


def test_recheck_requires_service_auth(seeded_client):
    reference = _issued_reference(seeded_client)
    resp = seeded_client.post(
        f"/v1/tenants/{BIZ_A}/dispatch-recheck/email",
        json={**_context(), "reference": reference, "idempotencyKey": "dispatch-1"},
    )  # no X-Service-Auth
    assert resp.status_code == 401


def test_unwired_channel_is_disabled(seeded_client):
    reference = _issued_reference(seeded_client)
    resp = _recheck(seeded_client, BIZ_A, reference, "dispatch-1", channel="whatsapp")
    assert resp.status_code == 422
    assert resp.json()["error"]["message"] == "channel_not_enabled"


def test_disabled_when_no_secret_configured():
    app = create_app(make_settings())  # no activation_reference_secret
    with TestClient(app) as client:
        _register_tenant(client, BIZ_A)
        with app.state.session_factory() as session:
            _seed_contact(session, BIZ_A, EMAIL_A)
            session.commit()
        resp = _recheck(client, BIZ_A, "dcaar1.x.y", "dispatch-1")
        assert resp.status_code == 422
        assert resp.json()["error"]["message"] == "dispatch_recheck_disabled"


# --------------------------------------------------------------------------------------------------
# The same consume-once + withdrawal behaviour on a disposable PostgreSQL (skips without a URL).
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)


@requires_pg
def test_consume_once_and_withdrawal_on_real_postgres():
    engine = make_engine(PG_URL)
    from app.db.base import Base

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    app = create_app(_settings(database_url=PG_URL, auto_create_schema=False))
    app.state.session_factory = factory
    with TestClient(app) as client:
        _register_tenant(client, BIZ_A)
        _register_tenant(client, BIZ_B)
        with factory() as session:
            _seed_contact(session, BIZ_A, EMAIL_A)
            _seed_contact(session, BIZ_B, EMAIL_B)
            session.commit()

        reference = _issued_reference(client, BIZ_A)

        # Grant consumes exactly one slot; a retry replays it (consume-once over the real engine).
        first = _recheck(client, BIZ_A, reference, "dispatch-1").json()
        second = _recheck(client, BIZ_A, reference, "dispatch-1").json()
        assert first["consumption"]["replay"] is False
        assert second["consumption"]["replay"] is True
        with factory() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(DispatchRecheckConsumption)
                    .where(DispatchRecheckConsumption.business_id == BIZ_A)
                )
                == 1
            )

        # A withdrawal under a FRESH key blocks dispatch and consumes nothing.
        blocked = _recheck(
            client, BIZ_A, reference, "dispatch-2", _context(suppressed=(EMAIL_A,))
        ).json()
        assert blocked["dispatch"]["allowed"] is False
        with factory() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(DispatchRecheckConsumption)
                    .where(DispatchRecheckConsumption.business_id == BIZ_A)
                )
                == 1
            )  # still only the one grant from dispatch-1

        # B cannot recheck A's hidden contact, nor consume against A's reference.
        assert (
            _recheck(
                client, BIZ_B, reference, "dispatch-1", _context(contact_point=EMAIL_A)
            ).status_code
            == 404
        )
    engine.dispose()
