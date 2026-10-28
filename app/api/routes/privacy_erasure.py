"""DCA-084 — erase permitted DCA operational data: the scoped-deletion execution API.

Structured HTTP contract (for a later Web/Admin operator console) over
:class:`~app.services.data_erasure.OperationalDataEraser`. Unlike the DCA-083 resolver (read-only), this
surface EXECUTES a deletion: for one of a business's subjects it erases the DCA's own operational
observations/evidence/projections and records a :class:`~app.db.models.DataErasureJournal` with justified
tombstone references. It does NOT touch the retained records (audit trail, consent receipts, restriction
ledgers) or any platform-owned data; acting beyond this scope stays a disabled future capability.

The surface is business-plane only:

- **Execute an erasure** — ``POST /v1/tenants/{business_id}/subjects/{subject_type}/{subject_id}/erasure``.
  Requires the business-plane ``data.erase`` scope for THIS business (an ordinary user, a read-only
  viewer and a non-member — incl. a platform operator — are denied before anything is read/deleted). An
  unknown/cross-tenant subject is 404; a missing justification is 422. On success the operational data is
  deleted and the journal row is returned.
- **Read the journal** — ``GET /v1/tenants/{business_id}/erasure-journal`` (list) and
  ``GET /v1/tenants/{business_id}/erasure-journal/{journal_id}`` (one). The same ``data.erase`` authority
  reads the durable tombstone record; a cross-tenant/unknown id reads as 404.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.api.idempotency import IdempotencyContext, commit_idempotent, idempotency
from app.audit.sink import AuditPayloadError
from app.core.security import require_business_permission_on_path
from app.db.models import DataErasureJournal
from app.identity.permissions import Scope
from app.identity.tenant_context import TenantContext
from app.services.data_erasure import (
    DataErasureAuthorizationError,
    DataErasureError,
    ErasurePrincipal,
    OperationalDataEraser,
)

router = APIRouter(prefix="/v1/tenants", tags=["privacy-erasure"])

SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyDep = Annotated[IdempotencyContext, Depends(idempotency)]
# Erasing (and reading the erasure journal) is an administrator action: an admin/owner of the business
# holds ``data.erase``; an ordinary acting user, a read-only viewer and a non-member (incl. a platform
# operator) do not.
EraseDep = Annotated[TenantContext, Depends(require_business_permission_on_path(Scope.DATA_ERASE))]


class ErasureBody(BaseModel):
    # extra='forbid' so an UNSUPPORTED field (a typo / a field from a future capability) is a clear 422
    # rather than being silently ignored.
    model_config = ConfigDict(extra="forbid")

    # REQUIRED: the short opaque justification for the erasure (never a contact value — screened).
    justification: str
    # OPTIONAL: the request this erasure answers (e.g. a grievance id) and its kind, both opaque.
    requestRef: str = ""
    requestKind: str = ""


def _journal_view(journal: DataErasureJournal) -> dict:
    """The structured, opaque journal projection — tombstone references, counts and provenance only.
    Carries NO contact value, NO org/person name and NO field value."""
    return {
        "erasureJournalId": journal.id,
        "businessId": journal.business_id,
        "subjectType": journal.subject_type,
        "subjectId": journal.subject_id,
        "requestKind": journal.request_kind or None,
        "requestRef": journal.request_ref or None,
        "justification": journal.justification,
        "authorityPlane": journal.authority_plane,
        "authorityScope": journal.authority_scope,
        "observationsDeleted": journal.observations_deleted,
        "evidenceDeleted": journal.evidence_deleted,
        "projectionsDeleted": journal.projections_deleted,
        "totalDeleted": journal.total_deleted,
        "tombstones": journal.tombstones,
        "erasedAt": journal.created_at.isoformat() if journal.created_at else None,
    }


@router.post(
    "/{business_id}/subjects/{subject_type}/{subject_id}/erasure",
    status_code=201,
)
def execute_erasure(
    business_id: str,
    subject_type: str,
    subject_id: str,
    body: ErasureBody,
    context: EraseDep,
    session: SessionDep,
    idem: IdempotencyDep,
) -> dict:
    """Erase the subject's DCA-owned operational data and return the execution journal.

    Requires the ``data.erase`` scope: an ordinary user, a read-only viewer or a non-member (incl. a
    platform operator) is denied (403) before anything is read or deleted. An unknown/cross-tenant
    subject is 404; a missing justification or unknown subject type is a clear 422 — each staging
    nothing. On success the observation/evidence/projection rows for the subject are deleted, an atomic
    audit entry + the journal row are staged in the same unit of work, and the journal (counts + opaque
    tombstone references) is returned. Retry-safe with an ``Idempotency-Key``.
    """
    eraser = OperationalDataEraser(session, context.business_id)
    principal = ErasurePrincipal(subject=context.subject, roles=context.roles)
    try:
        result = eraser.erase_subject(
            subject_type,
            subject_id,
            principal,
            justification=body.justification,
            request_ref=body.requestRef,
            request_kind=body.requestKind or "operator_erasure",
        )
    except DataErasureAuthorizationError as exc:  # pragma: no cover - dependency gates first
        raise HTTPException(status_code=403, detail=exc.code) from exc
    except DataErasureError as exc:
        if exc.code == "subject_not_found":
            raise HTTPException(status_code=404, detail=exc.code) from exc
        # unknown subject type / missing justification → a clear 422, staged nothing.
        raise HTTPException(status_code=422, detail=exc.code) from exc
    except AuditPayloadError as exc:
        # The justification carried raw contact/secret data — refuse, deleting nothing.
        raise HTTPException(status_code=422, detail="erasure_justification_not_opaque") from exc

    session.flush()
    journal = session.get(DataErasureJournal, result.journal_id)
    assert journal is not None  # just staged
    view = _journal_view(journal)
    return commit_idempotent(session, idem, view, status_code=201)


@router.get("/{business_id}/erasure-journal")
def list_erasure_journal(business_id: str, context: EraseDep, session: SessionDep) -> list[dict]:
    """Every erasure-journal entry for this business, oldest first (authorized operator only).

    Requires the ``data.erase`` scope (same denial as the execute route). Read-only; the projection
    carries opaque tombstone references and counts only, so it cannot be used to recover erased data.
    """
    rows = session.scalars(
        select(DataErasureJournal)
        .where(DataErasureJournal.business_id == context.business_id)
        .order_by(DataErasureJournal.created_at, DataErasureJournal.id)
    ).all()
    return [_journal_view(j) for j in rows]


@router.get("/{business_id}/erasure-journal/{journal_id}")
def get_erasure_journal(
    business_id: str, journal_id: str, context: EraseDep, session: SessionDep
) -> dict:
    """One erasure-journal entry within this business (404 if unknown/cross-tenant).

    Requires the ``data.erase`` scope. Read-only: a cross-tenant / unknown id reads exactly like
    not-found — never surfaced.
    """
    journal = session.scalars(
        select(DataErasureJournal)
        .where(DataErasureJournal.business_id == context.business_id)
        .where(DataErasureJournal.id == journal_id)
    ).one_or_none()
    if journal is None:
        raise HTTPException(status_code=404, detail="erasure_journal_not_found")
    return _journal_view(journal)
