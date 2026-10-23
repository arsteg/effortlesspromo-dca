"""Acquisition rulepacks — compliance as data, strictest-rules fallback (platform ADR-0008 pattern).

A rulepack says what may be ACQUIRED and kept, per jurisdiction. This is deliberately deterministic
Python (never AI): screening decisions must be reproducible and auditable. The India pack is
conservative pending DPDP clarity: official-registry tier only, organisation identifier mandatory,
role-address contacts only (no named-person emails/phones). The DEFAULT pack allows nothing, so an
unknown country screens everything out rather than guessing.

Screening does NOT grant sending rights: acceptance of a candidate is not recipient consent, and
all channel consent/suppression stays with the platform's send pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AcquisitionRulepack:
    jurisdiction: str
    allowed_trust_tiers: frozenset[str] = frozenset()
    allowed_contact_kinds: frozenset[str] = frozenset()
    require_org_identifier: bool = True
    retention_days: int = 30


_PACKS: dict[str, AcquisitionRulepack] = {
    "IN": AcquisitionRulepack(
        jurisdiction="IN",
        allowed_trust_tiers=frozenset({"official_registry"}),
        allowed_contact_kinds=frozenset({"role_email", "company_phone", "none"}),
        require_org_identifier=True,
        retention_days=30,
    ),
    # Strictest fallback: nothing is acquirable in an unmapped jurisdiction.
    "DEFAULT": AcquisitionRulepack(jurisdiction="DEFAULT"),
}


def rulepack_for(country: str) -> AcquisitionRulepack:
    return _PACKS.get(country, _PACKS["DEFAULT"])


@dataclass(frozen=True)
class ScreenResult:
    allowed: bool
    reason_code: str
    detail: str | None = None


# Reason codes are stable identifiers (auditable, test-asserted) — change them deliberately.
REASON_OK = "ok"
REASON_TIER = "source_tier_disallowed"
REASON_ORG_ID = "missing_org_identifier"
REASON_CONTACT = "contact_kind_disallowed"
REASON_DUPLICATE = "duplicate"


@dataclass(frozen=True)
class RawCandidate:
    """What a source adapter emits, before screening. ``contact.kind`` is adapter-declared."""

    org_name: str
    org_identifier: str
    source_key: str
    source_ref: str
    trust_tier: str
    contact: dict = field(default_factory=dict)


def screen(candidate: RawCandidate, pack: AcquisitionRulepack) -> ScreenResult:
    if candidate.trust_tier not in pack.allowed_trust_tiers:
        return ScreenResult(False, REASON_TIER, f"tier '{candidate.trust_tier}' not allowed")
    if pack.require_org_identifier and not candidate.org_identifier:
        return ScreenResult(False, REASON_ORG_ID)
    kind = str(candidate.contact.get("kind") or "none")
    if kind not in pack.allowed_contact_kinds:
        return ScreenResult(False, REASON_CONTACT, f"contact kind '{kind}' not allowed")
    return ScreenResult(True, REASON_OK)
