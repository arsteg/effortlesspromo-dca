"""DCA-097 — request cancellation of queued delivery when a permission is invalidated.

Key: ``cancel-queued`` · source sections 5, 10. Stage: India channel eligibility.

The DCA-096 contract lets the DCA REQUEST delivery of an already-decided dispatch through the
EffortlessPromo one send pipeline (ADR-0008); the DCA itself never sends. This story is the mirror
image: when a permission the DCA relied on is later INVALIDATED (consent withdrawn, the contact newly
suppressed, a source right expired) *after* a dispatch was already handed to the host, the DCA must
try to STOP the queued work before it leaves. It does that the only way it is allowed to — by REQUESTING
cancellation through EffortlessPromo. There is deliberately no ``recall`` / ``unsend`` / provider
method: the DCA can only ever ASK the platform to cancel; the platform owns the queue and performs (or
refuses) the cancellation.

Two realities shape the contract, and both are typed here:

- **Not everything can be stopped.** A piece of queued work that the provider has ALREADY accepted has
  left the building — it is **non-recallable**, and the honest outcome is to LABEL it so (never to
  pretend it was canceled). Only work still QUEUED inside the pipeline can actually be canceled. A
  cancellation request therefore returns a per-item outcome, not a single yes/no.
- **Acknowledgment is load-bearing and fail-closed.** Requesting cancellation is worthless unless the
  host GUARANTEES it will ACKNOWLEDGE the request — an unacknowledged "cancel" is indistinguishable
  from a dropped one, so the DCA could never know the queued send was actually stopped. A host that
  does not acknowledge (or does not support cancellation, or is unavailable/stale) therefore DISABLES
  cancellation rather than being treated as a silent success. :func:`authorize_cancellation` encodes
  that: an absent, stale, non-cancelling, or non-acknowledging host capability yields a deny, never a
  bypass. A disabled request changes NOTHING on the host — nothing is canceled and nothing is labeled.

A cancellation request is not itself recipient consent and not a send authorization (the send already
in flight stays owned by the one send pipeline). Mirrors the DCA-096 delivery contract: typed shapes +
a client ``Protocol`` + a deterministic fail-closed decision here, a synthetic fake alongside
(``app/delivery/cancellation_fake.py``), and a live HTTP adapter deferred to a later story (blocked on
DCA-001 B1 — there is no service-to-service contract into the .NET API yet). The contract stays
disabled in production until that adapter ships; the fake never stands in for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

# A cancellation-host capability answer older than this is treated as stale and cannot authorize a
# request — cancellation races the actual send, so a cached "I acknowledge cancellations" claim from
# long ago may no longer hold. Kept tight, matching the DCA-096 delivery freshness window.
DEFAULT_MAX_CAPABILITIES_AGE = timedelta(hours=1)


def _norm(value: str) -> str:
    """Case-insensitive, whitespace-trimmed key for matching a channel against the host's scope."""
    return value.strip().casefold()


class CancellationHostAvailability(str, Enum):
    """Whether the EffortlessPromo cancellation contract could be consulted for this request.

    - ``AVAILABLE`` — the platform answered; its capabilities are authoritative as of ``as_of``.
    - ``UNAVAILABLE`` — the contract could not be reached/answered. The DCA has no authoritative view
      of whether a cancellation would be acknowledged, so it must fail closed — an unavailable host
      authorizes nothing (the capability fields are meaningless here).
    """

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class QueuedWorkState(str, Enum):
    """The host-side state of one piece of queued work, which decides whether it can be stopped.

    - ``QUEUED`` — still inside the platform's one send pipeline, not yet handed to a provider. This is
      the only state that can actually be canceled.
    - ``PROVIDER_ACCEPTED`` — already accepted by the sending provider; it has left the pipeline and is
      **non-recallable**. The cancellation request can only LABEL it as such, never undo it.
    """

    QUEUED = "queued"
    PROVIDER_ACCEPTED = "provider_accepted"


class CancellationItemStatus(str, Enum):
    """The outcome of trying to cancel ONE queued-work item.

    - ``CANCELED`` — the item was still ``QUEUED`` and the host removed it before send.
    - ``NON_RECALLABLE`` — the item was already ``PROVIDER_ACCEPTED``; it is labeled non-recallable and
      left in place (the honest outcome — the DCA never pretends an in-flight send was stopped).
    - ``NOT_FOUND`` — the host has no queued work under that key (already sent, already canceled, or
      never queued). Reported, never fabricated as a cancel.
    """

    CANCELED = "canceled"
    NON_RECALLABLE = "non_recallable"
    NOT_FOUND = "not_found"


