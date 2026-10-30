"""DCA-096 — the delivery adapter contract: request delivery ONLY through EffortlessPromo.

Key: ``delivery-port`` · source sections 5, 10. Stage: India channel eligibility.

The DCA never sends anything itself. It holds NO marketing-provider SDK, key, or send path — the ONE
send pipeline inside the EffortlessPromo platform (ADR-0008) owns and performs every outbound send. So
the only shape the DCA can legitimately define is a *request-delivery* port: the DCA hands the platform
a fully-decided dispatch (the DCA-095 grant it already consumed) under an idempotency key, and the
platform's pipeline re-gates and performs the send. There is deliberately no ``send`` method, no
provider, no channel I/O — requesting delivery through EffortlessPromo is all the DCA is ever allowed
to do.

Two things make a request legitimate, and both are typed here:

- **Idempotency.** A request carries the SAME consume-once key the DCA-095 recheck used, so a retried
  delivery request is the SAME logical dispatch, never a second send.
- **Decision context.** A request carries the explainable context the dispatch rested on — the
  consumed slot, the recipient as a one-way digest (never the raw address), the channel, and the
  permission/source/suppression versions — so the platform (and audit) can see WHY delivery was asked
  for. Acceptance of a prospect is not consent and a consumed slot is not send authorization; the
  platform re-evaluates consent/suppression at send time regardless of upstream acceptance.

**Callback enforcement is load-bearing and fail-closed.** Requesting delivery blinds the DCA unless the
platform GUARANTEES it will report every terminal outcome back (delivered / bounced / complaint) —
those callbacks are what keep suppression and consent current. A host that does not ENFORCE delivery
callbacks therefore DISABLES live DCA delivery; the DCA must never fall back to a fire-and-forget
request, which is the exact fail-open this story forbids. :func:`authorize_delivery` encodes that: an
absent, stale, non-callback-enforcing, or channel-blind host capability yields a deny, never a bypass.

Mirrors the DCA-022 entitlement and DCA-079 suppression contracts: typed shapes + a client ``Protocol``
+ a deterministic fail-closed decision here, a synthetic fake alongside (``app/delivery/fake.py``), and
a live HTTP adapter deferred to a later story (blocked on DCA-001 B1 — there is no service-to-service
contract into the .NET API yet). The contract stays disabled in production until that adapter ships;
the fake never stands in for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

# A delivery-host capability answer older than this is treated as stale and cannot authorize a
# request. Delivery sits on the same fast-moving suppression/consent truth, so the freshness window is
# deliberately tight (far shorter than the 24h entitlement window): a cached "callback-enforcing" claim
# from long ago may no longer hold.
DEFAULT_MAX_CAPABILITIES_AGE = timedelta(hours=1)


def _norm(value: str) -> str:
    """Case-insensitive, whitespace-trimmed key for matching a channel against the host's scope."""
    return value.strip().casefold()


class DeliveryHostAvailability(str, Enum):
    """Whether the EffortlessPromo delivery contract could be consulted for this request.

    - ``AVAILABLE`` — the platform answered; its capabilities are authoritative as of ``as_of``.
    - ``UNAVAILABLE`` — the contract could not be reached/answered. The DCA has no authoritative view
      of whether callbacks are enforced, so it must fail closed — an unavailable host authorizes
      nothing (the capability fields are meaningless here).
    """

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class DeliveryCapabilities(BaseModel):
    """An immutable, point-in-time view of what the EffortlessPromo delivery contract offers a business.

    ``callback_enforcement`` is the load-bearing field: it states whether the platform GUARANTEES it
    will report every terminal delivery outcome (delivered / bounced / complaint) back to the DCA. Only
    a host that enforces callbacks may receive live delivery requests — without that guarantee the DCA
    would be requesting fire-and-forget sends it can never reconcile, the fail-open this story forbids.

    ``supported_channels`` is the affected scope: a channel outside it cannot be requested. ``as_of`` is
    when the platform produced the answer (its freshness); staleness is measured from it. ``synthetic``
    marks a non-production fixture so a test answer can never be mistaken for a real authoritative one.
    ``host_owns_send`` is always ``True`` and states the ownership boundary at the type level: the
    platform's one send pipeline owns and performs the send; the DCA only ever requests it. When
    ``availability`` is ``UNAVAILABLE`` the capability fields are not authoritative and the gate refuses
    regardless of them.
    """

    model_config = ConfigDict(frozen=True)

    business_id: str = Field(min_length=1)
    availability: DeliveryHostAvailability
    version: str = Field(min_length=1)
    as_of: datetime
    callback_enforcement: bool = False
    supported_channels: tuple[str, ...] = ()
    host_owns_send: bool = True
    synthetic: bool = False

    @property
    def is_available(self) -> bool:
        return self.availability is DeliveryHostAvailability.AVAILABLE

    @property
    def enforces_callbacks(self) -> bool:
        """Does the host GUARANTEE terminal-outcome callbacks? Only meaningful on an available answer;
        the gate checks availability/freshness first. ``False`` ⇒ live delivery is disabled."""
        return self.is_available and self.callback_enforcement

    def supports_channel(self, channel: str) -> bool:
        """Does the host's delivery contract cover ``channel``? If not, delivery cannot be requested."""
        target = _norm(channel)
        return any(_norm(c) == target for c in self.supported_channels)

    def age(self, *, now: datetime) -> timedelta:
        return now - self.as_of


