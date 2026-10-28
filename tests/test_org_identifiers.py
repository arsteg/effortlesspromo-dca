"""DCA-065 — validate typed Indian organization identifiers (CIN / LLPIN / GSTIN).

Pure, deterministic, network-free behavior: a validated identifier keeps its declared type and its
original evidence; an establishment id (GSTIN) is never conflated with the legal-entity id (CIN/LLPIN);
the embedded PAN of a GSTIN is never extracted; and every denial (unknown scheme, blank, malformed,
failed check digit) is reproducible and changes no state (the module has none).

GSTIN fixtures are synthetic but checksum-valid. ``27AAPFU0939F1ZV`` and ``29AAGCB7383J1Z4`` are
well-known real-world valid GSTINs used to anchor the standard check-digit algorithm; the two
same-PAN establishments (``27AAPFU0939F1ZV`` / ``29AAPFU0939F1ZR``) are a synthetic legal-entity →
establishments fixture.
"""

from __future__ import annotations

import pytest

from app.normalization import (
    ENTITY_CLASS_ESTABLISHMENT,
    ENTITY_CLASS_LEGAL_ENTITY,
    REASON_EMPTY_IDENTIFIER,
    REASON_IDENTIFIER_CHECKSUM_FAILED,
    REASON_MALFORMED_IDENTIFIER,
    REASON_UNKNOWN_IDENTIFIER_SCHEME,
    NormalizationError,
    entity_class_for,
    validate_org_identifier,
)

# A legal-entity CIN (U = unlisted, MH = Maharashtra ROC, PTC = private company).
CIN = "U72200MH2010PTC123456"
# An LLP identifier, commonly written with a hyphen.
LLPIN = "AAB-1234"
# Two checksum-valid GSTINs of the SAME legal entity (PAN AAPFU0939F) in two different states.
GSTIN_MH = "27AAPFU0939F1ZV"  # Maharashtra (also a real-world valid GSTIN)
GSTIN_KA = "29AAPFU0939F1ZR"  # Karnataka — same PAN, different establishment


# --------------------------------------------------------------------------------------------------
# Type is retained; legal entity vs establishment is never conflated
# --------------------------------------------------------------------------------------------------


def test_cin_is_a_legal_entity_identifier() -> None:
    result = validate_org_identifier("cin", CIN)
    assert result.scheme == "cin"
    assert result.entity_class == ENTITY_CLASS_LEGAL_ENTITY
    assert result.original == CIN
    assert result.normalized == CIN
    assert result.state_code == "MH"  # ROC state, a non-personal geography fact
    assert result.validated is True


def test_llpin_is_a_legal_entity_identifier_and_canonicalizes_hyphen() -> None:
    result = validate_org_identifier("llpin", LLPIN)
    assert result.scheme == "llpin"
    assert result.entity_class == ENTITY_CLASS_LEGAL_ENTITY
    assert result.original == LLPIN  # original evidence kept verbatim
    assert result.normalized == "AAB1234"  # hyphen removed in the canonical form
    assert result.state_code == ""  # no jurisdiction embedded in an LLPIN


def test_gstin_is_an_establishment_identifier_not_the_legal_entity() -> None:
    result = validate_org_identifier("gstin", GSTIN_MH)
    assert result.scheme == "gstin"
    # The load-bearing distinction: a GSTIN is an ESTABLISHMENT reference, never the legal entity.
    assert result.entity_class == ENTITY_CLASS_ESTABLISHMENT
    assert result.entity_class != ENTITY_CLASS_LEGAL_ENTITY
    assert result.original == GSTIN_MH
    assert result.state_code == "27"  # GST state code, non-personal


def test_entity_class_is_derived_from_scheme() -> None:
    assert entity_class_for("cin") == ENTITY_CLASS_LEGAL_ENTITY
    assert entity_class_for("llpin") == ENTITY_CLASS_LEGAL_ENTITY
    assert entity_class_for("gstin") == ENTITY_CLASS_ESTABLISHMENT


def test_two_establishments_of_one_legal_entity_stay_distinct() -> None:
    # Two GSTINs of the SAME legal entity (same PAN) in two states: both are establishments, each
    # keeps its own evidence/state, and neither is treated as the legal entity.
    mh = validate_org_identifier("gstin", GSTIN_MH)
    ka = validate_org_identifier("gstin", GSTIN_KA)
    assert mh.entity_class == ka.entity_class == ENTITY_CLASS_ESTABLISHMENT
    assert mh.normalized != ka.normalized  # distinct establishments, never merged
    assert mh.state_code == "27" and ka.state_code == "29"
    # A legal-entity id (CIN) of the same company is a different TYPE — never equated to its GSTINs.
    legal = validate_org_identifier("cin", CIN)
    assert legal.entity_class == ENTITY_CLASS_LEGAL_ENTITY
    assert legal.normalized not in {mh.normalized, ka.normalized}


