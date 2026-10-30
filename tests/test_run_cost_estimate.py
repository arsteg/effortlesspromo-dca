"""DCA-112 — estimate bounded run costs.

The estimator is PURE (no database, no network, no state), so this suite is always-on: it drives the
deterministic estimate and its failure case directly. The only I/O is the model-quote seam
(:func:`request_model_quote`), exercised against an ``httpx.MockTransport`` to prove it issues a
``dryRun`` quote (no paid model) and persists nothing.

Covered acceptance:
- a bounded estimate retains currency, rate-card source/version and validity;
- the estimate is capped (per-operation clamp + total cap) so it cannot run away;
- an operation the card does not price is BLOCKED, never costed as zero;
- a model quote with a known currency is included under its OWN currency (no fabricated INR conversion);
- the denial case: a ``pricingUnavailable`` model quote blocks paid model work and is NEVER zero;
- the result (and the denial) reproduce exactly from the same inputs, with no state change.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from app.clients.ai_client import AiClient
from app.cost.estimate import (
    DEFAULT_OPERATION_RATE_CARD,
    OPERATIONAL_CURRENCY,
    REASON_MODEL_PRICING_INVALID,
    REASON_MODEL_PRICING_UNAVAILABLE,
    REASON_OPERATIONAL_RATE_UNAVAILABLE,
    ModelQuote,
    ModelWorkItem,
    OperationKind,
    PlannedOperation,
    RateCard,
    estimate_run_cost,
    request_model_quote,
)

NOW = datetime(2026, 10, 31, 9, 0, 0, tzinfo=UTC)

# A known-currency AI dry-run envelope (shape per docs/DCA-001 §2 / tests/test_ai_client.py).
QUOTE_ENVELOPE = {
    "jobId": "dca:q1",
    "capability": "research",
    "status": "completed",
    "model": "claude-sonnet-5",
    "provider": "anthropic",
    "costEstimate": {"amount": 0.01, "currency": "USD", "estimated": True},
}
PRICING_UNAVAILABLE_ENVELOPE = {
    "jobId": "dca:q2",
    "capability": "research",
    "status": "completed",
    "model": "claude-sonnet-5",
    "costEstimate": {"available": False},
}


# --------------------------------------------------------------------------------------------------
# Operational estimate: provenance, currency, validity, and the bounds
# --------------------------------------------------------------------------------------------------


def test_operational_estimate_sums_and_retains_provenance_and_validity():
    est = estimate_run_cost(
        [
            PlannedOperation(OperationKind.SOURCE_LOOKUP, 10),  # 10 * 50 = 500 paise
            PlannedOperation(OperationKind.EMAIL_VERIFICATION, 4),  # 4 * 25 = 100 paise
        ],
        now=NOW,
    )
    assert est.operational_subtotal_minor == 600
    assert est.totals_by_currency == {OPERATIONAL_CURRENCY: 600}
    # Currency + rate-card source/version + validity are carried onto the estimate.
    assert est.currency == "INR"
    assert est.rate_card_version == DEFAULT_OPERATION_RATE_CARD.version
    assert est.rate_card_source == DEFAULT_OPERATION_RATE_CARD.source
    assert est.is_valid_at(NOW) is True
    assert est.is_valid_at(datetime(2025, 1, 1, tzinfo=UTC)) is False  # before valid_from
    # Structurally bounded; no bound bit on this small run.
    assert est.bounded is True
    assert est.units_clamped is False and est.total_capped is False
    assert est.paid_model_work_allowed is True  # no model work requested → nothing blocked


def test_estimate_is_capped_per_operation_and_in_total():
    card = RateCard(
        version="test.1",
        source="test",
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        unit_rates_minor={OperationKind.SOURCE_LOOKUP.value: 100},
        max_units_per_operation=5,
        max_total_operational_minor=300,
    )
    est = estimate_run_cost(
        [PlannedOperation(OperationKind.SOURCE_LOOKUP, 1_000_000)],
        rate_card=card,
        now=NOW,
    )
    line = est.operation_lines[0]
    # Per-operation cap bit: 1,000,000 requested → counted 5.
    assert line.units_requested == 1_000_000 and line.units_counted == 5
    assert line.clamped is True and est.units_clamped is True
    # Raw would be 5 * 100 = 500, but the total cap (300) clamps it.
    assert est.operational_subtotal_minor == 300
    assert est.total_capped is True


def test_unpriced_operation_is_blocked_not_zero():
    card = RateCard(
        version="test.1",
        source="test",
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        unit_rates_minor={
            OperationKind.SOURCE_LOOKUP.value: 50
        },  # ENRICHMENT deliberately unpriced
    )
    est = estimate_run_cost(
        [
            PlannedOperation(OperationKind.SOURCE_LOOKUP, 2),
            PlannedOperation(OperationKind.SOURCE_ENRICHMENT, 3),
        ],
        rate_card=card,
        now=NOW,
    )
    priced, blocked = est.operation_lines
    assert priced.subtotal_minor == 100 and priced.blocked is False
    # The unpriced line is BLOCKED and contributes nothing — but it is not silently costed as zero.
    assert blocked.blocked is True and blocked.unit_rate_minor is None
    assert blocked.reason == REASON_OPERATIONAL_RATE_UNAVAILABLE
    assert (
        f"{REASON_OPERATIONAL_RATE_UNAVAILABLE}:{OperationKind.SOURCE_ENRICHMENT.value}"
        in est.blocked_reasons
    )
    assert est.operational_subtotal_minor == 100  # only the priced line


# --------------------------------------------------------------------------------------------------
# Model work: available quote vs the pricing-unavailable DENIAL (never zero)
# --------------------------------------------------------------------------------------------------


def test_available_model_quote_is_kept_in_its_own_currency():
    quote = ModelQuote.from_envelope(QUOTE_ENVELOPE)
    assert quote.available is True
    assert quote.amount_minor == 1 and quote.currency == "USD"  # 0.01 USD → 1 cent (ceil)
    assert quote.source == "claude-sonnet-5"

    est = estimate_run_cost(
        [PlannedOperation(OperationKind.SOURCE_LOOKUP, 2)],  # 2 * 50 = 100 INR paise
        [ModelWorkItem(capability="research", calls=3, quote=quote)],  # 3 * 1 = 3 USD cents
        now=NOW,
    )
    # Operational INR and the model USD are kept DISTINCT — no fabricated conversion into one number.
    assert est.totals_by_currency == {"INR": 100, "USD": 3}
    model_line = est.model_lines[0]
    assert model_line.priceable is True and model_line.subtotal_minor == 3
    assert model_line.estimated is True  # a dry-run figure is an estimate
    assert est.paid_model_work_allowed is True


def test_pricing_unavailable_blocks_paid_model_work_and_is_never_zero():
    quote = ModelQuote.from_envelope(PRICING_UNAVAILABLE_ENVELOPE)
    assert quote.available is False and quote.reason == REASON_MODEL_PRICING_UNAVAILABLE

    est = estimate_run_cost(
        [
            PlannedOperation(OperationKind.SOURCE_LOOKUP, 2)
        ],  # operational still estimates: 100 paise
        [ModelWorkItem(capability="research", calls=5, quote=quote)],
        now=NOW,
    )
    model_line = est.model_lines[0]
    # The model line is BLOCKED — no number — not a zero cost.
    assert model_line.priceable is False
    assert model_line.subtotal_minor is None and model_line.unit_amount_minor is None
    assert model_line.reason == REASON_MODEL_PRICING_UNAVAILABLE
    # Paid model work is blocked for the whole run; the figure never implies the model is free.
    assert est.paid_model_work_allowed is False
    assert f"{REASON_MODEL_PRICING_UNAVAILABLE}:research" in est.blocked_reasons
    # The operational part is unaffected and the USD/zero line never enters any currency total.
    assert est.totals_by_currency == {"INR": 100}


@pytest.mark.parametrize(
    "envelope,expected_reason",
    [
        (
            {"status": "failed", "costEstimate": {"amount": 0.01, "currency": "USD"}},
            REASON_MODEL_PRICING_UNAVAILABLE,
        ),
        ({"status": "completed"}, REASON_MODEL_PRICING_UNAVAILABLE),  # no costEstimate at all
        (
            {"status": "completed", "costEstimate": {"currency": "USD"}},
            REASON_MODEL_PRICING_UNAVAILABLE,
        ),  # null amount
        (
            {"status": "completed", "costEstimate": {"amount": 0.01}},
            REASON_MODEL_PRICING_UNAVAILABLE,
        ),  # no currency
        (
            {
                "status": "completed",
                "pricingUnavailable": True,
                "costEstimate": {"amount": 1, "currency": "USD"},
            },
            REASON_MODEL_PRICING_UNAVAILABLE,
        ),
        (
            {"status": "completed", "costEstimate": {"amount": -1, "currency": "USD"}},
            REASON_MODEL_PRICING_INVALID,
        ),
    ],
)
def test_from_envelope_never_coerces_absent_price_to_zero(envelope, expected_reason):
    quote = ModelQuote.from_envelope(envelope)
    assert quote.available is False
    assert quote.amount_minor is None
    assert quote.reason == expected_reason


def test_conservative_ceil_rounding_of_fractional_minor_units():
    # 0.015 USD = 1.5 cents → ceil to 2 (an upper bound, never an under-estimate).
    quote = ModelQuote.from_envelope(
        {"status": "completed", "costEstimate": {"amount": 0.015, "currency": "USD"}}
    )
    assert quote.available is True and quote.amount_minor == 2


# --------------------------------------------------------------------------------------------------
# The dry-run quote seam: a quote invokes no paid model and persists nothing
# --------------------------------------------------------------------------------------------------


def test_request_model_quote_is_a_dry_run_and_persists_nothing():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.read().decode()
        seen["request_id"] = request.headers.get("X-Request-Id", "")
        return httpx.Response(200, json=QUOTE_ENVELOPE)

    client = AiClient("http://ai.internal", "secret", transport=httpx.MockTransport(handler))
    quote = request_model_quote(
        client,
        "research",
        business_id="biz-1",
        job_id="dca:quote-1",
        request_id="dca:quote-1",
        input_context={"q": "x"},
    )
    assert quote.available is True and quote.currency == "USD"
    # The gateway call is a dry run — it projects cost without invoking a paid model.
    assert '"dryRun":true' in seen["body"].replace(" ", "")


def test_request_model_quote_unavailable_gateway_blocks_not_zero():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)  # transport/5xx → AiServiceUnavailable

    client = AiClient("http://ai.internal", "secret", transport=httpx.MockTransport(handler))
    quote = request_model_quote(
        client, "research", business_id="b", job_id="dca:q", request_id="dca:q"
    )
    # A flaky gateway blocks paid model work; it never implies the model is free.
    assert quote.available is False and quote.reason == REASON_MODEL_PRICING_UNAVAILABLE


# --------------------------------------------------------------------------------------------------
# Reproducibility: same inputs → same estimate (and same denial), no state change
# --------------------------------------------------------------------------------------------------


def test_estimate_is_reproducible_and_deterministic():
    ops = [PlannedOperation(OperationKind.SOURCE_LOOKUP, 10)]
    model = [ModelWorkItem("research", 2, ModelQuote.from_envelope(PRICING_UNAVAILABLE_ENVELOPE))]
    first = estimate_run_cost(ops, model, now=NOW)
    second = estimate_run_cost(ops, model, now=NOW)
    # Pure: identical inputs reproduce the full estimate AND the denial exactly.
    assert first == second
    assert (
        first.paid_model_work_allowed is False and first.blocked_reasons == second.blocked_reasons
    )
