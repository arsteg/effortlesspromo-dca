"""DCA-016 — resolve an immutable tenant context only after membership verification.

An authenticated A-only subject can resolve Business A but NOT Business B; a missing scope and a
disabled contract both fail closed; the resolved context is immutable and carries the *membership's*
roles, never the bearer token's self-asserted claims. The diagnostic route is read-only, so every
denial is reproduced with no side effect.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.identity.fake import (
    BUSINESS_A,
    BUSINESS_B,
    USER_ALICE,
    USER_BOB,
    FakeIdentityClient,
)
from app.identity.tenant_context import (
    TenantContext,
    TenantResolutionError,
    resolve_tenant_context,
)
from app.identity.tokens import AuthenticatedPrincipal
from app.main import create_app
from tests.conftest import make_settings

ISSUER = "https://issuer.dca.test"
AUDIENCE = "dca-api"
SECRET = "dca-test-signing-secret-value"  # synthetic HS256 key, not a real credential


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


def _principal(subject: str, roles: tuple[str, ...] = ()) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(subject=subject, roles=roles)


# --- pure resolver (no HTTP) ----------------------------------------------------------------------


def test_active_membership_resolves_immutable_context():
    ctx = resolve_tenant_context(
        principal=_principal(USER_ALICE),
        requested_business_id=BUSINESS_A,
        identity_client=FakeIdentityClient(),
    )
    assert isinstance(ctx, TenantContext)
    assert ctx.subject == USER_ALICE
    assert ctx.business_id == BUSINESS_A
    # roles come from the membership record, not from anything the caller asserted.
    assert ctx.roles == ("owner", "campaign.admin")
    assert ctx.membership_version


def test_context_is_frozen():
    ctx = resolve_tenant_context(
        principal=_principal(USER_ALICE),
        requested_business_id=BUSINESS_A,
        identity_client=FakeIdentityClient(),
    )
    with pytest.raises(ValidationError):  # pydantic frozen model rejects any mutation
        ctx.business_id = BUSINESS_B  # type: ignore[misc]


def test_a_only_subject_cannot_select_business_b():
    # Alice is active in A, expired in B => B must be denied (the core acceptance criterion).
    with pytest.raises(TenantResolutionError) as exc:
        resolve_tenant_context(
            principal=_principal(USER_ALICE),
            requested_business_id=BUSINESS_B,
            identity_client=FakeIdentityClient(),
        )
    assert exc.value.scope_supplied is True


def test_revoked_membership_denied():
    # Bob's A link is revoked.
    with pytest.raises(TenantResolutionError):
        resolve_tenant_context(
            principal=_principal(USER_BOB),
            requested_business_id=BUSINESS_A,
            identity_client=FakeIdentityClient(),
        )


def test_unknown_membership_denied():
    with pytest.raises(TenantResolutionError):
        resolve_tenant_context(
            principal=_principal("stranger"),
            requested_business_id=BUSINESS_A,
            identity_client=FakeIdentityClient(),
        )


def test_missing_scope_fails_closed():
    for empty in (None, "", "   "):
        with pytest.raises(TenantResolutionError) as exc:
            resolve_tenant_context(
                principal=_principal(USER_ALICE),
                requested_business_id=empty,
                identity_client=FakeIdentityClient(),
            )
        assert exc.value.scope_supplied is False


# --- the protected route end to end ---------------------------------------------------------------


def _client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def _headers(token: str, business_id: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if business_id is not None:
        headers["X-Business-Id"] = business_id
    return headers


def test_route_resolves_authorized_business():
    with _client(_auth_settings()) as client:
        resp = client.get(
            "/v1/tenant-context", headers=_headers(_mint(subject=USER_ALICE), BUSINESS_A)
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["resolved"] is True
    assert body["subject"] == USER_ALICE
    assert body["businessId"] == BUSINESS_A
    assert body["roles"] == ["owner", "campaign.admin"]


def test_route_denies_unauthorized_business():
    with _client(_auth_settings()) as client:
        resp = client.get(
            "/v1/tenant-context", headers=_headers(_mint(subject=USER_ALICE), BUSINESS_B)
        )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    # Generic message only — never reveals that the link exists-but-expired vs does-not-exist.
    assert BUSINESS_B not in resp.text


def test_route_missing_business_header_fails_closed():
    with _client(_auth_settings()) as client:
        resp = client.get("/v1/tenant-context", headers=_headers(_mint(subject=USER_ALICE)))
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_request"


def test_route_requires_authentication():
    with _client(_auth_settings()) as client:
        # No bearer token at all — the auth dependency rejects before any tenant resolution.
        assert (
            client.get("/v1/tenant-context", headers={"X-Business-Id": BUSINESS_A}).status_code
            == 401
        )


def test_token_claimed_roles_never_widen_authority():
    # A token self-asserting elevated roles for B still cannot select B, and a token for A gets only
    # the membership's roles — the token's "superadmin" claim is ignored for authorization.
    token = _mint(subject=USER_ALICE, roles=["superadmin"])
    with _client(_auth_settings()) as client:
        denied = client.get("/v1/tenant-context", headers=_headers(token, BUSINESS_B))
        granted = client.get("/v1/tenant-context", headers=_headers(token, BUSINESS_A))
    assert denied.status_code == 403
    assert granted.status_code == 200
    assert granted.json()["roles"] == ["owner", "campaign.admin"]


def test_fake_token_subject_resolves_membership_in_dev():
    # The dev fake verifier maps ``fake:<subject>`` to the subject; membership still gates the scope.
    with _client(make_settings(environment="development")) as client:
        granted = client.get(
            "/v1/tenant-context", headers=_headers(f"fake:{USER_ALICE}", BUSINESS_A)
        )
        denied = client.get(
            "/v1/tenant-context", headers=_headers(f"fake:{USER_ALICE}", BUSINESS_B)
        )
    assert granted.status_code == 200
    assert denied.status_code == 403


def test_production_without_identity_client_fails_closed():
    # Production: the identity contract is disabled (no live adapter) => tenant resolution 403s even
    # with a perfectly valid bearer token.
    prod = Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="production",
        deployment_mode="live",
        database_url="postgresql+psycopg://user@db/dca",
        service_auth_token="prod-service-token",
        auth_token_issuer=ISSUER,
        auth_token_audience=AUDIENCE,
        auth_token_signing_secret=SECRET,
        synthetic_sources_enabled=False,
        auto_create_schema=False,
    )
    app = create_app(prod)
    assert app.state.identity_client is None
    with TestClient(app) as client:
        resp = client.get(
            "/v1/tenant-context", headers=_headers(_mint(subject=USER_ALICE), BUSINESS_A)
        )
    assert resp.status_code == 403
