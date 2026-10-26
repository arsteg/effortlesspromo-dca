"""DCA-020 — scoped service-to-service credentials.

A service credential is a signed JWT bound to issuer/audience/expiry plus explicit allowed
operations and businesses. A credential scoped to one operation cannot call another, and cannot act
on a business outside its allow-list; an expired or forged credential is rejected; and a public-form
(end-user) token is never accepted as a service credential. The probe routes are read-only, so each
denial is reproduced with no side effect.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, validate_required
from app.identity.fake import BUSINESS_A, BUSINESS_B
from app.identity.service_credentials import (
    JwtServiceCredentialVerifier,
    ServiceCredentialError,
    build_service_credential_verifier,
)
from app.main import create_app
from tests.conftest import make_settings

SVC_ISSUER = "https://platform.dca.test"
SVC_AUDIENCE = "dca-service"
SVC_SECRET = "dca-service-signing-secret"  # synthetic HS256 key, not a real credential
SERVICE = "platform-integration"

# End-user (public-form) token settings — deliberately a DIFFERENT audience/secret.
USER_AUDIENCE = "dca-api"
USER_SECRET = "dca-end-user-signing-secret"


def _svc_settings(**overrides) -> Settings:
    base = {
        "service_token_issuer": SVC_ISSUER,
        "service_token_audience": SVC_AUDIENCE,
        "service_token_signing_secret": SVC_SECRET,
    }
    base.update(overrides)
    return make_settings(**base)


def _mint_service(
    *,
    operations: list[str],
    businesses: list[str],
    secret: str = SVC_SECRET,
    issuer: str = SVC_ISSUER,
    audience: str = SVC_AUDIENCE,
    expires_in: timedelta = timedelta(minutes=5),
    subject: str = SERVICE,
) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": subject,
        "iss": issuer,
        "aud": audience,
        "iat": int(now.timestamp()),
        "exp": int((now + expires_in).timestamp()),
        "operations": operations,
        "businesses": businesses,
    }
    return jwt.encode(claims, secret, algorithm="HS256")


def _client(settings: Settings | None = None) -> TestClient:
    return TestClient(create_app(settings or _svc_settings()))


def _hdr(token: str) -> dict[str, str]:
    return {"X-Service-Credential": token}


# --- the happy path -------------------------------------------------------------------------------


def test_scoped_credential_authorizes_its_own_operation_and_business():
    token = _mint_service(operations=["tenants.read"], businesses=[BUSINESS_A])
    with _client() as client:
        resp = client.get(f"/v1/service/probe/tenants-read/{BUSINESS_A}", headers=_hdr(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["authorized"] is True
    assert body["businessId"] == BUSINESS_A
    assert body["service"] == SERVICE


# --- the acceptance denials -----------------------------------------------------------------------


def test_credential_cannot_call_another_operation():
    # Scoped to tenants.read only — the discovery-trigger operation must be refused.
    token = _mint_service(operations=["tenants.read"], businesses=[BUSINESS_A])
    with _client() as client:
        resp = client.get(f"/v1/service/probe/discovery-trigger/{BUSINESS_A}", headers=_hdr(token))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_credential_cannot_change_its_allowed_business():
    # Scoped to business A — presenting business B (even for an allowed operation) is refused; the
    # request cannot widen the credential's business bound.
    token = _mint_service(operations=["tenants.read"], businesses=[BUSINESS_A])
    with _client() as client:
        resp = client.get(f"/v1/service/probe/tenants-read/{BUSINESS_B}", headers=_hdr(token))
    assert resp.status_code == 403


def test_expired_credential_is_rejected():
    token = _mint_service(
        operations=["tenants.read"], businesses=[BUSINESS_A], expires_in=timedelta(minutes=-5)
    )
    with _client() as client:
        resp = client.get(f"/v1/service/probe/tenants-read/{BUSINESS_A}", headers=_hdr(token))
    assert resp.status_code == 401


def test_forged_signature_is_rejected():
    token = _mint_service(
        operations=["tenants.read"], businesses=[BUSINESS_A], secret="attacker-secret"
    )
    with _client() as client:
        resp = client.get(f"/v1/service/probe/tenants-read/{BUSINESS_A}", headers=_hdr(token))
    assert resp.status_code == 401


def test_missing_credential_is_rejected():
    with _client() as client:
        assert client.get(f"/v1/service/probe/tenants-read/{BUSINESS_A}").status_code == 401


def test_public_form_token_is_not_accepted_as_a_service_credential():
    # A valid END-USER token (different audience + secret) must never pass the service-credential
    # verifier — service credentials do not reuse public-form tokens.
    now = datetime.now(UTC)
    user_token = jwt.encode(
        {
            "sub": "a-user",
            "iss": SVC_ISSUER,
            "aud": USER_AUDIENCE,  # the end-user audience, not the service audience
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
            "operations": ["tenants.read"],  # even if it forges scope claims
            "businesses": [BUSINESS_A],
        },
        USER_SECRET,
        algorithm="HS256",
    )
    with _client() as client:
        resp = client.get(f"/v1/service/probe/tenants-read/{BUSINESS_A}", headers=_hdr(user_token))
    assert resp.status_code == 401


def test_alg_none_credential_is_rejected():
    now = datetime.now(UTC)
    unsigned = jwt.encode(
        {
            "sub": SERVICE,
            "iss": SVC_ISSUER,
            "aud": SVC_AUDIENCE,
            "exp": int((now + timedelta(minutes=5)).timestamp()),
            "operations": ["tenants.read"],
            "businesses": [BUSINESS_A],
        },
        key="",
        algorithm="none",
    )
    with _client() as client:
        resp = client.get(f"/v1/service/probe/tenants-read/{BUSINESS_A}", headers=_hdr(unsigned))
    assert resp.status_code == 401


# --- disabled contract + verifier units -----------------------------------------------------------


def test_unconfigured_contract_fails_closed():
    # No service-token settings => no verifier => every service-scoped call is rejected.
    with _client(make_settings()) as client:
        assert build_service_credential_verifier(make_settings()) is None
        token = _mint_service(operations=["tenants.read"], businesses=[BUSINESS_A])
        assert (
            client.get(
                f"/v1/service/probe/tenants-read/{BUSINESS_A}", headers=_hdr(token)
            ).status_code
            == 401
        )


def test_verifier_extracts_scope_claims():
    verifier = JwtServiceCredentialVerifier(
        issuer=SVC_ISSUER, audience=SVC_AUDIENCE, secret=SVC_SECRET
    )
    cred = verifier.verify(
        _mint_service(operations=["tenants.read", "discovery.trigger"], businesses=[BUSINESS_A])
    )
    assert cred.subject == SERVICE
    assert cred.allows_operation("tenants.read")
    assert cred.allows_operation("discovery.trigger")
    assert cred.allows_business(BUSINESS_A)
    assert not cred.allows_business(BUSINESS_B)


def test_wrong_audience_credential_is_rejected_by_verifier():
    verifier = JwtServiceCredentialVerifier(
        issuer=SVC_ISSUER, audience=SVC_AUDIENCE, secret=SVC_SECRET
    )
    with pytest.raises(ServiceCredentialError):
        verifier.verify(
            _mint_service(operations=["tenants.read"], businesses=[BUSINESS_A], audience="other")
        )


# --- config: audience must differ from end-user (no public-form reuse) ----------------------------


def test_boot_rejects_shared_audience_between_service_and_end_user():
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="testing",
        database_url="sqlite+pysqlite:///:memory:",
        auth_token_issuer="iss",
        auth_token_audience="shared-aud",
        auth_token_signing_secret="end-user-secret",
        service_token_issuer="iss",
        service_token_audience="shared-aud",
        service_token_signing_secret="service-secret",
    )
    with pytest.raises(RuntimeError, match="audiences must differ"):
        validate_required(settings)