class CancellationCapabilities(BaseModel):
    """An immutable, point-in-time view of what the EffortlessPromo cancellation contract offers.

    ``acknowledges`` is the load-bearing field: it states whether the platform GUARANTEES it will
    ACKNOWLEDGE each cancellation request. Without that guarantee the DCA could never know a queued send
    was actually stopped, so an unacknowledged host DISABLES cancellation — never a silent success.
    ``supports_cancellation`` states whether the host offers a cancel path at all. ``supported_channels``
    is the affected scope. ``as_of`` is when the platform produced the answer (its freshness); staleness
    is measured from it. ``synthetic`` marks a non-production fixture. ``host_owns_send`` is always
    ``True`` and states the ownership boundary at the type level: the platform owns the queue and the
    send; the DCA only ever requests a cancellation. When ``availability`` is ``UNAVAILABLE`` the
    capability fields are not authoritative and the gate refuses regardless of them.
    """

    model_config = ConfigDict(frozen=True)

    business_id: str = Field(min_length=1)
    availability: CancellationHostAvailability
    version: str = Field(min_length=1)
    as_of: datetime
    supports_cancellation: bool = False
    acknowledges: bool = False
    supported_channels: tuple[str, ...] = ()
    host_owns_send: bool = True
    synthetic: bool = False

    @property
    def is_available(self) -> bool:
        return self.availability is CancellationHostAvailability.AVAILABLE

    @property
    def offers_cancellation(self) -> bool:
        """Does the host expose a cancel path at all? Only meaningful on an available answer."""
        return self.is_available and self.supports_cancellation

    @property
    def acknowledges_cancellations(self) -> bool:
        """Does the host GUARANTEE it will acknowledge each cancellation? Only meaningful on an
        available answer; the gate checks availability/freshness/support first. ``False`` ⇒ cancellation
        is disabled, because an unacknowledged cancel can never be confirmed."""
        return self.offers_cancellation and self.acknowledges

    def supports_channel(self, channel: str) -> bool:
        target = _norm(channel)
        return any(_norm(c) == target for c in self.supported_channels)

    def age(self, *, now: datetime) -> timedelta:
        return now - self.as_of


class QueuedWorkRef(BaseModel):
    """A pointer to ONE piece of queued host work the DCA wants canceled — provenance only, no content.

    ``idempotency_key`` is the SAME consume-once key the DCA-095 recheck / DCA-096 delivery request used
    for this dispatch, so the host can find the exact queued work and a retried cancellation targets the
    SAME logical item. The recipient is carried ONLY as a one-way ``recipient_digest`` — never the raw
    address — exactly as the dispatch-consumption ledger stores it. There is no message body: the DCA
    does not hold content; it only names the dispatch to stop.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=128)
    channel: str = Field(min_length=1)
    recipient_digest: str = Field(min_length=1)
    consumption_id: str = ""


class CancellationRequest(BaseModel):
    """A request to CANCEL queued delivery THROUGH EffortlessPromo because a permission was invalidated.

    ``cancellation_key`` is the consume-once idempotency key for this cancellation operation itself, so
    a retried cancellation request is the SAME logical request and returns the SAME acknowledgment.
    ``invalidation_reason`` is WHY the permission is no longer valid (e.g. consent withdrawn, newly
    suppressed, source right expired) — audit provenance, never itself a send decision. ``items`` are
    the queued-work pointers to stop. ``correlation_id`` is carried through so the (future) HTTP call and
    its logs share one id. There is no payload/body: the DCA names dispatches to cancel, it does not
    compose content.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    business_id: str = Field(min_length=1)
    cancellation_key: str = Field(min_length=1, max_length=128)
    invalidation_reason: str = Field(min_length=1)
    items: tuple[QueuedWorkRef, ...] = Field(min_length=1)
    correlation_id: str | None = None


class CancellationItemOutcome(BaseModel):
    """What happened to ONE queued-work item in a cancellation request.

    ``status`` is one of the :class:`CancellationItemStatus` values. ``recallable`` is ``True`` only for
    an item that was still queued and was actually canceled — an already-provider-accepted item is
    ``recallable=False`` and labeled ``NON_RECALLABLE``. ``note`` is a human-readable explanation. The
    recipient is never echoed — only the ``idempotency_key`` that named the item.
    """

    model_config = ConfigDict(frozen=True)

    idempotency_key: str = Field(min_length=1)
    status: CancellationItemStatus
    recallable: bool
    note: str = ""


