"""DCA-038 — deterministic recipient-jurisdiction resolution and the India-only targeting gate.

Pins the acceptance criteria:

- Unknown / weak-only / foreign recipients are UNRESOLVED or FOREIGN → **blocked**.
- ``+91`` or ``.in`` alone never proves the recipient's jurisdiction (the headline denial).
- A genuinely evidenced Indian recipient (PIN / state-UT / GST / declared IN / Indian address) → IN.
- Legal address and operational area are kept SEPARATE: an Indian business context never flips a
  foreign/unresolved recipient to allowed.
- Pure/deterministic: resolving changes nothing and is reproducible.
"""

from __future__ import annotations

from app.geography import india
from app.geography.evidence import (
    EvidenceKind,
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
    JurisdictionStatus,
    resolve_jurisdiction,
    validate_targeting,
)


def ev(kind: EvidenceKind, value: str = "", country: str = "") -> LocationEvidence:
    return LocationEvidence(kind=kind, value=value, country=country)


# -- reference data / structured-field validation ---------------------------------------------------


def test_states_and_uts_catalog_is_complete():
    # 28 states + 8 union territories = 36 recognized subdivisions.
    assert len(india.STATES_AND_UTS) == 36
    assert india.normalize_state_ut("mh") == "MH"
    assert india.normalize_state_ut("Maharashtra") == "MH"
    assert india.normalize_state_ut("Atlantis") is None


def test_pin_validation_rejects_malformed_and_zero_leading():
    assert india.is_valid_pin("400001")
    assert not india.is_valid_pin("012345")  # leading zero is not a valid postal region
    assert not india.is_valid_pin("40001")  # five digits
    assert not india.is_valid_pin("4000011")  # seven digits
    assert not india.is_valid_pin("4000A1")  # non-numeric


def test_gst_and_country_normalization():
    assert india.is_valid_gst_state_code("27")  # Maharashtra
    assert not india.is_valid_gst_state_code("00")
    assert not india.is_valid_gst_state_code("40")
    assert india.normalize_country("India") == "IN"
    assert india.normalize_country("ind") == "IN"
    assert india.normalize_country("us") == "US"
    assert india.normalize_country("") == ""


# -- single-context resolution ---------------------------------------------------------------------


def test_no_evidence_is_unresolved():
    res = resolve_jurisdiction([])
    assert res.status is JurisdictionStatus.UNRESOLVED
    assert res.reason_code == REASON_NO_EVIDENCE


def test_phone_plus91_alone_does_not_prove_jurisdiction():
    res = resolve_jurisdiction([ev(EvidenceKind.PHONE_COUNTRY_CODE, "+91")])
    assert res.status is JurisdictionStatus.UNRESOLVED
    assert res.reason_code == REASON_WEAK_SIGNAL_ONLY
    assert res.had_weak_signal is True
    assert res.country == ""


def test_dot_in_tld_alone_does_not_prove_jurisdiction():
    res = resolve_jurisdiction([ev(EvidenceKind.DOMAIN_TLD, ".in")])
    assert res.status is JurisdictionStatus.UNRESOLVED
    assert res.reason_code == REASON_WEAK_SIGNAL_ONLY


def test_all_weak_signals_together_still_unresolved():
    res = resolve_jurisdiction(
        [
            ev(EvidenceKind.PHONE_COUNTRY_CODE, "+91"),
            ev(EvidenceKind.DOMAIN_TLD, ".in"),
            ev(EvidenceKind.LANGUAGE, "hi"),
            ev(EvidenceKind.IP_GEO, "IN"),
            ev(EvidenceKind.EMPLOYER_HQ, "IN"),
        ]
    )
    assert res.status is JurisdictionStatus.UNRESOLVED
    assert res.reason_code == REASON_WEAK_SIGNAL_ONLY


def test_valid_indian_pin_resolves_in():
    res = resolve_jurisdiction([ev(EvidenceKind.PIN_CODE, "560001")])
    assert res.status is JurisdictionStatus.IN
    assert res.reason_code == REASON_RESOLVED_IN
    assert res.country == india.INDIA_COUNTRY


def test_malformed_pin_is_not_a_proof():
    res = resolve_jurisdiction([ev(EvidenceKind.PIN_CODE, "0000")])
    assert res.status is JurisdictionStatus.UNRESOLVED
    assert (
        res.reason_code == REASON_NO_EVIDENCE
    )  # the bad PIN proved nothing; no weak signal either


def test_recognized_state_ut_resolves_in():
    assert resolve_jurisdiction([ev(EvidenceKind.STATE_UT, "GJ")]).status is JurisdictionStatus.IN
    assert (
        resolve_jurisdiction([ev(EvidenceKind.STATE_UT, "Tamil Nadu")]).status
        is JurisdictionStatus.IN
    )


def test_gst_state_code_resolves_in():
    assert resolve_jurisdiction([ev(EvidenceKind.GST_STATE_CODE, "27")]).status is (
        JurisdictionStatus.IN
    )


def test_declared_foreign_country_resolves_foreign():
    res = resolve_jurisdiction([ev(EvidenceKind.DECLARED_COUNTRY, "US")])
    assert res.status is JurisdictionStatus.FOREIGN
    assert res.reason_code == REASON_RESOLVED_FOREIGN
    assert res.country == "US"


