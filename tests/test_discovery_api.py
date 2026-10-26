"""End-to-end service flow: register → enable → ICP → discovery → candidates → accept/dismiss.
Also proves the compliance screens, dedupe, tenant isolation, and the disabled-by-default gates."""

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import AUTH, make_settings

BIZ = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


def setup_tenant(client, business_id=BIZ, industries=None):
    assert (
        client.post("/v1/tenants", json={"businessId": business_id}, headers=AUTH).status_code
        == 201
    )
    assert client.post(f"/v1/tenants/{business_id}/enable", headers=AUTH).status_code == 200
    body = {"industries": industries or [], "regions": [], "roles": []}
    assert client.put(f"/v1/tenants/{business_id}/icp", json=body, headers=AUTH).status_code == 200


def test_non_india_registration_rejected(client):
    response = client.post("/v1/tenants", json={"businessId": BIZ, "country": "US"}, headers=AUTH)
    assert response.status_code == 422
    assert "country_not_allowed" in response.json()["error"]["message"]


def test_discovery_blocked_until_enabled_and_icp(client):
    client.post("/v1/tenants", json={"businessId": BIZ}, headers=AUTH)
    response = client.post(f"/v1/tenants/{BIZ}/discovery/run", headers=AUTH)
    assert response.status_code == 409
    assert response.json()["error"]["message"] == "tenant_not_enabled"

    client.post(f"/v1/tenants/{BIZ}/enable", headers=AUTH)
    response = client.post(f"/v1/tenants/{BIZ}/discovery/run", headers=AUTH)
    assert response.status_code == 409
    assert response.json()["error"]["message"] == "icp_missing"


def test_discovery_blocked_when_no_sources_enabled():
    app = create_app(make_settings(synthetic_sources_enabled=False))
    with TestClient(app) as client:
        setup_tenant(client)
        response = client.post(f"/v1/tenants/{BIZ}/discovery/run", headers=AUTH)
        assert response.status_code == 409
        assert response.json()["error"]["message"] == "no_sources_enabled"


def test_discovery_screens_dedupes_and_suggests(client):
    setup_tenant(client)
    first = client.post(f"/v1/tenants/{BIZ}/discovery/run", headers=AUTH).json()
    # Fixtures: 5 raw; 2 must be screened out (personal_email contact, missing org identifier).
    assert first["found"] == 5
    assert first["screenedOut"] == 2
    assert first["suggested"] == 3
    assert first["sources"] == ["synthetic.mca"]

    candidates = client.get(f"/v1/tenants/{BIZ}/candidates", headers=AUTH).json()
    assert len(candidates) == 3
    assert all(c["status"] == "suggested" for c in candidates)
    assert all(c["trustTier"] == "official_registry" for c in candidates)
    assert all(c["sourceRef"].startswith("synthetic://") for c in candidates)

    # Idempotent re-run: everything previously suggested is now a duplicate, nothing new appears.
    second = client.post(f"/v1/tenants/{BIZ}/discovery/run", headers=AUTH).json()
    assert second["suggested"] == 0
    assert second["screenedOut"] == 5
    assert len(client.get(f"/v1/tenants/{BIZ}/candidates", headers=AUTH).json()) == 3


def test_icp_filters_narrow_the_sweep(client):
    setup_tenant(client, industries=["software"])
    run = client.post(f"/v1/tenants/{BIZ}/discovery/run", headers=AUTH).json()
    assert run["found"] == 1
    assert run["suggested"] == 1


def test_accept_and_dismiss_lifecycle(client):
    setup_tenant(client)
    client.post(f"/v1/tenants/{BIZ}/discovery/run", headers=AUTH)
    candidate = client.get(f"/v1/tenants/{BIZ}/candidates", headers=AUTH).json()[0]

    accepted = client.post(
        f"/v1/tenants/{BIZ}/candidates/{candidate['id']}/accept", headers=AUTH
    ).json()
    assert accepted["status"] == "accepted"
    assert accepted["consent"] is False  # acceptance is never recipient consent

    # Double-accept conflicts; dismiss after accept is allowed.
    assert (
        client.post(
            f"/v1/tenants/{BIZ}/candidates/{candidate['id']}/accept", headers=AUTH
        ).status_code
        == 409
    )
    assert (
        client.post(f"/v1/tenants/{BIZ}/candidates/{candidate['id']}/dismiss", headers=AUTH).json()[
            "status"
        ]
        == "dismissed"
    )


def test_tenant_isolation_candidate_ids_do_not_cross(client):
    setup_tenant(client)
    client.post(f"/v1/tenants/{BIZ}/discovery/run", headers=AUTH)
    candidate = client.get(f"/v1/tenants/{BIZ}/candidates", headers=AUTH).json()[0]

    setup_tenant(client, business_id=OTHER)
    # OTHER presenting BIZ's candidate id gets 404 — never a read or mutation across tenants.
    response = client.post(f"/v1/tenants/{OTHER}/candidates/{candidate['id']}/accept", headers=AUTH)
    assert response.status_code == 404
    assert client.get(f"/v1/tenants/{OTHER}/candidates", headers=AUTH).json() == []


def test_runs_are_recorded(client):
    setup_tenant(client)
    client.post(f"/v1/tenants/{BIZ}/discovery/run", headers=AUTH)
    runs = client.get(f"/v1/tenants/{BIZ}/runs", headers=AUTH).json()
    assert len(runs) == 1
    assert runs[0]["status"] == "completed"
