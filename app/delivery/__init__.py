"""DCA-096 — the delivery adapter contract: request delivery ONLY through EffortlessPromo.

The DCA never sends; it holds no marketing-provider SDK/key/send path. This package defines only the
contract it uses to REQUEST delivery through the platform's one send pipeline: a typed request (an
idempotency key + the consumed DCA-095 decision context), a host-capabilities snapshot, the client
``Protocol`` (one method, ``request_delivery`` — no send/provider method), the fail-closed rule that
turns capabilities into an allow/deny, and a deterministic synthetic fake for local tests. A host that
does not ENFORCE delivery callbacks disables live delivery rather than being bypassed; an allow is
never recipient consent and never a send authorization. The live adapter is a later story (DCA-001 B1)
and stays unbuilt; production refuses the fake.
"""

from __future__ import annotations

from app.delivery.cancellation import (
    CancellationCapabilities,
    CancellationDecision,
    CancellationHostAvailability,
    CancellationItemOutcome,
    CancellationItemStatus,
    CancellationPort,
    CancellationReceipt,
    CancellationRequest,
    QueuedWorkRef,
    QueuedWorkState,
    authorize_cancellation,
)
from app.delivery.cancellation_fake import (
    CancellationContractDisabled,
    FakeEffortlessPromoCancellationPort,
    build_cancellation_port,
)
from app.delivery.contract import (
    DEFAULT_MAX_CAPABILITIES_AGE,
    DeliveryCapabilities,
    DeliveryContext,
    DeliveryDecision,
    DeliveryHostAvailability,
    DeliveryPort,
    DeliveryReceipt,
    DeliveryRequest,
    authorize_delivery,
)
from app.delivery.fake import (
    DEFAULT_SUPPORTED_CHANNELS,
    SYNTHETIC_VERSION,
    DeliveryContractDisabled,
    FakeEffortlessPromoDeliveryPort,
    build_delivery_port,
)

__all__ = [
    "DEFAULT_MAX_CAPABILITIES_AGE",
    "DEFAULT_SUPPORTED_CHANNELS",
    "SYNTHETIC_VERSION",
    "CancellationCapabilities",
    "CancellationContractDisabled",
    "CancellationDecision",
    "CancellationHostAvailability",
    "CancellationItemOutcome",
    "CancellationItemStatus",
    "CancellationPort",
    "CancellationReceipt",
    "CancellationRequest",
    "DeliveryCapabilities",
    "DeliveryContext",
    "DeliveryContractDisabled",
    "DeliveryDecision",
    "DeliveryHostAvailability",
    "DeliveryPort",
    "DeliveryReceipt",
    "DeliveryRequest",
    "FakeEffortlessPromoCancellationPort",
    "FakeEffortlessPromoDeliveryPort",
    "QueuedWorkRef",
    "QueuedWorkState",
    "authorize_cancellation",
    "authorize_delivery",
    "build_cancellation_port",
    "build_delivery_port",
]
