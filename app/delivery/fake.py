"""DCA-096 — deterministic, in-memory EffortlessPromo delivery fake for local tests.

The only delivery-port implementation today: the live HTTP adapter into the platform's one send
pipeline is a later story (blocked on DCA-001 B1 — there is no service-to-service contract into the
.NET API yet). It is dev/test-only — production refuses it (``build_delivery_port``), mirroring the
identity/entitlement/suppression fakes and the seed's dev-only stance. Every capability answer it
produces is flagged ``synthetic`` and carries a plainly-fake version, so a test answer can never be
mistaken for a real authoritative one. It performs NO send and holds NO provider: an accepted receipt
only acknowledges the request was taken into the platform's pipeline, which owns the actual send.

Two knobs make the DCA-096 behaviour and its failure case testable without a wall clock:

- ``callback_enforcement`` — when ``False`` the fake host does NOT guarantee terminal-outcome
  callbacks, so :meth:`request_delivery` returns a DISABLED receipt (live delivery off) rather than a
  silent accept. This is the acceptance-criterion failure case.
- ``available`` — when ``False`` the delivery contract simulates being unreachable: capabilities read
  ``UNAVAILABLE`` and every request is disabled (the fail-closed path).

``now`` is injected into each capability answer's ``as_of`` so freshness/staleness is exercised by
asking :func:`~app.delivery.contract.authorize_delivery` with a later ``now``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.delivery.contract import (
    DeliveryCapabilities,
    DeliveryDecision,
    DeliveryHostAvailability,
    DeliveryReceipt,
    DeliveryRequest,
    authorize_delivery,
)

SYNTHETIC_VERSION = "fake-delivery-v1"

# The channels the synthetic EffortlessPromo delivery contract speaks for. A channel outside this
# cannot be requested — the gate refuses it (CHANNEL_NOT_SUPPORTED).
DEFAULT_SUPPORTED_CHANNELS: tuple[str, ...] = ("email", "sms")


class DeliveryContractDisabled(RuntimeError):
    """Raised when a delivery port is requested where the contract is not wired (production)."""


class FakeEffortlessPromoDeliveryPort:
    """A deterministic :class:`~app.delivery.contract.DeliveryPort`. Pure: no I/O, no send, no provider.

    Models the EffortlessPromo platform's request-delivery contract. ``callback_enforcement=False``
    makes EVERY request disabled (the acceptance-criterion failure case); ``available=False`` makes the
    contract read ``UNAVAILABLE`` (the registry-down path). ``now`` is injected into ``as_of`` so
    freshness/staleness is testable; it defaults to real UTC now. Every capability answer is
    ``synthetic=True`` and ``host_owns_send=True``.

    An accepted request yields a receipt whose ``host_reference`` is DETERMINISTIC in the idempotency
    key, so a replayed request returns the same handle — the contract's idempotency, visible. Accepting
    is never sending: ``authorizes_send`` is always ``False``.
    """

    def __init__(
        self,
        *,
        callback_enforcement: bool = True,
        available: bool = True,
        supported_channels: tuple[str, ...] = DEFAULT_SUPPORTED_CHANNELS,
        now: datetime | None = None,
        version: str = SYNTHETIC_VERSION,
    ) -> None:
        self._callback_enforcement = callback_enforcement
        self._available = available
        self._supported_channels = supported_channels
        self._now = now
        self._version = version

    def capabilities(self, business_id: str) -> DeliveryCapabilities:
        """What this fake host advertises for a business. Unavailable ⇒ capability fields are empty and
        the gate refuses on availability regardless of them."""
        now = self._now or datetime.now(UTC)
        if not self._available:
            return DeliveryCapabilities(
                business_id=business_id,
                availability=DeliveryHostAvailability.UNAVAILABLE,
                version=self._version,
                as_of=now,
                synthetic=True,
            )
        return DeliveryCapabilities(
            business_id=business_id,
            availability=DeliveryHostAvailability.AVAILABLE,
            version=self._version,
            as_of=now,
            callback_enforcement=self._callback_enforcement,
            supported_channels=self._supported_channels,
            synthetic=True,
        )

    def request_delivery(self, request: DeliveryRequest) -> DeliveryReceipt:
        """Request delivery through the (fake) platform. Fail-closed: a host that is unavailable, stale,
        not callback-enforcing, or channel-blind returns a DISABLED receipt carrying the deny reason —
        never a silent accept, never a send. A cleared request is ACCEPTED into the pipeline (still not
        a send) with a deterministic ``host_reference`` so a replay is idempotent."""
        now = self._now or datetime.now(UTC)
        caps = self.capabilities(request.context.business_id)
        decision = authorize_delivery(caps, request.context, now=now)
        if not decision.allowed:
            return DeliveryReceipt(
                accepted=False,
                status=DeliveryReceipt.DISABLED,
                reason=decision.reason,
                idempotency_key=request.idempotency_key,
                callback_enforced=caps.enforces_callbacks,
                synthetic=True,
            )
        return DeliveryReceipt(
            accepted=True,
            status=DeliveryReceipt.ACCEPTED,
            reason=DeliveryDecision.OK,
            idempotency_key=request.idempotency_key,
            callback_enforced=True,
            host_reference=f"synthetic-accepted-{request.idempotency_key}",
            synthetic=True,
        )


def build_delivery_port(settings: Any) -> FakeEffortlessPromoDeliveryPort:
    """The delivery port for this deployment.

    Only the dev/test fake exists; the live adapter into the platform's one send pipeline is unbuilt
    (DCA-001 B1). Production therefore has NO delivery port — the contract stays disabled until that
    story ships. Calling this under production configuration raises rather than silently handing back a
    fake (which would risk treating a synthetic accept as a real one — the exact fail-open this story
    forbids).
    """
    if getattr(settings, "is_production", False):
        raise DeliveryContractDisabled(
            "delivery contract is disabled in production: the live adapter into the one send pipeline "
            "is not built yet (DCA-001 B1) and the synthetic fake is dev/test-only"
        )
    return FakeEffortlessPromoDeliveryPort()
