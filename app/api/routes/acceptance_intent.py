"""DCA-153 — the audience-acceptance INTENT endpoint: one reviewable, write HTTP capability.

A thin, authenticated HTTP contract (for a later Web/Admin consumer) over the DCA-153
:class:`~app.services.acceptance_intent.AcceptanceIntentRecorder`. It performs ONE action for a
business's OWN candidate: record the tenant's INTENT to accept a DCA-145 prospect suggestion into their
audience — persisting a ``pending_sync`` intent, its atomic audit entry and a durable outbox sync
REQUEST in one unit of work. It performs **NO** EffortlessPromo CRM sync / send, confers **NO**
recipient consent (**acceptance is NOT consent**), and creates **NO** audience membership; billable
acceptance / usage emission is a later story and is NOT done here.

Authorization uses the DCA-019 end-user permission model, like the sibling DCA-151 dismiss
(``candidate.accept``): accepting a candidate you are reviewing is the positive counterpart of
dismissing one, so it requires the ``candidate.accept`` scope on a membership-verified tenant context. A
read-only viewer, a non-member and a platform operator (who holds no membership in any client business)
are all denied (403, audited) before anything is read or written. The path ``business_id`` is the row
scope: the recorder is bound to the verified business and every query is confined to it, so an unknown /
cross-tenant suggestion id reads as not-found (404) — an acceptance can never touch another tenant's row.

Idempotent: re-accepting the same suggestion is a 200 no-op (no duplicate intent, no second audit, no
second outbox request), and a retried request carrying the same ``Idempotency-Key`` replays verbatim.
Writes nothing beyond the one intent row + its atomic audit entry + its pending outbox request; syncs
nothing, sends nothing and spends nothing.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.api.idempotency import IdempotencyContext, commit_idempotent, idempotency
from app.core.security import require_business_permission_on_path
from app.identity.permissions import Scope
from app.identity.tenant_context import TenantContext
from app.services.acceptance_intent import (
    AcceptanceIntentActor,
    AcceptanceIntentAuthorizationError,
    AcceptanceIntentError,
    AcceptanceIntentRecorder,
    AcceptanceIntentResult,
)

router = APIRouter(prefix="/v1/tenants", tags=["acceptance-intent"])

SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyDep = Annotated[IdempotencyContext, Depends(idempotency)]
# Recording an acceptance intent is a WRITE (DCA-019 ``candidate.accept``): an acting user and an
# administrator hold it; a read-only viewer (who may only VIEW) and a non-member (incl. a platform
# operator) are denied before anything is read or written.
CandidateAcceptDep = Annotated[
    TenantContext, Depends(require_business_permission_on_path(Scope.CANDIDATE_ACCEPT))
]


class AcceptBody(BaseModel):
    # extra='forbid' so an UNSUPPORTED field (a typo / a field from a future story) is a clear 422
    # rather than being silently ignored. The acceptance carries no body today — the suggestion id in
    # the path is the whole input — but a strict empty body keeps the contract explicit.
    model_config = ConfigDict(extra="forbid")


def _intent_view(result: AcceptanceIntentResult) -> dict:
    intent = result.intent
    return {
        "id": intent.id,
        "businessId": intent.business_id,
        "suggestionId": intent.suggestion_id,
        "runId": intent.run_id,
        "subjectType": intent.subject_type,
        "subjectId": intent.subject_id,
        # Rights-aware: the research-only vs sendable distinction is explicit; a recorded intent is
        # always research-only here, so ``sendable`` is always False (no contact value is ever exposed).
        "visibility": intent.visibility,
        "sendable": result.sendable,
        "status": intent.status,
        # The CAPTURED selection versions frozen at acceptance time.
        "scoredIcpVersion": intent.scored_icp_version,
        "suggestionSchemaVersion": intent.suggestion_schema_version,
        "schemaVersion": intent.schema_version,
        "matchScore": intent.match_score,
        # The durable sync REQUEST staged with this intent (an opaque reference; nothing is delivered).
        "outboxEventId": intent.outbox_event_id,
        "alreadyAccepted": result.already_accepted,
        # The invariants, stated on the response so no consumer can mistake an intent for an action.
        "syncPerformed": False,
        "consentConferred": False,
        "membershipCreated": False,
        "note": (
            "An acceptance intent records a decision + a pending sync request only. It performs NO "
            "EffortlessPromo sync, sends nothing, confers NO recipient consent (acceptance is not "
            "consent) and creates NO audience membership. The actual sync and any billing stay disabled "
            "future capabilities; a research-only organization stays research-only."
        ),
    }


@router.post("/{business_id}/prospect-suggestions/{suggestion_id}/accept", status_code=200)
def accept_prospect_suggestion(
    business_id: str,
    suggestion_id: str,
    body: AcceptBody,
    context: CandidateAcceptDep,
    session: SessionDep,
    idem: IdempotencyDep,
) -> dict:
    """DCA-153 — record THIS business's INTENT to accept one prospect suggestion into its audience.

    Requires the ``candidate.accept`` scope on a membership-verified tenant context: a read-only viewer,
    a non-member and a platform operator are all denied (403, audited) before anything is read or
    written. The recorder is bound to the verified business, so an unknown or cross-tenant suggestion id
    reads as not-found (404), and a suggestion that is not a reviewable, unexpired ``suggested``
    candidate is refused (409) — each denial changes nothing. On success it persists a ``pending_sync``
    intent, its atomic audit entry and a durable outbox sync REQUEST in one unit of work.

    Idempotent: re-accepting the same suggestion is a 200 no-op (no duplicate intent, no second audit,
    no second outbox request), and a retried request carrying the same ``Idempotency-Key`` replays
    verbatim. Performs NO CRM sync, send, spend, consent or audience membership — all disabled future
    capabilities.
    """
    actor = AcceptanceIntentActor(subject=context.subject, roles=context.roles, actor_type="user")
    try:
        result = AcceptanceIntentRecorder(session, context.business_id).accept(suggestion_id, actor)
    except AcceptanceIntentAuthorizationError as exc:  # pragma: no cover - gated first by the dep
        raise HTTPException(status_code=403, detail=exc.code) from exc
    except AcceptanceIntentError as exc:
        # Not-found maps to 404; an un-acceptable (dismissed / expired / not-reviewable) candidate is a
        # 409 conflict with the current suggestion state.
        status = 404 if exc.code == "prospect_suggestion_not_found" else 409
        raise HTTPException(status_code=status, detail=exc.code) from exc
    return commit_idempotent(session, idem, _intent_view(result), status_code=200)
