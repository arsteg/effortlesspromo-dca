"""DCA-098 — advertising-audience removal contract: request removal ONLY through EffortlessPromo.

The DCA never touches an ad-platform SDK/key/audience API; it holds no upload or send path. This package
defines only the contract it uses to REQUEST removal of a revoked, previously-uploaded member from an
advertising audience (e.g. Google Customer Match) through the platform's integration: a typed request (a
removal idempotency key + hashed-member pointers + the withdrawal reason), a host-capabilities snapshot,
the client ``Protocol`` (one method, ``request_removal`` — no upload/add/sync/provider method), the
fail-closed rule that turns capabilities into an allow/deny, and a deterministic synthetic fake for local
tests. A removal that cannot be confirmed stays PENDING (visible, retryable) rather than being faked as
done, and the prior upload never overrides the current withdrawal. An allow is never recipient consent
and never a send/upload authorization. The live adapter is a later story (DCA-001 B1) and stays unbuilt;
production refuses the fake.
"""

from __future__ import annotations

from app.advertising.audience_removal import (
    DEFAULT_MAX_CAPABILITIES_AGE,
    AudienceMemberRef,
    AudienceMemberState,
    RemovalCapabilities,
    RemovalDecision,
    RemovalHostAvailability,
    RemovalItemOutcome,
    RemovalItemStatus,
    RemovalPort,
    RemovalReceipt,
    RemovalRequest,
    authorize_removal,
)
from app.advertising.audience_removal_fake import (
    DEFAULT_SUPPORTED_AUDIENCES,
    SYNTHETIC_VERSION,
    AudienceRemovalContractDisabled,
    FakeEffortlessPromoAudienceRemovalPort,
    build_removal_port,
)

__all__ = [
    "DEFAULT_MAX_CAPABILITIES_AGE",
    "DEFAULT_SUPPORTED_AUDIENCES",
    "SYNTHETIC_VERSION",
    "AudienceMemberRef",
    "AudienceMemberState",
    "AudienceRemovalContractDisabled",
    "FakeEffortlessPromoAudienceRemovalPort",
    "RemovalCapabilities",
    "RemovalDecision",
    "RemovalHostAvailability",
    "RemovalItemOutcome",
    "RemovalItemStatus",
    "RemovalPort",
    "RemovalReceipt",
    "RemovalRequest",
    "authorize_removal",
    "build_removal_port",
]
