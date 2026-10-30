# DCA-112 — Estimate bounded run costs

Stage: Cost reservations and provider accounting · key: `cost-estimate`

## What this adds

A **pure, deterministic estimator** that projects what one *planned* acquisition run would cost this
service to **operate** — advisory data only, never a customer price and never an actual charge. It is
the first capability of Stage 10; reserving budget and recording actual provider spend are later,
still-disabled stories (DCA-113+).

The estimate is trustworthy to review on its own because of three guarantees:

- **Bounded / capped.** Every planned operation count is clamped to the rate card's
  `max_units_per_operation`, and the operational subtotal is clamped to `max_total_operational_minor`,
  so an estimate can never run away. The result reports whether either bound actually bit
  (`units_clamped` / `total_capped`). Amounts are INR **minor units (paise)** — integers, exact.
- **Versioned, provenanced, time-bounded.** Operational unit prices come from a **versioned**
  `RateCard` carrying its `version`, a `source` label, a `currency` (INR, India-only) and a validity
  window (`valid_from` / `valid_until`). The estimate copies those onto itself, so every figure is
  attributable and `is_valid_at(now)` is answerable.
- **AI pricing is the gateway's, and absence is never zero.** DCA keeps **no** model-pricing table
  (the `effortlesspromo-ai` gateway owns that). Model-work cost is taken from the gateway's verified
  **dry-run quote** — `request_model_quote` issues a `dryRun=True` call, which the gateway honours by
  projecting cost **without invoking a paid model** (AI-025). When a quote has no usable price, that
  model line is **blocked** (not costed as zero) and `paid_model_work_allowed` becomes `False` —
  unknown/unavailable AI prices **block** paid model work rather than imply it is free.

Operational INR and a model quote's own currency (e.g. USD) are kept as **separate per-currency
subtotals** — the estimator never fabricates a foreign-exchange conversion.

## Components touched (DCA only)

- **New package**: `app/cost/__init__.py` (public surface) + `app/cost/estimate.py`:
  - `OperationKind` — the DCA's own (non-model) operations the rate card prices.
  - `RateCard` + `DEFAULT_OPERATION_RATE_CARD` — the versioned operational rates and the two caps.
  - `ModelQuote` (`from_envelope`, `unavailable`) — the parsed, PII-free dry-run price; `available`
    is load-bearing (absence ⇒ blocked, never zero).
  - `PlannedOperation`, `ModelWorkItem` — the planned-run inputs.
  - `RunCostEstimate` — the inert, inspectable result (lines, per-currency subtotals, bound flags,
    `paid_model_work_allowed`, `blocked_reasons`, provenance, `is_valid_at`).
  - `estimate_run_cost(...)` — the pure estimator (no DB, no network, no state).
  - `request_model_quote(...)` — the thin dry-run quote seam; **persists nothing** (a quote is not an
    execution — no durable invocation row, no reservation, no spend).
- **Manual check**: `scripts/manual_check.py` → registered `DCA-112` scenario.

No schema change, so **no migration** and no `tests/test_rls.py` / `tests/test_migrations.py` change —
the estimator writes nothing. No HTTP route is added (the capability is a reusable pure function, like
`app/jobs/retry.py`); a later story exposes it where a consumer needs it.

## Reason codes (on a blocked line / `blocked_reasons`)

`operational_rate_unavailable` (the rate card does not price an operation — blocked, not free),
`model_pricing_unavailable` (the gateway returned no usable price), `model_pricing_invalid` (a
present-but-nonsensical amount, e.g. negative). None of these is ever coerced to a zero cost.

## Tests

- `tests/test_run_cost_estimate.py` (always-on; the estimator is pure, so no Postgres needed):
  operational sum + retained currency/version/source/validity; the per-operation and total caps biting;
  an unpriced operation blocked (not zero); an available quote kept in its own currency and distinct
  from INR; the **denial** — a `pricingUnavailable` quote blocks paid model work and is never zero;
  `from_envelope` never coercing an absent/invalid price to zero (parametrized edge cases); conservative
  ceil rounding; the dry-run quote seam proving `dryRun=true` (no paid model) and an unavailable gateway
  blocking rather than zeroing; and reproducibility (identical inputs reproduce the estimate and the
  denial, with no state change).

## Developer manual check

```text
python scripts/manual_check.py DCA-112
```

Runs an inspectable scenario (local synthetic, AI disabled, **no DB writes**): prints a bounded estimate
(operational INR + a model dry-run quote kept in its own currency, with full provenance), shows the caps
biting on an absurdly large plan, and reproduces the **denial** (a `pricingUnavailable` quote blocks paid
model work and is never costed as zero, while the operational part still estimates). Shows real figures
and a `checks` map — not a bare PASS.
