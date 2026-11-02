"""DCA-150 — the tenant-scoped VISIBLE prospect-suggestion list: one read-only HTTP capability.

A thin, authenticated HTTP contract (for a later Web/Admin consumer) over the DCA-150
:class:`~app.services.prospect_suggestion.ProspectSuggestionReader`. It answers ONE question for a
business's OWN candidates: *which prospect suggestions are currently VISIBLE for review* — paginated,
with safe optional filters (one run, a minimum ICP fit). It is strictly read-only: it writes nothing,
spends nothing and sends nothing, so the answer reproduces with no state change.

Authorization uses the DCA-019 end-user permission model (like the sibling ``spend/summary`` read): a
suggestion is a candidate an ordinary business user may VIEW, so reading the list requires the
``candidate.read`` scope on a membership-verified tenant context. A non-member — including a platform
operator, who holds no membership in any client business — is denied (403, audited) before any row is
read. The path ``business_id`` is the row scope: the reader is bound to the verified business and every
query is confined to it, so one tenant can never read another's candidates, and pagination can never
page INTO another tenant's rows.

Only VISIBLE suggestions are listed — the reader honours every DCA-145 visibility constraint
(``research_only`` vs the disabled ``sendable`` future capability, the reviewable ``suggested`` status,
and expiry), so a dismissed / expired / sendable suggestion never appears. The response carries only
rights-aware fields (the opaque scoring metadata, never a contact value).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.core.security import require_business_permission_on_path
from app.identity.permissions import Scope
from app.identity.tenant_context import TenantContext
from app.services.prospect_suggestion import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    ProspectSuggestionReader,
    SuggestionListFilters,
    SuggestionListItem,
    SuggestionPage,
)

router = APIRouter(prefix="/v1/tenants", tags=["prospect-suggestions"])

SessionDep = Annotated[Session, Depends(get_session)]
# Viewing suggestions is an ordinary-business-user action (DCA-019 ``candidate.read``): a viewer, an
# acting user and an administrator all hold it; a non-member (incl. a platform operator) is denied.
CandidateReadDep = Annotated[
    TenantContext, Depends(require_business_permission_on_path(Scope.CANDIDATE_READ))
]


def _item_view(item: SuggestionListItem) -> dict:
    return {
        "id": item.id,
        "runId": item.run_id,
        "subjectType": item.subject_type,
        "subjectId": item.subject_id,
        # Rights-aware: the research-only vs sendable distinction is explicit; a listed suggestion is
        # always research-only here, so ``sendable`` is always False (no contact value is ever exposed).
        "visibility": item.visibility,
        "sendable": item.sendable,
        "status": item.status,
        "matchScore": item.match_score,
        "rationale": item.rationale,
        "scoredIcpVersion": item.scored_icp_version,
        "schemaVersion": item.schema_version,
        "matchedDimensions": item.matched_dimensions,
        "createdAt": item.created_at.isoformat() if item.created_at else None,
        "expiresAt": item.expires_at.isoformat() if item.expires_at else None,
    }


def _page_view(page: SuggestionPage) -> dict:
    return {
        "businessId": page.business_id,
        "generatedAt": page.generated_at.isoformat(),
        "limit": page.limit,
        "offset": page.offset,
        "total": page.total,
        "hasMore": page.has_more,
        "items": [_item_view(item) for item in page.items],
    }


@router.get("/{business_id}/prospect-suggestions")
def list_prospect_suggestions(
    business_id: str,
    context: CandidateReadDep,
    session: SessionDep,
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    runId: str | None = Query(default=None),
    minScore: float | None = Query(default=None, ge=0.0, le=1.0),
) -> dict:
    """This business's VISIBLE prospect suggestions (paginated, newest-and-best first).

    Requires the ``candidate.read`` scope on a membership-verified tenant context: a non-member
    (including a platform operator) is denied (403, audited) before any row is read. The reader is bound
    to the verified business, so another tenant's candidates are never visible and a page never leaks
    cross-tenant. Only VISIBLE suggestions are returned — a dismissed, expired or sendable suggestion is
    omitted — and each row carries only rights-aware fields (opaque scoring metadata, never a contact
    value). Read-only: nothing is written, spent or sent, so the list reproduces with no state change.
    """
    page = ProspectSuggestionReader(session, context.business_id).list_visible(
        filters=SuggestionListFilters(run_id=runId, min_score=minScore),
        limit=limit,
        offset=offset,
    )
    return _page_view(page)
