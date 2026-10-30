"""DCA-112 — estimate bounded run costs.

Key: ``cost-estimate`` · source sections 8, 13. Stage: Cost reservations and provider accounting.

One capability: given a *planned* acquisition run, produce a CONSERVATIVE, BOUNDED estimate of what it
would cost this service to OPERATE — never a customer price, never an actual charge. Three guarantees
make the estimate trustworthy enough to review on its own:

- **Bounded / capped.** An estimate can never run away. Every planned operation count is clamped to
  :attr:`RateCard.max_units_per_operation` and the operational subtotal is clamped to
  :attr:`RateCard.max_total_operational_minor`, so a caller cannot ask the estimator to project an
  arbitrarily large number. The result records whether either bound actually bit (``units_clamped`` /
  ``total_capped``). Amounts are INR **minor units (paise)** — integers, exact, never a float.

- **Versioned, provenanced, time-bounded.** Operational unit prices come from a VERSIONED
  :class:`RateCard` that carries its ``version``, a ``source`` label and a validity window
  (``valid_from`` / ``valid_until``). The estimate copies those onto itself, so a reviewer always sees
  which rate version and currency produced a figure and whether it was valid at the time
  (:meth:`RunCostEstimate.is_valid_at`). The service is India-only, so the operational currency is INR.

- **AI pricing is the gateway's, and absence is NEVER zero.** DCA does not keep a model-pricing table —
  that is the existing effortlesspromo-ai gateway's authority (the DCA boundary). Model-work cost is
  taken from the gateway's verified **dry-run quote** (:func:`request_model_quote` issues a ``dryRun``
  call, which projects cost WITHOUT invoking a paid model). When the quote has no usable price
  (:class:`ModelQuote` ``available=False``), that model line is **BLOCKED**, not counted as zero, and
  :attr:`RunCostEstimate.paid_model_work_allowed` becomes ``False`` — unknown/unavailable AI prices
  block paid model work rather than silently imply it is free.

The estimator is **pure**: :func:`estimate_run_cost` is a deterministic function of its inputs and
touches no database, no network and no state — so the same inputs always reproduce the same estimate
(and the same denial), which is exactly the DCA-112 acceptance. Obtaining a model quote is the only I/O
and it lives behind the thin :func:`request_model_quote` seam, which performs a ``dryRun`` quote and
PERSISTS NOTHING (a quote is not an execution; there is no durable invocation row, no reservation, no
spend). Reserving budget and recording actual provider spend are later, still-disabled stories.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import only for type checking, no runtime dependency
    from app.clients.ai_client import AiClient

# The one currency this India-only service prices OPERATIONS in. A model quote keeps its OWN currency
# (whatever the gateway declares) — the estimate never fabricates a foreign-exchange conversion, so
# per-currency subtotals stay honest and distinct.
OPERATIONAL_CURRENCY = "INR"

# Stable denial codes surfaced on a blocked line (and in ``blocked_reasons``). Each is a "this cost
# could not be priced" marker — never a reason to treat the cost as zero.
REASON_OPERATIONAL_RATE_UNAVAILABLE = "operational_rate_unavailable"
REASON_MODEL_PRICING_UNAVAILABLE = "model_pricing_unavailable"
REASON_MODEL_PRICING_INVALID = "model_pricing_invalid"


class OperationKind(str, Enum):
    """The DCA's own (NON-model) operational unit operations whose cost comes from the rate card.

    These are the data-acquisition operations the service performs itself — the cost of running them is
    OPERATIONAL (what it costs DCA), kept strictly distinct from any customer price. A kind the active
    rate card does not price is not assumed free: its line is BLOCKED (``operational_rate_unavailable``).
    """

    SOURCE_LOOKUP = "source_lookup"
    SOURCE_ENRICHMENT = "source_enrichment"
    EMAIL_VERIFICATION = "email_verification"
    PHONE_VERIFICATION = "phone_verification"
    IDENTITY_RESOLUTION = "identity_resolution"


def _major_to_minor_ceil(amount: float | int | str) -> int:
    """Convert a major-unit money amount to integer minor units, rounding UP (conservative).

    Uses :class:`~decimal.Decimal` over the string form so ``0.01`` → ``1`` paise exactly (never the
    binary-float ``1.0000000000000002`` → ``2`` artefact). Rounds toward +inf so the estimate is a
    conservative upper bound, never an under-estimate. A negative or non-numeric amount raises
    :class:`ValueError` (the caller treats that as an unusable quote)."""
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"amount {amount!r} is not a usable money value") from exc
    if value < 0:
        raise ValueError(f"amount {amount!r} must not be negative")
    return int((value * 100).to_integral_value(rounding=ROUND_CEILING))


# --------------------------------------------------------------------------------------------------
# The versioned operational rate card (data, not code — DCA prices only its OWN operations here)
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RateCard:
    """A VERSIONED, time-bounded set of conservative per-operation operational rates (INR minor units).

    Prices only DCA's own data-source operations (:class:`OperationKind`) — NOT model work (that is the
    AI gateway's authority) and NOT a customer price. Carries its own provenance so every estimate it
    produces is attributable: ``version`` + ``source`` + ``currency`` + a ``[valid_from, valid_until)``
    validity window. The bounds (:attr:`max_units_per_operation`, :attr:`max_total_operational_minor`)
    make every estimate built from this card capped rather than open-ended."""

    version: str
    source: str
    unit_rates_minor: Mapping[str, int]
    valid_from: datetime
    valid_until: datetime | None = None
    currency: str = OPERATIONAL_CURRENCY
    # Bounds. A planned operation's unit count is clamped to this, and the operational subtotal is
    # clamped to the total — so an estimate is always finite and conservative.
    max_units_per_operation: int = 100_000
    max_total_operational_minor: int = 100_000_000  # ₹1,000,000 — a hard ceiling on one estimate

    def rate_for(self, kind: str) -> int | None:
        """The conservative per-unit operational rate (INR minor units) for ``kind``, or ``None`` if the
        card does not price it. ``None`` means "cannot be priced", which the estimator treats as a
        BLOCKED line — never as free."""
        token = kind.value if isinstance(kind, OperationKind) else str(kind)
        return self.unit_rates_minor.get(token)

    def is_valid_at(self, moment: datetime) -> bool:
        """Whether this rate version is in force at ``moment`` (``valid_from <= moment < valid_until``)."""
        if moment < self.valid_from:
            return False
        return self.valid_until is None or moment < self.valid_until


# The default platform operational rate card. Deliberately conservative (upper-bound) placeholder rates
# in INR paise; a real priced card is supplied by a later source-rights/pricing story. Versioned so a
# change is a NEW version rather than a silent edit.
DEFAULT_OPERATION_RATE_CARD = RateCard(
    version="2026.10.0",
    source="dca.operational-rate-card",
    valid_from=datetime(2026, 1, 1, tzinfo=UTC),
    valid_until=None,
    unit_rates_minor={
        OperationKind.SOURCE_LOOKUP.value: 50,  # ₹0.50 per lookup
        OperationKind.SOURCE_ENRICHMENT.value: 150,  # ₹1.50 per enrichment
        OperationKind.EMAIL_VERIFICATION.value: 25,  # ₹0.25 per email verification
        OperationKind.PHONE_VERIFICATION.value: 40,  # ₹0.40 per phone verification
        OperationKind.IDENTITY_RESOLUTION.value: 200,  # ₹2.00 per identity resolution
    },
)


# --------------------------------------------------------------------------------------------------
# A model-work quote taken from the AI gateway's verified dry-run (never DCA's own pricing table)
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelQuote:
    """A single model-work cost quote derived from the AI gateway's dry-run envelope.

    The gateway owns all model pricing; this is just the parsed, PII-free money figure it projected.
    ``available`` is the load-bearing field: when it is ``False`` there is NO usable price and the cost
    must be treated as BLOCKED (``reason`` says why) — never as zero. ``amount_minor``/``currency`` are
    populated only when ``available`` is ``True`` and keep the gateway's OWN currency (e.g. USD), which
    the estimate never silently converts to INR."""

    available: bool
    amount_minor: int | None = None
    currency: str | None = None
    source: str | None = None  # the model/provider the gateway projected against, for attribution
    estimated: bool = True  # a dry-run figure is an estimate by nature
    reason: str | None = None  # populated iff not available

    @classmethod
    def unavailable(cls, reason: str, *, source: str | None = None) -> ModelQuote:
        """A quote with no usable price — paid model work using it is blocked (never costed as zero)."""
        return cls(available=False, reason=reason, source=source)

    @classmethod
    def from_envelope(cls, envelope: Mapping[str, Any]) -> ModelQuote:
        """Parse an effortlesspromo-ai invocation envelope into a quote — absence of a price is NEVER zero.

        A usable quote requires a ``completed`` envelope that carries a ``costEstimate`` with a non-null
        ``amount`` and a currency, and that does not explicitly flag pricing unavailable. Anything else —
        a failed/unavailable envelope, a missing cost estimate, a null amount, an explicit
        ``available: false`` / ``pricingUnavailable: true`` — yields :meth:`unavailable`
        (``model_pricing_unavailable``); a present-but-nonsensical amount (negative / non-numeric) yields
        ``model_pricing_invalid``. In no case is a missing price coerced to zero."""
        source = envelope.get("model") or envelope.get("provider")
        source = str(source) if source else None

        if envelope.get("status") != "completed":
            return cls.unavailable(REASON_MODEL_PRICING_UNAVAILABLE, source=source)
        if envelope.get("pricingUnavailable") is True:
            return cls.unavailable(REASON_MODEL_PRICING_UNAVAILABLE, source=source)

        cost = envelope.get("costEstimate")
        if not isinstance(cost, Mapping):
            return cls.unavailable(REASON_MODEL_PRICING_UNAVAILABLE, source=source)
        if cost.get("available") is False:
            return cls.unavailable(REASON_MODEL_PRICING_UNAVAILABLE, source=source)

        amount = cost.get("amount")
        currency = cost.get("currency")
        if amount is None or not currency:
            return cls.unavailable(REASON_MODEL_PRICING_UNAVAILABLE, source=source)
        try:
            amount_minor = _major_to_minor_ceil(amount)
        except ValueError:
            # A present-but-nonsensical amount (negative / non-numeric) is NOT a free result.
            return cls.unavailable(REASON_MODEL_PRICING_INVALID, source=source)
        return cls(
            available=True,
            amount_minor=amount_minor,
            currency=str(currency),
            source=source,
            estimated=bool(cost.get("estimated", True)),
        )


# --------------------------------------------------------------------------------------------------
# The planned run (inputs) and the resulting estimate (outputs) — pure value objects
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedOperation:
    """One line of planned operational work: a recognized :class:`OperationKind` and a unit count.

    ``units`` is the number of times the operation would run; it is clamped to the rate card's
    per-operation bound when the estimate is built (so a single line cannot blow the estimate up)."""

    kind: OperationKind
    units: int


@dataclass(frozen=True)
class ModelWorkItem:
    """One line of planned model work: a capability, a call count, and the gateway's dry-run quote.

    The ``quote`` is obtained separately (via :func:`request_model_quote`, a ``dryRun`` call) and handed
    in, keeping the estimator pure. An unavailable quote blocks this line rather than costing it zero."""

    capability: str
    calls: int
    quote: ModelQuote


@dataclass(frozen=True)
class OperationCostLine:
    """The costed (or blocked) result for one planned operational line — inspectable, inert."""

    kind: str
    units_requested: int
    units_counted: int
    unit_rate_minor: int | None
    subtotal_minor: int
    currency: str
    clamped: bool
    blocked: bool
    reason: str | None = None


@dataclass(frozen=True)
class ModelCostLine:
    """The costed (or blocked) result for one planned model-work line — inspectable, inert."""

    capability: str
    calls: int
    unit_amount_minor: int | None
    subtotal_minor: int | None
    currency: str | None
    source: str | None
    priceable: bool
    estimated: bool
    reason: str | None = None


@dataclass(frozen=True)
class RunCostEstimate:
    """The conservative, bounded estimate for one planned run — advisory data, never a charge.

    This is an OPERATIONAL estimate (what it would cost DCA to run), explicitly NOT a customer price and
    not a reservation. It carries the rate card's ``version`` / ``source`` / ``currency`` / validity so a
    reviewer can attribute every figure, a per-line breakdown, per-currency subtotals (operational INR is
    kept distinct from a model quote's own currency — no fabricated conversion), and the bound flags.
    ``paid_model_work_allowed`` is ``False`` whenever any requested model line could not be priced."""

    kind: str
    computed_at: datetime
    rate_card_version: str
    rate_card_source: str
    currency: str
    rate_card_valid_from: datetime
    rate_card_valid_until: datetime | None
    operation_lines: tuple[OperationCostLine, ...]
    model_lines: tuple[ModelCostLine, ...]
    operational_subtotal_minor: int
    totals_by_currency: Mapping[str, int]
    max_units_per_operation: int
    operational_cap_minor: int
    units_clamped: bool
    total_capped: bool
    paid_model_work_allowed: bool
    blocked_reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def bounded(self) -> bool:
        """Always ``True``: this estimator always applies a per-operation cap and a total cap. The flags
        ``units_clamped`` / ``total_capped`` say whether a bound actually bit on THIS estimate."""
        return True

    def is_valid_at(self, moment: datetime) -> bool:
        """Whether the rate version behind this estimate is in force at ``moment``."""
        if moment < self.rate_card_valid_from:
            return False
        return self.rate_card_valid_until is None or moment < self.rate_card_valid_until


def estimate_run_cost(
    operations: Sequence[PlannedOperation],
    model_work: Sequence[ModelWorkItem] = (),
    *,
    rate_card: RateCard = DEFAULT_OPERATION_RATE_CARD,
    now: datetime | None = None,
) -> RunCostEstimate:
    """Estimate a planned run's operational cost — pure, deterministic, conservative and bounded.

    Operational lines are priced from ``rate_card``: each line's ``units`` is clamped to
    :attr:`RateCard.max_units_per_operation` (``clamped``), then multiplied by the card's conservative
    unit rate. An operation the card does not price is a BLOCKED line (``operational_rate_unavailable``,
    subtotal 0, excluded) — never assumed free. The operational subtotal is then clamped to
    :attr:`RateCard.max_total_operational_minor` (``total_capped``), so the estimate is always finite.

    Model lines are costed from each item's gateway dry-run :class:`ModelQuote`: an available quote adds
    ``amount_minor * calls`` under the quote's OWN currency; an unavailable quote is BLOCKED
    (``model_pricing_unavailable``, no number) and flips ``paid_model_work_allowed`` to ``False`` — an
    absent AI price blocks paid model work rather than implying it is free. Operational INR and a model
    quote's currency are kept as separate per-currency subtotals (no fabricated conversion).

    Side-effect-free: no I/O, no persistence — the same inputs always reproduce the same estimate and the
    same denial. ``now`` (default: current UTC) only stamps ``computed_at`` and does not change any figure."""
    now = now or datetime.now(UTC)
    blocked_reasons: list[str] = []

    # -- operational lines (DCA's own operations, priced in INR from the versioned card) ------------
    operation_lines: list[OperationCostLine] = []
    operational_raw = 0
    units_clamped = False
    for op in operations:
        requested = max(0, int(op.units))
        counted = min(requested, rate_card.max_units_per_operation)
        clamped = counted < requested
        units_clamped = units_clamped or clamped
        kind_token = op.kind.value if isinstance(op.kind, OperationKind) else str(op.kind)
        rate = rate_card.rate_for(op.kind)
        if rate is None:
            # Unpriced operation: blocked, not free. Recorded so a reviewer sees the gap explicitly.
            reason = f"{REASON_OPERATIONAL_RATE_UNAVAILABLE}:{kind_token}"
            blocked_reasons.append(reason)
            operation_lines.append(
                OperationCostLine(
                    kind=kind_token,
                    units_requested=requested,
                    units_counted=counted,
                    unit_rate_minor=None,
                    subtotal_minor=0,
                    currency=rate_card.currency,
                    clamped=clamped,
                    blocked=True,
                    reason=REASON_OPERATIONAL_RATE_UNAVAILABLE,
                )
            )
            continue
        subtotal = counted * rate
        operational_raw += subtotal
        operation_lines.append(
            OperationCostLine(
                kind=kind_token,
                units_requested=requested,
                units_counted=counted,
                unit_rate_minor=rate,
                subtotal_minor=subtotal,
                currency=rate_card.currency,
                clamped=clamped,
                blocked=False,
            )
        )

    operational_subtotal = min(operational_raw, rate_card.max_total_operational_minor)
    total_capped = operational_subtotal < operational_raw

    # -- model lines (costed ONLY from the gateway's dry-run quote; absence blocks, never zero) ------
    model_lines: list[ModelCostLine] = []
    totals_by_currency: dict[str, int] = {rate_card.currency: operational_subtotal}
    paid_model_work_allowed = True
    for item in model_work:
        calls = max(0, int(item.calls))
        quote = item.quote
        if not quote.available or quote.amount_minor is None or not quote.currency:
            # No usable price: blocked, and paid model work is not allowed for this run.
            paid_model_work_allowed = False
            reason = quote.reason or REASON_MODEL_PRICING_UNAVAILABLE
            blocked_reasons.append(f"{reason}:{item.capability}")
            model_lines.append(
                ModelCostLine(
                    capability=item.capability,
                    calls=calls,
                    unit_amount_minor=None,
                    subtotal_minor=None,
                    currency=quote.currency,
                    source=quote.source,
                    priceable=False,
                    estimated=quote.estimated,
                    reason=reason,
                )
            )
            continue
        subtotal = quote.amount_minor * calls
        totals_by_currency[quote.currency] = totals_by_currency.get(quote.currency, 0) + subtotal
        model_lines.append(
            ModelCostLine(
                capability=item.capability,
                calls=calls,
                unit_amount_minor=quote.amount_minor,
                subtotal_minor=subtotal,
                currency=quote.currency,
                source=quote.source,
                priceable=True,
                estimated=quote.estimated,
            )
        )

    return RunCostEstimate(
        kind="operational_run_cost_estimate",
        computed_at=now,
        rate_card_version=rate_card.version,
        rate_card_source=rate_card.source,
        currency=rate_card.currency,
        rate_card_valid_from=rate_card.valid_from,
        rate_card_valid_until=rate_card.valid_until,
        operation_lines=tuple(operation_lines),
        model_lines=tuple(model_lines),
        operational_subtotal_minor=operational_subtotal,
        totals_by_currency=dict(totals_by_currency),
        max_units_per_operation=rate_card.max_units_per_operation,
        operational_cap_minor=rate_card.max_total_operational_minor,
        units_clamped=units_clamped,
        total_capped=total_capped,
        paid_model_work_allowed=paid_model_work_allowed,
        blocked_reasons=tuple(blocked_reasons),
    )


def request_model_quote(
    client: AiClient,
    capability: str,
    *,
    business_id: str,
    job_id: str,
    request_id: str,
    input_context: Mapping[str, Any] | None = None,
) -> ModelQuote:
    """Obtain a model-work cost quote from the AI gateway via a DRY-RUN call — no paid model is invoked.

    The call sets ``dryRun=True``, which the gateway honours by projecting cost WITHOUT a provider
    (model) call (the AI-025 contract recorded in ``docs/DCA-001-integration-contracts.md``). This is a
    quote, not an execution: it PERSISTS NOTHING in DCA (no durable invocation row, no reservation, no
    spend) and returns a parsed :class:`ModelQuote`. A transport/5xx failure surfaces as an unavailable
    quote (``model_pricing_unavailable``) so a flaky gateway blocks paid model work rather than implying
    it is free. The pure :func:`estimate_run_cost` stays the costing authority; this is only the seam
    that fetches the gateway's price."""
    from app.clients.ai_client import AiServiceUnavailable

    try:
        envelope = client.invoke(
            capability,
            business_id=business_id,
            job_id=job_id,
            request_id=request_id,
            input_context=dict(input_context) if input_context is not None else None,
            dry_run=True,
        )
    except AiServiceUnavailable:
        return ModelQuote.unavailable(REASON_MODEL_PRICING_UNAVAILABLE)
    return ModelQuote.from_envelope(envelope)
