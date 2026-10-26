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
import logging
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request

from app.identity.permissions import (
    Scope,
    scopes_for_business_roles,
    scopes_for_platform_roles,
)
from app.identity.service_credentials import ServiceCredential, ServiceCredentialError
from app.identity.tenant_context import (
    TenantContext,
    TenantResolutionError,
    resolve_tenant_context,
)
from app.identity.tokens import AuthenticatedPrincipal, TokenValidationError

# Authorization decisions (grants AND denials) are logged here as an audit trail. The DCA-012
# structured logger stamps the correlation id, and its central redaction keeps the line PII-safe.
_authz_log = logging.getLogger("dca.authz")


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


def require_tenant_context(
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_subject)],
    x_business_id: str | None = Header(default=None),
) -> TenantContext:
    """DCA-016 — resolve an immutable tenant scope AFTER verifying membership.

    Composes on top of :func:`require_authenticated_subject`: a valid bearer token establishes *who*
    the subject is, then the requested ``X-Business-Id`` is authorized only if that subject holds an
    active membership in that business (via the identity contract). The ``X-Business-Id`` header is
    never trusted on its own — it only ever names the business whose membership is checked.

    Fail-closed:
    - No identity client wired (contract disabled, e.g. production without the live adapter) → 403.
    - No ``X-Business-Id`` supplied → 400 (``business_scope_required``); unresolved scope grants
      nothing.
    - Subject has no active membership in the requested business → 403; the message is generic so it
      never reveals whether the business exists or why a link is inactive.
    """
    identity_client = getattr(request.app.state, "identity_client", None)
    if identity_client is None:
        raise HTTPException(status_code=403, detail="tenant resolution is not available")
    try:
        context = resolve_tenant_context(
            principal=principal,
            requested_business_id=x_business_id,
            identity_client=identity_client,
        )
    except TenantResolutionError as exc:
        if not exc.scope_supplied:
            raise HTTPException(
                status_code=400, detail="business scope (X-Business-Id header) is required"
            ) from exc
        raise HTTPException(
            status_code=403, detail="not authorized for the requested business"
        ) from exc
    # Resolve-once: publish the immutable context for this request. FastAPI caches a dependency's
    # result per request, so the scope is never re-resolved within one request.
    request.state.tenant_context = context
    return context


def _audit_authz(
    *, plane: str, subject: str, scope: Scope, allowed: bool, business_id: str | None
) -> None:
    """Emit one audit record for an authorization decision — granted or denied, both planes."""
    _authz_log.info(
        "authz decision",
        extra={
            "authz_plane": plane,
            "authz_subject": subject,
            "authz_scope": scope.value,
            "authz_business_id": business_id,
            "authz_outcome": "granted" if allowed else "denied",
        },
    )


def require_business_permission(
    scope: Scope,
) -> Callable[[TenantContext], TenantContext]:
    """DCA-019 — require a business-plane ``scope``, resolved from the caller's membership roles.

    Builds on :func:`require_tenant_context`, so the caller must already hold an *active membership*
    in the requested business; this adds the explicit-scope check on top. An ordinary business user
    (whose roles map to read-only scopes) is denied the administrator scopes — approving
    sources/policies or changing budgets. A platform operator has no membership in the business, so
    it never even reaches this check (the tenant-context dependency denies first): global operator
    rights never authorize a client-business action.
    """

    def dependency(
        context: Annotated[TenantContext, Depends(require_tenant_context)],
    ) -> TenantContext:
        allowed = scope in scopes_for_business_roles(context.roles)
        _audit_authz(
            plane="business",
            subject=context.subject,
            scope=scope,
            allowed=allowed,
            business_id=context.business_id,
        )
        if not allowed:
            raise HTTPException(status_code=403, detail="operation not permitted for this role")
        return context

    return dependency


def require_platform_permission(
    scope: Scope,
) -> Callable[[Request, AuthenticatedPrincipal], AuthenticatedPrincipal]:
    """DCA-019 — require a platform-plane ``scope`` held by a platform operator.

    Platform-operator status is read from the *authenticated* (signed) principal's roles — never a
    bare header. The scope sets are disjoint from the business plane, so this grants no authority
    over any client business; acting as a client (delegated tenant support) is a separate, scoped,
    expiring grant and stays disabled. Platform actions are audited via :func:`_audit_authz`.
    """

    def dependency(
        request: Request,
        principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_subject)],
    ) -> AuthenticatedPrincipal:
        allowed = scope in scopes_for_platform_roles(principal.roles)
        _audit_authz(
            plane="platform",
            subject=principal.subject,
            scope=scope,
            allowed=allowed,
            business_id=None,
        )
        if not allowed:
            raise HTTPException(status_code=403, detail="platform operation not permitted")
        return principal

    return dependency


def require_service_credential(
    operation: str,
) -> Callable[[Request, str, str | None], ServiceCredential]:
    """DCA-020 — require a scoped service credential permitting ``operation`` on the path business.

    The credential is presented in ``X-Service-Credential`` (never the end-user ``Authorization``
    bearer), validated for issuer/audience/signature/expiry, then checked against its OWN allow-lists:
    the ``operation`` must be in the credential's operations and the request's ``business_id`` must be
    in its businesses. A credential scoped to one operation/business therefore cannot call another
    privileged operation or act on a different business — the request can never widen its own bounds.

    Fail-closed: no verifier configured → 401; missing/invalid credential → 401 (generic); out-of-scope
    operation or business → 403. Every decision is audited.
    """

    def dependency(
        request: Request,
        business_id: str,
        x_service_credential: str | None = Header(default=None),
    ) -> ServiceCredential:
        verifier = getattr(request.app.state, "service_credential_verifier", None)
        if verifier is None:
            raise HTTPException(status_code=401, detail="service credentials are not configured")
        if not x_service_credential or not x_service_credential.strip():
            raise HTTPException(status_code=401, detail="missing service credential")
        try:
            credential = verifier.verify(x_service_credential.strip())
        except ServiceCredentialError as exc:
            raise HTTPException(status_code=401, detail="invalid service credential") from exc

        op_ok = credential.allows_operation(operation)
        biz_ok = credential.allows_business(business_id)
        _audit_service_auth(
            subject=credential.subject,
            operation=operation,
            business_id=business_id,
            operation_allowed=op_ok,
            business_allowed=biz_ok,
        )
        if not op_ok:
            raise HTTPException(
                status_code=403, detail="operation not permitted for this service credential"
            )
        if not biz_ok:
            raise HTTPException(
                status_code=403, detail="business not permitted for this service credential"
            )
        return credential

    return dependency


def _audit_service_auth(
    *,
    subject: str,
    operation: str,
    business_id: str,
    operation_allowed: bool,
    business_allowed: bool,
) -> None:
    """Audit a service-credential authorization decision (the scope that failed is recorded)."""
    allowed = operation_allowed and business_allowed
    _authz_log.info(
        "service-credential decision",
        extra={
            "authz_plane": "service",
            "authz_subject": subject,
            "authz_operation": operation,
            "authz_business_id": business_id,
            "authz_outcome": "granted" if allowed else "denied",
            "authz_denied_scope": (
                None if allowed else ("operation" if not operation_allowed else "business")
            ),
        },
    )
