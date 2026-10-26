"""DCA-022 — the subscription-entitlement contract (platform-owned billing, DCA-side port).

The DCA never owns subscriptions; this package defines only the contract it uses to ask the platform
"what is business B entitled to?": a typed request/snapshot, the client ``Protocol``, the fail-closed
rule that turns a snapshot into an allow/deny for paid work, and a deterministic synthetic fake for
local tests. The live adapter is a later story and stays unbuilt; production refuses the fake.
"""

from __future__ import annotations

from app.entitlements.contract import (
    DEFAULT_MAX_SNAPSHOT_AGE,
    EntitlementClient,
    EntitlementDecision,
    EntitlementRequest,
    EntitlementSnapshot,
    EntitlementStatus,
    authorize_paid_work,
)
from app.entitlements.fake import (
    EntitlementContractDisabled,
    FakeEntitlementClient,
    build_entitlement_client,
)

__all__ = [
    "DEFAULT_MAX_SNAPSHOT_AGE",
    "EntitlementClient",
    "EntitlementContractDisabled",
    "EntitlementDecision",
    "EntitlementRequest",
    "EntitlementSnapshot",
    "EntitlementStatus",
    "FakeEntitlementClient",
    "authorize_paid_work",
    "build_entitlement_client",
]
