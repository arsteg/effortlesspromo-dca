"""DCA-066 — normalize contact phone observations to E.164 (bounded, India-only supported plan).

Pure, deterministic, network-free behavior:

- E.164 is produced ONLY when supported country evidence is available (an explicit ``+91`` / ``0091``
  prefix, or a declared ``IN`` country); the original value is always preserved.
- An ambiguous number stays UNRESOLVED (national format with no country, or an international number
  for an unsupported country calling code) — the raw is preserved and the uncertainty named; it is
  NOT a denial.
- A ``+91`` E.164 is a FORMATTING fact, never a jurisdiction or consent fact
  (``jurisdiction_proof`` is always ``False``).
- Blank / no-digit / malformed-for-the-supported-plan values are refused with stable codes, and the
  refusal is reproducible and changes no state (the module has none).
"""

from __future__ import annotations

import pytest

from app.normalization import (
    REASON_EMPTY_PHONE,
    REASON_INVALID_PHONE,
    REASON_UNPARSEABLE_PHONE,
    STATUS_NORMALIZED,
    STATUS_UNRESOLVED,
    UNCERTAINTY_NO_COUNTRY,
    UNCERTAINTY_UNSUPPORTED_COUNTRY,
    NormalizationError,
    normalize_phone,
    share_e164,
)

# A valid Indian mobile, written in several human forms that all reduce to the same NSN.
IN_MOBILE_NSN = "9876543210"
IN_MOBILE_E164 = "+919876543210"
# A valid Indian landline (Delhi area code 11 -> NSN leading digit 1).
IN_LANDLINE_NSN = "1123456789"


# --------------------------------------------------------------------------------------------------
# E.164 only when supported country evidence is available
# --------------------------------------------------------------------------------------------------


def test_explicit_plus91_normalizes_to_e164() -> None:
    result = normalize_phone("+91 98765 43210")
    assert result.status == STATUS_NORMALIZED
    assert result.e164 == IN_MOBILE_E164
    assert result.country == "IN"
    assert result.country_calling_code == "+91"
    assert result.national_number == IN_MOBILE_NSN
    assert result.uncertainty == ""


def test_idd_00_91_prefix_normalizes_to_e164() -> None:
    # The "00" international direct-dial prefix is an explicit country declaration too.
    result = normalize_phone("0091-9876543210")
    assert result.status == STATUS_NORMALIZED
    assert result.e164 == IN_MOBILE_E164


def test_declared_country_in_normalizes_national_number() -> None:
    result = normalize_phone("98765 43210", country="IN")
    assert result.status == STATUS_NORMALIZED
    assert result.e164 == IN_MOBILE_E164


@pytest.mark.parametrize(
    "raw",
    [
        "09876543210",  # leading trunk 0
        "919876543210",  # un-prefixed country code
        "091-98765-43210",  # 0 + country code
        "(98765) 43210",  # punctuation noise
        "9876543210",  # bare 10-digit NSN
    ],
)
def test_declared_in_accepts_common_national_forms(raw: str) -> None:
    result = normalize_phone(raw, country="IN")
    assert result.status == STATUS_NORMALIZED
    assert result.e164 == IN_MOBILE_E164


def test_indian_landline_leading_digit_one_is_valid() -> None:
    # Delhi landline: trunk 011 -> NSN "1123456789" (leading 1 is a valid area-code digit).
    result = normalize_phone("011-2345-6789", country="IN")
    assert result.status == STATUS_NORMALIZED
    assert result.national_number == IN_LANDLINE_NSN
    assert result.e164 == "+91" + IN_LANDLINE_NSN


def test_india_variant_india_token_resolves_country() -> None:
    result = normalize_phone("9876543210", country="India")
    assert result.status == STATUS_NORMALIZED
    assert result.country == "IN"


# --------------------------------------------------------------------------------------------------
# +91 is a formatting fact, NOT a jurisdiction or consent fact
# --------------------------------------------------------------------------------------------------


def test_plus91_does_not_prove_jurisdiction_or_consent() -> None:
    # The acceptance criterion: +91 alone does not establish recipient jurisdiction or consent.
    result = normalize_phone("+91 98765 43210")
    assert result.status == STATUS_NORMALIZED  # it IS formatted to E.164...
    assert result.jurisdiction_proof is False  # ...but that proves no jurisdiction/consent.


def test_declared_in_normalized_number_still_proves_no_jurisdiction() -> None:
    result = normalize_phone("9876543210", country="IN")
    assert result.jurisdiction_proof is False


# --------------------------------------------------------------------------------------------------
# An ambiguous number remains UNRESOLVED (preserved + uncertainty named, NOT a denial)
# --------------------------------------------------------------------------------------------------


