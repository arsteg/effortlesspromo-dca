"""DCA-063 — rights-aware record & evidence projection: the read-only service API.

Structured HTTP contract (for later Web/Admin consumers) that serves a subject's **record** (its
field observations) and its **evidence objects** back to a business through the DCA-063 display
projection (:mod:`app.projection.field_visibility`). All routes require X-Service-Auth (fail-closed)
and treat the path ``business_id`` purely as the row scope — one business can never read another's
subject, fields, or evidence through these routes (a cross-tenant / unknown id reads as 404).

The load-bearing guarantee (the DCA-063 acceptance criterion): a restricted value is **absent from
the payload**, not merely hidden. A field or evidence payload carries the ``value`` / ``rawContent``
key ONLY when the business holds a DISPLAY entitlement (DCA-047) for that licensed ``(source, field)``
— a restricted licensed fact is serialized with ``restricted: true`` and a stable reason but **no**
value key at all. A private overlay (the business's own un-sourced data) is always shown. These routes
are read-only: no row is read-modified and nothing is written, so a result and its denial are fully
reproducible with no side effects (future acquisition/write capabilities stay disabled).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.core.security import require_service_auth
from app.db.models import FieldSubjectType, TenantAgent
from app.projection.field_visibility import (
    ProjectedEvidence,
    ProjectedField,
    display_decider_from_permits,
    project_evidence,
    project_fields,
)
from app.services.contact_point import ContactPointStore
from app.services.evidence_store import EvidenceObjectStore
from app.services.field_observation import FieldObservationStore
from app.services.org_observation import OrgObservationStore
from app.services.source_entitlement import BusinessSourceEntitlementStore

router = APIRouter(
    prefix="/v1/tenants", tags=["records"], dependencies=[Depends(require_service_auth)]
)

SessionDep = Annotated[Session, Depends(get_session)]

# The subject kinds a record route accepts in its path (the controlled DCA-061 vocabulary).
SUBJECT_TYPES: frozenset[str] = frozenset(t.value for t in FieldSubjectType)


def _require_tenant(session: Session, business_id: str) -> TenantAgent:
    tenant = session.scalar(select(TenantAgent).where(TenantAgent.business_id == business_id))
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant agent not registered")
    return tenant


def _display_decider(session: Session, business_id: str):
    """The DCA-063 decider for this business: wires the DCA-047 ``permits(..., 'display')`` read.

    Deny-by-default — the intersection of the platform ceiling and the business's own narrowing — so a
    field with no display entitlement is withheld. Resolved once per request; a pure read.
    """
    store = BusinessSourceEntitlementStore(session, business_id)
    return display_decider_from_permits(store.permits)


def _subject_exists(session: Session, business_id: str, subject_type: str, subject_id: str) -> bool:
    """Whether ``(subject_type, subject_id)`` names a subject in THIS business (cross-tenant => False)."""
    if subject_type == FieldSubjectType.ORGANIZATION.value:
        return OrgObservationStore(session, business_id).get(subject_id) is not None
    return ContactPointStore(session, business_id).get_person(subject_id) is not None


def _field_view(field: ProjectedField) -> dict:
    """Serialize one projected field. The ``value`` key is present ONLY for a visible field — a
    restricted value is absent from the payload entirely (not nulled), with the denial made explicit."""
    view: dict = {
        "fieldKey": field.field_key,
        "inferenceLabel": field.inference_label,
        "source": field.source_key or None,
        "sourceVersion": field.source_version,
        "scope": field.scope,
        "observedAt": field.observed_at.isoformat() if field.observed_at else None,
        # A private overlay is the business's own un-sourced data; a licensed shared fact carries a source.
        "overlay": field.overlay,
        "visible": field.visible,
    }
    if field.visible:
        view["value"] = field.value
    else:
        view["restricted"] = True
        view["restrictedReason"] = field.restricted_reason
    return view


def _evidence_view(evidence: ProjectedEvidence) -> dict:
    """Serialize one projected evidence object. ``rawContent`` is present ONLY when display-entitled and
    the payload was retained; otherwise it is absent from the payload and the denial is made explicit."""
    view: dict = {
        "evidenceId": evidence.evidence_id,
        "source": evidence.source_key,
        "sourceVersion": evidence.source_version,
        "subjectType": evidence.subject_type or None,
        "subjectId": evidence.subject_id or None,
        "contentKind": evidence.content_kind,
        "contentRef": evidence.content_ref,
        "contentDigest": evidence.content_digest,
        "contentMediaType": evidence.content_media_type or None,
        "retentionMode": evidence.retention_mode,
        "retentionReason": evidence.retention_reason,
        "rawVisible": evidence.raw_visible,
    }
    if evidence.raw_visible:
        view["rawContent"] = evidence.raw_content
    else:
        view["rawRestricted"] = True
        view["rawRestrictedReason"] = evidence.restricted_reason
    return view


@router.get("/{business_id}/records/{subject_type}/{subject_id}")
def get_record(business_id: str, subject_type: str, subject_id: str, session: SessionDep) -> dict:
    """Serve a subject's record (its field observations) through the DCA-063 display projection.

    Every licensed shared fact is screened against this business's DISPLAY entitlement (DCA-047); a
    restricted value is absent from the payload. Private overlays (the business's own un-sourced data)
    are always shown. A subject that does not exist in this business — including another tenant's
    subject — reads as 404. Read-only: nothing is written, so the result is fully reproducible.
    """
    _require_tenant(session, business_id)
    subject_type = (subject_type or "").strip().lower()
    if subject_type not in SUBJECT_TYPES:
        raise HTTPException(status_code=404, detail="unknown_subject_type")
    if not _subject_exists(session, business_id, subject_type, subject_id):
        # A cross-tenant / unknown subject reads exactly like not-found — never surfaced.
        raise HTTPException(status_code=404, detail="subject_not_found")

    observations = FieldObservationStore(session, business_id).for_subject(subject_type, subject_id)
    fields = project_fields(observations, _display_decider(session, business_id))
    visible = sum(1 for f in fields if f.visible)
    return {
        "businessId": business_id,
        "subjectType": subject_type,
        "subjectId": subject_id,
        "fields": [_field_view(f) for f in fields],
        # A summary a consumer can trust: how many fields were withheld by the display projection.
        "fieldCount": len(fields),
        "visibleFieldCount": visible,
        "restrictedFieldCount": len(fields) - visible,
    }


@router.get("/{business_id}/evidence/{evidence_id}")
def get_evidence(business_id: str, evidence_id: str, session: SessionDep) -> dict:
    """Serve one evidence object through the DCA-063 display projection.

    The raw captured payload is served back ONLY when this business holds a DISPLAY entitlement
    (DCA-047) for the evidence's ``(source, content_kind)`` AND the payload was retained (DCA-062);
    otherwise ``rawContent`` is absent from the payload. The locator / digest / retention metadata
    stays visible as traceability. Another tenant's (or an unknown) evidence id reads as 404.
    """
    _require_tenant(session, business_id)
    evidence = EvidenceObjectStore(session, business_id).get(evidence_id)
    if evidence is None:
        # A cross-tenant / unknown id reads exactly like not-found — never surfaced.
        raise HTTPException(status_code=404, detail="evidence_not_found")
    projected = project_evidence(evidence, _display_decider(session, business_id))
    return _evidence_view(projected)
