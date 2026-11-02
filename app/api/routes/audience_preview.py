"""DCA-152 — the audience-selection PREVIEW endpoint: one reviewable, read-only HTTP capability.

A thin, authenticated HTTP contract (for a later Web/Admin consumer) over the DCA-152
:class:`~app.services.audience_selection_preview.AudienceSelectionPreviewer`. It answers ONE question
for a business's OWN selected candidates: *if I built an audience from THIS selection, what would it
contain* — how many records, how many are activatable vs merely research-only, which are excluded and
WHY (freshness / dismissed / research-only rights), and the would-be cost — WITHOUT creating any
audience membership or incurring any billing.

Authorization uses the DCA-019 end-user permission model, identical to the sibling DCA-150 candidate
list (``candidate.read``): previewing a selection is a reviewing action an ordinary business user may
perform, so it requires the ``candidate.read`` scope on a membership-verified tenant context. A
non-member — including a platform operator, who holds no membership in any client business — is denied
(403, audited) before any row is read. The path ``business_id`` is the row scope: the previewer is
bound to the verified business and every query is confined to it, so one tenant can never preview
another's candidates, and an unknown / cross-tenant suggestion id reads as a ``not_found`` line, never
an oracle for another tenant's row.

It is a POST only because the selection (a list of suggestion ids) travels in the body — it is strictly
NON-MUTATING: it writes nothing, spends nothing, sends nothing and creates no audience membership, so
the projection and every denial reproduce with no state change. Acting on the preview stays a disabled
future capability.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.core.security import require_business_permission_on_path
from app.identity.permissions import Scope
from app.identity.tenant_context import TenantContext
from app.services.audience_selection_preview import (
    MAX_SELECTION_SIZE,
    AudiencePreviewLine,
    AudienceSelectionPreview,
    AudienceSelectionPreviewer,
)

router = APIRouter(prefix="/v1/tenants", tags=["audience-preview"])

SessionDep = Annotated[Session, Depends(get_session)]
# Previewing a selection is an ordinary-business-user review action (DCA-019 ``candidate.read``), the
# same scope the DCA-150 candidate list requires: a viewer, an acting user and an administrator all hold
# it; a non-member (incl. a platform operator) is denied before any row is read.
CandidateReadDep = Annotated[
    TenantContext, Depends(require_business_permission_on_path(Scope.CANDIDATE_READ))
]

# Bound the maximum length a caller may submit at the HTTP boundary (defence in depth — the service
# also caps/dedupes). A longer body is a clear 422 rather than an unbounded read.
MAX_SELECTION_BODY = MAX_SELECTION_SIZE


class PreviewBody(BaseModel):
    # extra='forbid' so an UNSUPPORTED field (a typo / a field from a future story) is a clear 422
    # rather than being silently ignored.
    model_config = ConfigDict(extra="forbid")

    # The selected subset: suggestion ids from THIS business's candidate list (DCA-150). An unknown or
    # cross-tenant id is reported as a ``not_found`` line, never an error that leaks another tenant.
    suggestionIds: list[str] = Field(default_factory=list, max_length=MAX_SELECTION_BODY)
    # Optional, opaque review context echoed back onto the projection (never qualifies the result).
    intendedUse: str = Field(default="", max_length=200)
    channel: str = Field(default="", max_length=64)


def _line_view(line: AudiencePreviewLine) -> dict:
    return {
        "suggestionId": line.suggestion_id,
        "found": line.found,
        "activatable": line.activatable,
        "exclusionReason": line.exclusion_reason,
        "subjectType": line.subject_type,
        "subjectId": line.subject_id,
        # Rights-aware: the research-only vs sendable distinction is explicit; no contact value exists on
        # a research-only organization suggestion, so none is ever exposed.
        "visibility": line.visibility,
        "sendable": line.sendable,
        "status": line.status,
        "matchScore": line.match_score,
        "fresh": line.fresh,
        "expiresAt": line.expires_at.isoformat() if line.expires_at else None,
    }


def _preview_view(preview: AudienceSelectionPreview) -> dict:
    return {
        "businessId": preview.business_id,
        "generatedAt": preview.generated_at.isoformat(),
        "intendedUse": preview.intended_use,
        "channel": preview.channel,
        # Aggregate counts: the selection sizing + the research-only vs activatable separation.
        "selected": preview.selected,
        "found": preview.found,
        "notFound": preview.not_found,
        "activatable": preview.activatable,
        "researchOnly": preview.research_only,
        # Per-reason exclusion tally (freshness / dismissed / research-only rights / not-found / ...).
        "excluded": preview.excluded,
        # The COST projection — a would-be figure only. No billing is committed; no membership created.
        "estimatedChargeableContacts": preview.estimated_chargeable_contacts,
        "billingCommitted": preview.billing_committed,
        "membershipCreated": preview.membership_created,
        "note": (
            "A preview is a read-only projection: it creates NO audience membership, commits NO "
            "billing, and sends nothing. Research-only organizations are never activatable (no "
            "sendable contact, no recipient consent); acting on this selection stays a disabled "
            "future capability."
        ),
        "lines": [_line_view(line) for line in preview.lines],
    }


@router.post("/{business_id}/audience-selection/preview", status_code=200)
def preview_audience_selection(
    business_id: str,
    body: PreviewBody,
    context: CandidateReadDep,
    session: SessionDep,
) -> dict:
    """Preview what an audience built from THIS selection would contain (non-mutating projection).

    Requires the ``candidate.read`` scope on a membership-verified tenant context: a non-member
    (including a platform operator) is denied (403, audited) before any row is read. The previewer is
    bound to the verified business, so another tenant's candidates are never read and an unknown /
    cross-tenant suggestion id reads as a ``not_found`` line. The projection separates research-only
    records from activatable ones, tallies the exclusion reasons (freshness / dismissed / rights), and
    reports the would-be cost WITHOUT committing any billing or creating any audience membership.
    Read-only: nothing is written, spent or sent, so the result reproduces with no state change.
    """
    preview = AudienceSelectionPreviewer(session, context.business_id).preview(
        body.suggestionIds, intended_use=body.intendedUse, channel=body.channel
    )
    return _preview_view(preview)
