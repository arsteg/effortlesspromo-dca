"""DCA-020 — read-only probes that exercise scoped service credentials.

Two probe routes standing for two different privileged service operations, each scoped to the
business in its path. They perform NO real work — nothing is read or mutated — they only return 200
when the presented ``X-Service-Credential`` permits that operation on that business, and 401/403
otherwise, so valid / expired / wrong-scope credentials can be compared with no side effect. The
real operations are future stories and stay disabled; these just prove the credential gate.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.security import require_service_credential
from app.identity.service_credentials import ServiceCredential

router = APIRouter(prefix="/v1/service", tags=["service"])

# Two distinct service operations — a credential scoped to one must not satisfy the other.
OPERATION_TENANTS_READ = "tenants.read"
OPERATION_DISCOVERY_TRIGGER = "discovery.trigger"

TenantsReadCred = Annotated[
    ServiceCredential, Depends(require_service_credential(OPERATION_TENANTS_READ))
]
DiscoveryTriggerCred = Annotated[
    ServiceCredential, Depends(require_service_credential(OPERATION_DISCOVERY_TRIGGER))
]


@router.get("/probe/tenants-read/{business_id}")
def probe_tenants_read(business_id: str, credential: TenantsReadCred) -> dict:
    return {
        "authorized": True,
        "operation": OPERATION_TENANTS_READ,
        "businessId": business_id,
        "service": credential.subject,
    }


@router.get("/probe/discovery-trigger/{business_id}")
def probe_discovery_trigger(business_id: str, credential: DiscoveryTriggerCred) -> dict:
    return {
        "authorized": True,
        "operation": OPERATION_DISCOVERY_TRIGGER,
        "businessId": business_id,
        "service": credential.subject,
    }
