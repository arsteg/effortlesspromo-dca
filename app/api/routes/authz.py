"""DCA-019 — read-only authorization probes that exercise the permission model.

Two restricted diagnostic routes, one per plane. They perform NO real work — no source is approved,
no budget is changed, no platform state is read — they only return 200 when the caller holds the
required scope and 403 otherwise, so each fixture role's authorization outcome can be inspected with
no side effect. The underlying capabilities (source/policy approval, budget management, platform ops)
are future stories and stay disabled; these probes just prove the gate.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.security import require_business_permission, require_platform_permission
from app.identity.permissions import Scope
from app.identity.tenant_context import TenantContext
from app.identity.tokens import AuthenticatedPrincipal

router = APIRouter(prefix="/v1/authz", tags=["authz"])

# Administrator-only business scope: ordinary users are denied; a non-member (incl. a platform
# operator) never reaches the scope check because the tenant-context dependency denies first.
SourceApprovalDep = Annotated[
    TenantContext, Depends(require_business_permission(Scope.SOURCE_APPROVE))
]
# Platform-operator-only scope: a business administrator does not hold it.
PlatformOpsDep = Annotated[
    AuthenticatedPrincipal, Depends(require_platform_permission(Scope.PLATFORM_OPS_READ))
]


@router.get("/business/source-approval")
def probe_source_approval(context: SourceApprovalDep) -> dict:
    return {
        "authorized": True,
        "plane": "business",
        "scope": Scope.SOURCE_APPROVE.value,
        "businessId": context.business_id,
        "roles": list(context.roles),
    }


@router.get("/platform/ops")
def probe_platform_ops(principal: PlatformOpsDep) -> dict:
    return {
        "authorized": True,
        "plane": "platform",
        "scope": Scope.PLATFORM_OPS_READ.value,
        "subject": principal.subject,
    }
