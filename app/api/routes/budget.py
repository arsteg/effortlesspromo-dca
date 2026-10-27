"""DCA-042 — per-business spending limits: the service API.

Structured HTTP contract (for later Web/Admin consumers) over
:class:`~app.services.budget_store.BudgetLimitStore`. A business's INR spend caps (daily, monthly,
per-run) are stored as **versioned** settings: saving always creates a NEW version and preserves prior
ones, so the limit history is an immutable, auditable record.

Authorization (the headline acceptance criterion — *unauthorized users cannot increase caps*) uses the
DCA-019 end-user permission model, not the coarse service-auth gate: the write route requires the
``budget.manage`` administrator scope (held by an owner/admin of the business, via a membership-verified
tenant context), so an ordinary business user — or a platform operator with no membership — is denied
with a 403 and the decision is audited. Reads require only membership (``candidate.read``). The path
``business_id`` is the row scope; one business can never read or change another's limits.

Validation is in the store and runs before anything is staged: a non-INR currency, a negative or
incoherent cap, or a non-India timezone is a clear 422 with a stable code. Successful changes stage an
atomic DCA-026 audit entry in the same unit of work. Writes are retry-safe (DCA-035 ``Idempotency-Key``).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.api.idempotency import IdempotencyContext, commit_idempotent, idempotency
from app.audit.sink import ActorType, AuditActor, AuditPayloadError
from app.core.security import require_business_permission_on_path
from app.db.models import BudgetLimit
from app.identity.permissions import Scope
from app.identity.tenant_context import TenantContext
from app.services.budget_store import (
    CANONICAL_TIMEZONE,
    SUPPORTED_CURRENCY,
    BudgetLimitInput,
    BudgetLimitStore,
    BudgetValidationError,
)

router = APIRouter(prefix="/v1/tenants", tags=["budget"])

SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyDep = Annotated[IdempotencyContext, Depends(idempotency)]
# Setting/raising caps is an administrator action; reading them only needs membership.
BudgetManageDep = Annotated[
    TenantContext, Depends(require_business_permission_on_path(Scope.BUDGET_MANAGE))
]
BudgetReadDep = Annotated[
    TenantContext, Depends(require_business_permission_on_path(Scope.CANDIDATE_READ))
]


class BudgetLimitBody(BaseModel):
    # extra='forbid' so an UNSUPPORTED field (a typo / a field from a future capability) is a clear
    # 422 rather than being silently ignored.
    model_config = ConfigDict(extra="forbid")

    # INR MINOR units (paise) — integers, exact. Negative/incoherent values are rejected by the store
    # (a stable-code 422), so the rejection is auditable and reproducible rather than a bare schema error.
    dailyCapMinor: int
    monthlyCapMinor: int
    perRunCapMinor: int
    currency: str = SUPPORTED_CURRENCY
    timezone: str = CANONICAL_TIMEZONE


def _limit_view(limit: BudgetLimit) -> dict:
    return {
        "businessId": limit.business_id,
        "version": limit.version,
        "currency": limit.currency,
        "dailyCapMinor": limit.daily_cap_minor,
        "monthlyCapMinor": limit.monthly_cap_minor,
        "perRunCapMinor": limit.per_run_cap_minor,
        # Derived major-unit (rupee) view for display only; the minor-unit integer is the source of
        # truth. INR has 100 minor units per major unit.
        "dailyCapMajor": limit.daily_cap_minor / 100,
        "monthlyCapMajor": limit.monthly_cap_minor / 100,
        "perRunCapMajor": limit.per_run_cap_minor / 100,
        "timezone": limit.timezone,
        "actorId": limit.actor_id,
        "actorType": limit.actor_type,
        "createdAt": limit.created_at.isoformat() if limit.created_at else None,
    }


def _limit_summary(limit: BudgetLimit) -> dict:
    return {
        "version": limit.version,
        "currency": limit.currency,
        "dailyCapMinor": limit.daily_cap_minor,
        "monthlyCapMinor": limit.monthly_cap_minor,
        "perRunCapMinor": limit.per_run_cap_minor,
        "timezone": limit.timezone,
        "createdAt": limit.created_at.isoformat() if limit.created_at else None,
    }


@router.post("/{business_id}/budget/limits", status_code=201)
def set_budget_limits(
    business_id: str,
    body: BudgetLimitBody,
    context: BudgetManageDep,
    session: SessionDep,
    idem: IdempotencyDep,
) -> dict:
    """Store a NEW version of this business's spending limits (administrator only).

    Requires the ``budget.manage`` scope: an ordinary user or a non-member is denied (403, audited)
    before any row is touched. A non-INR currency, a negative/incoherent cap, or a non-India timezone
    is a clear 422 (stable code), staging nothing. On success a new version is inserted (prior versions
    preserved) and an atomic audit entry is recorded in the same unit of work.
    """
    store = BudgetLimitStore(session, context.business_id)
    actor = AuditActor(id=context.subject, type=ActorType.USER)
    try:
        limit = store.save(
            BudgetLimitInput(
                daily_cap_minor=body.dailyCapMinor,
                monthly_cap_minor=body.monthlyCapMinor,
                per_run_cap_minor=body.perRunCapMinor,
                currency=body.currency,
                timezone=body.timezone,
            ),
            actor,
        )
    except BudgetValidationError as exc:
        # Unsupported currency / negative or incoherent cap / non-India timezone: a clear 422, staged
        # nothing (the store validates before touching the session).
        raise HTTPException(status_code=422, detail=exc.code) from exc
    except (
        AuditPayloadError
    ) as exc:  # pragma: no cover - subject is an opaque id from the verified token
        raise HTTPException(status_code=422, detail="actor_id_not_opaque") from exc
    # Flush so the DB-assigned fields (created_at) are populated for the response view.
    session.flush()
    return commit_idempotent(session, idem, _limit_view(limit), status_code=201)


@router.get("/{business_id}/budget/limits")
def get_budget_limits(business_id: str, context: BudgetReadDep, session: SessionDep) -> dict:
    """The business's current (in-force, highest-version) spending limits, or none if unset."""
    limit = BudgetLimitStore(session, context.business_id).latest()
    if limit is None:
        return {"businessId": context.business_id, "configured": False}
    return {"configured": True, **_limit_view(limit)}


@router.get("/{business_id}/budget/limits/versions")
def list_budget_limit_versions(
    business_id: str, context: BudgetReadDep, session: SessionDep
) -> list[dict]:
    """Every spending-limit version for this business, oldest first (immutable history)."""
    return [_limit_summary(v) for v in BudgetLimitStore(session, context.business_id).versions()]


@router.get("/{business_id}/budget/limits/versions/{version}")
def get_budget_limit_version(
    business_id: str, version: int, context: BudgetReadDep, session: SessionDep
) -> dict:
    """One historical spending-limit version within this business (404 if unknown)."""
    limit = BudgetLimitStore(session, context.business_id).get_version(version)
    if limit is None:
        raise HTTPException(status_code=404, detail="budget_limit_version_not_found")
    return _limit_view(limit)
