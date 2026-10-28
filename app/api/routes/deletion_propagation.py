"""DCA-085 — propagate deletion to external integrations: the propagation execution/status API.

Structured HTTP contract (for a later Web/Admin operator console) over
:class:`~app.services.deletion_propagation.DeletionPropagator`. For one erasure journal (DCA-084) it
sends an idempotent deletion/removal request through each supported external destination contract and
records the per-destination outcome: an acknowledgment, an explicit unsupported state, a
pending-reconciliation timeout, or a failure.

The surface is business-plane only and uses the SAME administrator authority that executed the erasure
(``data.erase`` for THIS business — an ordinary user, a read-only viewer and a non-member/platform
operator are denied before anything is read, sent or written):

- **Propagate** — ``POST /v1/tenants/{business_id}/erasure-journal/{journal_id}/external-deletions``.
  Sends the deletion request to every supported destination and returns the per-destination status. A
  timeout is recorded ``pending`` (pending reconciliation), never reported as a completed deletion.
  Idempotent: a re-run never re-sends to a destination that already acknowledged / is unsupported.
- **Reconcile** —
  ``POST /v1/tenants/{business_id}/erasure-journal/{journal_id}/external-deletions/reconcile``. Re-sends
  the SAME idempotent request for this journal's ``pending`` destinations; a destination that has
  recovered advances to ``acknowledged``.
- **Read status** — ``GET /v1/tenants/{business_id}/erasure-journal/{journal_id}/external-deletions``.
  The authorized operator reads the per-destination propagation receipts (opaque references only).

The destination contract is DISABLED in production (only the synthetic fake exists; the live adapter is
a later story), so a propagate/reconcile there fails closed with 503 rather than acknowledging against a
synthetic destination.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.api.idempotency import IdempotencyContext, commit_idempotent, idempotency
from app.core.security import require_business_permission_on_path
from app.db.models import DataErasureJournal, ExternalDeletionPropagation
from app.identity.permissions import Scope
from app.identity.tenant_context import TenantContext
from app.integration.deletion import DeletionContractDisabled
from app.services.data_erasure import ErasurePrincipal
from app.services.deletion_propagation import (
    DeletionPropagationAuthorizationError,
    DeletionPropagator,
    PropagationResult,
)

router = APIRouter(prefix="/v1/tenants", tags=["deletion-propagation"])

SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyDep = Annotated[IdempotencyContext, Depends(idempotency)]
# Propagating (and reading the propagation receipts) is the same administrator authority that executed
# the erasure: an admin/owner of the business holds ``data.erase``; an ordinary acting user, a
# read-only viewer and a non-member (incl. a platform operator) do not.
PropagateDep = Annotated[
    TenantContext, Depends(require_business_permission_on_path(Scope.DATA_ERASE))
]


class ReconcileBody(BaseModel):
    # No fields today — reconcile acts on this journal's pending destinations. extra='forbid' so an
    # unsupported field is a clear 422 rather than being silently ignored.
    model_config = ConfigDict(extra="forbid")


def _resolve_journal(session: Session, business_id: str, journal_id: str) -> DataErasureJournal:
    """The erasure journal WITHIN this business (404 if unknown/cross-tenant — never an oracle)."""
    journal = session.scalars(
        select(DataErasureJournal)
        .where(DataErasureJournal.business_id == business_id)
        .where(DataErasureJournal.id == journal_id)
    ).one_or_none()
    if journal is None:
        raise HTTPException(status_code=404, detail="erasure_journal_not_found")
    return journal


def _client_or_503(request: Request):
    client = getattr(request.app.state, "deletion_destination_client", None)
    if client is None:
        # Contract disabled (production, no live adapter) — fail closed rather than acknowledge
        # against a synthetic destination.
        raise HTTPException(status_code=503, detail="external_deletion_contract_disabled")
    return client


def _outcome_view(o) -> dict:
    return {
        "destination": o.destination,
        "status": o.status,
        "propagationId": o.propagation_id or None,
        "acknowledgmentRef": o.acknowledgment_ref or None,
        "detailCode": o.detail_code or None,
        "attempts": o.attempts,
        "reused": o.reused,
    }


def _result_view(journal_id: str, result: PropagationResult) -> dict:
    return {
        "erasureJournalId": journal_id,
        "complete": result.is_complete,
        "counts": {
            "acknowledged": result.acknowledged_count,
            "unsupported": result.unsupported_count,
            "pending": result.pending_count,
            "failed": result.failed_count,
        },
        "destinations": [_outcome_view(o) for o in result.outcomes],
    }


def _row_view(row: ExternalDeletionPropagation) -> dict:
    return {
        "propagationId": row.id,
        "erasureJournalId": row.erasure_journal_id,
        "subjectType": row.subject_type,
        "subjectId": row.subject_id,
        "destination": row.destination,
        "status": row.status,
        "acknowledgmentRef": row.acknowledgment_ref or None,
        "detailCode": row.detail_code or None,
        "attempts": row.attempts,
        "requestedBy": row.requested_by or None,
        "requestedByType": row.requested_by_type or None,
        "createdAt": row.created_at.isoformat() if row.created_at else None,
        "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.post("/{business_id}/erasure-journal/{journal_id}/external-deletions", status_code=201)
def propagate_deletion(
    business_id: str,
    journal_id: str,
    request: Request,
    context: PropagateDep,
    session: SessionDep,
    idem: IdempotencyDep,
) -> dict:
    """Send the deletion request to every supported destination and return the per-destination status.

    Requires ``data.erase`` (denied 403 before any read/send/write otherwise). The journal must exist
    in this business (404). A timeout is recorded ``pending`` (pending reconciliation) — never a
    completed deletion. Idempotent: a destination already acknowledged / unsupported is not re-sent to.
    Retry-safe with an ``Idempotency-Key``.
    """
    client = _client_or_503(request)
    journal = _resolve_journal(session, context.business_id, journal_id)
    propagator = DeletionPropagator(session, context.business_id, client)
    principal = ErasurePrincipal(subject=context.subject, roles=context.roles)
    try:
        result = propagator.propagate(journal, principal)
    except (
        DeletionPropagationAuthorizationError
    ) as exc:  # pragma: no cover - dependency gates first
        raise HTTPException(status_code=403, detail=exc.code) from exc
    except DeletionContractDisabled as exc:  # pragma: no cover - _client_or_503 gates first
        raise HTTPException(status_code=503, detail="external_deletion_contract_disabled") from exc

    view = _result_view(journal.id, result)
    return commit_idempotent(session, idem, view, status_code=201)


@router.post(
    "/{business_id}/erasure-journal/{journal_id}/external-deletions/reconcile", status_code=200
)
def reconcile_deletion(
    business_id: str,
    journal_id: str,
    body: ReconcileBody,
    request: Request,
    context: PropagateDep,
    session: SessionDep,
    idem: IdempotencyDep,
) -> dict:
    """Re-send the SAME idempotent request for this journal's ``pending`` destinations and return the
    reconciled status. A destination that has recovered advances to ``acknowledged``; one still not
    answering stays ``pending``. Requires ``data.erase``; the journal must exist in this business."""
    client = _client_or_503(request)
    journal = _resolve_journal(session, context.business_id, journal_id)
    propagator = DeletionPropagator(session, context.business_id, client)
    principal = ErasurePrincipal(subject=context.subject, roles=context.roles)
    try:
        result = propagator.reconcile(principal, journal_id=journal.id)
    except (
        DeletionPropagationAuthorizationError
    ) as exc:  # pragma: no cover - dependency gates first
        raise HTTPException(status_code=403, detail=exc.code) from exc

    view = _result_view(journal.id, result)
    return commit_idempotent(session, idem, view, status_code=200)


@router.get("/{business_id}/erasure-journal/{journal_id}/external-deletions")
def list_deletion_propagations(
    business_id: str,
    journal_id: str,
    context: PropagateDep,
    session: SessionDep,
) -> list[dict]:
    """Every per-destination propagation receipt for this journal, oldest first (authorized operator
    only). Requires ``data.erase``; the journal must exist in this business (404). Read-only; opaque
    references only."""
    _resolve_journal(session, context.business_id, journal_id)
    rows = session.scalars(
        select(ExternalDeletionPropagation)
        .where(ExternalDeletionPropagation.business_id == context.business_id)
        .where(ExternalDeletionPropagation.erasure_journal_id == journal_id)
        .order_by(ExternalDeletionPropagation.created_at, ExternalDeletionPropagation.id)
    ).all()
    return [_row_view(r) for r in rows]
