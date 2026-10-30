"""DCA cost estimation — the operational run-cost estimator (DCA-112).

Stage 10 ("Cost reservations and provider accounting") opens here with a single, narrow capability:
produce a *conservative, bounded* estimate of what one planned acquisition run would cost to OPERATE,
so the estimate can be reviewed and accepted on its own before any reservation/spend story (DCA-113+)
is built on top of it.

Public surface is the pure estimator in :mod:`app.cost.estimate`:

- the versioned operational rate card (:class:`~app.cost.estimate.RateCard`,
  :data:`~app.cost.estimate.DEFAULT_OPERATION_RATE_CARD`) that prices DCA's own data-source
  operations in INR minor units, carrying its ``version`` / ``source`` / validity window;
- :func:`~app.cost.estimate.estimate_run_cost` — a deterministic, side-effect-free function that
  sums a bounded operational estimate and folds in model-work costs taken from the AI gateway's
  verified dry-run quote (:class:`~app.cost.estimate.ModelQuote`);
- :func:`~app.cost.estimate.request_model_quote` — the thin seam that obtains a model quote by a
  ``dryRun`` gateway call, which projects cost WITHOUT invoking a paid model.

DCA-113 (:mod:`app.cost.reserve`) builds the next capability on top: :class:`~app.cost.reserve.BudgetReservationStore`
ATOMICALLY RESERVES a precise amount of spend against a business's in-force budget caps so concurrent
reservations can never collectively overshoot a cap. A reservation is a HOLD, not a charge and not a
send — the estimator still spends/sends nothing, and settling a hold / recording actual provider spend
remain later, disabled stories.
"""

from __future__ import annotations

from app.cost.estimate import (
    DEFAULT_OPERATION_RATE_CARD,
    REASON_MODEL_PRICING_INVALID,
    REASON_MODEL_PRICING_UNAVAILABLE,
    REASON_OPERATIONAL_RATE_UNAVAILABLE,
    ModelCostLine,
    ModelQuote,
    ModelWorkItem,
    OperationCostLine,
    OperationKind,
    PlannedOperation,
    RateCard,
    RunCostEstimate,
    estimate_run_cost,
    request_model_quote,
)
from app.cost.reserve import (
    AUDIT_ACTION_RESERVE,
    PLATFORM_DAILY_CEILING_MINOR,
    PLATFORM_PER_RESERVATION_CEILING_MINOR,
    REASON_DAILY_EXCEEDED,
    REASON_INVALID_AMOUNT,
    REASON_MONTHLY_EXCEEDED,
    REASON_NO_BUDGET,
    REASON_NOT_AUTHORIZED,
    REASON_PER_RUN_EXCEEDED,
    REASON_PLATFORM_EXCEEDED,
    REASON_PROVIDER_EXCEEDED,
    BudgetReservationStore,
    ReservationActor,
    ReservationAuthorizationError,
    ReservationError,
    ReservationRequest,
    ReservationWindow,
)

__all__ = [
    "DEFAULT_OPERATION_RATE_CARD",
    "REASON_MODEL_PRICING_INVALID",
    "REASON_MODEL_PRICING_UNAVAILABLE",
    "REASON_OPERATIONAL_RATE_UNAVAILABLE",
    "ModelCostLine",
    "ModelQuote",
    "ModelWorkItem",
    "OperationCostLine",
    "OperationKind",
    "PlannedOperation",
    "RateCard",
    "RunCostEstimate",
    "estimate_run_cost",
    "request_model_quote",
    # DCA-113 — atomic spend reservation
    "AUDIT_ACTION_RESERVE",
    "PLATFORM_DAILY_CEILING_MINOR",
    "PLATFORM_PER_RESERVATION_CEILING_MINOR",
    "REASON_DAILY_EXCEEDED",
    "REASON_INVALID_AMOUNT",
    "REASON_MONTHLY_EXCEEDED",
    "REASON_NO_BUDGET",
    "REASON_NOT_AUTHORIZED",
    "REASON_PER_RUN_EXCEEDED",
    "REASON_PLATFORM_EXCEEDED",
    "REASON_PROVIDER_EXCEEDED",
    "BudgetReservationStore",
    "ReservationActor",
    "ReservationAuthorizationError",
    "ReservationError",
    "ReservationRequest",
    "ReservationWindow",
]
