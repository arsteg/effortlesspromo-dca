"""DCA-015 — inbound end-user authentication: validate a caller's bearer token.

This is the *authentication* half of the auth-port seam (DCA-014 defined the membership/authorization
half). The DCA never owns users or credentials; a token is minted elsewhere (the platform / an IdP)
and forwarded to this service. Here we only establish *who* the bearer is by validating the token's
issuer, audience, signature and expiry — not *what* they may do (authorization is a later story over
the identity contract).

Two implementations behind one ``TokenVerifier`` port:

- :class:`JwtTokenVerifier` — the real validator. HS256 (symmetric) only; it enforces the configured
  issuer/audience, a valid signature, and the required ``exp``/``iss``/``aud``/``sub`` claims, with a
  small leeway for clock skew. Asymmetric (RS256/JWKS) and token introspection are the "trusted
  introspection contract" deferral — a later story / the live platform adapter (B1) — and slot in as
  another implementation of this same port without touching the dependency or routes.
- :class:`FakeTokenVerifier` — a dev/test-only convenience that accepts an obviously-fake
  ``fake:<subject>[:role,role]`` token with no cryptography. It mirrors the identity fake and the
  .NET "Header" scheme's dev raw-id fallback: :func:`build_token_verifier` NEVER hands it back under
  production configuration.

A missing contract stays disabled: with nothing configured, production has no verifier at all
(``build_token_verifier`` returns ``None``) and the dependency fails closed — every call is rejected.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

import jwt
from pydantic import BaseModel, ConfigDict, Field

_SUPPORTED_ALGORITHMS = frozenset({"HS256"})
# Claims every accepted token must carry — a token missing any of these is rejected, so we never
# authenticate a subject-less or non-expiring token.
_REQUIRED_CLAIMS = ("exp", "iss", "aud", "sub")

_FAKE_PREFIX = "fake:"
_FAKE_ISSUER = "fake-dev-issuer"
_FAKE_AUDIENCE = "fake-dev-audience"


class TokenValidationError(Exception):
    """A token could not be authenticated (missing/forged/expired/wrong-audience/malformed).

    Deliberately carries no distinction between failure modes at the HTTP boundary — the dependency
    maps every instance to a single generic 401 so the response is never an oracle for *why* a token
    was rejected. The (safe) reason is kept here only for server-side logging.
    """


class AuthenticatedPrincipal(BaseModel):
    """The validated identity of a bearer. A curated, PII-light projection of the token claims —
    never the raw claim set — so a diagnostic echo cannot surface arbitrary personal data."""

    model_config = ConfigDict(frozen=True)

    subject: str = Field(min_length=1)
    issuer: str | None = None
    audience: str | None = None
    expires_at: datetime | None = None
    roles: tuple[str, ...] = ()


@runtime_checkable
class TokenVerifier(Protocol):
    """Port the inbound auth dependency depends on. Synchronous, matching the other service clients."""

    def verify(self, token: str) -> AuthenticatedPrincipal: ...


def _coerce_roles(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if str(item).strip())
    return ()


class JwtTokenVerifier:
    """Validate an HS256 JWT against a configured issuer/audience/signing secret.

    Passing an explicit ``algorithms`` allow-list makes PyJWT reject ``alg: none`` and algorithm-
    confusion attempts; ``options.require`` enforces that the security-relevant claims are present
    rather than merely well-formed-if-supplied.
    """

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        secret: str,
        algorithm: str = "HS256",
        leeway_seconds: int = 60,
    ) -> None:
        if algorithm.strip().upper() not in _SUPPORTED_ALGORITHMS:
            raise ValueError(f"unsupported token algorithm: {algorithm!r} (HS256 only)")
        if not secret:
            raise ValueError("a signing secret is required for HS256 verification")
        self._issuer = issuer
        self._audience = audience
        self._secret = secret
        self._algorithm = algorithm.strip().upper()
        self._leeway = leeway_seconds

    def verify(self, token: str) -> AuthenticatedPrincipal:
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                self._secret,
                algorithms=[self._algorithm],
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": list(_REQUIRED_CLAIMS)},
            )
        except jwt.PyJWTError as exc:  # expired / wrong-aud / wrong-iss / forged / malformed / alg
            raise TokenValidationError(f"jwt rejected: {type(exc).__name__}") from exc

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject.strip():
            raise TokenValidationError("token subject (sub) missing or not a string")

        expires_at: datetime | None = None
        exp = claims.get("exp")
        if isinstance(exp, (int, float)):
            expires_at = datetime.fromtimestamp(exp, UTC)

        return AuthenticatedPrincipal(
            subject=subject.strip(),
            issuer=self._issuer,
            audience=self._audience,
            expires_at=expires_at,
            roles=_coerce_roles(claims.get("roles")),
        )


class FakeTokenVerifier:
    """Dev/test-only verifier. Accepts ``fake:<subject>`` or ``fake:<subject>:role1,role2``.

    No signature, no expiry — it exists so local development and tests can exercise authenticated
    routes without an IdP. :func:`build_token_verifier` refuses to construct it in production, so it
    can never authenticate a real caller.
    """

    def verify(self, token: str) -> AuthenticatedPrincipal:
        if not token.startswith(_FAKE_PREFIX):
            raise TokenValidationError("fake token must start with 'fake:'")
        body = token[len(_FAKE_PREFIX) :]
        subject, _, roles_csv = body.partition(":")
        subject = subject.strip()
        if not subject:
            raise TokenValidationError("fake token has no subject")
        roles = _coerce_roles([r for r in roles_csv.split(",") if r.strip()])
        return AuthenticatedPrincipal(
            subject=subject,
            issuer=_FAKE_ISSUER,
            audience=_FAKE_AUDIENCE,
            expires_at=None,
            roles=roles,
        )


def build_token_verifier(settings: Any) -> TokenVerifier | None:
    """The end-user token verifier for this deployment, or ``None`` when the contract is disabled.

    - Fully configured (issuer + audience + signing secret) → the real :class:`JwtTokenVerifier`,
      in every environment.
    - Not configured and NOT production → the dev/test :class:`FakeTokenVerifier`.
    - Not configured and production → ``None``: the contract is disabled and the dependency fails
      closed (the fake is never served in production, and no real validation is wired yet).
    """
    if settings.end_user_auth_configured:
        return JwtTokenVerifier(
            issuer=settings.auth_token_issuer,
            audience=settings.auth_token_audience,
            secret=settings.auth_token_signing_secret,
            algorithm=settings.auth_token_algorithm,
            leeway_seconds=settings.auth_token_leeway_seconds,
        )
    if getattr(settings, "is_production", False):
        return None
    return FakeTokenVerifier()