def test_foreign_postal_address_resolves_foreign():
    res = resolve_jurisdiction([ev(EvidenceKind.POSTAL_ADDRESS, "221B Baker Street", country="GB")])
    assert res.status is JurisdictionStatus.FOREIGN
    assert res.country == "GB"


def test_postal_address_without_country_proves_nothing():
    res = resolve_jurisdiction([ev(EvidenceKind.POSTAL_ADDRESS, "somewhere")])
    assert res.status is JurisdictionStatus.UNRESOLVED
    assert res.reason_code == REASON_NO_EVIDENCE


def test_conflicting_strong_proofs_are_unresolved():
    res = resolve_jurisdiction(
        [ev(EvidenceKind.PIN_CODE, "560001"), ev(EvidenceKind.DECLARED_COUNTRY, "US")]
    )
    assert res.status is JurisdictionStatus.UNRESOLVED
    assert res.reason_code == REASON_CONFLICTING_EVIDENCE
    assert res.proven_countries == ("IN", "US")


def test_plus91_with_a_valid_pin_resolves_in():
    # A weak +91 does not block a genuinely evidenced Indian location.
    res = resolve_jurisdiction(
        [ev(EvidenceKind.PHONE_COUNTRY_CODE, "+91"), ev(EvidenceKind.PIN_CODE, "110001")]
    )
    assert res.status is JurisdictionStatus.IN
    assert res.had_weak_signal is True


# -- the India-only targeting gate -----------------------------------------------------------------


def test_gate_allows_only_an_indian_recipient():
    geo = TargetingGeography(recipient_location=[ev(EvidenceKind.PIN_CODE, "400001")])
    decision = validate_targeting(geo)
    assert decision.allowed is True
    assert decision.reason_code == REASON_TARGETING_ALLOWED


def test_gate_blocks_weak_only_recipient():
    geo = TargetingGeography(recipient_location=[ev(EvidenceKind.PHONE_COUNTRY_CODE, "+91")])
    decision = validate_targeting(geo)
    assert decision.allowed is False
    assert decision.reason_code == REASON_BLOCKED_UNRESOLVED
    assert decision.recipient.reason_code == REASON_WEAK_SIGNAL_ONLY


def test_gate_blocks_foreign_recipient():
    geo = TargetingGeography(recipient_location=[ev(EvidenceKind.DECLARED_COUNTRY, "US")])
    decision = validate_targeting(geo)
    assert decision.allowed is False
    assert decision.reason_code == REASON_BLOCKED_FOREIGN


def test_indian_legal_address_does_not_rescue_a_weak_recipient():
    # The separation guarantee: an Indian legal address + operational area, but the recipient is only
    # evidenced by +91 — still blocked, because an employer's/business's location is not the recipient's.
    geo = TargetingGeography(
        recipient_location=[ev(EvidenceKind.PHONE_COUNTRY_CODE, "+91")],
        legal_address=[ev(EvidenceKind.PIN_CODE, "560001")],
        operational_area=[ev(EvidenceKind.STATE_UT, "KA")],
    )
    decision = validate_targeting(geo)
    assert decision.allowed is False
    assert decision.reason_code == REASON_BLOCKED_UNRESOLVED
    assert decision.legal_address.status is JurisdictionStatus.IN
    assert decision.operational_area.status is JurisdictionStatus.IN
    assert decision.notes  # explicitly notes the separation


def test_contexts_are_resolved_independently():
    geo = TargetingGeography(
        recipient_location=[ev(EvidenceKind.PIN_CODE, "110001")],
        legal_address=[ev(EvidenceKind.DECLARED_COUNTRY, "US")],
        operational_area=[],
    )
    decision = validate_targeting(geo)
    assert decision.allowed is True  # the recipient is Indian
    assert decision.legal_address.status is JurisdictionStatus.FOREIGN
    assert decision.operational_area.status is JurisdictionStatus.UNRESOLVED


# -- structured-geography decomposition ------------------------------------------------------------


def test_structured_location_decomposes_to_evidence():
    loc = GeoLocation(
        country="IN", state_ut="MH", city="Mumbai", district="Mumbai", pin_code="400001"
    )
    kinds = {e.kind for e in loc.iter_evidence()}
    assert kinds == {
        EvidenceKind.DECLARED_COUNTRY,
        EvidenceKind.STATE_UT,
        EvidenceKind.PIN_CODE,
    }  # city/district are descriptive, never proof evidence


def test_structured_city_only_is_unresolved():
    # A bare city/district with no country/state/PIN is ambiguous → unresolved.
    geo = TargetingGeography.from_locations(
        recipient_location=GeoLocation(city="Hyderabad", district="Hyderabad")
    )
    decision = validate_targeting(geo)
    assert decision.allowed is False
    assert decision.recipient.reason_code == REASON_NO_EVIDENCE


def test_evidence_from_dicts_ignores_unknown_kinds():
    items = [{"kind": "pin_code", "value": "560001"}, {"kind": "totally_made_up", "value": "x"}]
    built = evidence_from_dicts(items)
    assert [e.kind for e in built] == [EvidenceKind.PIN_CODE]


def test_resolution_is_pure_and_repeatable():
    evidence = [ev(EvidenceKind.PIN_CODE, "560001")]
    first = resolve_jurisdiction(evidence)
    second = resolve_jurisdiction(evidence)
    assert first == second  # deterministic, no state mutated
