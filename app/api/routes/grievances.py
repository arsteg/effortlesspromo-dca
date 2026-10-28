"""DCA-082 — record an India privacy grievance: the intake/status service API.

Structured HTTP contract (for later Web public forms / Admin operator consoles) over
:class:`~app.services.grievance_intake.PrivacyGrievanceStore`. It records a data principal's privacy
grievance with a PROPORTIONATE verification requirement and a CONFIGURABLE, REVIEWED response deadline,
and exposes a safe status lookup. It performs NO erasure, no send and no model call — acting on a
grievance stays a disabled future capability.

The surface has two planes, matching the DCA boundary:

- **Public intake + requester status** (``X-Service-Auth`` only — the platform relays an anonymous
  public-form submission). ``POST /{business_id}/grievances`` records the grievance and returns the
  requester's ONE-TIME claim token; ``GET /{business_id}/grievances/{id}/status`` returns its status
  ONLY when that claim token (header ``X-Grievance-Token``) matches — a wrong/absent token reads as a
  404, so it is never an existence oracle (no contact enumeration).
- **Operator reads** (end-user auth + the ``grievance.read`` scope for THIS business, via a
  membership-verified tenant context — a platform operator with no membership is denied before the
  scope check). ``GET /{business_id}/grievances`` lists the business's grievances and
  ``GET /{business_id}/grievances/{id}`` reads one. Operator projections carry grievance metadata only
  — never the requester's contact digest or the claim-token hash — so handling a grievance is never
  authority to harvest contacts. The path ``business_id`` is the row scope throughout; one business can
  never read another's grievances (a cross-tenant / unknown id reads as 404).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.api.idempotency import IdempotencyContext, commit_idempotent, idempotency
from app.audit.sink import AuditPayloadError
from app.core.security import require_business_permission_on_path, require_service_auth
from app.db.models import PrivacyGrievance
from app.identity.permissions import Scope
from app.identity.tenant_context import TenantContext
from app.services.grievance_intake import (
    GrievanceIntakeError,
    GrievanceIntakeInput,
    PrivacyGrievanceStore,
)

router = APIRouter(prefix="/v1/tenants", tags=["grievances"])

SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyDep = Annotated[IdempotencyContext, Depends(idempotency)]
# Reading a business's grievances (list / by-id) is an authorized-handler action: an acting user or an
# administrator of the business holds ``grievance.read``; a read-only viewer and a non-member (incl. a
# platform operator) do not.
GrievanceReadDep = Annotated[
    TenantContext, Depends(require_business_permission_on_path(Scope.GRIEVANCE_READ))
]


class GrievanceIntakeBody(BaseModel):
    # extra='forbid' so an UNSUPPORTED field (a typo / a field from a future capability) is a clear 422
    # rather than being silently ignored.
    model_config = ConfigDict(extra="forbid")

    category: str
    # The requester's contact value — used ONLY to derive the one-way digest and then discarded; it is
    # never stored raw and never echoed back.
    contact: str = ""
    channel: str = ""
    detail: str = ""


def _status_view(grievance: PrivacyGrievance) -> dict:
    """The safe status projection — grievance metadata only. Carries NO contact digest, NO claim-token
    hash and NO free-text detail, so it can be returned to the requester or an operator without leaking
    who filed or how to impersonate them."""
    return {
        "grievanceId": grievance.id,
        "businessId": grievance.business_id,
        "category": grievance.category,
        "status": grievance.status,
        "verificationState": grievance.verification_state,
        "verificationLevelRequired": grievance.verification_level_required,
        "channel": grievance.channel or None,
        "responseDeadlineDays": grievance.response_deadline_days,
        "respondBy": grievance.respond_by.isoformat() if grievance.respond_by else None,
        "receivedAt": grievance.created_at.isoformat() if grievance.created_at else None,
    }


def _operator_view(grievance: PrivacyGrievance) -> dict:
    """The authorized-operator projection: the safe status view PLUS the requester's own description,
    which a handler needs to action the grievance. Still never the contact digest or the token hash."""
    return {**_status_view(grievance), "detail": grievance.detail or ""}


@router.post(
    "/{business_id}/grievances",
    status_code=201,
    dependencies=[Depends(require_service_auth)],
)
def record_grievance(
    business_id: str,
    body: GrievanceIntakeBody,
    request: Request,
    session: SessionDep,
    idem: IdempotencyDep,
) -> dict:
    """Record a privacy grievance for this business and return the requester's one-time claim token.

    Service-auth gated (the platform relays an anonymous public-form submission); it needs no end-user
    login. An unknown category/channel is a clear 422 (stable code), staging nothing. On success a
    grievance is recorded (``received`` / ``unverified``) with its proportionate verification level and
    the REVIEWED response deadline (from ``grievance_response_days``), an atomic audit entry is staged
    in the same unit of work, and the ONE-TIME ``statusToken`` is returned (it is never stored or
    recoverable afterwards). Retry-safe with an ``Idempotency-Key`` (a replay returns the SAME token).
    """
    deadline_days = request.app.state.settings.grievance_response_days
    store = PrivacyGrievanceStore(session, business_id)
    try:
        issued = store.record(
            GrievanceIntakeInput(
                category=body.category,
                contact=body.contact,
                channel=body.channel,
                detail=body.detail,
            ),
            response_deadline_days=deadline_days,
        )
    except GrievanceIntakeError as exc:
        # Unknown category/channel: a clear 422, staged nothing (validated before the session is touched).
        raise HTTPException(status_code=422, detail=exc.code) from exc
    except AuditPayloadError as exc:  # pragma: no cover - references are opaque by construction
        raise HTTPException(status_code=422, detail="grievance_reference_not_opaque") from exc
    # Flush so the DB-assigned fields (created_at) are populated for the response view.
    session.flush()
    result = {**_status_view(issued.grievance), "statusToken": issued.access_token}
    return commit_idempotent(session, idem, result, status_code=201)


@router.get(
    "/{business_id}/grievances/{grievance_id}/status",
    dependencies=[Depends(require_service_auth)],
)
def get_grievance_status(
    business_id: str,
    grievance_id: str,
    session: SessionDep,
    x_grievance_token: Annotated[str | None, Header()] = None,
) -> dict:
    """The REQUESTER's status lookup — authorized by the claim token issued at intake.

    Service-auth gated; the claim token (header ``X-Grievance-Token``) is the requester's authority. A
    grievance that is not in this business OR whose token does not match reads as 404 — a wrong/absent
    token is indistinguishable from not-found, so it never confirms a grievance exists or who filed.
    Read-only: nothing is written, so the result and the denial reproduce with no state change.
    """
    grievance = PrivacyGrievanceStore(session, business_id).get_for_requester(
        grievance_id, (x_grievance_token or "").strip()
    )
    if grievance is None:
        # Wrong token / cross-tenant / unknown id all read exactly like not-found — never surfaced.
        raise HTTPException(status_code=404, detail="grievance_not_found")
    return _status_view(grievance)


@router.get("/{business_id}/grievances")
def list_grievances(business_id: str, context: GrievanceReadDep, session: SessionDep) -> list[dict]:
    """Every grievance for this business, oldest first (authorized operator only).

    Requires the ``grievance.read`` scope: a read-only viewer or a non-member (incl. a platform
    operator) is denied (403) before any row is read. The projection carries grievance metadata only —
    no contact digest, no claim-token hash — so it cannot be used to enumerate contacts.
    """
    return [
        _status_view(g) for g in PrivacyGrievanceStore(session, context.business_id).grievances()
    ]


@router.get("/{business_id}/grievances/{grievance_id}")
def get_grievance(
    business_id: str, grievance_id: str, context: GrievanceReadDep, session: SessionDep
) -> dict:
    """One grievance within this business (authorized operator only; 404 if unknown/cross-tenant).

    Requires the ``grievance.read`` scope (same denial as the list). Read-only: nothing is written.
    """
    grievance = PrivacyGrievanceStore(session, context.business_id).get(grievance_id)
    if grievance is None:
        raise HTTPException(status_code=404, detail="grievance_not_found")
    return _operator_view(grievance)
