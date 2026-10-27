"""DCA-038 — structured Indian geography + deterministic recipient-jurisdiction validation.

Public surface:

- :mod:`app.geography.india` — the controlled Indian reference data (states/UTs, PIN, GST state codes)
  and structured-field validation/normalization.
- :mod:`app.geography.evidence` — the structured geography value objects: :class:`GeoContext`,
  :class:`EvidenceKind`, :class:`LocationEvidence`, :class:`GeoLocation`, :class:`TargetingGeography`.
- :mod:`app.geography.resolver` — :func:`resolve_jurisdiction` and the India-only targeting gate
  :func:`validate_targeting`, with their stable reason codes.
"""

from __future__ import annotations

from app.geography.evidence import (
    STRONG_KINDS,
    WEAK_KINDS,
    EvidenceKind,
    GeoContext,
    GeoLocation,
    LocationEvidence,
    TargetingGeography,
    evidence_from_dicts,
)
from app.geography.resolver import (
    REASON_BLOCKED_FOREIGN,
    REASON_BLOCKED_UNRESOLVED,
    REASON_CONFLICTING_EVIDENCE,
    REASON_NO_EVIDENCE,
    REASON_RESOLVED_FOREIGN,
    REASON_RESOLVED_IN,
    REASON_TARGETING_ALLOWED,
    REASON_WEAK_SIGNAL_ONLY,
    JurisdictionResolution,
    JurisdictionStatus,
    TargetingDecision,
    resolve_jurisdiction,
    validate_targeting,
)

__all__ = [
    "EvidenceKind",
    "GeoContext",
    "GeoLocation",
    "JurisdictionResolution",
    "JurisdictionStatus",
    "LocationEvidence",
    "REASON_BLOCKED_FOREIGN",
    "REASON_BLOCKED_UNRESOLVED",
    "REASON_CONFLICTING_EVIDENCE",
    "REASON_NO_EVIDENCE",
    "REASON_RESOLVED_FOREIGN",
    "REASON_RESOLVED_IN",
    "REASON_TARGETING_ALLOWED",
    "REASON_WEAK_SIGNAL_ONLY",
    "STRONG_KINDS",
    "TargetingDecision",
    "TargetingGeography",
    "WEAK_KINDS",
    "evidence_from_dicts",
    "resolve_jurisdiction",
    "validate_targeting",
]
