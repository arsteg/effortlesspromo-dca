# DCA-158 — Deliver usage to platform billing

Stage: User selection and EffortlessPromo sync · key `billing-port` · source sections 7, 10, 12, 13.

## What this delivers

The metering-**delivery** step after DCA-157. DCA-157 records a durable, idempotent
`BillableAcceptance` ledger entry (status `recorded`) for an accepted prospect whose audience sync was
ACKNOWLEDGED, and stages a DCA-027 billing-usage outbox record alongside it. DCA-158 **delivers** that
usage FACT to the EffortlessPromo platform's billing capability through a **typed Python contract + a
synthetic fake**, and advances the ledger entry `recorded → delivered` **only on an acknowledgment**.

The DCA never bills anyone — it holds no payment processor, no price book and no charge path. The
platform owns metering, pricing and every charge. So the only shape defined here is a *deliver-usage*
port: the DCA hands the platform a minimized usage record under the entry's **stable `billing_key`**, and
the platform meters it and decides the price. There is deliberately **no** `charge` / `invoice` / `price`
method the DCA could call.

The **live HTTP adapter into the .NET billing module is deferred (DCA-001 blocker B1)** — there is no
service-to-service contract into the platform's billing yet. The contract stays **disabled in
production**; the synthetic fake is dev/test-only and production refuses it.

## Acceptance criteria → how they are met

- **Repeated delivery cannot double-bill.** `recorded → delivered` is advanced **only** on an
  ACKNOWLEDGED receipt (a host usage-record handle present). An already-`delivered` entry short-circuits —
  no second port call, no second meter, no second audit. The request key is the entry's stable
  `billing_key` (deterministic in business + intent), so a retried delivery is the SAME logical usage and
  the fake host returns the SAME handle.
- **A missing configured price does not invent a charge.** The platform advertises which meters it has a
  **configured price** for (`BillingCapabilities.priced_meters`). A meter with no configured price →
  `authorize_usage_delivery` returns a `DISABLED` decision → the entry stays `recorded`, no handle/price is
  persisted and nothing is charged.
- **Provider costs stay distinct from customer prices.** The usage record carries the DCA's own
  `provider_cost_minor` (an internal cost basis, explicitly `is_charge == False`); the **customer price**
  is minted by the platform and returned only on the `UsageReceipt` (`customer_price_minor`). Two distinct
  fields on two distinct messages — a provider cost can never become the customer charge, and the DCA
  never sets the customer price.
- **An unacknowledged delivery is never treated as success.** A PENDING hand-off (a lost / timed-out
  response) leaves the entry `recorded` (retried later), never `delivered`.
- **Reproduce the result and the relevant denial without unintended state changes.** Authority is checked
  first and an unknown / cross-tenant id reads as not-found — both raised **before** any mutation, so a
  denial changes nothing. Production refusal raises before the port is called.

## Changed / new files (all under `apps/effortlesspromo-dca`)

- `app/billing/contract.py` *(new)* — typed shapes (`UsageMeter`, `BillingCapabilities`, `UsageRecord`,
  `UsageDeliveryRequest`, `UsageReceipt`), the `UsageBillingPort` `Protocol`, the fail-closed
  `authorize_usage_delivery` decision, and `usage_record_from_entry`.
- `app/billing/fake.py` *(new)* — `FakeEffortlessPromoBillingPort` (synthetic, deterministic; knobs
  `acknowledges` / `priced_meters` / `available`) + `build_billing_usage_port` (refuses production).
- `app/billing/__init__.py` *(new)* — package re-exports.
- `app/services/usage_delivery.py` *(new)* — `UsageBillingDeliverer` (`deliver_entry` one entry + the
  `deliver_recorded` drain), `UsageDeliveryActor`, `UsageDeliveryResult`, errors. Reads recorded entries,
  delivers via the port idempotently, advances `recorded → delivered` and settles the DCA-027 usage record
  on acknowledgment, atomic DCA-026 audit.
- `app/db/models.py` *(edit)* — four delivery-bookkeeping columns on `BillableAcceptance`:
  `delivered_at`, `delivery_receipt_id`, `delivery_attempt_count`, `last_delivery_attempt_at`.
- `migrations/versions/0072_billable_acceptance_delivery.py` *(new)* — add-column branch (revision id
  `0072_billable_delivery`, down_revision `0071_billable_acceptance`). Dual-path safe, no RLS change
  (`billable_acceptances` is already tenant-owned/RLS from 0002/0071).
- `tests/test_usage_delivery.py` *(new)* — focused behaviour + denial tests (SQLite) and a PG-gated
  tenant-scoped/atomic test.
- `tests/test_migrations.py` *(edit)* — `test_0072_adds_delivery_columns_on_a_preexisting_database`
  (add-column branch test, PG-gated).
- `scripts/manual_check.py` *(edit)* — registered `@register("DCA-158")`.
- `docs/DCA-158-deliver-usage-to-platform-billing.md` *(new)* — this file.

## Migration / config notes

- New Alembic migration `0072` (revision id `0072_billable_delivery`, ≤32 chars). Add-column only; no new
  table, no RLS/policy change. `downgrade` is a no-op (expand→contract, BR-201).
- No new configuration/env keys. Live billing stays disabled (fake-only); production refuses the fake.

## Manual check

```
python scripts/manual_check.py DCA-158
```

All `checks` are `True`: delivered-once-advanced-to-delivered, provider-cost-distinct-from-customer-price,
replay-idempotent-no-double-bill, pending-delivery-not-marked-success, missing-price-invents-no-charge,
production-refuses-fake, unauthorized-viewer-refused, cross-tenant-not-found, exactly-one-delivered-entry.

## Boundary

Python DCA only. No UI, no AI-service internals, no .NET changes, no second AI gateway, no provider
SDKs/keys. Acceptance is not recipient consent; delivering usage performs no send and the DCA charges
nothing. Did not start DCA-159.
