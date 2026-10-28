"""DCA-066 — contact phone-number normalization to E.164 (bounded, India-only supported plan).

:func:`normalize_phone` turns a raw, human-entered phone observation into a canonical **E.164** form —
but ONLY when **supported country evidence** is available, and WITHOUT ever discarding the original.
It is deliberately pure, network-free, deterministic Python (like :mod:`app.normalization.names` and
:mod:`app.geography.india`): identity-resolution signals must be reproducible and auditable, and a
model is never consulted.

"Supported country evidence" is bounded to this India-only service. It is one of:

- an explicit international prefix embedded in the value (``+91 …`` or ``0091 …``), i.e. the number
  declares its own country calling code; or
- an explicitly declared ``country`` argument that resolves to India (``IN`` / ``IND`` / ``INDIA``).

Only the Indian numbering plan (country calling code ``91``, a 10-digit national significant number
whose leading digit is 1–9) is supported. Any other country calling code, or a national-format value
with no declared country, is **ambiguous** and is returned :data:`STATUS_UNRESOLVED` with the raw
value preserved and the uncertainty named — never guessed, never coerced.

The load-bearing guarantees (the DCA-066 acceptance criteria):

- **E.164 only with supported country evidence.** A ``+91`` / declared-``IN`` value with a valid NSN
  yields a canonical ``+91XXXXXXXXXX`` E.164 string.
- **An ambiguous number remains unresolved.** A bare national number with no country, or an
  international number for a country this service does not carry a plan for, is NOT normalized to
  E.164 — it is returned :data:`STATUS_UNRESOLVED` with the ``original`` preserved, ``e164`` ``None``,
  and ``uncertainty`` set (``no_country_evidence`` / ``unsupported_country``). It is NOT a denial.
- **``+91`` is a formatting fact, never a jurisdiction or consent fact.** Producing an E.164 string
  from a ``+91`` number says only *how the digits are written*. It does NOT establish the recipient's
  jurisdiction or any consent to be contacted — :attr:`NormalizedPhone.jurisdiction_proof` is ALWAYS
  ``False`` (jurisdiction resolution stays :mod:`app.geography.resolver`'s job, where ``+91`` is an
  explicitly insufficient weak signal). This module records nothing and contacts no one.
- **A denial changes nothing.** A blank value, a value with no dialable digit, or a value that DOES
  carry supported country evidence yet is malformed for that plan, is refused with a stable code; the
  function is pure, so the refusal is perfectly reproducible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.geography.india import INDIA_COUNTRY, normalize_country
from app.normalization.errors import (
    REASON_EMPTY_PHONE,
    REASON_INVALID_PHONE,
    REASON_UNPARSEABLE_PHONE,
    NormalizationError,
)

# Outcome of a normalization: either a canonical E.164 was derived, or the number stayed ambiguous.
STATUS_NORMALIZED = "normalized"
STATUS_UNRESOLVED = "unresolved"

# Why a number stayed UNRESOLVED (set on :attr:`NormalizedPhone.uncertainty`; "" when normalized).
# A national-format value with no declared country — we will not guess the country from digits alone.
UNCERTAINTY_NO_COUNTRY = "no_country_evidence"
# An international value whose country calling code is not one this India-only service carries a plan
# for (anything other than +91). We neither validate nor guess it — it is out of the supported scope.
UNCERTAINTY_UNSUPPORTED_COUNTRY = "unsupported_country"

# The single supported numbering plan: India, country calling code 91, NSN = 10 digits.
_INDIA_CALLING_CODE = "91"
INDIA_CALLING_CODE_DISPLAY = "+91"
_INDIA_NSN_LEN = 10
# A valid Indian national significant number: exactly 10 digits, leading digit 1–9 (never 0 — that is
# the stripped trunk prefix). This admits every mobile (6–9) and landline area code (e.g. Delhi "11").
_INDIA_NSN_RE = re.compile(r"^[1-9]\d{9}$")

# Characters allowed as human formatting noise around the digits of a phone value.
_PHONE_PUNCT_RE = re.compile(r"[\s()\-.‐-―/]")
_NON_DIGIT_RE = re.compile(r"\D")


@dataclass(frozen=True)
class NormalizedPhone:
    """The normalized form of ONE contact phone observation. ``original`` is always preserved verbatim.

    When ``status`` is :data:`STATUS_NORMALIZED`, ``e164`` / ``country`` / ``country_calling_code`` /
    ``national_number`` are populated and ``uncertainty`` is ``""``. When ``status`` is
    :data:`STATUS_UNRESOLVED`, ``e164`` is ``None``, the derived fields are empty, and ``uncertainty``
    names why (the raw value is still preserved in ``original``).

    ``jurisdiction_proof`` is ALWAYS ``False``: an E.164 form (even a ``+91`` one) is a formatting
    fact, never proof of the recipient's jurisdiction or of consent to contact them.
    """

    original: str
    status: str
    e164: str | None
    country: str
    country_calling_code: str
    national_number: str
    uncertainty: str
    jurisdiction_proof: bool = False


def _unresolved(original: str, uncertainty: str) -> NormalizedPhone:
    """Build an UNRESOLVED result that preserves the original and names the uncertainty."""
    return NormalizedPhone(
        original=original,
        status=STATUS_UNRESOLVED,
        e164=None,
        country="",
        country_calling_code="",
        national_number="",
        uncertainty=uncertainty,
    )


def _india_result(original: str, nsn: str) -> NormalizedPhone:
    """Build a NORMALIZED India result from a validated 10-digit national significant number."""
    return NormalizedPhone(
        original=original,
        status=STATUS_NORMALIZED,
        e164=f"+{_INDIA_CALLING_CODE}{nsn}",
        country=INDIA_COUNTRY,
        country_calling_code=INDIA_CALLING_CODE_DISPLAY,
        national_number=nsn,
        uncertainty="",
    )


def _india_nsn_from_national(digits: str) -> str | None:
    """Reduce a national-format Indian digit string to its 10-digit NSN, or ``None`` if it cannot map.

    Accepts the plain 10-digit NSN, a leading trunk ``0`` (11 digits), an un-prefixed country code
    ``91`` (12 digits) and ``091`` (13 digits). Returns ``None`` when the length does not fit any of
    these — the caller then treats it as malformed for the supported plan.
    """
    if len(digits) == _INDIA_NSN_LEN:
        return digits
    if len(digits) == _INDIA_NSN_LEN + 1 and digits.startswith("0"):
        return digits[1:]
    if len(digits) == _INDIA_NSN_LEN + 2 and digits.startswith(_INDIA_CALLING_CODE):
        return digits[2:]
    if len(digits) == _INDIA_NSN_LEN + 3 and digits.startswith("0" + _INDIA_CALLING_CODE):
        return digits[3:]
    return None


def normalize_phone(raw: str, *, country: str | None = None) -> NormalizedPhone:
    """Normalize ``raw`` into a :class:`NormalizedPhone`, preserving the original value verbatim.

    ``country`` is an OPTIONAL explicitly-declared country token (e.g. ``"IN"``). Supported country
    evidence is either that declared country resolving to India, or an explicit international prefix
    (``+91`` / ``0091``) embedded in ``raw``; an embedded international prefix always wins when both
    are present (the number declares its own country).

    Returns (never raises) an :data:`STATUS_UNRESOLVED` result — with the original preserved and the
    uncertainty named — for an ambiguous number: a national-format value with no supported country
    evidence (``no_country_evidence``), or an international value for an unsupported country calling
    code (``unsupported_country``).

    Raises :class:`NormalizationError`, mutating nothing (the function is pure), when the value is
    blank (``empty_phone``), holds no dialable digit (``unparseable_phone``), or DOES carry supported
    country evidence yet is malformed for the Indian plan (``invalid_phone``).
    """
    original = raw if raw is not None else ""
    if not original.strip():
        raise NormalizationError(REASON_EMPTY_PHONE, "a phone value is required", detail=original)

    # Trim only formatting noise (spaces, hyphens, dots, parentheses, slashes). A leading "+" or the
    # "00" IDD prefix is the international marker; everything else must reduce to digits.
    trimmed = _PHONE_PUNCT_RE.sub("", original.strip())

    digits = _NON_DIGIT_RE.sub("", trimmed)
    if not digits:
        # No dialable digit at all — nothing to parse (e.g. "n/a", "call me").
        raise NormalizationError(
            REASON_UNPARSEABLE_PHONE,
            "the phone value has no dialable digits",
            detail=original,
        )

    # Is this value stated in international form? A leading "+" (E.164) or a leading "00" IDD prefix is
    # an explicit country declaration embedded in the number.
    is_international = trimmed.startswith("+") or trimmed.startswith("00")
    if is_international:
        # Strip the IDD "00" prefix if that was the marker; a "+" is already gone from `digits`.
        cc_and_nsn = (
            digits[2:] if (trimmed.startswith("00") and not trimmed.startswith("+")) else digits
        )
        if cc_and_nsn.startswith(_INDIA_CALLING_CODE):
            nsn = cc_and_nsn[len(_INDIA_CALLING_CODE) :]
            if not _INDIA_NSN_RE.match(nsn):
                # Explicit +91 country evidence is present, so a bad national number is a DENIAL — not
                # an ambiguous/unresolved case. Reproducible, changes nothing.
                raise NormalizationError(
                    REASON_INVALID_PHONE,
                    "an Indian (+91) number must have a 10-digit national number (leading digit 1-9)",
                    detail=original,
                )
            return _india_result(original, nsn)
        # A well-formed-looking international number for a country this India-only service carries no
        # plan for. We neither validate nor guess it — it stays UNRESOLVED (the raw value is kept).
        return _unresolved(original, UNCERTAINTY_UNSUPPORTED_COUNTRY)

    # National format (no embedded international prefix): the ONLY country evidence is the declared
    # `country` argument. Without it, the number is ambiguous and stays UNRESOLVED — we never infer a
    # country from the digits alone.
    declared = normalize_country(country) if country else ""
    if not declared:
        return _unresolved(original, UNCERTAINTY_NO_COUNTRY)
    if declared != INDIA_COUNTRY:
        return _unresolved(original, UNCERTAINTY_UNSUPPORTED_COUNTRY)

    # Declared India: we have supported country evidence, so we VALIDATE against the Indian plan and a
    # malformed value is a reproducible DENIAL.
    national_nsn = _india_nsn_from_national(digits)
    if national_nsn is None or not _INDIA_NSN_RE.match(national_nsn):
        raise NormalizationError(
            REASON_INVALID_PHONE,
            "a declared-IN number must reduce to a 10-digit national number (leading digit 1-9)",
            detail=original,
        )
    return _india_result(original, national_nsn)


def share_e164(a: NormalizedPhone, b: NormalizedPhone) -> bool:
    """Whether two normalized phones share the same (non-empty) E.164 form.

    This is a SEARCH/CLUSTERING HINT, not an identity, jurisdiction or consent decision: a ``True``
    here means "these two observations reach the same dialable number", never "merge these records" or
    "it is permitted to contact them". Two UNRESOLVED numbers never share a key (both ``e164`` None).
    """
    return a.e164 is not None and a.e164 == b.e164
