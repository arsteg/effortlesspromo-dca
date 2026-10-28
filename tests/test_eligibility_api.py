"""DCA-093 — the scoped channel-eligibility endpoint.

Two layers, matching the DCA-063 record/evidence suite:

- **Always-on route tests** (in-memory SQLite) drive the real HTTP contract. Two isolated synthetic
  businesses A and B each privately record an email contact point; A's eligibility question resolves
  and is evaluated, while B asking for A's exact address reads as 404 — the headline guarantee that a
  caller cannot evaluate another business's hidden contact. The endpoint folds the binary channel
  decision into eligible / blocked / **review**, and proves REVIEW (and BLOCKED) is never activation
  permission while even ELIGIBLE never authorizes a send. The route is read-only, so a result and its
  denial reproduce with no state change, and every response carries expiring metadata + evidence refs.
- **The same A-vs-B isolation on a disposable PostgreSQL** (``DCA_TEST_DATABASE_URL``) exercises the
  real transaction / RLS engine rather than SQLite; it skips when no URL is supplied.

Spin up a throwaway Postgres, e.g.:
    docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55490:5432 postgres:16-alpine
    export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55490/dca"
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.audit.sink import ActorType, AuditActor
from app.db.base import make_engine, make_session_factory
from app.main import create_app
from app.services.contact_point import ContactPointInput, ContactPointStore, PersonInput
from tests.conftest import AUTH, make_settings

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"
EMAIL_A = "consented@acme-synthetic.example"
EMAIL_B = "consented@globex-synthetic.example"
ACTOR = AuditActor(id="00000000-0000-4000-9000-0000000a0093", type=ActorType.USER)


def _register_tenant(client: TestClient, business_id: str) -> None:
    assert (
        client.post("/v1/tenants", json={"businessId": business_id}, headers=AUTH).status_code
        == 201
    )


def _seed_contact(session, business_id: str, email: str) -> str:
    """Record one tenant-private person + email contact point in ``business_id``; return the CP id."""
    store = ContactPointStore(session, business_id)
    person = store.create_person(PersonInput(display_name="Synthetic Person"), actor=ACTOR)
    cp = store.add_contact_point(
        ContactPointInput(
            person_observation_id=person.id,
            channel="email",
            value=email,
            evidence="self_reported",
        ),
        actor=ACTOR,
    )
    return cp.id


def _body(
    *,
    contact_point: str = EMAIL_A,
    permission_state: str = "granted",
    consent_days_ago: int | None = 7,
    source_permits: bool = True,
    suppression_available: bool = True,
    covers_email: bool = True,
    suppressed: tuple[str, ...] = (),
    provider_allowed: bool = True,
    now: datetime | None = None,
) -> dict:
    """A fully-eligible eligibility question, overridable gate by gate for the failure fixtures."""
    moment = now or datetime.now(UTC)
    collected = (
        None
        if consent_days_ago is None
        else (moment - timedelta(days=consent_days_ago)).isoformat()
    )
    return {
        "contactPoint": contact_point,
        "purpose": "marketing_email",
        "basis": "consent",
        "permissionState": permission_state,
        "permissionScopeChannel": "email",
        "consentCollectedAt": collected,
        "sourceRights": {"permitsEmailContact": source_permits},
        "suppression": {
            "availability": "available" if suppression_available else "unavailable",
            "version": "supp-v1",
            "asOf": moment.isoformat(),
            "coveredChannels": ["email"] if covers_email else [],
            "suppressedContactPoints": list(suppressed),
        },
        "provider": {"allowed": provider_allowed},
    }


@pytest.fixture
def seeded_client():
    """A client with tenants A and B registered, each privately recording its own email contact."""
    app = create_app(make_settings())
    with TestClient(app) as client:
        _register_tenant(client, BIZ_A)
        _register_tenant(client, BIZ_B)
        with app.state.session_factory() as session:
            _seed_contact(session, BIZ_A, EMAIL_A)
            _seed_contact(session, BIZ_B, EMAIL_B)
            session.commit()
        yield client


def _post(client, business_id, body, channel="email"):
    return client.post(f"/v1/tenants/{business_id}/eligibility/{channel}", json=body, headers=AUTH)


# --------------------------------------------------------------------------------------------------
# Headline: tenant isolation — a caller cannot evaluate another business's hidden contact.
# --------------------------------------------------------------------------------------------------


def test_owner_business_evaluates_its_own_contact(seeded_client):
    resp = _post(seeded_client, BIZ_A, _body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["businessId"] == BIZ_A
    assert body["channel"] == "email"
    assert body["contactPoint"] == EMAIL_A
    assert body["outcome"] == "eligible"
    assert body["reasons"] == ["ok"]
    assert body["blockers"] == []
    # ELIGIBLE clears the DCA's blockers (may be offered to the pipeline) but never authorizes a send.
    assert body["activation"]["eligible"] is True
    assert body["activation"]["authorizesSend"] is False
    # Expiring metadata + evidence/version references are present.
    assert body["ttlSeconds"] == 3600
    assert body["expiresAt"] > body["evaluatedAt"]
    assert body["evidence"]["suppressionVersion"] == "supp-v1"
    assert body["evidence"]["permissionState"] == "granted"


def test_other_business_cannot_evaluate_hidden_contact(seeded_client):
    # B asks for A's exact address. It is private to A (no global index on the value), so B's own store
    # resolves nothing and the endpoint reads it as not-found — never confirming it exists elsewhere.
    resp = _post(seeded_client, BIZ_B, _body(contact_point=EMAIL_A))
    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == "contact_point_not_found"


def test_unknown_business_is_not_found(seeded_client):
    resp = _post(seeded_client, "99999999-9999-9999-9999-999999999999", _body())
    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == "tenant_not_registered"


# --------------------------------------------------------------------------------------------------
# REVIEW is not activation permission; neither is BLOCKED.
# --------------------------------------------------------------------------------------------------


def test_indeterminate_suppression_is_review_not_activation(seeded_client):
    # The authoritative suppression view is unavailable -> the gate is indeterminate, so the outcome is
    # REVIEW (defer), never a fabricated allow. REVIEW must NOT be activation permission.
    resp = _post(seeded_client, BIZ_A, _body(suppression_available=False))
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "review"
    assert body["activation"]["eligible"] is False
    assert body["activation"]["authorizesSend"] is False
    # The headline reason is the gate; the actual indeterminate suppression code is in the detail.
    assert "suppression_blocked" in body["reasons"]
    assert body["decisive"]["detail"] == "suppression_lookup_unavailable"


def test_unknown_consent_age_is_review(seeded_client):
    # No consent-collection time supplied -> freshness cannot be confirmed -> indeterminate -> REVIEW.
    resp = _post(seeded_client, BIZ_A, _body(consent_days_ago=None))
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "review"
    assert body["activation"]["eligible"] is False


def test_determinate_deny_is_blocked(seeded_client):
    # No consent in force is a determinate deny (not an indeterminacy) -> BLOCKED, not REVIEW.
    resp = _post(seeded_client, BIZ_A, _body(permission_state="none"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "blocked"
    assert "consent_not_in_force" in body["reasons"]
    assert body["activation"]["eligible"] is False
    assert body["activation"]["authorizesSend"] is False


def test_suppressed_contact_is_blocked(seeded_client):
    resp = _post(seeded_client, BIZ_A, _body(suppressed=(EMAIL_A,)))
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "blocked"
    assert "suppression_blocked" in body["reasons"]
    assert body["activation"]["eligible"] is False


def test_verified_high_fit_without_consent_stays_blocked(seeded_client):
    # Verification / fit are recorded but never qualify an address absent in-force consent.
    body_in = _body(permission_state="none")
    body_in["emailVerified"] = True
    body_in["fitBand"] = "high"
    resp = _post(seeded_client, BIZ_A, body_in)
    body = resp.json()
    assert body["outcome"] == "blocked"
    assert body["recordedContext"] == {"emailVerified": True, "fitBand": "high"}
    assert body["activation"]["eligible"] is False


# --------------------------------------------------------------------------------------------------
# Reproducible, no state change; auth and channel gating.
# --------------------------------------------------------------------------------------------------


def test_result_reproduces_with_no_state_change(seeded_client):
    before = _post(seeded_client, BIZ_A, _body()).json()
    after = _post(seeded_client, BIZ_A, _body()).json()
    # The time-independent decision reproduces exactly (evaluatedAt/expiresAt move with the clock).
    for key in ("outcome", "reasons", "blockers", "activation", "recordedContext"):
        assert before[key] == after[key]


def test_requires_service_auth(seeded_client):
    resp = seeded_client.post(
        f"/v1/tenants/{BIZ_A}/eligibility/email", json=_body()
    )  # no X-Service-Auth
    assert resp.status_code == 401


def test_unwired_channel_is_explicitly_disabled(seeded_client):
    resp = _post(seeded_client, BIZ_A, _body(), channel="whatsapp")
    assert resp.status_code == 422
    assert resp.json()["error"]["message"] == "channel_not_enabled"


def test_unknown_channel_is_not_found(seeded_client):
    resp = _post(seeded_client, BIZ_A, _body(), channel="carrier-pigeon")
    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == "unknown_channel"


# --------------------------------------------------------------------------------------------------
# The same A-vs-B isolation on a disposable PostgreSQL (skips without DCA_TEST_DATABASE_URL).
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)


@requires_pg
def test_isolation_on_real_postgres():
    engine = make_engine(PG_URL)
    from app.db.base import Base

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    app = create_app(make_settings(database_url=PG_URL, auto_create_schema=False))
    app.state.session_factory = factory
    with TestClient(app) as client:
        _register_tenant(client, BIZ_A)
        _register_tenant(client, BIZ_B)
        with factory() as session:
            _seed_contact(session, BIZ_A, EMAIL_A)
            session.commit()

        owner = _post(client, BIZ_A, _body())
        assert owner.status_code == 200
        assert owner.json()["outcome"] == "eligible"

        other = _post(client, BIZ_B, _body(contact_point=EMAIL_A))
        assert other.status_code == 404
        assert other.json()["error"]["message"] == "contact_point_not_found"
    engine.dispose()