class CancellationReceipt(BaseModel):
    """What the EffortlessPromo cancellation contract returns for a request. Never a send confirmation.

    ``acknowledged`` is the load-bearing flag: whether the host ACKNOWLEDGED the cancellation request.
    Only an acknowledged receipt lets the DCA trust the per-item outcomes; a disabled request is
    ``acknowledged=False`` and carries no outcomes. ``status`` is one of :data:`ACKNOWLEDGED` /
    :data:`DISABLED`; ``reason`` is a stable code (a :class:`CancellationDecision` reason on a disable).
    ``outcomes`` is the per-item result (empty when disabled). ``canceled_count`` /
    ``non_recallable_count`` summarise it. ``acknowledgment_reference`` is an opaque handle the platform
    returns for an acknowledged request (empty when disabled); it is deterministic in the cancellation
    key so a replay returns the same handle. ``cancellation_key`` echoes the request's key.
    """

    model_config = ConfigDict(frozen=True)

    business_id: str = Field(min_length=1)
    cancellation_key: str = Field(min_length=1)
    status: str
    reason: str
    acknowledged: bool = False
    outcomes: tuple[CancellationItemOutcome, ...] = ()
    acknowledgment_reference: str = ""
    synthetic: bool = False

    # stable status strings the caller reads uniformly
    ACKNOWLEDGED: ClassVar[str] = "acknowledged"
    DISABLED: ClassVar[str] = "disabled"

    @property
    def canceled_count(self) -> int:
        return sum(1 for o in self.outcomes if o.status is CancellationItemStatus.CANCELED)

    @property
    def non_recallable_count(self) -> int:
        return sum(1 for o in self.outcomes if o.status is CancellationItemStatus.NON_RECALLABLE)

    @property
    def authorizes_send(self) -> bool:
        """A cancellation receipt never authorizes a send — it only reports what was stopped. Kept
        explicit so no caller mistakes it for a send decision."""
        return False


@runtime_checkable
class CancellationPort(Protocol):
    """Port the DCA uses to REQUEST cancellation of queued work through the one send pipeline.

    Synchronous, matching the DCA-096 delivery port and the other service-to-service clients. There is
    exactly one method — ``request_cancellation`` — and deliberately no ``recall`` / ``unsend`` /
    provider / channel-I/O method: the DCA can only ever REQUEST a cancellation; the platform owns the
    queue and performs (or refuses) it. The only implementation today is the dev/test fake; the live
    HTTP adapter is a later story (blocked on DCA-001 B1) and until it ships the contract stays disabled
    in production.
    """

    def request_cancellation(self, request: CancellationRequest) -> CancellationReceipt: ...


@dataclass(frozen=True)
class CancellationDecision:
    """Whether the host's capabilities permit REQUESTING cancellation. ``reason`` is a stable code.

    An allow is permission only to hand the cancellation request to the platform — it does not itself
    cancel anything (the host does), and it is never recipient consent or a send authorization
    (``authorizes_send`` is always ``False``).
    """

    allowed: bool
    reason: str

    # stable reason codes
    OK = "ok"
    HOST_UNAVAILABLE = "cancellation_host_unavailable"
    STALE = "cancellation_capabilities_stale"
    CANCELLATION_NOT_SUPPORTED = "cancellation_not_supported"
    ACKNOWLEDGMENT_NOT_GUARANTEED = "cancellation_acknowledgment_unavailable"

    @property
    def authorizes_send(self) -> bool:
        return False


def authorize_cancellation(
    capabilities: CancellationCapabilities | None,
    *,
    now: datetime | None = None,
    max_age: timedelta = DEFAULT_MAX_CAPABILITIES_AGE,
) -> CancellationDecision:
    """Decide whether the EffortlessPromo cancellation contract permits requesting a cancellation.

    Fail-closed at each step: absent/unavailable → stale → cancellation-not-supported → acknowledgment-
    not-guaranteed → allowed. The decision is a pure function of the capabilities and the moment — it
    reads nothing and changes nothing, so the allow and every denial are perfectly reproducible.

    The acknowledgment step is the load-bearing one: a host that does not guarantee it will acknowledge
    a cancellation DISABLES the request rather than letting the DCA treat an unconfirmable "cancel" as a
    success.
    """
    moment = now or datetime.now(UTC)

    # Absent or unavailable: no authoritative view of whether a cancellation would be acknowledged.
    if capabilities is None or not capabilities.is_available:
        return CancellationDecision(False, CancellationDecision.HOST_UNAVAILABLE)

    # Stale: fetched too long ago. Cancellation races the send, so an old answer can no longer be
    # trusted — refuse.
    if capabilities.age(now=moment) > max_age:
        return CancellationDecision(False, CancellationDecision.STALE)

    # The host must actually offer a cancel path.
    if not capabilities.offers_cancellation:
        return CancellationDecision(False, CancellationDecision.CANCELLATION_NOT_SUPPORTED)

    # The host must GUARANTEE acknowledgment — an unacknowledged cancel can never be confirmed, so the
    # DCA would not know the queued send was stopped. Disable rather than bypass.
    if not capabilities.acknowledges_cancellations:
        return CancellationDecision(False, CancellationDecision.ACKNOWLEDGMENT_NOT_GUARANTEED)

    # Cleared to REQUEST cancellation. Not a cancel by itself, not consent, not a send.
    return CancellationDecision(True, CancellationDecision.OK)
