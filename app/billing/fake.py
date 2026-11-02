"""DCA-158 — deterministic, in-memory EffortlessPromo usage-billing fake for local tests.

The only usage-billing implementation today: the live HTTP adapter into the platform's billing module is a
later story (blocked on DCA-001 B1 — there is no service-to-service contract into the .NET API yet). It is
dev/test-only — production refuses it (``build_billing_usage_port``), mirroring the delivery / audience-sync
fakes and the seed's dev-only stance. Every receipt it produces is flagged ``synthetic``, so a test answer
can never be mistaken for a real one. It performs NO charge and holds NO payment processor: a PENDING /
ACKNOWLEDGED receipt only models the platform taking / durably metering the usage; a refused delivery is
DISABLED and meters nothing.

Three knobs make the DCA-158 outcomes testable without a wall clock or a real billing system:

- ``acknowledges`` — when ``True`` (the default) the fake platform returns an ACKNOWLEDGED receipt carrying
  a synthetic host usage-record handle AND the platform's ``customer_price_minor`` (a durably-metered,
  priced usage). When ``False`` it returns a PENDING hand-off (queued, no handle, no price) — exactly how a
  lost / timed-out response is modelled, so the ledger entry stays ``recorded`` and is never marked
  delivered on a non-acknowledgment.
- ``priced_meters`` — the set of meters the fake platform has a CONFIGURED PRICE for. Construct it EMPTY to
  exercise the acceptance-criterion failure case: a meter with no configured price is DISABLED and invents
  NO charge (no ``customer_price_minor``), never a silent accept.
- ``available`` — when ``False`` the billing contract simulates being unreachable: capabilities read
  ``UNAVAILABLE`` and every delivery is disabled (the fail-closed path).

``customer_price_minor`` (the price the platform returns on an ack) is DISTINCT from the usage record's
``provider_cost_minor`` (the DCA's own cost basis) — the fake never derives one from the other, so a test
can prove a provider cost never becomes the customer charge. ``now`` is injected into each capability
answer's ``as_of`` so freshness/staleness is exercised by asking
:func:`~app.billing.contract.authorize_usage_delivery` with a later ``now``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.billing.contract import (
    DEFAULT_CURRENCY,
    BillingCapabilities,
    BillingHostAvailability,
    UsageDeliveryDecision,
    UsageDeliveryRequest,
    UsageDeliveryStatus,
    UsageMeter,
    UsageReceipt,
    authorize_usage_delivery,
)

SYNTHETIC_VERSION = "fake-usage-billing-v1"

# The meters the synthetic EffortlessPromo billing contract has a CONFIGURED PRICE for. A meter outside
# this set is DISABLED by the gate (METER_NOT_PRICED) and invents no charge.
DEFAULT_PRICED_METERS: tuple[str, ...] = (UsageMeter.ACCEPTED_PROSPECT.value,)

# The synthetic customer price (minor units — paise) the fake platform returns on an acknowledged meter.
# Deliberately a round, obviously-synthetic value that is NOT derived from any provider cost, so a test can
# prove the customer price and the provider cost are distinct.
DEFAULT_CUSTOMER_PRICE_MINOR = 5000


class BillingUsageContractDisabled(RuntimeError):
    """Raised when a usage-billing port is requested where the contract is not wired (production).

    Mirrors :class:`~app.delivery.fake.DeliveryContractDisabled` /
    :class:`~app.audience.fake.AudienceSyncContractDisabled`.
    """


class FakeEffortlessPromoBillingPort:
    """A deterministic :class:`~app.billing.contract.UsageBillingPort`. Pure: no I/O, no charge, no store.

    Models the EffortlessPromo platform's deliver-usage contract. ``acknowledges=False`` makes EVERY allowed
    delivery a PENDING hand-off (queued, no handle, no price — the lost-response fixture); ``priced_meters``
    EMPTY makes every delivery DISABLED on the meter-not-priced gate (the acceptance-criterion failure
    case); ``available=False`` makes the contract read ``UNAVAILABLE`` (the host-down path). ``now`` is
    injected into ``as_of`` so freshness/staleness is testable; it defaults to real UTC now. Every receipt
    is ``synthetic=True``.

    An acknowledged delivery yields a receipt whose ``host_reference`` is DETERMINISTIC in the request key,
    so a replayed delivery returns the same handle — the contract's idempotency, visible — and carries the
    platform's ``customer_price_minor`` (DISTINCT from the request's ``provider_cost_minor``). Delivering is
    never the DCA charging anything: ``charged_by_dca`` is always ``False``.
    """

    def __init__(
        self,
        *,
        acknowledges: bool = True,
        available: bool = True,
        priced_meters: tuple[str, ...] = DEFAULT_PRICED_METERS,
        customer_price_minor: int = DEFAULT_CUSTOMER_PRICE_MINOR,
        currency: str = DEFAULT_CURRENCY,
        now: datetime | None = None,
        version: str = SYNTHETIC_VERSION,
    ) -> None:
        self._acknowledges = acknowledges
        self._available = available
        self._priced_meters = priced_meters
        self._customer_price_minor = customer_price_minor
        self._currency = currency
        self._now = now
        self._version = version

    def capabilities(self, business_id: str) -> BillingCapabilities:
        """What this fake host advertises for a business. Unavailable ⇒ capability fields are empty and the
        gate refuses on availability regardless of them."""
        now = self._now or datetime.now(UTC)
        if not self._available:
            return BillingCapabilities(
                business_id=business_id,
                availability=BillingHostAvailability.UNAVAILABLE,
                version=self._version,
                as_of=now,
                currency=self._currency,
                synthetic=True,
            )
        return BillingCapabilities(
            business_id=business_id,
            availability=BillingHostAvailability.AVAILABLE,
            version=self._version,
            as_of=now,
            priced_meters=self._priced_meters,
            currency=self._currency,
            synthetic=True,
        )

    def deliver(self, request: UsageDeliveryRequest) -> UsageReceipt:
        """Deliver a usage fact through the (fake) platform. Fail-closed: a host that is unavailable, stale,
        or has no configured price for the meter returns a DISABLED receipt carrying the deny reason — never
        a silent accept, never an invented charge. A cleared delivery is taken into billing: PENDING
        (queued, no handle, no price) unless the host acknowledges, in which case ACKNOWLEDGED with a
        deterministic host handle AND the platform's customer price (distinct from the provider cost)."""
        now = self._now or datetime.now(UTC)
        caps = self.capabilities(request.record.business_id)
        decision = authorize_usage_delivery(caps, request.record, now=now)
        if not decision.allowed:
            return UsageReceipt(
                accepted=False,
                status=UsageDeliveryStatus.DISABLED,
                reason=decision.reason,
                request_key=request.request_key,
                meter=request.record.meter.value,
                currency=self._currency,
                synthetic=True,
            )
        if self._acknowledges:
            return UsageReceipt(
                accepted=True,
                status=UsageDeliveryStatus.ACKNOWLEDGED,
                reason=UsageDeliveryDecision.OK,
                request_key=request.request_key,
                meter=request.record.meter.value,
                host_reference=f"synthetic-usage-{request.request_key}",
                customer_price_minor=self._customer_price_minor,
                currency=self._currency,
                synthetic=True,
            )
        return UsageReceipt(
            accepted=True,
            status=UsageDeliveryStatus.PENDING,
            reason=UsageDeliveryDecision.OK,
            request_key=request.request_key,
            meter=request.record.meter.value,
            currency=self._currency,
            synthetic=True,
        )


def build_billing_usage_port(settings: Any) -> FakeEffortlessPromoBillingPort:
    """The usage-billing port for this deployment.

    Only the dev/test fake exists; the live adapter into the platform's billing module is unbuilt
    (DCA-001 B1). Production therefore has NO usage-billing port — the contract stays disabled until that
    story ships. Calling this under production configuration raises rather than silently handing back a fake
    (which would risk treating a synthetic meter as a real charge — the exact fail-open this story forbids).
    """
    if getattr(settings, "is_production", False):
        raise BillingUsageContractDisabled(
            "usage-billing contract is disabled in production: the live adapter into the platform billing "
            "module is not built yet (DCA-001 B1) and the synthetic fake is dev/test-only"
        )
    return FakeEffortlessPromoBillingPort()
