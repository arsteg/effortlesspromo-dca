"""DCA-158 — the usage-billing contract: deliver usage ONLY through EffortlessPromo platform billing.

Key: ``billing-port`` · source sections 7, 10, 12, 13. Stage: User selection and EffortlessPromo sync.

The DCA never bills anyone. It holds NO payment processor, no price book, and no charge path — the
EffortlessPromo platform owns metering, pricing and every charge. So the only shape the DCA can
legitimately define here is a *deliver-usage* port: the DCA hands the platform a durable DCA-157
:class:`~app.db.models.BillableAcceptance` (a usage FACT it already recorded — an accepted prospect that
became billable after its DCA-155/156 audience sync was ACKNOWLEDGED) under a stable request key, and the
platform's own billing capability meters it and decides the price. There is deliberately no
``charge`` / ``invoice`` / ``price`` method the DCA could call — delivering a usage fact through
EffortlessPromo is all the DCA is ever allowed to do.

Three things make a delivery legitimate and inspectable, and all three are typed here:

- **Idempotency.** A request carries the SAME stable key the billable acceptance was recorded under (its
  DCA-157 ``billing_key``, deterministic in business + intent), so a retried delivery is the SAME logical
  usage, never a second meter — repeated delivery cannot double-bill.
- **A configured meter + its configured price.** A delivery names a controlled :class:`UsageMeter`. The
  platform must have a CONFIGURED PRICE for that meter (advertised in :class:`BillingCapabilities`); if it
  does not, :func:`authorize_usage_delivery` DISABLES the delivery — a missing configured price invents NO
  charge. The DCA never supplies the price.
- **Provider cost kept distinct from the customer price.** A usage record MAY carry the DCA/platform's own
  ``provider_cost_minor`` (what it cost to acquire the prospect — an internal cost basis, explicitly
  ``is_charge == False``). That is NEVER the customer charge: the customer price is minted by the platform
  and returned ONLY on an :class:`UsageReceipt` (``customer_price_minor``). The two are different fields on
  two different messages, so a provider cost can never be mistaken for, or silently become, a charge.

**An acknowledgment is load-bearing and fail-closed.** Delivering blinds the DCA unless the platform
confirms it DURABLY metered the usage. Only an ACKNOWLEDGED receipt (a host usage-record handle present)
advances the ledger entry ``recorded → delivered``; a PENDING hand-off (a lost / timed-out response) or a
DISABLED refusal leaves it ``recorded`` so it is retried, never marked delivered — an unacknowledged
delivery is never treated as success.

Mirrors the DCA-096 delivery and DCA-154 audience-sync contracts: typed shapes + a client ``Protocol`` +
a deterministic fail-closed decision here, a synthetic fake alongside (``app/billing/fake.py``), and a
live HTTP adapter deferred to a later story (blocked on DCA-001 B1 — there is no service-to-service
contract into the .NET API / its billing module yet). The contract stays DISABLED in production until that
adapter ships; the fake never stands in for it. Authority is always the platform's (blocker B3):
``X-Service-Auth`` authenticates the caller service, NOT a tenant, so a delivery only ever targets a
business the DCA has itself authorized and is scoped to that one ``business_id``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:  # annotation only — the contract keeps no runtime dependency on the ORM model.
    from app.db.models import BillableAcceptance

# A billing-host capability answer older than this is treated as stale and cannot authorize a delivery.
# Pricing/meter configuration is control-plane state that changes rarely, but a cached "priced" claim from
# long ago may no longer hold; the window matches the DCA-096 delivery freshness bound.
DEFAULT_MAX_CAPABILITIES_AGE = timedelta(hours=1)

# The default settlement currency the DCA records against (India-only). The DCA never sets a price; this is
# only the unit the platform's configured price / the DCA's cost basis are expressed in.
DEFAULT_CURRENCY = "INR"


def _norm(value: str) -> str:
    """Case-insensitive, whitespace-trimmed key for matching a meter against the host's priced set."""
    return value.strip().casefold()


class UsageMeter(str, Enum):
    """A controlled usage meter the DCA may deliver to platform billing (controlled vocabulary).

    - ``ACCEPTED_PROSPECT`` — one accepted prospect that became BILLABLE after an ACKNOWLEDGED audience
      sync (the DCA-157 :class:`~app.db.models.BillableAcceptance`). The only meter this service delivers;
      a new meter is a new, narrowly-scoped story, not a free-form string.
    """

    ACCEPTED_PROSPECT = "accepted_prospect"


