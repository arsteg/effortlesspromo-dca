"""DCA-039 — versioned ideal-customer-profile (ICP) drafts: the service API.

Structured HTTP contract (for later Web/Admin consumers) over
:class:`~app.services.icp_store.IcpStore`. All routes require X-Service-Auth (fail-closed) and treat
the path ``business_id`` purely as the row scope — one business can never read or mutate another's ICP
versions through these routes.

Saving a draft always creates a NEW version and preserves prior ones. The request body is
``extra='forbid'`` so an **unsupported field** is a clear 422; the store rejects an **invalid size
band**, a non-India **geography**, or an unknown linked **profile version** with a stable code (422) —
each before any row is staged. Writes are retry-safe (DCA-035 ``Idempotency-Key``).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.api.idempotency import IdempotencyContext, commit_idempotent, idempotency
from app.core.security import require_service_auth
from app.db.models import IcpDraft, TenantAgent
from app.services.icp_store import (
    SIZE_BANDS,
    IcpDraftInput,
    IcpStore,
    IcpValidationError,
)

router = APIRouter(prefix="/v1/tenants", tags=["icp"], dependencies=[Depends(require_service_auth)])

SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyDep = Annotated[IdempotencyContext, Depends(idempotency)]


class IcpDraftBody(BaseModel):
    # extra='forbid' so an UNSUPPORTED field (a typo / a field from a future capability) is a clear
    # 422 rather than being silently ignored — an acceptance criterion of this story.
    model_config = ConfigDict(extra="forbid")

    industries: list[str] = []
    sizeBands: list[str] = []
    roles: list[str] = []
    geography: list[str] = []
    exclusions: list[str] = []
    # Optional link to the DCA-036 business-profile draft version this ICP was authored against.
    profileVersion: int | None = None


def _require_tenant(session: Session, business_id: str) -> TenantAgent:
    tenant = session.scalar(select(TenantAgent).where(TenantAgent.business_id == business_id))
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant agent not registered")
    return tenant


def _draft_view(draft: IcpDraft) -> dict:
    return {
        "businessId": draft.business_id,
        "version": draft.version,
        "status": draft.status,
        "industries": draft.industries,
        "sizeBands": draft.size_bands,
        "roles": draft.roles,
        "geography": draft.geography,
        "exclusions": draft.exclusions,
        "profileVersion": draft.profile_version,
        "createdAt": draft.created_at.isoformat() if draft.created_at else None,
    }


def _draft_summary(draft: IcpDraft) -> dict:
    return {
        "version": draft.version,
        "status": draft.status,
        "profileVersion": draft.profile_version,
        "createdAt": draft.created_at.isoformat() if draft.created_at else None,
    }


@router.post("/{business_id}/icp/drafts", status_code=201)
def save_icp_draft(
    business_id: str, body: IcpDraftBody, session: SessionDep, idem: IdempotencyDep
) -> dict:
    _require_tenant(session, business_id)
    store = IcpStore(session, business_id)
    try:
        draft = store.save_draft(
            IcpDraftInput(
                industries=body.industries,
                size_bands=body.sizeBands,
                roles=body.roles,
                geography=body.geography,
                exclusions=body.exclusions,
                profile_version=body.profileVersion,
            )
        )
    except IcpValidationError as exc:
        # Invalid band / non-India geography / unknown linked profile version: a clear 422, staged
        # nothing (the store validates before touching the session).
        raise HTTPException(status_code=422, detail=exc.code) from exc
    # Flush so the DB-assigned fields (created_at) are populated for the response view.
    session.flush()
    return commit_idempotent(session, idem, _draft_view(draft), status_code=201)


@router.get("/{business_id}/icp/drafts")
def list_icp_drafts(business_id: str, session: SessionDep) -> list[dict]:
    _require_tenant(session, business_id)
    return [_draft_summary(d) for d in IcpStore(session, business_id).versions()]


@router.get("/{business_id}/icp/drafts/{version}")
def get_icp_draft(business_id: str, version: int, session: SessionDep) -> dict:
    _require_tenant(session, business_id)
    draft = IcpStore(session, business_id).get_version(version)
    if draft is None:
        raise HTTPException(status_code=404, detail="icp_draft_version_not_found")
    return _draft_view(draft)


@router.get("/{business_id}/icp/reference")
def icp_reference(business_id: str, session: SessionDep) -> dict:
    """The controlled vocabulary a consumer validates an ICP draft against (static reference data)."""
    _require_tenant(session, business_id)
    from app.geography import india as india_ref

    return {
        "sizeBands": list(SIZE_BANDS),
        # Geography is India-only: the recognized states/UTs (ISO 3166-2:IN code -> name).
        "geography": india_ref.STATES_AND_UTS,
    }
