"""DCA-117 — the tenant-scoped spend-summary endpoint: one reviewable read-only HTTP capability.

A thin, authenticated HTTP contract (for a later Web/Admin consumer) over the DCA-117
:class:`~app.cost.summary.BudgetSpendSummaryStore`. It answers ONE question for a business's OWN ledger:
*how much has this business RESERVED, INCURRED, left UNCERTAIN, and how much budget REMAINS* — broken
down by run, civil day and civil month. It is strictly read-only: it writes nothing, spends nothing and
sends nothing, so the answer reproduces with no state change.

Authorization uses the DCA-019 end-user permission model (like the sibling ``budget`` write routes), NOT
the coarse service-auth gate: spend / cost figures are budget-control accounting data, so — exactly like
RESERVING and RECONCILING that spend (DCA-113/114/115, all ``budget.manage``) — reading them requires the
``budget.manage`` administrator scope on a membership-verified tenant context. An ordinary business user
(a viewer holding only ``candidate.read``) and a platform operator (who holds no membership in any client
business) are both denied with a 403, and the decision is audited. The path ``business_id`` is the row
scope: the aggregator is bound to the verified business and every query is confined to it, so one tenant
can never read another's cost details — a non-member of the path business is refused before any row is
read.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.cost.summary import BudgetSpendSummaryStore, RunSpendLine, SpendBucket, SpendSummary
from app.core.security import require_business_permission_on_path
from app.identity.permissions import Scope
from app.identity.tenant_context import TenantContext

router = APIRouter(prefix="/v1/tenants", tags=["spend-summary"])

SessionDep = Annotated[Session, Depends(get_session)]
# Reading spend / cost details is an administrator action, consistent with reserving and reconciling that
# same spend (DCA-113/114/115 are all ``budget.manage``); an ordinary viewer and a non-member are denied.
SpendReadDep = Annotated[
    TenantContext, Depends(require_business_permission_on_path(Scope.BUDGET_MANAGE))
]


def _bucket_view(bucket: SpendBucket) -> dict:
    return {
        "key": bucket.key,
        "reservedMinor": bucket.reserved_minor,
        "incurredMinor": bucket.incurred_minor,
        "uncertainMinor": bucket.uncertain_minor,
        "releasedMinor": bucket.released_minor,
        "committedMinor": bucket.committed_minor,
        "overspendMinor": bucket.overspend_minor,
        "reservationCount": bucket.reservation_count,
    }


def _run_view(line: RunSpendLine) -> dict:
    return {
        "reservationId": line.reservation_id,
        "runKey": line.run_key,
        "provider": line.provider,
        "status": line.status,
        "dayKey": line.day_key,
        "monthKey": line.month_key,
        "reservedMinor": line.reserved_minor,
        "incurredMinor": line.incurred_minor,
        "committedMinor": line.committed_minor,
        "overspendMinor": line.overspend_minor,
        "uncertain": line.uncertain,
    }


def _minor_to_major(amount: int | None) -> float | None:
    """A derived major-unit (rupee) view for display only; the minor-unit integer is the source of
    truth. INR has 100 minor units per major unit."""
    return None if amount is None else amount / 100


def _summary_view(summary: SpendSummary) -> dict:
    return {
        "businessId": summary.business_id,
        "generatedAt": summary.generated_at.isoformat(),
        "configured": summary.configured,
        "budgetVersion": summary.budget_version,
        "currency": summary.currency,
        "timezone": summary.timezone,
        # The CURRENT civil day/month the caps are measured over (India-local), and the headroom left in
        # each — matching exactly what the next reservation would see (uncertain holds already reduce it).
        "dayKey": summary.day_key,
        "monthKey": summary.month_key,
        "dailyCapMinor": summary.daily_cap_minor,
        "monthlyCapMinor": summary.monthly_cap_minor,
        "perRunCapMinor": summary.per_run_cap_minor,
        "dailyRemainingMinor": summary.daily_remaining_minor,
        "monthlyRemainingMinor": summary.monthly_remaining_minor,
        "dailyCapMajor": _minor_to_major(summary.daily_cap_minor),
        "monthlyCapMajor": _minor_to_major(summary.monthly_cap_minor),
        "dailyRemainingMajor": _minor_to_major(summary.daily_remaining_minor),
        "monthlyRemainingMajor": _minor_to_major(summary.monthly_remaining_minor),
        # Whole-ledger roll-ups (all time). ``total`` carries the headline committed/uncertain figures.
        "total": _bucket_view(summary.total),
        "byDay": [_bucket_view(b) for b in summary.by_day],
        "byMonth": [_bucket_view(b) for b in summary.by_month],
        "byRun": [_run_view(line) for line in summary.by_run],
    }


@router.get("/{business_id}/spend/summary")
def get_spend_summary(business_id: str, context: SpendReadDep, session: SessionDep) -> dict:
    """This business's read-only spend summary (reserved / incurred / uncertain / remaining by run,
    day and month).

    Requires the ``budget.manage`` scope on a membership-verified tenant context: an ordinary viewer or a
    non-member (including a platform operator) is denied (403, audited) before any row is read. The
    aggregation is bound to the verified business, so another tenant's spend is never visible. Read-only:
    nothing is written, spent or sent, so the summary reproduces with no state change. An uncertain hold
    is reported in its own bucket AND counted toward the committed total — never shown as free budget.
    """
    summary = BudgetSpendSummaryStore(session, context.business_id).summarize()
    return _summary_view(summary)
