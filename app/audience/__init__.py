"""DCA-154 — the audience-sync contract: sync an accepted prospect ONLY through EffortlessPromo.

The DCA owns no CRM and no audience; the EffortlessPromo platform is the system of record. This package
defines only the contract the DCA uses to REQUEST a sync of one DCA-153 acceptance intent through the
platform's CRM: typed request models (a stable request key + minimized, class-tagged fields), the
external-id / acknowledgment result, the client ``Protocol`` (one method, ``sync`` — no create/promote
method), the fail-closed rule that refuses a sendable sync / a restricted contact field / a disallowed
purpose rather than silently promoting it, a builder from an acceptance intent, and a deterministic
synthetic fake for local tests. A sync is never recipient consent and never a send; PENDING and
ACKNOWLEDGED outcomes differ. The live adapter is a later story (DCA-001 B1) and stays unbuilt;
production refuses the fake.
"""

from __future__ import annotations

from app.audience.contract import (
    VISIBILITY_RESEARCH_ONLY,
    VISIBILITY_SENDABLE,
    AudienceField,
    AudienceFieldClass,
    AudienceSyncAck,
    AudienceSyncDecision,
    AudienceSyncPort,
    AudienceSyncPurpose,
    AudienceSyncRequest,
    AudienceSyncStatus,
    ExternalRecordRef,
    audience_sync_request_from_intent,
    authorize_audience_sync,
)
from app.audience.fake import (
    SYNTHETIC_VERSION,
    AudienceSyncContractDisabled,
    FakeEffortlessPromoAudienceSyncPort,
    build_audience_sync_port,
)

__all__ = [
    "SYNTHETIC_VERSION",
    "VISIBILITY_RESEARCH_ONLY",
    "VISIBILITY_SENDABLE",
    "AudienceField",
    "AudienceFieldClass",
    "AudienceSyncAck",
    "AudienceSyncContractDisabled",
    "AudienceSyncDecision",
    "AudienceSyncPort",
    "AudienceSyncPurpose",
    "AudienceSyncRequest",
    "AudienceSyncStatus",
    "ExternalRecordRef",
    "FakeEffortlessPromoAudienceSyncPort",
    "audience_sync_request_from_intent",
    "authorize_audience_sync",
    "build_audience_sync_port",
]
