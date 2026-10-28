"""DCA-083 — resolve a privacy request's affected scope: the operator-reviewed resolution API.

Structured, READ-ONLY HTTP contract (for later Web/Admin operator consoles) over
:class:`~app.services.privacy_scope.PrivacyScopeResolver`. It identifies the records, the
controllers/fiduciaries and the permissible actions a privacy request reaches — and executes nothing
(no deletion, no send, no model call, no state change). Acting on the resolved scope stays a disabled
future capability.

The surface has two planes, matching the DCA boundary and the two privacy-request kinds:

- **Local grievance scope** (end-user auth + the ``grievance.read`` scope for THIS business, via a
  membership-verified tenant context — a platform operator with no membership is denied before the
  scope check). ``GET /v1/tenants/{business_id}/grievances/{id}/affected-scope`` resolves ONLY that
  business's records that pertain to the requester; it never selects another tenant's records, and
  surfaces platform-controlled scope it cannot resolve as ``needsReview``.
- **Platform-source removal scope** (end-user auth + the platform-plane ``platform.source.restrict``
  scope, read from the signed principal — a tenant administrator is denied).
  ``GET /v1/platform/source-removals/{source_key}/affected-scope`` resolves EVERY tenant's records
  acquired from the removed source.

Both routes are side-effect-free GETs: the result and the relevant denial reproduce with no state
change.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.core.security import (
    require_business_permission_on_path,
    require_platform_permission,
)
from app.db.models import PlatformSourceRestriction, RestrictionAuthorityPlane
from app.identity.permissions import Scope
from app.identity.tenant_context import TenantContext
from app.identity.tokens import AuthenticatedPrincipal
from app.services.grievance_intake import PrivacyGrievanceStore
from app.services.privacy_scope import (
    GrievanceScopePrincipal,
    PlatformScopePrincipal,
    PrivacyScopeAuthorizationError,
    PrivacyScopeResolver,
    ScopeResolution,
)

router = APIRouter(prefix="/v1", tags=["privacy-scope"])

SessionDep = Annotated[Session, Depends(get_session)]
# Resolving a grievance's scope is an authorized-handler action: an acting user or an administrator of
# the business holds ``grievance.read``; a read-only viewer and a non-member (incl. a platform operator)
# do not.
GrievanceScopeDep = Annotated[
    TenantContext, Depends(require_business_permission_on_path(Scope.GRIEVANCE_READ))
]
# Resolving a platform-wide source removal's scope is a platform-operator action; a tenant administrator
# does not hold ``platform.source.restrict``.
PlatformScopeDep = Annotated[
    AuthenticatedPrincipal, Depends(require_platform_permission(Scope.PLATFORM_SOURCE_RESTRICT))
]


def _resolution_view(resolution: ScopeResolution) -> dict:
    """The structured, opaque resolution projection. Carries record IDs / statuses / source keys and
    controller/fiduciary references only — never a contact value or an org name."""
    return {
        "requestKind": resolution.request_kind,
        "requestId": resolution.request_id,
        "authorityPlane": resolution.authority_plane,
        "sourceKey": resolution.source_key or None,
        "recordCount": resolution.record_count,
        "affectedBusinessIds": resolution.affected_business_ids,
        "affectedRecords": [
            {
                "recordId": r.record_id,
                "businessId": r.business_id,
                "sourceKey": r.source_key or None,
                "status": r.status,
            }
            for r in resolution.affected_records
        ],
        "controllers": [
            {
                "role": c.role,
                "businessId": c.business_id or None,
                "sourceKey": c.source_key or None,
            }
            for c in resolution.controllers
        ],
        "permissibleActions": list(resolution.permissible_actions),
        # Execution (incl. deletion) is a disabled future capability — a resolution only identifies scope.
        "actionsExecutable": resolution.actions_executable,
        "needsReview": resolution.needs_review,
        "unresolvedAuthority": list(resolution.unresolved_authority),
    }


@router.get("/tenants/{business_id}/grievances/{grievance_id}/affected-scope")
def resolve_grievance_scope(
    business_id: str,
    grievance_id: str,
    context: GrievanceScopeDep,
    session: SessionDep,
) -> dict:
    """Resolve the affected scope of one grievance within THIS business (authorized operator only).

    Requires the ``grievance.read`` scope: a read-only viewer or a non-member (incl. a platform
    operator) is denied (403) before anything is read. A cross-tenant / unknown grievance id reads as
    404. Read-only: nothing is written, so the result and the denial reproduce with no state change. The
    resolution identifies this tenant's affected records, the fiduciary and the permissible action —
    and executes nothing; platform-controlled scope it cannot resolve is flagged ``needsReview``.
    """
    store = PrivacyGrievanceStore(session, context.business_id)
    grievance = store.get(grievance_id)
    if grievance is None:
        raise HTTPException(status_code=404, detail="grievance_not_found")
    # The path business holder is, by construction, an authorized operator of this tenant; resolving the
    # scope of a grievance that belongs to this tenant is within that authority.
    principal = GrievanceScopePrincipal(subject=context.subject, roles=context.roles)
    resolution = PrivacyScopeResolver(session).resolve_local(grievance, principal)
    return _resolution_view(resolution)


@router.get("/platform/source-removals/{source_key}/affected-scope")
def resolve_platform_source_scope(
    source_key: str,
    principal: PlatformScopeDep,
    session: SessionDep,
) -> dict:
    """Resolve the affected scope of a platform-wide source removal (platform operator only).

    Requires the platform-plane ``platform.source.restrict`` scope (read from the signed principal); a
    tenant administrator is denied (403). An unknown / not-removed source reads as 404. Read-only: the
    resolution identifies EVERY tenant's records from the removed source, the platform source controller
    and each affected tenant — executing nothing.
    """
    restriction = session.scalars(
        select(PlatformSourceRestriction)
        .where(PlatformSourceRestriction.source_key == source_key)
        .order_by(PlatformSourceRestriction.version.desc())
        .limit(1)
    ).first()
    if restriction is None:
        raise HTTPException(status_code=404, detail="source_removal_not_found")
    scope_principal = PlatformScopePrincipal(subject=principal.subject, roles=principal.roles)
    try:
        resolution = PrivacyScopeResolver(session).resolve_platform_source(
            restriction, scope_principal
        )
    except PrivacyScopeAuthorizationError as exc:  # pragma: no cover - dependency gates first
        raise HTTPException(status_code=403, detail=exc.code) from exc
    # Defensive: the ledger row is a platform-plane removal by construction.
    assert resolution.authority_plane == RestrictionAuthorityPlane.PLATFORM.value
    return _resolution_view(resolution)