class BillingHostAvailability(str, Enum):
    """Whether the EffortlessPromo billing contract could be consulted for this delivery.

    - ``AVAILABLE`` — the platform answered; its capabilities are authoritative as of ``as_of``.
    - ``UNAVAILABLE`` — the contract could not be reached/answered. The DCA has no authoritative view of
      whether a meter is priced, so it must fail closed — an unavailable host authorizes nothing.
    """

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class UsageDeliveryStatus(str, Enum):
    """The outcome of a usage delivery (controlled vocabulary). The three differ on purpose.

    - ``PENDING`` — the platform TOOK the usage into its billing queue but has NOT confirmed it durably
      metered it (no host usage-record handle yet). A hand-off, never a completed meter — exactly how a
      lost / timed-out response is modelled. The ledger entry stays ``recorded`` (retried, not delivered).
    - ``ACKNOWLEDGED`` — the platform durably metered the usage and returned its host usage-record handle
      (and the ``customer_price_minor`` it will bill). Only this terminal status advances the entry to
      ``delivered``.
    - ``DISABLED`` — the delivery was refused (host unavailable / stale, or the meter has no configured
      price). Nothing was metered and NO charge was invented.
    """

    PENDING = "pending"
    ACKNOWLEDGED = "acknowledged"
    DISABLED = "disabled"


class BillingCapabilities(BaseModel):
    """An immutable, point-in-time view of what the EffortlessPromo billing contract offers a business.

    ``priced_meters`` is the load-bearing field: the set of meters the platform has a CONFIGURED PRICE for.
    A meter outside it has no configured price, so a delivery for it is DISABLED and invents no charge — the
    DCA never fabricates a price. ``currency`` is the unit the platform prices in (India-only ⇒ ``INR``).
    ``as_of`` is when the platform produced the answer (its freshness); staleness is measured from it.
    ``synthetic`` marks a non-production fixture so a test answer can never be mistaken for a real one.
    ``host_owns_pricing`` is always ``True`` and states the ownership boundary at the type level: the
    platform owns metering, pricing and every charge; the DCA only ever delivers a usage fact. When
    ``availability`` is ``UNAVAILABLE`` the capability fields are not authoritative and the gate refuses
    regardless of them.
    """

    model_config = ConfigDict(frozen=True)

    business_id: str = Field(min_length=1)
    availability: BillingHostAvailability
    version: str = Field(min_length=1)
    as_of: datetime
    priced_meters: tuple[str, ...] = ()
    currency: str = DEFAULT_CURRENCY
    host_owns_pricing: bool = True
    synthetic: bool = False

    @property
    def is_available(self) -> bool:
        return self.availability is BillingHostAvailability.AVAILABLE

    def prices_meter(self, meter: str) -> bool:
        """Does the host have a CONFIGURED PRICE for ``meter``? Only meaningful on an available answer;
        the gate checks availability/freshness first. ``False`` ⇒ the delivery is disabled (no charge
        invented)."""
        if not self.is_available:
            return False
        target = _norm(meter)
        return any(_norm(m) == target for m in self.priced_meters)

    def age(self, *, now: datetime) -> timedelta:
        return now - self.as_of