class DeliveryContext(BaseModel):
    """The explainable decision context a delivery request rests on — the consumed DCA-095 grant.

    Every field is provenance the platform (and audit) can read to see WHY delivery was asked for; none
    of it is itself a send authorization. The recipient is carried ONLY as a one-way ``recipient_digest``
    — never the raw address — exactly as the dispatch-consumption ledger stores it. ``consumption_id``
    pins the single dispatch slot the DCA-095 recheck consumed; ``reference_fingerprint`` + the version
    stamps are the context the grant was computed over, so a request can be tied back to its grant.
    ``provider`` / ``campaign_revision`` are host-side provenance (the provider the PLATFORM will use),
    never a DCA-held provider.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    business_id: str = Field(min_length=1)
    channel: str = Field(min_length=1)
    recipient_digest: str = Field(min_length=1)
    contact_point_id: str = Field(min_length=1)
    consumption_id: str = Field(min_length=1)
    reference_fingerprint: str = ""
    permission_version: str = ""
    source_version: str = ""
    suppression_version: str = ""
    provider: str = ""
    campaign_revision: str = ""

    @property
    def authorizes_send(self) -> bool:
        """A decision context is never a send authorization. Kept explicit so no caller mistakes it."""
        return False


class DeliveryRequest(BaseModel):
    """A request to deliver one dispatch THROUGH EffortlessPromo — idempotency key + decision context.

    ``idempotency_key`` is the SAME consume-once key the DCA-095 recheck used for this dispatch, so a
    retried request is the SAME logical delivery and can never become a second send. ``context`` is the
    consumed grant the request rests on. ``correlation_id`` is carried through so the (future) HTTP call
    and its logs share one id. There is no payload/body here on purpose: the DCA does not compose or hold
    message content for sending — it requests delivery of an already-decided dispatch; the platform's one
    send pipeline owns rendering and the send.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=128)
    context: DeliveryContext
    correlation_id: str | None = None


class DeliveryReceipt(BaseModel):
    """What the EffortlessPromo delivery contract returns for a request. Never a send confirmation.

    ``accepted`` is whether the platform took the request INTO its one send pipeline — not whether it
    sent anything (the pipeline re-gates and sends later; that stays a disabled future capability).
    ``status`` is one of :data:`ACCEPTED` / :data:`DISABLED` / :data:`REJECTED`; ``reason`` is a stable
    code (a :class:`DeliveryDecision` reason on a disable). ``callback_enforced`` echoes the guarantee
    the request was accepted under. ``host_reference`` is an opaque handle the platform returns for an
    accepted request (empty when disabled/rejected); it is deterministic in the idempotency key so a
    replay returns the same handle. ``idempotency_key`` echoes the request's key.
    """

    model_config = ConfigDict(frozen=True)

    accepted: bool
    status: str
    reason: str
    idempotency_key: str = Field(min_length=1)
    callback_enforced: bool = False
    host_reference: str = ""
    synthetic: bool = False

    # stable status strings the caller reads uniformly
    ACCEPTED: ClassVar[str] = "accepted"
    DISABLED: ClassVar[str] = "disabled"
    REJECTED: ClassVar[str] = "rejected"

    @property
    def authorizes_send(self) -> bool:
        """Accepting a delivery request is NOT performing a send. Always ``False`` — the one send
        pipeline owns the actual send, which stays a disabled future capability."""
        return False


