"""DCA-094 — the activation-reference HTTP contract.

Two layers, matching the DCA-093 eligibility suite:

- **Always-on route tests** (in-memory SQLite) drive the real contract. Two isolated synthetic
  businesses A and B each privately record an email contact point. A issues a reference for its OWN
  eligible contact; a non-eligible outcome mints nothing; B cannot issue (or verify) against A's hidden
  contact. The issued reference then verifies, and the acceptance failure cases — altered recipient,
  altered channel, expired — are each rejected. With no signing secret the capability is disabled.
- **The same A-vs-B isolation on a disposable PostgreSQL** (``DCA_TEST_DATABASE_URL``) exercises the
  real engine; it skips when no URL is supplied.

Spin up a throwaway Postgres, e.g.:
    docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55490:5432 postgres:16-alpine
    export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55490/dca"
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.activation.activation_reference import recipient_digest
from app.audit.sink import ActorType, AuditActor
from app.db.base import make_engine, make_session_factory
from app.main import create_app
from app.services.contact_point import ContactPointInput, ContactPointStore, PersonInput
from tests.conftest import AUTH, make_settings

BIZ_A = "11111111-1111-1111-1111-1111111111a4"
BIZ_B = "22222222-2222-2222-2222-2222222222b4"
EMAIL_A = "owner@acme-synthetic.example"
EMAIL_B = "owner@globex-synthetic.example"
ACTOR = AuditActor(id="00000000-0000-4000-9000-0000000a0094", type=ActorType.USER)
SECRET = "activation-reference-test-secret"


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


def _body(**overrides) -> dict:
    """A fully-eligible issue request. ``provider`` is the eligibility RULE object (allowed/reason);
    ``providerName`` is the provider IDENTITY string the reference binds — two distinct things."""
    moment = overrides.pop("now", None) or datetime.now(UTC)
    consent_days_ago = overrides.pop("consent_days_ago", 7)
    collected = (
        None
        if consent_days_ago is None
        else (moment - timedelta(days=consent_days_ago)).isoformat()
    )
    body = {
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
            "version": "supp-v1",
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
    body.update(overrides)
    return body


def _issue(client, business_id, body=None, channel="email"):
    return client.post(
        f"/v1/tenants/{business_id}/activation-references/{channel}",
        json=body if body is not None else _body(),
        headers=AUTH,
    )


def _verify(client, business_id, reference, channel="email", expected=None):
    payload: dict = {"reference": reference}
    if expected is not None:
        payload["expected"] = expected
    return client.post(
        f"/v1/tenants/{business_id}/activation-references/{channel}/verify",
        json=payload,
        headers=AUTH,
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


# --------------------------------------------------------------------------------------------------
# Issue: a definitive ELIGIBLE mints a short-lived, fully-bound reference.
# --------------------------------------------------------------------------------------------------


def test_eligible_contact_issues_a_bound_reference(seeded_client):
    resp = _issue(seeded_client, BIZ_A)
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "eligible"
    assert body["reference"]
    assert body["ttlSeconds"] == 900
    assert body["expiresAt"] > body["issuedAt"]
    # The binding fixes the whole activation context; the recipient is a digest, never the raw value.
    binding = body["binding"]
    assert binding["businessId"] == BIZ_A
    assert binding["channel"] == "email"
    assert binding["purpose"] == "marketing_email"
    assert binding["provider"] == "ses"
    assert binding["campaignRevision"] == "rev-7"
    assert binding["permissionVersion"] == "perm-v3"
    assert binding["sourceVersion"] == "src-v2"
    assert binding["recipientDigest"] == recipient_digest(EMAIL_A)
    assert EMAIL_A not in body["reference"]
    # A reference is never a send authorization.
    assert body["activation"] == {
        "eligible": True,
        "authorizesSend": False,
        "note": body["activation"]["note"],
    }


def test_issue_reproduces_with_no_state_change(seeded_client):
    first = _issue(seeded_client, BIZ_A).json()
    second = _issue(seeded_client, BIZ_A).json()
    # The binding reproduces exactly (issuedAt/expiresAt/reference move with the clock).
    assert first["binding"] == second["binding"]
    assert first["outcome"] == second["outcome"] == "eligible"


def test_review_outcome_mints_no_reference(seeded_client):
    # Indeterminate suppression -> REVIEW -> not eligible -> 200 with issued:false, no reference.
    resp = _issue(seeded_client, BIZ_A, _body(suppression_available=False))
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "review"
    assert body["issued"] is False
    assert body["reference"] is None
    assert body["activation"]["eligible"] is False


def test_blocked_outcome_mints_no_reference(seeded_client):
    resp = _issue(seeded_client, BIZ_A, _body(permission_state="none"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "blocked"
    assert body["issued"] is False
    assert body["reference"] is None
    assert "consent_not_in_force" in body["reasons"]


def test_other_business_cannot_issue_for_hidden_contact(seeded_client):
    # B names A's exact address; it is private to A, so B's store resolves nothing -> 404.
    resp = _issue(seeded_client, BIZ_B, _body(contact_point=EMAIL_A))
    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == "contact_point_not_found"


def test_issue_requires_service_auth(seeded_client):
    resp = seeded_client.post(
        f"/v1/tenants/{BIZ_A}/activation-references/email", json=_body()
    )  # no X-Service-Auth
    assert resp.status_code == 401


def test_unwired_channel_is_disabled(seeded_client):
    resp = _issue(seeded_client, BIZ_A, _body(), channel="whatsapp")
    assert resp.status_code == 422
    assert resp.json()["error"]["message"] == "channel_not_enabled"


# --------------------------------------------------------------------------------------------------
# Verify: a valid reference passes; altered / expired / wrong-tenant references are rejected.
# --------------------------------------------------------------------------------------------------


def test_issued_reference_verifies(seeded_client):
    reference = _issue(seeded_client, BIZ_A).json()["reference"]
    resp = _verify(seeded_client, BIZ_A, reference)
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is True
    assert body["binding"]["businessId"] == BIZ_A
    assert body["activation"]["authorizesSend"] is False


def test_verify_against_matching_intended_context(seeded_client):
    reference = _issue(seeded_client, BIZ_A).json()["reference"]
    expected = {"recipient": EMAIL_A, "provider": "ses", "campaignRevision": "rev-7"}
    resp = _verify(seeded_client, BIZ_A, reference, expected=expected)
    assert resp.status_code == 200


def test_altered_recipient_is_rejected(seeded_client):
    reference = _issue(seeded_client, BIZ_A).json()["reference"]
    # The caller intends a DIFFERENT recipient than the reference was minted for.
    resp = _verify(
        seeded_client, BIZ_A, reference, expected={"recipient": "someone-else@acme.example"}
    )
    assert resp.status_code == 422
    assert (
        resp.json()["error"]["message"] == "activation_reference_binding_mismatch:recipient_digest"
    )


def test_altered_channel_is_rejected(seeded_client):
    # A reference minted for email, verified on a different (known) channel path -> channel mismatch.
    # (whatsapp is a known channel so the path normalizes; the signed channel is email.)
    reference = _issue(seeded_client, BIZ_A).json()["reference"]
    resp = _verify(seeded_client, BIZ_A, reference, channel="whatsapp")
    # whatsapp is not WIRED into this endpoint, so the channel gate rejects first (defense in depth).
    assert resp.status_code == 422
    assert resp.json()["error"]["message"] == "channel_not_enabled"


def test_altered_channel_via_expected_is_rejected(seeded_client):
    reference = _issue(seeded_client, BIZ_A).json()["reference"]
    resp = _verify(seeded_client, BIZ_A, reference, expected={"channel": "sms"})
    assert resp.status_code == 422
    assert resp.json()["error"]["message"] == "activation_reference_binding_mismatch:channel"


def test_expired_reference_is_rejected(seeded_client):
    # Mint with a 1-second TTL so it is already expired by the time it is verified.
    app = create_app(_settings(activation_reference_ttl_seconds=1))
    with TestClient(app) as client:
        _register_tenant(client, BIZ_A)
        with app.state.session_factory() as session:
            _seed_contact(session, BIZ_A, EMAIL_A)
            session.commit()
        reference = _issue(client, BIZ_A).json()["reference"]
        import time

        time.sleep(1.1)
        resp = _verify(client, BIZ_A, reference)
        assert resp.status_code == 422
        assert resp.json()["error"]["message"] == "activation_reference_expired"


def test_reference_for_another_tenant_is_rejected(seeded_client):
    # A reference minted in A, presented on B's verify path -> business mismatch (the path pins B).
    reference = _issue(seeded_client, BIZ_A).json()["reference"]
    resp = _verify(seeded_client, BIZ_B, reference)
    assert resp.status_code == 422
    assert resp.json()["error"]["message"] == "activation_reference_binding_mismatch:business_id"


def test_tampered_reference_fails_signature(seeded_client):
    reference = _issue(seeded_client, BIZ_A).json()["reference"]
    version, payload_b64, sig = reference.split(".")
    tampered = f"{version}.{payload_b64[:-1] + ('A' if payload_b64[-1] != 'A' else 'B')}.{sig}"
    resp = _verify(seeded_client, BIZ_A, tampered)
    assert resp.status_code == 422
    assert resp.json()["error"]["message"] == "activation_reference_bad_signature"


# --------------------------------------------------------------------------------------------------
# Fail-closed: no signing secret => the whole capability is disabled.
# --------------------------------------------------------------------------------------------------


def test_disabled_when_no_secret_configured():
    app = create_app(make_settings())  # no activation_reference_secret
    with TestClient(app) as client:
        _register_tenant(client, BIZ_A)
        with app.state.session_factory() as session:
            _seed_contact(session, BIZ_A, EMAIL_A)
            session.commit()
        issue = _issue(client, BIZ_A)
        assert issue.status_code == 422
        assert issue.json()["error"]["message"] == "activation_references_disabled"
        verify = _verify(client, BIZ_A, "dcaar1.x.y")
        assert verify.status_code == 422
        assert verify.json()["error"]["message"] == "activation_references_disabled"


# --------------------------------------------------------------------------------------------------
# The same A-vs-B isolation on a disposable PostgreSQL (skips without DCA_TEST_DATABASE_URL).
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)


@requires_pg
def test_issue_and_isolation_on_real_postgres():
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
            session.commit()

        issued = _issue(client, BIZ_A)
        assert issued.status_code == 200
        reference = issued.json()["reference"]
        assert _verify(client, BIZ_A, reference).status_code == 200

        # B cannot issue for A's hidden contact, nor verify A's reference against its own path.
        assert _issue(client, BIZ_B, _body(contact_point=EMAIL_A)).status_code == 404
        cross = _verify(client, BIZ_B, reference)
        assert cross.status_code == 422
        assert (
            cross.json()["error"]["message"] == "activation_reference_binding_mismatch:business_id"
        )
    engine.dispose()
