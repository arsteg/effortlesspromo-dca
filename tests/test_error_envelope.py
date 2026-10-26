"""DCA-011 — standardized API error envelope.

Every error (validation, deliberate rejection, unexpected exception) returns the same shape with a
stable reason code, a safe message, and a correlation id — and the unexpected path never leaks a
stack trace, exception text, or submitted request data.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.testclient import TestClient

from app.core.errors import CORRELATION_HEADER, INTERNAL_ERROR_MESSAGE, ErrorCode
from app.main import create_app
from tests.conftest import AUTH, make_settings


def _assert_envelope(body: dict, code: str) -> dict:
    assert set(body) == {"error"}, body
    error = body["error"]
    assert error["code"] == code
    assert isinstance(error["message"], str) and error["message"]
    assert isinstance(error["correlationId"], str) and error["correlationId"]
    return error


def test_validation_error_envelope_hides_submitted_values(client):
    # Missing required businessId => standardized 422, field location exposed, value NOT echoed.
    secret = "sekret-value-should-not-appear"
    response = client.post("/v1/tenants", json={"country": secret}, headers=AUTH)
    assert response.status_code == 422
    error = _assert_envelope(response.json(), ErrorCode.VALIDATION_FAILED)
    locs = {f["field"] for f in error["fields"]}
    assert any(loc.endswith("businessId") for loc in locs)
    # PII-safe: the default FastAPI 422 body would echo `input`; ours must not contain the value.
    assert secret not in response.text
    assert response.headers[CORRELATION_HEADER] == error["correlationId"]


def test_http_exception_keeps_safe_detail_as_message(client):
    # A deliberate 404 keeps the route's own safe detail string, under the stable not_found code.
    response = client.post("/v1/tenants/biz-unknown/enable", headers=AUTH)
    assert response.status_code == 404
    error = _assert_envelope(response.json(), ErrorCode.NOT_FOUND)
    assert error["message"] == "tenant agent not registered"


def test_auth_rejection_is_enveloped(client):
    # No X-Service-Auth => 401 in the same envelope (not a bare FastAPI {"detail": ...}).
    response = client.post("/v1/tenants", json={"businessId": "b"})
    assert response.status_code == 401
    _assert_envelope(response.json(), ErrorCode.UNAUTHORIZED)


def test_conflict_envelope_carries_route_reason_code(client):
    # A registered-but-disabled tenant cannot run discovery => the route's 409 (detail=exc.code)
    # is wrapped under the stable conflict code with the domain reason kept as the safe message.
    client.post("/v1/tenants", json={"businessId": "biz-c"}, headers=AUTH)
    response = client.post("/v1/tenants/biz-c/discovery/run", headers=AUTH)
    assert response.status_code == 409
    error = _assert_envelope(response.json(), ErrorCode.CONFLICT)
    assert error["message"] == "tenant_not_enabled"


def test_correlation_id_is_echoed_from_inbound_header(client):
    response = client.post(
        "/v1/tenants/biz-unknown/enable",
        headers={**AUTH, "X-Correlation-Id": "trace-abc-123"},
    )
    assert response.json()["error"]["correlationId"] == "trace-abc-123"
    assert response.headers[CORRELATION_HEADER] == "trace-abc-123"


def test_unexpected_exception_returns_generic_envelope_without_leaking():
    # A controlled, unexpected failure must become a generic 500 envelope — no traceback, no
    # exception text, no leaked local data in the response body.
    app = create_app(make_settings())
    boom = APIRouter()

    @boom.get("/_debug/boom")
    def _boom() -> dict:
        raise RuntimeError("secret=TOPSECRET email=victim@example.com")

    app.include_router(boom)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/_debug/boom", headers={"X-Correlation-Id": "trace-500"})
    assert response.status_code == 500
    error = _assert_envelope(response.json(), ErrorCode.INTERNAL_ERROR)
    assert error["message"] == INTERNAL_ERROR_MESSAGE
    assert error["correlationId"] == "trace-500"
    # The generic path leaks nothing from the exception or its locals.
    for leaked in ("TOPSECRET", "victim@example.com", "Traceback", "RuntimeError"):
        assert leaked not in response.text


def test_success_response_carries_correlation_header(client):
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.headers.get(CORRELATION_HEADER)