@runtime_checkable
class DeliveryPort(Protocol):
    """Port the DCA uses to REQUEST delivery through the EffortlessPromo one send pipeline.

    Synchronous, matching the other service-to-service clients (identity/entitlement/suppression).
    There is exactly one method — ``request_delivery`` — and deliberately no ``send`` / provider /
    channel-I/O method: the DCA can only ever REQUEST delivery; the platform performs it. The only
    implementation today is the dev/test fake; the live HTTP adapter is a later story (blocked on
    DCA-001 B1) and until it ships the contract stays disabled in production.
    """

    def request_delivery(self, request: DeliveryRequest) -> DeliveryReceipt: ...


@dataclass(frozen=True)
class DeliveryDecision:
    """Whether the host's delivery capabilities permit REQUESTING delivery. ``reason`` is a stable code.

    An allow is permission only to hand the request to the platform's pipeline — it is NOT recipient
    consent and NOT a send authorization (``authorizes_send`` is always ``False``). The platform still
    re-gates consent/suppression at send time, and the send itself stays a disabled future capability.
    """

    allowed: bool
    reason: str

    # stable reason codes
    OK = "ok"
    HOST_UNAVAILABLE = "delivery_host_unavailable"
    STALE = "delivery_capabilities_stale"
    CALLBACK_ENFORCEMENT_UNAVAILABLE = "callback_enforcement_unavailable"
    CHANNEL_NOT_SUPPORTED = "delivery_channel_not_supported"

    @property
    def authorizes_send(self) -> bool:
        """Clearing the delivery gate is not a send authorization. Kept explicit for every caller."""
        return False


def authorize_delivery(
    capabilities: DeliveryCapabilities | None,
    context: DeliveryContext,
    *,
    now: datetime | None = None,
    max_age: timedelta = DEFAULT_MAX_CAPABILITIES_AGE,
) -> DeliveryDecision:
    """Decide whether the EffortlessPromo delivery contract permits requesting ``context``'s dispatch.

    Fail-closed at each step: absent/unavailable → stale → callback-enforcement-missing → channel-not-
    supported → allowed. The decision is a pure function of the capabilities and the moment — it reads
    nothing and changes nothing, so the allow and every denial are perfectly reproducible.

    The callback-enforcement step is the load-bearing one: a host that does not guarantee terminal-
    outcome callbacks DISABLES live delivery rather than being bypassed with a fire-and-forget request.
    """
    moment = now or datetime.now(UTC)

    # Absent or unavailable: no authoritative view of whether callbacks are enforced. Refuse — absence
    # of a guarantee is not the guarantee.
    if capabilities is None or not capabilities.is_available:
        return DeliveryDecision(False, DeliveryDecision.HOST_UNAVAILABLE)

    # Stale: fetched too long ago. A "callback-enforcing" claim from long ago may no longer hold, so a
    # clean answer can no longer be trusted — refuse.
    if capabilities.age(now=moment) > max_age:
        return DeliveryDecision(False, DeliveryDecision.STALE)

    # The host must GUARANTEE terminal-outcome callbacks. Without that the DCA would be requesting sends
    # it can never reconcile — disable live delivery instead of bypassing the missing capability.
    if not capabilities.enforces_callbacks:
        return DeliveryDecision(False, DeliveryDecision.CALLBACK_ENFORCEMENT_UNAVAILABLE)

    # The host's delivery contract must cover the channel; otherwise the DCA cannot request it.
    if not capabilities.supports_channel(context.channel):
        return DeliveryDecision(False, DeliveryDecision.CHANNEL_NOT_SUPPORTED)

    # Cleared to REQUEST delivery. Not consent, not a send — the platform pipeline still owns the send.
    return DeliveryDecision(True, DeliveryDecision.OK)
