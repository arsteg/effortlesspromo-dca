"""DCA-140 — bounded discovery runs: the service API.

Structured HTTP contract (for later Web/Admin consumers + workers) over
:func:`~app.services.discovery_run.create_discovery_run`. All routes require X-Service-Auth
(fail-closed) and treat the path ``business_id`` purely as the row scope — one business can never read
or create another's runs through these routes.

Creating a run persists a bounded :class:`~app.db.models.DiscoveryRunRequest` in the not-yet-executing
PENDING state, grounded in the business's CONFIRMED ICP and bounded to the sources it is already
approved+entitled to within the (clamped) scope / budget caps. Creating a run executes NOTHING: it runs
no task, enqueues no job, suggests no prospect, sends no message and spends nothing. Advancing the run is
the later, still-disabled DCA-141..149 pipeline. Retry-safe (DCA-035 ``Idempotency-Key``).

This sits alongside the legacy deterministic ``POST .../discovery/run`` cycle (singular); the new
pipeline envelope is ``.../discovery/runs`` (plural).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.api.idempotency import IdempotencyContext, commit_idempotent, idempotency
from app.core.security import require_service_auth
from app.db.models import DiscoveryRunRequest, TenantAgent
from app.services.discovery_run import (
    ENTITLEMENT_REASON_PREFIX,
    REASON_FEATURE_DISABLED,
    DiscoveryRunResult,
    DiscoveryRunStore,
    create_discovery_run,
)

router = APIRouter(
    prefix="/v1/tenants", tags=["discovery-runs"], dependencies=[Depends(require_service_auth)]
)

SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyDep = Annotated[IdempotencyContext, Depends(idempotency)]


class CreateRunBody(BaseModel):
    # extra='forbid' so an UNSUPPORTED field (a typo / a field from a future pipeline story) is a clear
    # 422 rather than being silently ignored.
    model_config = ConfigDict(extra="forbid")

    # Optional scope / budget caps the caller may request; each is clamped to the creator's absolute
    # ceiling, so a run can never exceed the scope / budget limits regardless of what is asked for.
    maxTargets: int | None = Field(default=None, ge=0)
    maxTasks: int | None = Field(default=None, ge=0)
    maxQueriesPerTask: int | None = Field(default=None, ge=1)
    budgetMinor: int | None = Field(default=None, ge=0)


def _require_tenant(session: Session, business_id: str) -> TenantAgent:
    tenant = session.scalar(select(TenantAgent).where(TenantAgent.business_id == business_id))
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant agent not registered")
    return tenant


def _run_view(run: DiscoveryRunRequest) -> dict:
    return {
        "businessId": run.business_id,
        "runId": run.id,
        "status": run.status,
        "icpVersion": run.icp_version,
        "sourceScope": run.source_scope,
        "targetCap": run.target_cap,
        "taskCap": run.task_cap,
        "queryCap": run.query_cap,
        "perRunBudgetMinor": run.per_run_budget_minor,
        "budgetLimitVersion": run.budget_limit_version,
        "requested": run.requested,
        "clamped": run.clamped,
        "clampNotes": run.clamp_notes,
        "provenance": run.provenance or {},
        # Explicit: a run is CREATED only — never executed at creation.
        "executed": False,
        "createdAt": run.created_at.isoformat() if run.created_at else None,
    }


def _run_summary(run: DiscoveryRunRequest) -> dict:
    return {
        "runId": run.id,
        "status": run.status,
        "icpVersion": run.icp_version,
        "sourceCount": len(run.source_scope or []),
        "clamped": run.clamped,
        "createdAt": run.created_at.isoformat() if run.created_at else None,
    }


@router.post("/{business_id}/discovery/runs", status_code=201)
def create_run(
    business_id: str,
    body: CreateRunBody,
    request: Request,
    session: SessionDep,
    idem: IdempotencyDep,
) -> dict:
    """DCA-140 — create a bounded discovery run in the not-yet-executing PENDING state.

    Grounds the run in the confirmed ICP, bounds it to the business's approved+entitled source scope and
    the (clamped) scope / budget caps, and stages a NEW run. Creating a run executes nothing: no task, no
    job, no suggestion, no send, no spend. Retry-safe (DCA-035).

    Fail-closed with NO state change on refusal: the feature disabled / entitlement check failed → 403;
    the tenant not enabled, out of country, no confirmed ICP, or an existing active run for the same
    scope → 409. Each refuses before any run is staged.
    """
    _require_tenant(session, business_id)
    result: DiscoveryRunResult = create_discovery_run(
        session,
        request.app.state.settings,
        business_id,
        entitlement_client=getattr(request.app.state, "entitlement_client", None),
        target_cap=body.maxTargets,
        task_cap=body.maxTasks,
        query_cap=body.maxQueriesPerTask,
        requested_budget_minor=body.budgetMinor,
    )
    if result.run is None:
        reason = result.refused_reason or ""
        # Gate denials (feature off / entitlement) are 403 (not permitted); other refusals are 409
        # conflicts, matching the legacy deterministic cycle's mapping.
        gated = reason == REASON_FEATURE_DISABLED or reason.startswith(ENTITLEMENT_REASON_PREFIX)
        raise HTTPException(status_code=403 if gated else 409, detail=reason)
    session.flush()  # materialize created_at for the response view
    return commit_idempotent(session, idem, _run_view(result.run), status_code=201)


@router.get("/{business_id}/discovery/runs")
def list_runs(business_id: str, session: SessionDep) -> list[dict]:
    _require_tenant(session, business_id)
    return [_run_summary(r) for r in DiscoveryRunStore(session, business_id).runs()]


@router.get("/{business_id}/discovery/runs/{run_id}")
def get_run(business_id: str, run_id: str, session: SessionDep) -> dict:
    _require_tenant(session, business_id)
    run = DiscoveryRunStore(session, business_id).get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="discovery_run_not_found")
    return _run_view(run)
