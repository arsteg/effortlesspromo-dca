"""DCA-036 — versioned business-profile drafts: the service API.

Structured HTTP contract (for later Web/Admin consumers) over
:class:`~app.services.profile_store.ProfileStore`. All routes require X-Service-Auth (fail-closed) and
treat the path ``business_id`` purely as the row scope — one business can never read or mutate
another's profile versions through these routes.

Writes are retry-safe (DCA-035 ``Idempotency-Key``). Saving a draft always creates a NEW version and
preserves prior ones; it never creates or replaces the confirmed profile. Confirming a draft is a
DISABLED future capability — its endpoint refuses with a stable code and changes nothing.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.api.idempotency import IdempotencyContext, commit_idempotent, idempotency
from app.core.security import require_service_auth
from app.db.models import BusinessProfileDraft, TenantAgent
from app.services.profile_store import ProfileDraftInput, ProfileError, ProfileStore

router = APIRouter(
    prefix="/v1/tenants", tags=["profiles"], dependencies=[Depends(require_service_auth)]
)

SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyDep = Annotated[IdempotencyContext, Depends(idempotency)]


class ProfileDraftBody(BaseModel):
    products: list[str] = []
    businessType: str = ""
    valueProposition: str = ""
    languages: list[str] = []
    confirmedFacts: list[str] = []
    # Aspects the caller explicitly declares not-yet-known; the store also auto-retains any empty one.
    unknowns: list[str] = []


def _require_tenant(session: Session, business_id: str) -> TenantAgent:
    tenant = session.scalar(select(TenantAgent).where(TenantAgent.business_id == business_id))
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant agent not registered")
    return tenant


def _draft_view(draft: BusinessProfileDraft) -> dict:
    return {
        "businessId": draft.business_id,
        "version": draft.version,
        "status": draft.status,
        "products": draft.products,
        "businessType": draft.business_type,
        "valueProposition": draft.value_proposition,
        "languages": draft.languages,
        "confirmedFacts": draft.confirmed_facts,
        "unknowns": draft.unknowns,
        "createdAt": draft.created_at.isoformat() if draft.created_at else None,
    }


def _draft_summary(draft: BusinessProfileDraft) -> dict:
    return {
        "version": draft.version,
        "status": draft.status,
        "unknowns": draft.unknowns,
        "createdAt": draft.created_at.isoformat() if draft.created_at else None,
    }


@router.post("/{business_id}/profile/drafts", status_code=201)
def save_draft(
    business_id: str, body: ProfileDraftBody, session: SessionDep, idem: IdempotencyDep
) -> dict:
    _require_tenant(session, business_id)
    store = ProfileStore(session, business_id)
    draft = store.save_draft(
        ProfileDraftInput(
            products=body.products,
            business_type=body.businessType,
            value_proposition=body.valueProposition,
            languages=body.languages,
            confirmed_facts=body.confirmedFacts,
            unknowns=body.unknowns,
        )
    )
    # Flush so the DB-assigned fields (created_at) are populated for the response view.
    session.flush()
    return commit_idempotent(session, idem, _draft_view(draft), status_code=201)


@router.get("/{business_id}/profile/drafts")
def list_drafts(business_id: str, session: SessionDep) -> list[dict]:
    _require_tenant(session, business_id)
    return [_draft_summary(d) for d in ProfileStore(session, business_id).versions()]


@router.get("/{business_id}/profile/drafts/{version}")
def get_draft(business_id: str, version: int, session: SessionDep) -> dict:
    _require_tenant(session, business_id)
    draft = ProfileStore(session, business_id).get_version(version)
    if draft is None:
        raise HTTPException(status_code=404, detail="profile_draft_version_not_found")
    return _draft_view(draft)


@router.get("/{business_id}/profile/confirmed")
def get_confirmed(business_id: str, session: SessionDep) -> dict:
    _require_tenant(session, business_id)
    confirmed = ProfileStore(session, business_id).confirmed()
    return {
        "businessId": business_id,
        "confirmed": confirmed is not None,
        "profile": _draft_view(confirmed) if confirmed is not None else None,
        # Explicit: saving drafts never creates a confirmed profile (confirmation is disabled).
        "note": "no confirmed profile exists until the (disabled) confirmation capability ships",
    }


@router.post("/{business_id}/profile/drafts/{version}/confirm")
def confirm_draft(business_id: str, version: int, session: SessionDep) -> dict:
    """Promote a draft to the confirmed profile — a DISABLED future capability.

    Refuses before touching any state (no idempotency record, no row), so the denial is reproducible
    and a draft can never silently replace the confirmed profile.
    """
    _require_tenant(session, business_id)
    try:
        ProfileStore(session, business_id).confirm(version)
    except ProfileError as exc:
        raise HTTPException(status_code=403, detail=exc.code) from exc
    raise AssertionError("unreachable: confirm always refuses in this build")  # pragma: no cover