class UsageRecord(BaseModel):
    """The usage FACT being delivered — a meter + quantity + opaque references, never money owed.

    Every field is provenance the platform (and audit) can read to meter and price the usage; none of it is
    a charge. ``meter`` + ``quantity`` are what the platform meters (``quantity`` is ``1`` for one accepted
    prospect). ``acceptance_intent_id`` / ``billing_key`` / ``external_crm_record_id`` are OPAQUE references
    the platform maps back to its own records — never a contact value. ``provider_cost_minor`` is the
    DCA/platform's OWN cost basis in minor units (what it cost to acquire the prospect); it is explicitly
    NOT the customer charge (``is_charge`` is always ``False``) and the platform never derives the customer
    price from it. There is deliberately NO customer-price field here: the DCA cannot set what the customer
    is billed — the platform mints that on the :class:`UsageReceipt`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    business_id: str = Field(min_length=1)
    meter: UsageMeter
    quantity: int = Field(default=1, ge=1)
    acceptance_intent_id: str = Field(min_length=1)
    billing_key: str = Field(min_length=1)
    external_crm_record_id: str = ""
    provider_cost_minor: int = Field(default=0, ge=0)
    currency: str = DEFAULT_CURRENCY
    correlation_id: str | None = None

    @property
    def is_charge(self) -> bool:
        """A usage record is NEVER a charge. The DCA delivers usage; the platform decides the charge."""
        return False


class UsageDeliveryRequest(BaseModel):
    """A request to deliver one usage fact THROUGH EffortlessPromo billing — a stable key + the fact.

    ``request_key`` is the SAME stable key the DCA-157 billable acceptance was recorded under (its
    ``billing_key``, deterministic in business + intent), so a retried delivery is the SAME logical usage
    and can never become a second meter. ``record`` is the usage fact. ``correlation_id`` is carried
    through so the (future) HTTP call and its logs share one id. There is no price and no charge here on
    purpose — the DCA delivers an already-recorded usage fact; the platform owns metering and pricing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_key: str = Field(min_length=1, max_length=128)
    record: UsageRecord
    correlation_id: str | None = None


class UsageReceipt(BaseModel):
    """What the EffortlessPromo billing contract returns for a delivery. Never a charge the DCA made.

    ``accepted`` is whether the platform TOOK the usage (PENDING or ACKNOWLEDGED) — a refused delivery is
    ``accepted=False`` with ``status=DISABLED``. ``status`` distinguishes a PENDING hand-off (queued, not
    durably metered) from an ACKNOWLEDGED meter (host handle present) — the two outcomes differ by
    construction, so "queued" can never masquerade as "metered". ``host_reference`` is the platform's OWN
    opaque usage-record handle, present ONLY on an ACKNOWLEDGED receipt and deterministic in the request
    key (a replay returns the same handle). ``customer_price_minor`` is the price the PLATFORM will bill the
    customer, in minor units — minted platform-side and present ONLY on an ACKNOWLEDGED receipt; it is a
    DISTINCT field from the request's ``provider_cost_minor`` and the DCA never sets it. ``reason`` is a
    stable code (an :class:`UsageDeliveryDecision` reason on a disable). ``request_key`` echoes the key so a
    replay is recognizable.
    """

    model_config = ConfigDict(frozen=True)

    accepted: bool
    status: UsageDeliveryStatus
    reason: str
    request_key: str = Field(min_length=1)
    meter: str = ""
    host_reference: str = ""
    customer_price_minor: int | None = None
    currency: str = DEFAULT_CURRENCY
    synthetic: bool = False

    @model_validator(mode="after")
    def _price_and_handle_only_when_acknowledged(self) -> UsageReceipt:
        # A host usage-record handle and a customer price exist exactly when the platform actually metered
        # the usage. A PENDING hand-off or a DISABLED refusal must NEVER carry them — so "queued" / "refused"
        # can never masquerade as "metered + priced", and no charge is ever invented on a non-acknowledgment.
        if self.status is UsageDeliveryStatus.ACKNOWLEDGED:
            if not self.host_reference:
                raise ValueError("an acknowledged delivery must carry its host usage-record handle")
            if self.customer_price_minor is None:
                raise ValueError(
                    "an acknowledged delivery must carry the platform's customer price"
                )
        else:
            if self.host_reference:
                raise ValueError(
                    "only an acknowledged delivery may carry a host usage-record handle"
                )
            if self.customer_price_minor is not None:
                raise ValueError("only an acknowledged delivery may carry a customer price")
        return self

    @property
    def is_acknowledged(self) -> bool:
        return self.status is UsageDeliveryStatus.ACKNOWLEDGED

    @property
    def is_pending(self) -> bool:
        return self.status is UsageDeliveryStatus.PENDING

    @property
    def charged_by_dca(self) -> bool:
        """The DCA charges NOTHING — the platform owns billing. Always ``False``, even on an ack."""
        return False


@runtime_checkable
class UsageBillingPort(Protocol):
    """Port the DCA uses to DELIVER a usage fact to EffortlessPromo platform billing.

    Synchronous, matching the other service-to-service clients (identity/entitlement/delivery/audience).
    There is exactly one method — ``deliver`` — and deliberately no ``charge`` / ``invoice`` / ``price``
    method: the DCA can only ever DELIVER a usage fact; the platform meters and prices it. The only
    implementation today is the dev/test fake; the live HTTP adapter is a later story (DCA-001 B1) and until
    it ships the contract stays disabled in production.
    """

    def deliver(self, request: UsageDeliveryRequest) -> UsageReceipt: ...


