"""Deterministic identity resolution for organization observations (DCA-067).

The pure, network-free decision layer on top of the DCA-064/065/066 normalizers: it compares two
bundles of already-normalized evidence and decides whether they are the SAME organization using STRONG
evidence (compatible typed registration identifiers) first, treating a shared name/domain as a weak
``REVIEW`` candidate and location as corroboration only. It records and merges nothing.

Public surface:

- :func:`match_organizations` / :class:`OrgMatchResult` / :class:`MatchOutcome` / :class:`Confidence`
- :class:`OrgMatchSubject` + :func:`build_subject` + :func:`subject_from_observation`
- :class:`MatchSignal` / :class:`SignalKind` / :class:`SignalStrength` / :class:`SignalRelation`
- :class:`OrgMatchDenial`
"""

from __future__ import annotations

from app.matching.org_match import (
    Confidence,
    MatchOutcome,
    MatchSignal,
    OrgMatchDenial,
    OrgMatchResult,
    OrgMatchSubject,
    SignalKind,
    SignalRelation,
    SignalStrength,
    build_subject,
    match_organizations,
    subject_from_observation,
)

__all__ = [
    "Confidence",
    "MatchOutcome",
    "MatchSignal",
    "OrgMatchDenial",
    "OrgMatchResult",
    "OrgMatchSubject",
    "SignalKind",
    "SignalRelation",
    "SignalStrength",
    "build_subject",
    "match_organizations",
    "subject_from_observation",
]
