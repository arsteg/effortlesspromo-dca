"""DCA-015 — a protected diagnostic route that proves end-user authentication works.

``GET /v1/whoami`` echoes the validated principal and nothing else. It is read-only: no database
access, no state change, no authorization decision (that is a later story over the identity
contract) — so it is safe to call repeatedly with valid, expired and forged tokens and compare the
responses without side effects. It is guarded ONLY by the end-user token dependency; the
service-to-service gate (``X-Service-Auth``) protects the tenant-management surface separately.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.security import require_authenticated_subject, require_tenant_context
from app.identity.tenant_context import TenantContext
from app.identity.tokens import AuthenticatedPrincipal

router = APIRouter(prefix="/v1", tags=["auth"])

PrincipalDep = Annotated[AuthenticatedPrincipal, Depends(require_authenticated_subject)]
TenantContextDep = Annotated[TenantContext, Depends(require_tenant_context)]


@router.get("/whoami")
def whoami(principal: PrincipalDep) -> dict:
    return {
        "authenticated": True,
        "subject": principal.subject,
        "issuer": principal.issuer,
        "audience": principal.audience,
        "roles": list(principal.roles),
        "expiresAt": principal.expires_at.isoformat() if principal.expires_at else None,
    }


@router.get("/tenant-context")
def tenant_context(context: TenantContextDep) -> dict:
    """DCA-016 — echo the resolved, immutable tenant scope and nothing else.

    Guarded by :func:`require_tenant_context`, so reaching the handler already proves the caller was
    authenticated AND holds an active membership in the requested ``X-Business-Id``. Read-only: no
    database access, no state change — so a grant and every denial are reproducible with no side
    effect. ``roles`` are the membership's roles, never the bearer token's claims.
    """
    return {
        "resolved": True,
        "subject": context.subject,
        "businessId": context.business_id,
        "roles": list(context.roles),
        "membershipVersion": context.membership_version,
    }
