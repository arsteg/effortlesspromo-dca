"""DCA-038 — deterministic recipient-jurisdiction resolution and the India-only targeting gate.

Given a bag of :class:`~app.geography.evidence.LocationEvidence`, resolve the jurisdiction it
establishes — or refuse to. The rules are the acceptance criteria of this story:

1. **Weak signals never resolve a jurisdiction.** A ``+91`` phone code, a ``.in`` domain, a display
   language, an IP-geo guess or an employer's HQ — even all of them together — leave the result
   ``UNRESOLVED``. The spec is explicit: *do not guess India from +91, .in, language, or an
   employer's headquarters alone.*
2. **A single strong proof resolves its country.** A valid Indian PIN / state-UT / GST state code /
   declared ``IN`` country / Indian postal address → ``IN``. An explicitly foreign country → ``FOREIGN``.
3. **Conflicting strong proofs are UNRESOLVED.** India + a foreign country at once is ambiguous and is
   never silently decided in India's favour.
4. **No evidence is UNRESOLVED.** An unknown target is never assumed to be Indian.

The targeting gate then allows DCA personal-data acquisition **only** when the *recipient-location*
jurisdiction resolves to India. The legal-address and operational-area jurisdictions are resolved
separately and reported for context, but they can never flip a blocked recipient to allowed — that is
the whole point of keeping the three contexts separate.

This module is pure, deterministic Python (never AI): every decision is reproducible and auditable,
and evaluating it changes no state.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum

from app.geography.evidence import WEAK_KINDS, LocationEvidence, TargetingGeography
from app.geography.india import INDIA_COUNTRY


class JurisdictionStatus(str, Enum):
    IN = "in"  # resolved to India
    FOREIGN = "foreign"  # resolved to a non-Indian country
    UNRESOLVED = "unresolved"  # cannot be resolved (no evidence / weak-only / conflicting)


# Stable, test-asserted reason codes for a single context's resolution.
REASON_RESOLVED_IN = "resolved_in"
REASON_RESOLVED_FOREIGN = "resolved_foreign"
REASON_NO_EVIDENCE = "unresolved_no_evidence"
REASON_WEAK_SIGNAL_ONLY = "unresolved_weak_signal_only"
REASON_CONFLICTING_EVIDENCE = "unresolved_conflicting_evidence"

# Stable reason codes for the overall targeting-gate decision.
REASON_TARGETING_ALLOWED = "targeting_allowed_in"
REASON_BLOCKED_FOREIGN = "blocked_foreign_jurisdiction"
REASON_BLOCKED_UNRESOLVED = "blocked_unresolved_jurisdiction"


@dataclass(frozen=True)
class JurisdictionResolution:
    """The resolution of one context's evidence."""

    status: JurisdictionStatus
    reason_code: str
    country: str = ""  # the resolved country code, or "" when unresolved
    proven_countries: tuple[str, ...] = ()  # every country any strong proof established (sorted)
    had_weak_signal: bool = False
    detail: str | None = None

    @property
    def is_india(self) -> bool:
        return self.status is JurisdictionStatus.IN


def resolve_jurisdiction(evidence: Sequence[LocationEvidence]) -> JurisdictionResolution:
    """Resolve the jurisdiction a bag of evidence establishes (fail-closed to ``UNRESOLVED``)."""
    proven: set[str] = set()
    had_weak = False
    for item in evidence:
        country = item.proven_country()
        if country:
            proven.add(country)
        elif item.kind in WEAK_KINDS:
            had_weak = True

    proven_tuple = tuple(sorted(proven))

    if len(proven) == 1:
        country = proven_tuple[0]
        if country == INDIA_COUNTRY:
            return JurisdictionResolution(
                JurisdictionStatus.IN,
                REASON_RESOLVED_IN,
                country=country,
                proven_countries=proven_tuple,
                had_weak_signal=had_weak,
            )
        return JurisdictionResolution(
            JurisdictionStatus.FOREIGN,
            REASON_RESOLVED_FOREIGN,
            country=country,
            proven_countries=proven_tuple,
            had_weak_signal=had_weak,
            detail=f"resolved to non-Indian jurisdiction '{country}'",
        )

    if len(proven) > 1:
        return JurisdictionResolution(
            JurisdictionStatus.UNRESOLVED,
            REASON_CONFLICTING_EVIDENCE,
            proven_countries=proven_tuple,
            had_weak_signal=had_weak,
            detail="strong evidence points at more than one country",
        )

    # No strong proof at all.
    if had_weak:
        return JurisdictionResolution(
            JurisdictionStatus.UNRESOLVED,
            REASON_WEAK_SIGNAL_ONLY,
            had_weak_signal=True,
            detail="only weak signals (+91 / .in / language / IP / employer HQ) were supplied",
        )
    return JurisdictionResolution(
        JurisdictionStatus.UNRESOLVED,
        REASON_NO_EVIDENCE,
        detail="no location evidence was supplied",
    )


@dataclass(frozen=True)
class TargetingDecision:
    """The India-only targeting gate's decision, with each context's resolution kept separate."""

    allowed: bool
    reason_code: str
    recipient: JurisdictionResolution
    legal_address: JurisdictionResolution
    operational_area: JurisdictionResolution
    notes: tuple[str, ...] = field(default_factory=tuple)


def validate_targeting(geography: TargetingGeography) -> TargetingDecision:
    """Resolve each context separately and gate on the **recipient** jurisdiction only.

    Personal-data acquisition is allowed only when the recipient resolves to India; a foreign or
    unresolved recipient is blocked even if the business's legal address or operational area is Indian.
    """
    recipient = resolve_jurisdiction(geography.recipient_location)
    legal = resolve_jurisdiction(geography.legal_address)
    operational = resolve_jurisdiction(geography.operational_area)

    notes: list[str] = []
    # Make the separation explicit when an Indian business context did NOT carry the recipient.
    if not recipient.is_india and (legal.is_india or operational.is_india):
        notes.append(
            "an Indian legal address / operational area does not establish the recipient's "
            "jurisdiction; the recipient location is evaluated on its own evidence"
        )

    if recipient.status is JurisdictionStatus.IN:
        return TargetingDecision(
            allowed=True,
            reason_code=REASON_TARGETING_ALLOWED,
            recipient=recipient,
            legal_address=legal,
            operational_area=operational,
            notes=tuple(notes),
        )
    reason = (
        REASON_BLOCKED_FOREIGN
        if recipient.status is JurisdictionStatus.FOREIGN
        else REASON_BLOCKED_UNRESOLVED
    )
    return TargetingDecision(
        allowed=False,
        reason_code=reason,
        recipient=recipient,
        legal_address=legal,
        operational_area=operational,
        notes=tuple(notes),
    )
