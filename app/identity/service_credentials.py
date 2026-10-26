"""DCA-020 — scoped service credentials for platform integration requests.

Distinct from both the coarse ``X-Service-Auth`` shared secret (which authenticates *any* caller
service) and the DCA-015 end-user (public-form) token. A service credential is a signed JWT bound to
an issuer, an audience, an expiry, AND an explicit allow-list of operations and businesses — so a
credential minted for one operation/business cannot be used for another. It is presented in its own
header (``X-Service-Credential``), never the end-user ``Authorization`` bearer, so the two token
types can never be confused; a distinct audience (enforced at config boot) is the second guard.

HS256 only (same stance as DCA-015). Asymmetric keys / introspection are a later story. With nothing
configured the contract is disabled and the dependency fails closed — every service-scoped call is
rejected.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

import jwt
from pydantic import BaseModel, ConfigDict, Field

_SUPPORTED_ALGORITHMS = frozenset({"HS256"})
# A service credential must carry these: without exp/iss/aud/sub it is never accepted.
_REQUIRED_CLAIMS = ("exp", "iss", "aud", "sub")


class ServiceCredentialError(Exception):
    """A service credential could not be validated (missing/forged/expired/wrong-audience/malformed).

    Mapped to a single generic 401 at the boundary so the response never reveals *why* it failed.
    """


class ServiceCredential(BaseModel):
    """A validated, immutable service credential: who is calling and exactly what it may do.

    ``operations`` and ``businesses`` are the credential's own bounds — a request is authorized only
    if the operation it performs AND the business it targets are both inside these sets. Neither can
    be widened by the request itself.
    """

    model_config = ConfigDict(frozen=True)

    subject: str = Field(min_length=1)  # the calling platform service identity
    issuer: str | None = None
    audience: str | None = None
    operations: tuple[str, ...] = ()
    businesses: tuple[str, ...] = ()
    expires_at: datetime | None = None

    def allows_operation(self, operation: str) -> bool:
        return operation in self.operations

    def allows_business(self, business_id: str) -> bool:
        return business_id in self.businesses


@runtime_checkable
class ServiceCredentialVerifier(Protocol):
    def verify(self, token: str) -> ServiceCredential: ...


def _coerce_str_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


class JwtServiceCredentialVerifier:
    """Validate an HS256 service-credential JWT against a configured issuer/audience/signing secret.

    The explicit ``algorithms`` allow-list rejects ``alg: none`` and algorithm confusion; the
    required-claims option enforces the security-relevant claims are present. ``operations`` and
    ``businesses`` are read from the (now-trusted) claims; an absent claim yields an empty set, i.e.
    the credential authorizes nothing (fail-closed).
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
            raise ValueError(
                f"unsupported service-credential algorithm: {algorithm!r} (HS256 only)"
            )
        if not secret:
            raise ValueError("a signing secret is required for HS256 verification")
        self._issuer = issuer
        self._audience = audience
        self._secret = secret
        self._algorithm = algorithm.strip().upper()
        self._leeway = leeway_seconds

    def verify(self, token: str) -> ServiceCredential:
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
        except jwt.PyJWTError as exc:
            raise ServiceCredentialError(
                f"service credential rejected: {type(exc).__name__}"
            ) from exc

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject.strip():
            raise ServiceCredentialError("service credential subject (sub) missing or not a string")

        expires_at: datetime | None = None
        exp = claims.get("exp")
        if isinstance(exp, (int, float)):
            expires_at = datetime.fromtimestamp(exp, UTC)

        return ServiceCredential(
            subject=subject.strip(),
            issuer=self._issuer,
            audience=self._audience,
            operations=_coerce_str_tuple(claims.get("operations")),
            businesses=_coerce_str_tuple(claims.get("businesses")),
            expires_at=expires_at,
        )


def build_service_credential_verifier(settings: Any) -> ServiceCredentialVerifier | None:
    """The service-credential verifier for this deployment, or ``None`` when the contract is disabled.

    Configured (issuer + audience + signing secret) → the real verifier, in every environment.
    Not configured → ``None``: the contract is disabled and the dependency fails closed. There is no
    dev/test fake — service credentials are always explicitly configured (tests mint real ones).
    """
    if settings.service_credentials_configured:
        return JwtServiceCredentialVerifier(
            issuer=settings.service_token_issuer,
            audience=settings.service_token_audience,
            secret=settings.service_token_signing_secret,
            algorithm=settings.service_token_algorithm,
            leeway_seconds=settings.service_token_leeway_seconds,
        )
    return None
