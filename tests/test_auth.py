from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import AUTH, make_settings


def test_no_token_configured_fails_closed():
    app = create_app(make_settings(service_auth_token=None))
    with TestClient(app) as client:
        response = client.post("/v1/tenants", json={"businessId": "b1"})
        assert response.status_code == 401
        # Even presenting a token is rejected when none is configured (fail-closed, never open).
        response = client.post("/v1/tenants", json={"businessId": "b1"}, headers=AUTH)
        assert response.status_code == 401


def test_wrong_and_missing_token_rejected(client):
    assert client.post("/v1/tenants", json={"businessId": "b1"}).status_code == 401
    assert (
        client.post(
            "/v1/tenants", json={"businessId": "b1"}, headers={"X-Service-Auth": "wrong"}
        ).status_code
        == 401
    )


def test_correct_token_accepted(client):
    response = client.post("/v1/tenants", json={"businessId": "b1"}, headers=AUTH)
    assert response.status_code == 201


def test_health_is_unauthenticated(client):
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200
