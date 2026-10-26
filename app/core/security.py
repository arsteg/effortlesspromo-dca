"""Inbound service-to-service authentication — same fail-closed pattern as effortlesspromo-ai.

Callers present a shared token in ``X-Service-Auth``. No token configured => every privileged call
is rejected (401), never served open. This gate authenticates the CALLER SERVICE only; it does not
authorize any tenant. Per DCA-001 B3, tenant authority must be established by the platform before a
request reaches this service — endpoints here only ever operate on the business id given in the
path and never let one tenant read or mutate another's rows.

Reads the app's own Settings (app.state.settings) so tests and multi-instance setups never fall
back to ambient environment state.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, Request

from app.identity.tokens import AuthenticatedPrincipal, TokenValidationError


def require_service_auth(
    request: Request, x_service_auth: str | None = Header(default=None)
) -> None:
    expected = request.app.state.settings.service_auth_token
    if not expected:
        raise HTTPException(status_code=401, detail="service auth not configured")
    if not x_service_auth or not hmac.compare_digest(x_service_auth, expected):
        raise HTTPException(status_code=401, detail="invalid or missing X-Service-Auth")


def _bearer_token(authorization: str | None) -> str:
    """Extract the raw token from an ``Authorization: Bearer <token>`` header, or 401."""
    if not authorization:
        raise HTTPException(status_code=401, detail="missing bearer token")
    scheme, _, token = authorization.partition(" ")
    if scheme.strip().lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="malformed authorization header")
    return token.strip()


def require_authenticated_subject(
    request: Request, authorization: str | None = Header(default=None)
) -> AuthenticatedPrincipal:
    """DCA-015 — authenticate the end user whose bearer token the caller forwards.

    Fail-closed: when no verifier is configured (``app.state.token_verifier is None`` — the contract
    is disabled, e.g. production with no issuer/audience/signing-secret) every call is rejected. A
    forged, expired, wrong-audience or otherwise invalid token raises
    :class:`~app.identity.tokens.TokenValidationError`, which is collapsed into a single generic 401
    so the response never reveals *why* a token failed.
    """
    verifier = getattr(request.app.state, "token_verifier", None)
    if verifier is None:
        raise HTTPException(status_code=401, detail="end-user authentication is not configured")
    token = _bearer_token(authorization)
    try:
        return verifier.verify(token)
    except TokenValidationError as exc:
        raise HTTPException(status_code=401, detail="invalid or unauthenticated token") from exc
