"""Deterministic identity resolution for organizations (DCA-067) and contacts (DCA-068).

The pure, network-free decision layer on top of the DCA-064/065/066 normalizers. It compares two
bundles of already-normalized evidence and decides whether they are the SAME entity using STRONG
evidence first, treating weak evidence as a ``REVIEW`` candidate only. It records, merges and transfers
nothing.

- **Organizations** (DCA-067): a compatible typed registration identifier is strong; a shared
  name/domain is a weak ``REVIEW`` candidate and location only corroborates.
- **Contacts/people** (DCA-068): a shared PERSONAL email mailbox is the only strong anchor; a
  shared/recycled phone, a common name, or a shared role mailbox is weak and never transfers a
  person's permission; reassignment/role changes require review.

Public surface:

- Organizations: :func:`match_organizations` / :class:`OrgMatchResult` / :class:`MatchOutcome` /
  :class:`OrgMatchSubject` + :func:`build_subject` + :func:`subject_from_observation` /
  :class:`MatchSignal` / :class:`SignalKind`.
- Contacts: :func:`match_contacts` / :class:`ContactMatchResult` / :class:`ContactMatchOutcome` /
  :class:`ContactMatchSubject` + :func:`build_contact_subject` +
  :func:`subject_from_person_observation` / :class:`ContactMatchSignal` / :class:`ContactSignalKind` /
  :class:`ContactMatchDenial`.
- Shared: :class:`Confidence` / :class:`SignalStrength` / :class:`SignalRelation`.
"""

from __future__ import annotations

from app.matching.contact_match import (
    ContactMatchDenial,
    ContactMatchOutcome,
    ContactMatchResult,
    ContactMatchSignal,
    ContactMatchSubject,
    ContactSignalKind,
    build_contact_subject,
    match_contacts,
    subject_from_person_observation,
)
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
    "ContactMatchDenial",
    "ContactMatchOutcome",
    "ContactMatchResult",
    "ContactMatchSignal",
    "ContactMatchSubject",
    "ContactSignalKind",
    "MatchOutcome",
    "MatchSignal",
    "OrgMatchDenial",
    "OrgMatchResult",
    "OrgMatchSubject",
    "SignalKind",
    "SignalRelation",
    "SignalStrength",
    "build_contact_subject",
    "build_subject",
    "match_contacts",
    "match_organizations",
    "subject_from_observation",
    "subject_from_person_observation",
]
