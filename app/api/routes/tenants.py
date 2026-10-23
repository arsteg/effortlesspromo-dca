"""Tenant-agent management + discovery output — the DCA's service API.

All routes require X-Service-Auth (fail-closed). The intended caller is the platform (.NET API)
acting on behalf of an already-authorized user; per DCA-001 B1 no such service path exists yet, so
in practice this surface is exercised by tests/ops only. It never trusts a businessId beyond using
it as the row scope — one business can never read or mutate another's rows through these routes.

Candidate "accept" is a DCA-local status change: it is NOT recipient consent and does not place the
prospect into any platform audience (that export is a later, platform-side story).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.core.security import require_service_auth
from app.db.models import DiscoveryRun, IcpProfile, ProspectCandidate, TenantAgent
from app.services.discovery import DiscoveryError, run_discovery

router = APIRouter(
    prefix="/v1/tenants", tags=["tenants"], dependencies=[Depends(require_service_auth)]
)

SessionDep = Annotated[Session, Depends(get_session)]


class RegisterTenant(BaseModel):
    businessId: str = Field(min_length=1)
    country: str = "IN"


class IcpBody(BaseModel):
    industries: list[str] = []
    regions: list[str] = []
    roles: list[str] = []
    exclusions: list[str] = []
    status: str = "active"


def _tenant(session: Session, business_id: str) -> TenantAgent:
    tenant = session.scalar(select(TenantAgent).where(TenantAgent.business_id == business_id))
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant agent not registered")
    return tenant


@router.post("", status_code=201)
def register(body: RegisterTenant, request: Request, session: SessionDep) -> dict:
    settings = request.app.state.settings
    if body.country not in settings.allowed_countries:
        raise HTTPException(status_code=422, detail="country_not_allowed: this build is India-only")
    existing = session.scalar(select(TenantAgent).where(TenantAgent.business_id == body.businessId))
    if existing is not None:
        return {"businessId": existing.business_id, "enabled": existing.enabled, "created": False}
    tenant = TenantAgent(business_id=body.businessId, country=body.country, enabled=False)
    session.add(tenant)
    session.commit()
    return {"businessId": tenant.business_id, "enabled": tenant.enabled, "created": True}


@router.post("/{business_id}/enable")
def enable(business_id: str, session: SessionDep) -> dict:
    tenant = _tenant(session, business_id)
    tenant.enabled = True
    session.commit()
    return {"businessId": business_id, "enabled": True}


@router.post("/{business_id}/disable")
def disable(business_id: str, session: SessionDep) -> dict:
    tenant = _tenant(session, business_id)
    tenant.enabled = False
    session.commit()
    return {"businessId": business_id, "enabled": False}


@router.put("/{business_id}/icp")
def upsert_icp(business_id: str, body: IcpBody, session: SessionDep) -> dict:
    _tenant(session, business_id)
    icp = session.scalar(select(IcpProfile).where(IcpProfile.business_id == business_id))
    if icp is None:
        icp = IcpProfile(business_id=business_id)
        session.add(icp)
    else:
        icp.version += 1
    icp.industries, icp.regions = body.industries, body.regions
    icp.roles, icp.exclusions = body.roles, body.exclusions
    icp.status = body.status
    session.commit()
    return {"businessId": business_id, "version": icp.version, "status": icp.status}


@router.post("/{business_id}/discovery/run")
def trigger_discovery(business_id: str, request: Request, session: SessionDep) -> dict:
    try:
        run = run_discovery(session, request.app.state.settings, business_id)
    except DiscoveryError as exc:
        raise HTTPException(status_code=409, detail=exc.code) from exc
    return {
        "runId": run.id,
        "status": run.status,
        "found": run.found,
        "screenedOut": run.screened_out,
        "suggested": run.suggested,
        "sources": run.source_keys,
    }


@router.get("/{business_id}/runs")
def list_runs(business_id: str, session: SessionDep) -> list[dict]:
    runs = session.scalars(
        select(DiscoveryRun)
        .where(DiscoveryRun.business_id == business_id)
        .order_by(DiscoveryRun.started_at.desc())
    ).all()
    return [
        {
            "runId": r.id,
            "status": r.status,
            "found": r.found,
            "screenedOut": r.screened_out,
            "suggested": r.suggested,
            "startedAt": r.started_at.isoformat(),
        }
        for r in runs
    ]


@router.get("/{business_id}/candidates")
def list_candidates(
    business_id: str, session: SessionDep, status: str | None = None
) -> list[dict]:
    query = select(ProspectCandidate).where(ProspectCandidate.business_id == business_id)
    if status:
        query = query.where(ProspectCandidate.status == status)
    rows = session.scalars(query.order_by(ProspectCandidate.created_at.desc())).all()
    return [
        {
            "id": c.id,
            "orgName": c.org_name,
            "orgIdentifier": c.org_identifier,
            "contact": c.contact,
            "sourceKey": c.source_key,
            "sourceRef": c.source_ref,
            "trustTier": c.trust_tier,
            "matchScore": c.match_score,
            "rationale": c.rationale,
            "status": c.status,
            "expiresAt": c.expires_at.isoformat() if c.expires_at else None,
        }
        for c in rows
    ]


def _candidate(session: Session, business_id: str, candidate_id: str) -> ProspectCandidate:
    candidate = session.get(ProspectCandidate, candidate_id)
    # Tenant isolation: a candidate id from another business is a 404, never a cross-tenant read.
    if candidate is None or candidate.business_id != business_id:
        raise HTTPException(status_code=404, detail="candidate not found")
    return candidate


@router.post("/{business_id}/candidates/{candidate_id}/accept")
def accept_candidate(business_id: str, candidate_id: str, session: SessionDep) -> dict:
    candidate = _candidate(session, business_id, candidate_id)
    if candidate.status != "suggested":
        raise HTTPException(status_code=409, detail=f"candidate is {candidate.status}")
    expires = candidate.expires_at
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)  # SQLite returns naive datetimes
    if expires is not None and expires <= datetime.now(UTC):
        candidate.status = "expired"
        session.commit()
        raise HTTPException(status_code=409, detail="candidate is expired")
    candidate.status = "accepted"
    session.commit()
    return {
        "id": candidate.id,
        "status": candidate.status,
        "consent": False,
        "note": "acceptance is not recipient consent; sending rights are platform-gated",
    }


@router.post("/{business_id}/candidates/{candidate_id}/dismiss")
def dismiss_candidate(business_id: str, candidate_id: str, session: SessionDep) -> dict:
    candidate = _candidate(session, business_id, candidate_id)
    if candidate.status not in ("suggested", "accepted"):
        raise HTTPException(status_code=409, detail=f"candidate is {candidate.status}")
    candidate.status = "dismissed"
    session.commit()
    return {"id": candidate.id, "status": candidate.status}
