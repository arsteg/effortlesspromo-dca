"""DCA-019 — operation permissions across business and platform planes.

Explicit scopes, no universal-admin bypass: an ordinary business user cannot approve sources (an
administrator scope); a platform operator — though globally privileged — cannot perform a
client-business action because it holds no membership in that business; the two scope planes are
disjoint; and every authorization decision is audited. The probe routes are read-only, so each
denial is reproduced with no side effect.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.identity.fake import BUSINESS_A, BUSINESS_B, USER_ALICE, USER_BOB, USER_CAROL
from app.identity.permissions import (
    Scope,
    is_platform_operator,
    scopes_for_business_roles,
    scopes_for_platform_roles,
)
from app.main import create_app
from tests.conftest import make_settings

ISSUER = "https://issuer.dca.test"
AUDIENCE = "dca-api"
SECRET = "dca-test-signing-secret-value"  # synthetic HS256 key, not a real credential
OPERATOR = "00000000-0000-4000-9000-00000090e701"  # platform operator; no business membership


def _auth_settings(**overrides) -> Settings:
    base = {
        "auth_token_issuer": ISSUER,
        "auth_token_audience": AUDIENCE,
        "auth_token_signing_secret": SECRET,
    }
    base.update(overrides)
    return make_settings(**base)


def _mint(*, subject: str, roles: list[str] | None = None) -> str:
    now = datetime.now(UTC)
    claims: dict = {
        "sub": subject,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    if roles is not None:
        claims["roles"] = roles
    return jwt.encode(claims, SECRET, algorithm="HS256")


def _client(settings: Settings | None = None) -> TestClient:
    return TestClient(create_app(settings or _auth_settings()))


def _hdrs(token: str, business_id: str | None = None) -> dict[str, str]:
    h = {"Authorization": f"Bearer {token}"}
    if business_id is not None:
        h["X-Business-Id"] = business_id
    return h


# --- pure scope model -----------------------------------------------------------------------------


def test_administrator_roles_grant_sensitive_scopes():
    owner = scopes_for_business_roles(("owner", "campaign.admin"))  # unknown role contributes none
    assert Scope.SOURCE_APPROVE in owner
    assert Scope.POLICY_APPROVE in owner
    assert Scope.BUDGET_MANAGE in owner


def test_ordinary_user_role_lacks_sensitive_scopes():
    viewer = scopes_for_business_roles(("viewer",))
    assert Scope.CANDIDATE_READ in viewer
    assert Scope.SOURCE_APPROVE not in viewer
    assert Scope.BUDGET_MANAGE not in viewer


def test_unknown_role_grants_nothing():
    assert scopes_for_business_roles(("not-a-real-role",)) == frozenset()


def test_planes_are_disjoint():
    # A business administrator never gets a platform scope...
    assert scopes_for_platform_roles(("owner", "admin")) == frozenset()
    # ...and a platform operator never gets a business scope.
    assert scopes_for_business_roles(("platform_operator",)) == frozenset()
    assert is_platform_operator(("platform_operator",)) is True
    assert is_platform_operator(("owner",)) is False


# --- business-plane route: approve a source (administrator scope) ----------------------------------


def test_business_admin_is_authorized_for_source_approval():
    with _client() as client:
        resp = client.get(
            "/v1/authz/business/source-approval",
            headers=_hdrs(_mint(subject=USER_ALICE), BUSINESS_A),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["authorized"] is True
    assert body["businessId"] == BUSINESS_A


def test_ordinary_user_cannot_approve_sources():
    # Carol is an active viewer in A — a real member, but without the administrator scope.
    with _client() as client:
        resp = client.get(
            "/v1/authz/business/source-approval",
            headers=_hdrs(_mint(subject=USER_CAROL), BUSINESS_A),
        )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_platform_operator_cannot_perform_a_client_business_action():
    # Global operator rights do NOT imply membership: no active membership in A => denied before the
    # scope check even runs. No universal bypass, no impersonation.
    token = _mint(subject=OPERATOR, roles=["platform_operator"])
    with _client() as client:
        resp = client.get("/v1/authz/business/source-approval", headers=_hdrs(token, BUSINESS_A))
    assert resp.status_code == 403


def test_business_admin_only_in_own_business():
    # Alice is an admin in A but her B membership is expired => approval in B is denied.
    with _client() as client:
        resp = client.get(
            "/v1/authz/business/source-approval",
            headers=_hdrs(_mint(subject=USER_ALICE), BUSINESS_B),
        )
    assert resp.status_code == 403


def test_business_route_requires_authentication():
    with _client() as client:
        assert (
            client.get(
                "/v1/authz/business/source-approval", headers={"X-Business-Id": BUSINESS_A}
            ).status_code
            == 401
        )


# --- platform-plane route: read platform ops -------------------------------------------------------


def test_platform_operator_is_authorized_for_platform_ops():
    token = _mint(subject=OPERATOR, roles=["platform_operator"])
    with _client() as client:
        resp = client.get("/v1/authz/platform/ops", headers=_hdrs(token))
    assert resp.status_code == 200
    assert resp.json()["scope"] == Scope.PLATFORM_OPS_READ.value


def test_business_admin_is_not_a_platform_operator():
    # A business owner/admin holds no platform scope — being an admin is not a platform role.
    with _client() as client:
        resp = client.get(
            "/v1/authz/platform/ops", headers=_hdrs(_mint(subject=USER_BOB, roles=["admin"]))
        )
    assert resp.status_code == 403


def test_platform_route_ignores_a_claimed_header_and_requires_auth():
    with _client() as client:
        # A bare "admin" header must not authorize anything.
        assert client.get("/v1/authz/platform/ops", headers={"X-Admin": "true"}).status_code == 401


# --- authorization decisions are audited ----------------------------------------------------------


@pytest.fixture
def authz_records():
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture()
    logger = logging.getLogger("dca.authz")
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)


def test_platform_grant_is_audited(authz_records):
    token = _mint(subject=OPERATOR, roles=["platform_operator"])
    with _client() as client:
        client.get("/v1/authz/platform/ops", headers=_hdrs(token))
    grants = [r for r in authz_records if getattr(r, "authz_outcome", None) == "granted"]
    assert any(
        r.authz_plane == "platform" and r.authz_scope == Scope.PLATFORM_OPS_READ.value
        for r in grants
    )


def test_business_denial_is_audited(authz_records):
    with _client() as client:
        client.get(
            "/v1/authz/business/source-approval",
            headers=_hdrs(_mint(subject=USER_CAROL), BUSINESS_A),
        )
    denials = [r for r in authz_records if getattr(r, "authz_outcome", None) == "denied"]
    assert any(
        r.authz_plane == "business"
        and r.authz_scope == Scope.SOURCE_APPROVE.value
        and r.authz_subject == USER_CAROL
        for r in denials
    )
