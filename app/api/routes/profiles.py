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

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.api.idempotency import IdempotencyContext, commit_idempotent, idempotency
from app.core.logging import get_correlation_id
from app.ai import AiCapabilityFacade
from app.core.security import require_service_auth
from app.db.models import BusinessProfileDraft, TenantAgent
from app.profiles import PlatformProfileClient
from app.services.profile_drafter import (
    REASON_CAPABILITY_DISABLED,
    RetrievedEvidence,
    draft_profile_from_evidence,
)
from app.services.profile_importer import (
    REASON_IMPORT_CONTRACT_DISABLED,
    ProfileImportError,
    import_profile_snapshot,
)
from app.services.profile_store import ProfileDraftInput, ProfileError, ProfileStore

router = APIRouter(
    prefix="/v1/tenants", tags=["profiles"], dependencies=[Depends(require_service_auth)]
)

SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyDep = Annotated[IdempotencyContext, Depends(idempotency)]


def _platform_profile_client(request: Request) -> PlatformProfileClient | None:
    """The DCA-037 platform-profile client, or ``None`` when the contract is disabled (fail-closed)."""
    return getattr(request.app.state, "platform_profile_client", None)


ProfileClientDep = Annotated[PlatformProfileClient | None, Depends(_platform_profile_client)]


def _ai_capability_facade(request: Request) -> AiCapabilityFacade | None:
    """The DCA-128 AI-capability facade, or ``None`` when AI is disabled (fail-closed)."""
    return getattr(request.app.state, "ai_capability_facade", None)


AiFacadeDep = Annotated[AiCapabilityFacade | None, Depends(_ai_capability_facade)]


class ProfileDraftBody(BaseModel):
    products: list[str] = []
    businessType: str = ""
    valueProposition: str = ""
    languages: list[str] = []
    confirmedFacts: list[str] = []
    # Aspects the caller explicitly declares not-yet-known; the store also auto-retains any empty one.
    unknowns: list[str] = []


class DraftEvidenceItem(BaseModel):
    ref: str
    content: str
    permittedFields: list[str] = []
    rightsValid: bool = True
    provenance: str | None = None


class DraftFromEvidenceBody(BaseModel):
    businessName: str
    domain: str | None = None
    evidence: list[DraftEvidenceItem] = []
    # Advisory hint of which profile aspects to draft (the model returns unknowns for the rest).
    draftFields: list[str] = []


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
        # DCA-037 provenance — empty for a manually-authored draft; populated on an import.
        "source": draft.source,
        "sourceVersion": draft.source_version,
        "provenance": draft.provenance or {},
        "createdAt": draft.created_at.isoformat() if draft.created_at else None,
    }


def _draft_summary(draft: BusinessProfileDraft) -> dict:
    return {
        "version": draft.version,
        "status": draft.status,
        "unknowns": draft.unknowns,
        "source": draft.source,
        "sourceVersion": draft.source_version,
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


@router.post("/{business_id}/profile/import", status_code=201)
def import_profile(
    business_id: str,
    request: Request,
    session: SessionDep,
    idem: IdempotencyDep,
    client: ProfileClientDep,
) -> dict:
    """DCA-037 — import an authorized platform profile snapshot into a new, provenance-bearing draft.

    Fetches the business's own profile + Brand KB facts through the typed platform-profile contract
    (the fake today; no host-table access) and stages them as a new draft version that retains the
    snapshot's source version and business scope and stays a ``draft``. Retry-safe (DCA-035).

    Fail-closed with no state change on refusal: a disabled contract (production) → 503; the platform
    reporting no profile for the business → 404. Both refuse before any row is staged.
    """
    _require_tenant(session, business_id)
    try:
        draft = import_profile_snapshot(
            session, client, business_id, correlation_id=get_correlation_id()
        )
    except ProfileImportError as exc:
        # A disabled contract is a disabled capability (503); an absent source profile is a 404.
        status = 503 if exc.code == REASON_IMPORT_CONTRACT_DISABLED else 404
        raise HTTPException(status_code=status, detail=exc.code) from exc
    # Flush so the DB-assigned fields (created_at) are populated for the response view.
    session.flush()
    return commit_idempotent(session, idem, _draft_view(draft), status_code=201)


@router.post("/{business_id}/profile/draft-from-evidence", status_code=201)
def draft_from_evidence(
    business_id: str,
    body: DraftFromEvidenceBody,
    request: Request,
    session: SessionDep,
    idem: IdempotencyDep,
    facade: AiFacadeDep,
) -> dict:
    """DCA-134 — draft a business profile grounded in authorised evidence (AI-capability integration).

    Runs the ``business_profile_draft`` capability through the one DCA AI facade: the business identity
    is the trusted context, the supplied evidence bodies ride the untrusted channel, and each
    model-proposed claim is independently re-grounded against the evidence (DCA-133) before anything is
    stored. The result is a NEW, reviewable draft version carrying its uncertainty (confidence) and
    references (grounded citations) plus what was dropped and why — never an auto-confirmed profile.
    Retry-safe (DCA-035).

    Fail-closed with NO state change on refusal: the capability/transport disabled → 503; a rejected
    (unsafe/invalid) gateway output → 502. Both refuse before any draft is staged.
    """
    _require_tenant(session, business_id)
    evidence = [
        RetrievedEvidence(
            ref=item.ref,
            content=item.content,
            permitted_fields=frozenset(item.permittedFields),
            rights_valid=item.rightsValid,
            provenance=(item.provenance or "").strip(),
        )
        for item in body.evidence
    ]
    # A deterministic DCA-owned invocation key so a retry replays the same gateway request id. Prefer the
    # client's Idempotency-Key (one logical draft = one invocation); else a per-request correlation id.
    correlation_id = get_correlation_id()
    invocation_key = (idem.key or correlation_id or f"{business_id}:draft").strip()
    result = draft_profile_from_evidence(
        session,
        facade,
        business_id,
        business_name=body.businessName,
        evidence=evidence,
        invocation_key=f"profile-draft:{business_id}:{invocation_key}",
        domain=body.domain,
        draft_fields=body.draftFields,
        correlation_id=correlation_id,
    )
    if result.draft is None:
        # Fail-closed: a disabled capability is 503; an unsafe/invalid gateway output is 502. Nothing staged.
        status = 503 if result.refused_reason == REASON_CAPABILITY_DISABLED else 502
        raise HTTPException(status_code=status, detail=result.refused_reason)
    session.flush()  # materialize created_at for the response view
    view = _draft_view(result.draft)
    view["grounding"] = {
        "grounded": [
            {
                "field": f.field_key,
                "citationRef": f.citation_ref,
                "inferenceLabel": f.inference_label,
            }
            for f in result.grounded
        ],
        "dropped": [{"field": v.fact.field_key, "reason": v.reason} for v in result.dropped],
        "heldForReview": bool(result.outcome.held_for_review) if result.outcome else False,
        "overallConfidence": result.overall_confidence,
        "modelProvider": result.model_provider,
        "synthetic": result.synthetic,
    }
    return commit_idempotent(session, idem, view, status_code=201)


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
