"""DCA-015 — inbound end-user API authentication.

A forged, expired or wrong-audience bearer token cannot authenticate against a protected diagnostic
route; a correctly-issued one can; the dev/test fake verifier works only outside production; and with
nothing configured the contract is disabled (fail-closed) in production. Rejections are read-only —
the diagnostic route touches no state — so each denial is reproducible with no side effects.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.identity.tokens import (
    FakeTokenVerifier,
    JwtTokenVerifier,
    TokenValidationError,
    build_token_verifier,
)
from app.main import create_app
from tests.conftest import make_settings

ISSUER = "https://issuer.dca.test"
AUDIENCE = "dca-api"
SECRET = "dca-test-signing-secret-value"  # synthetic HS256 key, not a real credential
SUBJECT = "00000000-0000-4000-9000-0000000a11ce"


def _auth_settings(**overrides) -> Settings:
    base = {
        "auth_token_issuer": ISSUER,
        "auth_token_audience": AUDIENCE,
        "auth_token_signing_secret": SECRET,
    }
    base.update(overrides)
    return make_settings(**base)


def _mint(
    *,
    secret: str = SECRET,
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
    subject: str = SUBJECT,
    expires_in: timedelta = timedelta(minutes=5),
    algorithm: str = "HS256",
    extra: dict | None = None,
) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": subject,
        "iss": issuer,
        "aud": audience,
        "iat": int(now.timestamp()),
        "exp": int((now + expires_in).timestamp()),
    }
    if extra:
        claims.update(extra)
    return jwt.encode(claims, secret, algorithm=algorithm)


def _client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- the happy path -------------------------------------------------------------------------------


def test_valid_token_authenticates():
    with _client(_auth_settings()) as client:
        resp = client.get(
            "/v1/whoami", headers=_bearer(_mint(extra={"roles": ["owner", "viewer"]}))
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["authenticated"] is True
        assert body["subject"] == SUBJECT
        assert body["issuer"] == ISSUER
        assert body["audience"] == AUDIENCE
        assert body["roles"] == ["owner", "viewer"]


# --- the required denials (acceptance criteria) ---------------------------------------------------


def test_forged_signature_is_rejected():
    forged = _mint(secret="attacker-controlled-secret")
    with _client(_auth_settings()) as client:
        resp = client.get("/v1/whoami", headers=_bearer(forged))
    assert resp.status_code == 401
    # Generic reason only — the body never reveals that it was specifically a signature failure.
    assert resp.json()["error"]["code"] == "unauthorized"
    assert "secret" not in resp.text.lower()


def test_expired_token_is_rejected():
    expired = _mint(expires_in=timedelta(minutes=-5))
    with _client(_auth_settings()) as client:
        resp = client.get("/v1/whoami", headers=_bearer(expired))
    assert resp.status_code == 401


def test_wrong_audience_is_rejected():
    wrong_aud = _mint(audience="some-other-api")
    with _client(_auth_settings()) as client:
        resp = client.get("/v1/whoami", headers=_bearer(wrong_aud))
    assert resp.status_code == 401


def test_wrong_issuer_is_rejected():
    wrong_iss = _mint(issuer="https://evil.example")
    with _client(_auth_settings()) as client:
        resp = client.get("/v1/whoami", headers=_bearer(wrong_iss))
    assert resp.status_code == 401


def test_alg_none_token_is_rejected():
    # 'alg: none' (unsigned) must never authenticate even with otherwise-valid claims.
    unsigned = jwt.encode(
        {
            "sub": SUBJECT,
            "iss": ISSUER,
            "aud": AUDIENCE,
            "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
        },
        key="",
        algorithm="none",
    )
    with _client(_auth_settings()) as client:
        resp = client.get("/v1/whoami", headers=_bearer(unsigned))
    assert resp.status_code == 401


def test_token_without_subject_is_rejected():
    no_sub = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
        },
        SECRET,
        algorithm="HS256",
    )
    with _client(_auth_settings()) as client:
        resp = client.get("/v1/whoami", headers=_bearer(no_sub))
    assert resp.status_code == 401


def test_missing_and_malformed_authorization_header_rejected():
    with _client(_auth_settings()) as client:
        assert client.get("/v1/whoami").status_code == 401
        assert client.get("/v1/whoami", headers={"Authorization": "Basic abc"}).status_code == 401
        assert client.get("/v1/whoami", headers={"Authorization": "Bearer "}).status_code == 401


# --- the dev/test fake, and the disabled (fail-closed) contract -----------------------------------


def test_fake_verifier_used_when_unconfigured_outside_production():
    # No issuer/audience/secret + non-production => the obviously-fake verifier is wired.
    with _client(make_settings(environment="development")) as client:
        resp = client.get("/v1/whoami", headers=_bearer("fake:alice:owner,viewer"))
        assert resp.status_code == 200
        body = resp.json()
        assert body["subject"] == "alice"
        assert body["roles"] == ["owner", "viewer"]
        # A malformed fake token is still rejected.
        assert client.get("/v1/whoami", headers=_bearer("not-a-fake-token")).status_code == 401


def test_production_without_config_disables_auth_and_fails_closed():
    prod = Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="production",
        deployment_mode="live",
        database_url="postgresql+psycopg://user@db/dca",
        service_auth_token="prod-service-token",
        synthetic_sources_enabled=False,
        auto_create_schema=False,
    )
    # The contract is disabled: no verifier, and even a syntactically fine fake token is refused.
    assert build_token_verifier(prod) is None
    with _client(prod) as client:
        assert client.get("/v1/whoami", headers=_bearer("fake:alice")).status_code == 401


def test_real_jwt_verifier_used_in_production_when_configured():
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
    verifier = build_token_verifier(prod)
    assert isinstance(verifier, JwtTokenVerifier)
    # A fake token can never authenticate against the real verifier.
    with pytest.raises(TokenValidationError):
        verifier.verify("fake:alice")


# --- verifier units -------------------------------------------------------------------------------


def test_fake_verifier_satisfies_the_port():
    from app.identity.tokens import TokenVerifier

    assert isinstance(FakeTokenVerifier(), TokenVerifier)
    assert isinstance(
        JwtTokenVerifier(issuer=ISSUER, audience=AUDIENCE, secret=SECRET), TokenVerifier
    )
