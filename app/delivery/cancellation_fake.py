"""DCA-097 — deterministic, in-memory EffortlessPromo cancellation fake for local tests.

The only cancellation-port implementation today: the live HTTP adapter into the platform's one send
pipeline is a later story (blocked on DCA-001 B1 — there is no service-to-service contract into the
.NET API yet). It is dev/test-only — production refuses it (``build_cancellation_port``), mirroring the
DCA-096 delivery fake and the identity/entitlement/suppression fakes. Every capability answer it
produces is flagged ``synthetic`` with a plainly-fake version, so a test answer can never be mistaken
for a real authoritative one. It performs NO send and NO recall: it models a host-side queue of
already-handed-over work and reports, per item, whether that work could be CANCELED (still queued) or
is NON_RECALLABLE (already accepted by a provider) — the acceptance behaviour of this story.

Knobs make the DCA-097 behaviour and its failure cases testable without a wall clock:

- ``queued`` — the host-side queue: a mapping of ``idempotency_key -> QueuedWorkState``. Keys in
  ``QUEUED`` are cancelable; keys in ``PROVIDER_ACCEPTED`` are non-recallable; unknown keys are
  ``NOT_FOUND``. Seeds the fixture before the DCA requests cancellation.
- ``acknowledges`` — when ``False`` the host does NOT guarantee acknowledgment, so a request returns a
  DISABLED receipt and the queue is left UNTOUCHED (the acceptance-criterion failure case).
- ``supports_cancellation`` — when ``False`` the host offers no cancel path at all (also disabled).
- ``available`` — when ``False`` the contract simulates being unreachable (fail-closed).

``now`` is injected into each capability answer's ``as_of`` so freshness/staleness is exercised by
passing :func:`~app.delivery.cancellation.authorize_cancellation` a later ``now``. The fake keeps an
in-memory acknowledgment ledger keyed on the request's ``cancellation_key`` so a replayed cancellation
returns the SAME receipt verbatim (the acknowledgment-tracking idempotency, visible) and never re-reads
a now-empty queue into a different answer.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from app.delivery.cancellation import (
    CancellationCapabilities,
    CancellationHostAvailability,
    CancellationItemOutcome,
    CancellationItemStatus,
    CancellationRequest,
    CancellationReceipt,
    QueuedWorkState,
    authorize_cancellation,
)

SYNTHETIC_VERSION = "fake-cancellation-v1"

# The channels the synthetic EffortlessPromo cancellation contract speaks for.
DEFAULT_SUPPORTED_CHANNELS: tuple[str, ...] = ("email", "sms")


class CancellationContractDisabled(RuntimeError):
    """Raised when a cancellation port is requested where the contract is not wired (production)."""


class FakeEffortlessPromoCancellationPort:
    """A deterministic :class:`~app.delivery.cancellation.CancellationPort`. No I/O, no send, no recall.

    Models the EffortlessPromo platform's request-cancellation contract over an in-memory queue of
    already-handed-over work. ``acknowledges=False`` / ``supports_cancellation=False`` /
    ``available=False`` each make EVERY request disabled (the failure cases) and leave the queue
    UNTOUCHED. ``now`` is injected into ``as_of`` so freshness/staleness is testable.

    An acknowledged request removes each still-``QUEUED`` item from the queue (status ``CANCELED``),
    labels each ``PROVIDER_ACCEPTED`` item ``NON_RECALLABLE`` and leaves it in place, and reports an
    unknown key as ``NOT_FOUND``. The ``acknowledgment_reference`` is DETERMINISTIC in the cancellation
    key, and the whole receipt is stored under that key so a replay returns it verbatim.
    """

    def __init__(
        self,
        *,
        queued: Mapping[str, QueuedWorkState] | None = None,
        acknowledges: bool = True,
        supports_cancellation: bool = True,
        available: bool = True,
        supported_channels: tuple[str, ...] = DEFAULT_SUPPORTED_CHANNELS,
        now: datetime | None = None,
        version: str = SYNTHETIC_VERSION,
    ) -> None:
        self._queue: dict[str, QueuedWorkState] = dict(queued or {})
        self._acknowledges = acknowledges
        self._supports_cancellation = supports_cancellation
        self._available = available
        self._supported_channels = supported_channels
        self._now = now
        self._version = version
        # Acknowledgment ledger: cancellation_key -> receipt already issued. Makes a replayed request
        # idempotent and keeps a re-run from re-reading the (now-mutated) queue into a different answer.
        self._ledger: dict[str, CancellationReceipt] = {}

    def queue_state(self, idempotency_key: str) -> QueuedWorkState | None:
        """The host-side state of one queued item, or ``None`` if the host has no such work."""
        return self._queue.get(idempotency_key)

    def capabilities(self, business_id: str) -> CancellationCapabilities:
        """What this fake host advertises for a business. Unavailable ⇒ capability fields are empty and
        the gate refuses on availability regardless of them."""
        now = self._now or datetime.now(UTC)
        if not self._available:
            return CancellationCapabilities(
                business_id=business_id,
                availability=CancellationHostAvailability.UNAVAILABLE,
                version=self._version,
                as_of=now,
                synthetic=True,
            )
        return CancellationCapabilities(
            business_id=business_id,
            availability=CancellationHostAvailability.AVAILABLE,
            version=self._version,
            as_of=now,
            supports_cancellation=self._supports_cancellation,
            acknowledges=self._acknowledges,
            supported_channels=self._supported_channels,
            synthetic=True,
        )

    def request_cancellation(self, request: CancellationRequest) -> CancellationReceipt:
        """Request cancellation of queued work through the (fake) platform. Fail-closed: a host that is
        unavailable, stale, non-cancelling, or non-acknowledging returns a DISABLED receipt and leaves
        the queue UNTOUCHED — never a silent success. An acknowledged request cancels still-queued work,
        labels already-provider-accepted work non-recallable, and is idempotent in its cancellation
        key."""
        # Replay: a previously acknowledged request returns its stored receipt verbatim.
        prior = self._ledger.get(request.cancellation_key)
        if prior is not None:
            return prior

        now = self._now or datetime.now(UTC)
        caps = self.capabilities(request.business_id)
        decision = authorize_cancellation(caps, now=now)
        if not decision.allowed:
            # Nothing is canceled and nothing is labeled — the queue is left exactly as it was. A
            # disabled request is not stored in the ledger: once the host recovers the DCA may retry.
            return CancellationReceipt(
                business_id=request.business_id,
                cancellation_key=request.cancellation_key,
                status=CancellationReceipt.DISABLED,
                reason=decision.reason,
                acknowledged=False,
                synthetic=True,
            )

        outcomes: list[CancellationItemOutcome] = []
        for item in request.items:
            state = self._queue.get(item.idempotency_key)
            if state is QueuedWorkState.QUEUED:
                # Still inside the pipeline — actually cancel it (remove from the queue).
                del self._queue[item.idempotency_key]
                outcomes.append(
                    CancellationItemOutcome(
                        idempotency_key=item.idempotency_key,
                        status=CancellationItemStatus.CANCELED,
                        recallable=True,
                        note="queued work canceled before send",
                    )
                )
            elif state is QueuedWorkState.PROVIDER_ACCEPTED:
                # Already accepted by a provider — label non-recallable, never pretend it was stopped.
                outcomes.append(
                    CancellationItemOutcome(
                        idempotency_key=item.idempotency_key,
                        status=CancellationItemStatus.NON_RECALLABLE,
                        recallable=False,
                        note="already accepted by provider; not recallable",
                    )
                )
            else:
                outcomes.append(
                    CancellationItemOutcome(
                        idempotency_key=item.idempotency_key,
                        status=CancellationItemStatus.NOT_FOUND,
                        recallable=False,
                        note="no queued work found for this key",
                    )
                )

        receipt = CancellationReceipt(
            business_id=request.business_id,
            cancellation_key=request.cancellation_key,
            status=CancellationReceipt.ACKNOWLEDGED,
            reason=decision.reason,
            acknowledged=True,
            outcomes=tuple(outcomes),
            acknowledgment_reference=f"synthetic-ack-{request.cancellation_key}",
            synthetic=True,
        )
        self._ledger[request.cancellation_key] = receipt
        return receipt


def build_cancellation_port(settings: Any) -> FakeEffortlessPromoCancellationPort:
    """The cancellation port for this deployment.

    Only the dev/test fake exists; the live adapter into the platform's one send pipeline is unbuilt
    (DCA-001 B1). Production therefore has NO cancellation port — the contract stays disabled until that
    story ships. Calling this under production configuration raises rather than silently handing back a
    fake (which would risk treating a synthetic acknowledgment as a real one — a fail-open this contract
    forbids, exactly as the DCA-096 delivery fake does).
    """
    if getattr(settings, "is_production", False):
        raise CancellationContractDisabled(
            "cancellation contract is disabled in production: the live adapter into the one send "
            "pipeline is not built yet (DCA-001 B1) and the synthetic fake is dev/test-only"
        )
    return FakeEffortlessPromoCancellationPort()
