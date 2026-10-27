"""India geography reference data + structured-field validation (deterministic, never AI).

This is the controlled vocabulary behind DCA-038's structured geography: the recognized Indian
states/UTs (ISO 3166-2:IN), PIN-code format, and GSTIN state codes. It is deliberately pure,
network-free, deterministic Python — targeting-jurisdiction decisions must be reproducible and
auditable, exactly like the acquisition rulepacks (``app/compliance/rulepacks.py``).

Nothing here *guesses* India. A phone country code (``+91``), a ``.in`` domain, a display language or
an employer's headquarters are NOT represented as Indian-geography facts — they are weak signals the
resolver (``app/geography/resolver.py``) treats as insufficient on their own. Only an explicitly
evidenced Indian place (a valid PIN, a recognized state/UT, a GST state code, a declared ``IN``
country) is a positive India proof.
"""

from __future__ import annotations

import re

INDIA_COUNTRY = "IN"

# ISO 3166-2:IN — the 28 states and 8 union territories. Code -> canonical name. This is a *controlled
# list*: a state/UT value that is not one of these is not a recognized Indian subdivision (so it can
# never be silently accepted as proof of an Indian location).
STATES_AND_UTS: dict[str, str] = {
    # States
    "AP": "Andhra Pradesh",
    "AR": "Arunachal Pradesh",
    "AS": "Assam",
    "BR": "Bihar",
    "CT": "Chhattisgarh",
    "GA": "Goa",
    "GJ": "Gujarat",
    "HR": "Haryana",
    "HP": "Himachal Pradesh",
    "JH": "Jharkhand",
    "KA": "Karnataka",
    "KL": "Kerala",
    "MP": "Madhya Pradesh",
    "MH": "Maharashtra",
    "MN": "Manipur",
    "ML": "Meghalaya",
    "MZ": "Mizoram",
    "NL": "Nagaland",
    "OR": "Odisha",
    "PB": "Punjab",
    "RJ": "Rajasthan",
    "SK": "Sikkim",
    "TN": "Tamil Nadu",
    "TG": "Telangana",
    "TR": "Tripura",
    "UP": "Uttar Pradesh",
    "UT": "Uttarakhand",
    "WB": "West Bengal",
    # Union territories
    "AN": "Andaman and Nicobar Islands",
    "CH": "Chandigarh",
    "DH": "Dadra and Nagar Haveli and Daman and Diu",
    "DL": "Delhi",
    "JK": "Jammu and Kashmir",
    "LA": "Ladakh",
    "LD": "Lakshadweep",
    "PY": "Puducherry",
}

# Reverse lookup (upper-cased canonical name -> code), so a value supplied as a full name resolves too.
_NAME_TO_CODE: dict[str, str] = {name.upper(): code for code, name in STATES_AND_UTS.items()}

# Common country tokens that denote India when declared explicitly. A *declared* country is not one of
# the forbidden weak signals; only derived guesses (phone/TLD/language/HQ) are.
_INDIA_COUNTRY_TOKENS = frozenset({"IN", "IND", "INDIA"})

# A valid Indian PIN code is six digits whose first digit (the postal region, 1-9) is never 0.
_PIN_RE = re.compile(r"^[1-9]\d{5}$")

# Valid GSTIN state codes are the two-digit prefixes 01-37 plus 97 (Other Territory) and 99 (Centre
# Jurisdiction). Anything else is not an Indian GST state code.
_GST_STATE_CODES = frozenset({f"{n:02d}" for n in range(1, 38)} | {"97", "99"})


def normalize_country(value: str) -> str:
    """Normalize a declared country token to an upper-case code (``IN`` for any India spelling).

    Returns ``""`` for an empty/blank value. A declared country is treated as given — this does NOT
    infer a country from a phone code, TLD, language or employer HQ (those never reach here).
    """
    token = (value or "").strip().upper()
    if not token:
        return ""
    if token in _INDIA_COUNTRY_TOKENS:
        return INDIA_COUNTRY
    return token


def is_india_country(value: str) -> bool:
    return normalize_country(value) == INDIA_COUNTRY


def normalize_state_ut(value: str) -> str | None:
    """Return the canonical state/UT *code* for a code or full-name value, or ``None`` if unrecognized.

    An unrecognized value yields ``None`` (never a guess): the caller then treats it as not a
    recognized Indian subdivision rather than silently accepting it.
    """
    token = (value or "").strip().upper()
    if not token:
        return None
    if token in STATES_AND_UTS:
        return token
    return _NAME_TO_CODE.get(token)


def state_ut_name(code: str) -> str | None:
    return STATES_AND_UTS.get((code or "").strip().upper())


def is_valid_pin(value: str) -> bool:
    """A well-formed Indian PIN: exactly six digits, first digit 1-9 (never 0)."""
    return bool(_PIN_RE.match((value or "").strip()))


def is_valid_gst_state_code(value: str) -> bool:
    return (value or "").strip() in _GST_STATE_CODES
