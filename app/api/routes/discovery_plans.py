"""DCA-135 — bounded discovery plans: the service API.

Structured HTTP contract (for later Web/Admin consumers + workers) over
:func:`~app.services.discovery_planner.plan_discovery`. All routes require X-Service-Auth (fail-closed)
and treat the path ``business_id`` purely as the row scope — one business can never read or generate
another's plans through these routes.

Generating a plan runs the ``discovery_plan`` capability through the one DCA AI facade, grounds the plan
in the business's CONFIRMED ICP, bounds it to the sources it is already approved+entitled to (and the
fields each permits) within the task/query caps, and stages a NEW draft version. A plan is a PROPOSAL:
no task is executed and it is never auto-approved — promotion/execution is a DISABLED future capability.
Retry-safe (DCA-035 ``Idempotency-Key``).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai import AiCapabilityFacade
from app.api.deps import get_session
from app.api.idempotency import IdempotencyContext, commit_idempotent, idempotency
from app.core.logging import get_correlation_id
from app.core.security import require_service_auth
from app.db.models import DiscoveryPlan, TenantAgent
from app.services.discovery_planner import (
    REASON_CAPABILITY_DISABLED,
    REASON_NO_CONFIRMED_ICP,
    PlanningResult,
    plan_discovery,
)

router = APIRouter(
    prefix="/v1/tenants", tags=["discovery-plans"], dependencies=[Depends(require_service_auth)]
)

SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyDep = Annotated[IdempotencyContext, Depends(idempotency)]


def _ai_capability_facade(request: Request) -> AiCapabilityFacade | None:
    """The DCA-128 AI-capability facade, or ``None`` when AI is disabled (fail-closed)."""
    return getattr(request.app.state, "ai_capability_facade", None)


AiFacadeDep = Annotated[AiCapabilityFacade | None, Depends(_ai_capability_facade)]


class GeneratePlanBody(BaseModel):
    # Optional caps the caller may request; each is clamped to the planner's absolute ceiling, so a plan
    # can never exceed the task/query limits regardless of what is asked for.
    maxTasks: int | None = Field(default=None, ge=0)
    maxQueriesPerTask: int | None = Field(default=None, ge=1)


def _require_tenant(session: Session, business_id: str) -> TenantAgent:
    tenant = session.scalar(select(TenantAgent).where(TenantAgent.business_id == business_id))
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant agent not registered")
    return tenant


def _plan_view(plan: DiscoveryPlan) -> dict:
    return {
        "businessId": plan.business_id,
        "version": plan.version,
        "status": plan.status,
        "icpVersion": plan.icp_version,
        "tasks": plan.tasks,
        "excluded": plan.excluded,
        "taskCap": plan.task_cap,
        "queryCap": plan.query_cap,
        "rationale": plan.rationale,
        "overallConfidence": plan.overall_confidence,
        "source": plan.source,
        "sourceVersion": plan.source_version,
        "provenance": plan.provenance or {},
        # Explicit: a plan is a proposal — never executed, never auto-approved.
        "executed": False,
        "autoApproved": False,
        "createdAt": plan.created_at.isoformat() if plan.created_at else None,
    }


def _plan_summary(plan: DiscoveryPlan) -> dict:
    return {
        "version": plan.version,
        "status": plan.status,
        "icpVersion": plan.icp_version,
        "taskCount": len(plan.tasks or []),
        "excludedCount": len(plan.excluded or []),
        "createdAt": plan.created_at.isoformat() if plan.created_at else None,
    }


@router.post("/{business_id}/discovery/plans", status_code=201)
def generate_plan(
    business_id: str,
    body: GeneratePlanBody,
    request: Request,
    session: SessionDep,
    idem: IdempotencyDep,
    facade: AiFacadeDep,
) -> dict:
    """DCA-135 — generate a bounded discovery plan grounded in the confirmed ICP (AI-capability integration).

    Runs the ``discovery_plan`` capability through the one DCA AI facade; the plan is bounded to the
    business's approved+entitled sources, their permitted fields and the (clamped) task/query caps, and
    grounded in the confirmed ICP. Every task the model proposes is re-checked and clamped/excluded DCA-side
    before storing. The result is a NEW, reviewable DRAFT version — never executed, never auto-approved.
    Retry-safe (DCA-035).

    Fail-closed with NO state change on refusal: the capability/transport disabled → 503; no confirmed ICP
    to ground the plan → 409; a rejected (unsafe/invalid) gateway output → 502. Each refuses before any
    plan is staged.
    """
    _require_tenant(session, business_id)
    correlation_id = get_correlation_id()
    invocation_key = (idem.key or correlation_id or f"{business_id}:plan").strip()
    result: PlanningResult = plan_discovery(
        session,
        facade,
        business_id,
        invocation_key=f"discovery-plan:{business_id}:{invocation_key}",
        task_cap=body.maxTasks,
        query_cap=body.maxQueriesPerTask,
        correlation_id=correlation_id,
    )
    if result.plan is None:
        # Fail-closed: disabled capability -> 503; no confirmed ICP -> 409; rejected output -> 502.
        if result.refused_reason == REASON_CAPABILITY_DISABLED:
            status = 503
        elif result.refused_reason == REASON_NO_CONFIRMED_ICP:
            status = 409
        else:
            status = 502
        raise HTTPException(status_code=status, detail=result.refused_reason)
    session.flush()  # materialize created_at for the response view
    return commit_idempotent(session, idem, _plan_view(result.plan), status_code=201)


@router.get("/{business_id}/discovery/plans")
def list_plans(business_id: str, session: SessionDep) -> list[dict]:
    _require_tenant(session, business_id)
    stmt = (
        select(DiscoveryPlan)
        .where(DiscoveryPlan.business_id == business_id)
        .order_by(DiscoveryPlan.version)
    )
    return [_plan_summary(p) for p in session.scalars(stmt).all()]


@router.get("/{business_id}/discovery/plans/{version}")
def get_plan(business_id: str, version: int, session: SessionDep) -> dict:
    _require_tenant(session, business_id)
    stmt = (
        select(DiscoveryPlan)
        .where(DiscoveryPlan.business_id == business_id)
        .where(DiscoveryPlan.version == version)
    )
    plan = session.scalars(stmt).one_or_none()
    if plan is None:
        raise HTTPException(status_code=404, detail="discovery_plan_version_not_found")
    return _plan_view(plan)