def test_missing_country_national_number_is_unresolved() -> None:
    raw = "98765 43210"
    result = normalize_phone(raw)  # no country evidence at all
    assert result.status == STATUS_UNRESOLVED
    assert result.e164 is None
    assert result.country == ""
    assert result.country_calling_code == ""
    assert result.national_number == ""
    assert result.uncertainty == UNCERTAINTY_NO_COUNTRY
    assert result.original == raw  # raw preserved verbatim


def test_international_unsupported_country_is_unresolved_not_denied() -> None:
    # +1 (US/Canada) is a well-formed country code this India-only service carries no plan for.
    result = normalize_phone("+1 415 555 2671")
    assert result.status == STATUS_UNRESOLVED
    assert result.e164 is None
    assert result.uncertainty == UNCERTAINTY_UNSUPPORTED_COUNTRY
    assert result.original == "+1 415 555 2671"


def test_declared_foreign_country_national_number_is_unresolved() -> None:
    # A declared non-India country is outside the supported plan — unresolved, not guessed.
    result = normalize_phone("415 555 2671", country="US")
    assert result.status == STATUS_UNRESOLVED
    assert result.uncertainty == UNCERTAINTY_UNSUPPORTED_COUNTRY


def test_embedded_plus91_overrides_a_conflicting_declared_country() -> None:
    # The number explicitly declares +91; an embedded international prefix wins over the argument.
    result = normalize_phone("+91 98765 43210", country="US")
    assert result.status == STATUS_NORMALIZED
    assert result.country == "IN"


# --------------------------------------------------------------------------------------------------
# Denials — reproducible, no state change (the module is pure)
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["", "   ", "\t\n"])
def test_blank_phone_is_refused(raw: str) -> None:
    with pytest.raises(NormalizationError) as exc:
        normalize_phone(raw)
    assert exc.value.code == REASON_EMPTY_PHONE


@pytest.mark.parametrize("raw", ["call me", "n/a", "+()- .", "phone"])
def test_value_with_no_dialable_digit_is_refused(raw: str) -> None:
    with pytest.raises(NormalizationError) as exc:
        normalize_phone(raw)
    assert exc.value.code == REASON_UNPARSEABLE_PHONE


@pytest.mark.parametrize(
    "raw",
    [
        "+91 12345",  # too short for the Indian plan
        "+91 098765 4321",  # NSN leading 0 is invalid
        "+919876543210123",  # too long
    ],
)
def test_malformed_plus91_number_is_refused(raw: str) -> None:
    # Explicit +91 country evidence is present, so a bad national number is a DENIAL, not unresolved.
    with pytest.raises(NormalizationError) as exc:
        normalize_phone(raw)
    assert exc.value.code == REASON_INVALID_PHONE


@pytest.mark.parametrize("raw", ["12345", "98765 4321", "0987654321"])
def test_malformed_declared_in_number_is_refused(raw: str) -> None:
    # Declared IN gives supported country evidence -> we validate the plan and deny a bad number.
    with pytest.raises(NormalizationError) as exc:
        normalize_phone(raw, country="IN")
    assert exc.value.code == REASON_INVALID_PHONE


def test_denial_is_reproducible_and_stateless() -> None:
    first: str | None = None
    second: str | None = None
    try:
        normalize_phone("+91 12345")
    except NormalizationError as exc:
        first = exc.code
    try:
        normalize_phone("+91 12345")
    except NormalizationError as exc:
        second = exc.code
    assert first == second == REASON_INVALID_PHONE


# --------------------------------------------------------------------------------------------------
# Repeatable; original always preserved; share_e164 is a hint only
# --------------------------------------------------------------------------------------------------


def test_normalization_is_repeatable() -> None:
    a = normalize_phone("+91 98765 43210")
    b = normalize_phone("+91 98765 43210")
    assert a == b


def test_original_is_preserved_verbatim_on_normalize_and_unresolve() -> None:
    noisy = "  +91 (98765)-43210  "
    assert normalize_phone(noisy).original == noisy
    ambiguous = "  98765 43210  "
    assert normalize_phone(ambiguous).original == ambiguous


def test_share_e164_is_a_hint_not_consent_or_identity() -> None:
    plus = normalize_phone("+91 98765 43210")
    national = normalize_phone("09876543210", country="IN")
    other = normalize_phone("+91 91234 56780")
    # Two spellings of the same reachable number share the E.164 hint...
    assert share_e164(plus, national) is True
    # ...different numbers do not...
    assert share_e164(plus, other) is False
    # ...and two UNRESOLVED numbers never share a key (both e164 None).
    unresolved_a = normalize_phone("98765 43210")
    unresolved_b = normalize_phone("98765 43210")
    assert share_e164(unresolved_a, unresolved_b) is False
    # The hint asserts nothing about consent/jurisdiction.
    assert plus.jurisdiction_proof is False