def test_gstin_value_declared_as_cin_is_refused_not_coerced() -> None:
    # Supplying an establishment id under the legal-entity type must be refused, never silently
    # accepted as the legal entity.
    with pytest.raises(NormalizationError) as exc:
        validate_org_identifier("cin", GSTIN_MH)
    assert exc.value.code == REASON_MALFORMED_IDENTIFIER


# --------------------------------------------------------------------------------------------------
# No embedded personal identifiers are extracted
# --------------------------------------------------------------------------------------------------


def test_embedded_pan_is_never_extracted_from_gstin() -> None:
    result = validate_org_identifier("gstin", GSTIN_MH)
    # The GSTIN embeds the PAN "AAPFU0939F"; the validator must not surface it as a field/value.
    assert not hasattr(result, "pan")
    field_values = [v for v in vars(result).values() if isinstance(v, str)]
    assert all("AAPFU0939F" not in v or v in (GSTIN_MH, result.normalized) for v in field_values)
    # Only the whole-GSTIN forms (original/normalized) and the non-personal state code are exposed.
    assert set(vars(result)) == {
        "scheme",
        "entity_class",
        "original",
        "normalized",
        "state_code",
        "validated",
    }


# --------------------------------------------------------------------------------------------------
# Normalization is repeatable and original evidence is preserved
# --------------------------------------------------------------------------------------------------


def test_validation_is_repeatable() -> None:
    first = validate_org_identifier("gstin", GSTIN_MH)
    second = validate_org_identifier("gstin", GSTIN_MH)
    assert first == second  # frozen dataclass — same input, same result


def test_canonicalizes_case_and_separators_but_keeps_original() -> None:
    raw = "  u72200 mh 2010 ptc 123456 "
    result = validate_org_identifier("cin", raw)
    assert result.original == raw  # evidence preserved exactly
    assert result.normalized == CIN  # upper-cased, whitespace/separators removed


@pytest.mark.parametrize("gstin", [GSTIN_MH, "29AAGCB7383J1Z4"])
def test_real_world_valid_gstins_pass_the_checksum(gstin: str) -> None:
    # Anchors the standard GST check-digit algorithm against known-valid real GSTINs.
    assert validate_org_identifier("gstin", gstin).validated is True


# --------------------------------------------------------------------------------------------------
# Denials — reproducible, with no state change
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("scheme", ["", "pan", "duns", "unknown"])
def test_unknown_scheme_is_refused(scheme: str) -> None:
    with pytest.raises(NormalizationError) as exc:
        validate_org_identifier(scheme, CIN)
    assert exc.value.code == REASON_UNKNOWN_IDENTIFIER_SCHEME


@pytest.mark.parametrize("scheme", ["cin", "llpin", "gstin"])
@pytest.mark.parametrize("raw", ["", "   ", "\t\n", "---"])
def test_empty_identifier_is_refused(scheme: str, raw: str) -> None:
    with pytest.raises(NormalizationError) as exc:
        validate_org_identifier(scheme, raw)
    assert exc.value.code == REASON_EMPTY_IDENTIFIER


@pytest.mark.parametrize(
    "scheme,raw",
    [
        ("cin", "U72200MH2010PTC12345"),  # 20 chars — too short
        ("cin", "X72200MH2010PTC123456"),  # bad listing status
        ("cin", "U72200ZZ2010PTC123456"),  # ZZ is not a recognized state
        ("llpin", "AB-1234"),  # only 2 letters
        ("llpin", "AAB-12345"),  # 5 digits
        ("gstin", "27AAPFU0939F1ZVX"),  # 16 chars — too long
        ("gstin", "ABAAPFU0939F1ZV"),  # non-numeric state prefix
    ],
)
def test_malformed_identifier_is_refused(scheme: str, raw: str) -> None:
    with pytest.raises(NormalizationError) as exc:
        validate_org_identifier(scheme, raw)
    assert exc.value.code == REASON_MALFORMED_IDENTIFIER


def test_gstin_with_unrecognized_state_code_is_malformed() -> None:
    # "00" is not a recognized GST state code (valid range is 01-37, 97, 99).
    with pytest.raises(NormalizationError) as exc:
        validate_org_identifier("gstin", "00AAPFU0939F1ZV")
    assert exc.value.code == REASON_MALFORMED_IDENTIFIER


def test_gstin_with_bad_check_digit_is_refused() -> None:
    # Well-shaped, recognized state, but the final check digit is wrong (V -> A).
    bad = GSTIN_MH[:-1] + "A"
    assert bad != GSTIN_MH
    with pytest.raises(NormalizationError) as exc:
        validate_org_identifier("gstin", bad)
    assert exc.value.code == REASON_IDENTIFIER_CHECKSUM_FAILED


def test_denial_is_reproducible_and_stateless() -> None:
    bad = GSTIN_MH[:-1] + "A"
    codes = []
    for _ in range(2):
        try:
            validate_org_identifier("gstin", bad)
            codes.append(None)
        except NormalizationError as exc:
            codes.append(exc.code)
    assert codes == [REASON_IDENTIFIER_CHECKSUM_FAILED, REASON_IDENTIFIER_CHECKSUM_FAILED]
