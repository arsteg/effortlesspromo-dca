"""DCA-079 — the authoritative suppression-lookup contract (host-owned, DCA-side port).

The DCA never owns suppression or recipient consent; this package defines only the contract it uses to
ask the host platform "is this contact point suppressed for this channel right now?": a typed
request/snapshot (disclosing version, freshness and affected scope), the client ``Protocol``, the
fail-closed rule that turns a snapshot into an allow/deny for activating a candidate on a channel, and
a deterministic synthetic fake for local tests. An unavailable or stale authoritative answer never
permits activation, and an allow is never recipient consent. The live adapter is a later story and
stays unbuilt; production refuses the fake.
"""

from __future__ import annotations

from app.suppression.contract import (
    DEFAULT_MAX_SUPPRESSION_AGE,
    ActivationDecision,
    PlatformSuppressionClient,
    SuppressionAvailability,
    SuppressionEntry,
    SuppressionLookupRequest,
    SuppressionSnapshot,
    authorize_activation,
)
from app.suppression.fake import (
    DEFAULT_COVERED_CHANNELS,
    FakePlatformSuppressionClient,
    SuppressionContractDisabled,
    build_platform_suppression_client,
)
from app.suppression.sync import (
    SUPPRESSION_AGGREGATE_TYPE,
    SUPPRESSION_EVENT_SOURCE,
    SUPPRESSION_EVENT_TYPE,
    PlatformSuppressionProjector,
    SuppressionState,
    SuppressionSyncDecision,
    SuppressionSyncOutcome,
)

__all__ = [
    "DEFAULT_COVERED_CHANNELS",
    "DEFAULT_MAX_SUPPRESSION_AGE",
    "SUPPRESSION_AGGREGATE_TYPE",
    "SUPPRESSION_EVENT_SOURCE",
    "SUPPRESSION_EVENT_TYPE",
    "ActivationDecision",
    "FakePlatformSuppressionClient",
    "PlatformSuppressionClient",
    "PlatformSuppressionProjector",
    "SuppressionAvailability",
    "SuppressionContractDisabled",
    "SuppressionEntry",
    "SuppressionLookupRequest",
    "SuppressionSnapshot",
    "SuppressionState",
    "SuppressionSyncDecision",
    "SuppressionSyncOutcome",
    "authorize_activation",
    "build_platform_suppression_client",
]
