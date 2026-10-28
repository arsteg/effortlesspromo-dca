"""Explainable canonical selection over a field's append-only observation history (DCA-069).

The pure, network-free *conclusion* layer on top of the DCA-061 field-observation history (and the
selection sibling of :mod:`app.matching`). For one subject + one ``field_key`` it chooses which
observed value is CANONICAL — and explains why — using evidence reliability, verified identity,
freshness and rights, NOT a rigid source-tier rule. It records, mutates and deletes nothing, so every
losing value remains in history; acting on the conclusion stays a disabled future capability.

Public surface:

- :func:`select_canonical` / :func:`select_canonical_by_field` — the selection.
- :class:`CanonicalSelection` / :class:`CandidateVerdict` / :class:`CanonicalCandidate` /
  :class:`Reliability`.
- :func:`candidates_from_observations` — the read-only adapter over stored field observations.
- The stable reason codes (``REASON_*`` winners, ``DENIAL_*`` ineligibility).
"""

from __future__ import annotations

from app.canonical.field_selection import (
    DENIAL_NO_RIGHTS_BASIS,
    DENIAL_RETENTION_LAPSED,
    DENIAL_SOURCE_RIGHTS_NOT_IN_FORCE,
    REASON_FRESHER,
    REASON_HIGHER_RELIABILITY,
    REASON_ONLY_ELIGIBLE,
    REASON_RECORDED_LATER_TIEBREAK,
    REASON_RIGHTS_IN_FORCE_TIEBREAK,
    REASON_STABLE_ID_TIEBREAK,
    REASON_VERIFIED_OVER_STALE_REGISTRY,
    CanonicalCandidate,
    CanonicalSelection,
    CandidateVerdict,
    Reliability,
    candidates_from_observations,
    select_canonical,
    select_canonical_by_field,
)

__all__ = [
    "DENIAL_NO_RIGHTS_BASIS",
    "DENIAL_RETENTION_LAPSED",
    "DENIAL_SOURCE_RIGHTS_NOT_IN_FORCE",
    "REASON_FRESHER",
    "REASON_HIGHER_RELIABILITY",
    "REASON_ONLY_ELIGIBLE",
    "REASON_RECORDED_LATER_TIEBREAK",
    "REASON_RIGHTS_IN_FORCE_TIEBREAK",
    "REASON_STABLE_ID_TIEBREAK",
    "REASON_VERIFIED_OVER_STALE_REGISTRY",
    "CandidateVerdict",
    "CanonicalCandidate",
    "CanonicalSelection",
    "Reliability",
    "candidates_from_observations",
    "select_canonical",
    "select_canonical_by_field",
]