@dataclass(frozen=True)
class UsageDeliveryDecision:
    """Whether the host's billing capabilities permit DELIVERING a usage fact. ``reason`` is a stable code.

    An allow is permission only to hand the usage to the platform's billing capability — it is NOT a charge
    (``charges`` is always ``False``). Fail-closed: an unavailable / stale host, or a meter with no
    configured price, DENIES rather than being bypassed — a missing configured price invents no charge.
    """

    allowed: bool
    reason: str

    OK: ClassVar[str] = "ok"
    HOST_UNAVAILABLE: ClassVar[str] = "billing_host_unavailable"
    STALE: ClassVar[str] = "billing_capabilities_stale"
    METER_NOT_PRICED: ClassVar[str] = "billing_meter_not_priced"

    @property
    def charges(self) -> bool:
        """Clearing the delivery gate is not a charge. Kept explicit for every caller."""
        return False


def authorize_usage_delivery(
    capabilities: BillingCapabilities | None,
    record: UsageRecord,
    *,
    now: datetime | None = None,
    max_age: timedelta = DEFAULT_MAX_CAPABILITIES_AGE,
) -> UsageDeliveryDecision:
    """Decide whether the billing contract permits delivering ``record`` — a pure, reproducible function.

    Fail-closed at each step: absent/unavailable → stale → meter-not-priced → allowed. The decision is a
    pure function of the capabilities and the moment — it reads nothing and changes nothing, so the allow
    and every denial are perfectly reproducible.

    The meter-not-priced step is load-bearing: a meter the platform has no CONFIGURED PRICE for DISABLES the
    delivery rather than inventing a charge (the DCA-158 acceptance criterion).
    """
    moment = now or datetime.now(UTC)

    # Absent or unavailable: no authoritative view of whether the meter is priced. Refuse — absence of a
    # configured price is not a price.
    if capabilities is None or not capabilities.is_available:
        return UsageDeliveryDecision(False, UsageDeliveryDecision.HOST_UNAVAILABLE)

    # Stale: fetched too long ago. A "priced" claim from long ago may no longer hold — refuse.
    if capabilities.age(now=moment) > max_age:
        return UsageDeliveryDecision(False, UsageDeliveryDecision.STALE)

    # The meter must have a CONFIGURED PRICE; otherwise delivering would invent a charge. Disable instead.
    if not capabilities.prices_meter(record.meter.value):
        return UsageDeliveryDecision(False, UsageDeliveryDecision.METER_NOT_PRICED)

    # Cleared to DELIVER the usage. Not a charge — the platform still meters and prices it.
    return UsageDeliveryDecision(True, UsageDeliveryDecision.OK)


def usage_record_from_entry(
    entry: BillableAcceptance,
    *,
    meter: UsageMeter = UsageMeter.ACCEPTED_PROSPECT,
    provider_cost_minor: int = 0,
    currency: str = DEFAULT_CURRENCY,
    correlation_id: str | None = None,
) -> UsageRecord:
    """Build a usage record from a DCA-157 :class:`~app.db.models.BillableAcceptance` — the billable fact.

    Captures the entry's business, the opaque references (intent / billing key / the platform's external
    record id) and a ``quantity`` of one. ``provider_cost_minor`` is the DCA/platform's OWN cost basis (NOT
    the customer charge) — defaulting to the entry's recorded ``providerCostMinor`` provenance when present.
    Pure: reads the entry's attributes only; delivers, meters and charges nothing.
    """
    recorded_cost = provider_cost_minor
    if not recorded_cost and isinstance(entry.provenance, dict):
        recorded_cost = int(entry.provenance.get("providerCostMinor", 0) or 0)
    return UsageRecord(
        business_id=entry.business_id,
        meter=meter,
        quantity=1,
        acceptance_intent_id=entry.acceptance_intent_id,
        billing_key=entry.billing_key,
        external_crm_record_id=entry.external_crm_record_id,
        provider_cost_minor=recorded_cost,
        currency=currency,
        correlation_id=correlation_id,
    )
