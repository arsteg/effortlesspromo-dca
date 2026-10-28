"""DCA-065 — validate TYPED Indian organization identifiers (CIN / LLPIN / GSTIN).

:func:`validate_org_identifier` takes a *declared* identifier scheme plus a raw value and, WITHOUT
ever discarding the original, returns a canonical, structurally-validated reference that keeps its
type. Like :mod:`app.normalization.names` / :mod:`app.normalization.domains` and the compliance
rulepacks it is pure, network-free, deterministic Python: no registry lookup, no WHOIS, no model.
It validates the STRING shape (and, for GSTIN, the built-in check digit); it asserts nothing about
whether the organization actually exists or is active — that would be a live-source story.

The supported, typed schemes (the caller states the type; the validator enforces it):

- ``cin`` — Corporate Identification Number: a 21-char MCA identifier of a **legal entity**.
- ``llpin`` — LLP Identification Number: a 7-char identifier of a **legal entity** (an LLP).
- ``gstin`` — GST Identification Number: a 15-char per-state registration of an **establishment**
  (in India a legal entity typically holds one GSTIN *per state* — so a GSTIN is an establishment-level
  reference, never the legal entity itself).

The load-bearing guarantees (the DCA-065 acceptance criteria):

- **Type + original evidence are retained; establishment is never conflated with legal entity.** The
  result carries the declared ``scheme`` and a derived ``entity_class`` (``legal_entity`` for CIN/LLPIN,
  ``establishment`` for GSTIN) computed deterministically from the scheme — so an establishment id
  (GSTIN) is never silently treated as the legal-entity id (CIN/LLPIN). ``original`` is preserved
  verbatim as the evidence. A value that is shaped like a *different* scheme than the one declared is
  refused (``malformed_identifier``), never accepted as the wrong type.

- **No embedded personal identifiers are extracted.** A GSTIN embeds a PAN (and a PAN can be an
  individual's, e.g. a sole proprietor's). This capability validates the GSTIN *as a whole* and NEVER
  surfaces, stores or returns the embedded PAN (or any other embedded personal field). The only
  non-personal sub-field exposed is the jurisdiction code (``state_code``) — a geography fact, not a
  personal identifier.

- **A denial changes nothing.** An empty value, an unknown scheme, a malformed value, or a failed
  GSTIN check digit is refused with a stable code; the function is pure (it records nothing), so the
  refusal is perfectly reproducible.

This module validates and canonicalizes only; it records and merges nothing. Mapping a validated id
onto a stored observation is :class:`~app.services.org_observation.OrgObservationStore`'s job
(DCA-059), and acquisition stays a disabled future capability.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.geography.india import is_valid_gst_state_code, normalize_state_ut
from app.normalization.errors import (
    REASON_EMPTY_IDENTIFIER,
    REASON_IDENTIFIER_CHECKSUM_FAILED,
    REASON_MALFORMED_IDENTIFIER,
    REASON_UNKNOWN_IDENTIFIER_SCHEME,
    NormalizationError,
)

# The typed schemes this capability validates (lower-case tokens; match OrgExternalScheme values where
# they overlap — CIN / GSTIN live there, LLPIN is validation-only and not yet a storage scheme).
SCHEME_CIN = "cin"
SCHEME_LLPIN = "llpin"
SCHEME_GSTIN = "gstin"
SUPPORTED_SCHEMES: tuple[str, ...] = (SCHEME_CIN, SCHEME_LLPIN, SCHEME_GSTIN)

# The load-bearing legal-entity vs establishment distinction — derived from the scheme, never guessed.
# Values match OrgObservationType (DCA-059): "legal_entity" / "establishment".
ENTITY_CLASS_LEGAL_ENTITY = "legal_entity"
ENTITY_CLASS_ESTABLISHMENT = "establishment"
_SCHEME_ENTITY_CLASS: dict[str, str] = {
    SCHEME_CIN: ENTITY_CLASS_LEGAL_ENTITY,
    SCHEME_LLPIN: ENTITY_CLASS_LEGAL_ENTITY,
    SCHEME_GSTIN: ENTITY_CLASS_ESTABLISHMENT,
}

# CIN: L/U (listing status) + 5-digit industry code + 2-letter ROC state + 4-digit year + 3-char
# ownership class + 6-digit registration number = 21 chars.
_CIN_RE = re.compile(r"^([LU])(\d{5})([A-Z]{2})(\d{4})([A-Z]{3})(\d{6})$")
# LLPIN: 3 letters + 4 digits (commonly written with a hyphen, e.g. "AAB-1234").
_LLPIN_RE = re.compile(r"^([A-Z]{3})-?(\d{4})$")
# GSTIN: 2-digit state code + 10-char PAN + 1 entity-count char + "Z" + 1 checksum char = 15 chars.
# The PAN group is captured only to validate shape — it is NEVER surfaced (no personal extraction).
_GSTIN_RE = re.compile(r"^(\d{2})([A-Z]{5}\d{4}[A-Z])([1-9A-Z])Z([0-9A-Z])$")

# Base-36 alphabet for the GSTIN check digit (value == index, 0-9 then A-Z).
_GSTIN_CODE_POINTS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


@dataclass(frozen=True)
class ValidatedOrgIdentifier:
    """A structurally-validated, typed Indian organization identifier. ``original`` is kept verbatim.

    - ``scheme`` — the declared, validated scheme (``cin`` / ``llpin`` / ``gstin``).
    - ``entity_class`` — ``legal_entity`` (CIN/LLPIN) or ``establishment`` (GSTIN), derived from the
      scheme so an establishment id is never conflated with the legal entity.
    - ``original`` — the value exactly as supplied (the retained evidence).
    - ``normalized`` — the canonical form: upper-cased, separators/whitespace removed.
    - ``state_code`` — the jurisdiction code embedded in the id (a non-personal geography fact): for
      CIN the 2-letter ISO state of the Registrar of Companies, for GSTIN the 2-digit GST state code,
      ``""`` for LLPIN. The embedded PAN of a GSTIN is deliberately NOT exposed here.

    ``validated`` means "well-formed and (for GSTIN) checksum-correct", NOT "exists / is active": no
    live register is consulted, so this is never proof the organization is real.
    """

    scheme: str
    entity_class: str
    original: str
    normalized: str
    state_code: str = ""
    validated: bool = True


def _canonicalize(raw: str) -> str:
    """Upper-case and strip all whitespace / common separators (spaces, hyphens, dots) for matching.

    Removing a hyphen from ``AAB-1234`` or stray spaces from a pasted CIN gives a stable canonical
    token; it does not change which characters the format check then validates.
    """
    return re.sub(r"[\s\-.]", "", (raw or "")).upper()


def _gstin_check_digit(body14: str) -> str:
    """The GST check digit for the first 14 characters (standard base-36 alternating-factor algorithm)."""
    factor = 2
    total = 0
    for ch in reversed(body14):
        product = factor * _GSTIN_CODE_POINTS.index(ch)
        factor = 1 if factor == 2 else 2
        total += (product // 36) + (product % 36)
    return _GSTIN_CODE_POINTS[(36 - (total % 36)) % 36]


def normalize_scheme(value: str) -> str:
    """Normalize a declared scheme token, or raise ``unknown_identifier_scheme`` (mutating nothing)."""
    token = (value or "").strip().lower()
    if token not in _SCHEME_ENTITY_CLASS:
        raise NormalizationError(
            REASON_UNKNOWN_IDENTIFIER_SCHEME,
            f"unknown identifier scheme '{value}'; expected one of: {', '.join(SUPPORTED_SCHEMES)}",
            detail=value,
        )
    return token


def entity_class_for(scheme: str) -> str:
    """The legal-entity vs establishment class for a (validated) scheme — deterministic, never guessed."""
    return _SCHEME_ENTITY_CLASS[normalize_scheme(scheme)]


def validate_org_identifier(scheme: str, raw: str) -> ValidatedOrgIdentifier:
    """Validate a TYPED organization identifier, preserving the original and keeping its type.

    Deterministic and repeatable. Raises :class:`NormalizationError` — mutating nothing — with a stable
    code when the scheme is unknown (``unknown_identifier_scheme``), the value is blank
    (``empty_identifier``), the value does not match the declared scheme's structure
    (``malformed_identifier`` — including a value shaped like a *different* scheme, so an establishment
    id is never accepted as a legal-entity id), or a GSTIN fails its check digit
    (``identifier_checksum_failed``).

    The embedded PAN of a GSTIN is validated for shape but never extracted or returned.
    """
    normalized_scheme = normalize_scheme(scheme)
    original = raw if raw is not None else ""
    candidate = _canonicalize(original)
    if not candidate:
        raise NormalizationError(
            REASON_EMPTY_IDENTIFIER,
            f"a {normalized_scheme} value is required",
            detail=original,
        )

    if normalized_scheme == SCHEME_CIN:
        return _validate_cin(original, candidate)
    if normalized_scheme == SCHEME_LLPIN:
        return _validate_llpin(original, candidate)
    return _validate_gstin(original, candidate)


def _malformed(scheme: str, original: str, message: str) -> NormalizationError:
    return NormalizationError(REASON_MALFORMED_IDENTIFIER, message, detail=original)


def _validate_cin(original: str, candidate: str) -> ValidatedOrgIdentifier:
    match = _CIN_RE.match(candidate)
    if match is None:
        raise _malformed(
            SCHEME_CIN,
            original,
            f"'{original}' is not a well-formed CIN (expected 21 chars: "
            "L/U + 5 digits + 2-letter state + 4-digit year + 3-char class + 6 digits)",
        )
    roc_state = match.group(3)
    if normalize_state_ut(roc_state) is None:
        # A well-shaped CIN whose embedded Registrar-of-Companies state is not a recognized Indian
        # subdivision is refused rather than silently accepted (never a guess).
        raise _malformed(
            SCHEME_CIN,
            original,
            f"CIN embeds '{roc_state}', which is not a recognized Indian state/UT code",
        )
    return ValidatedOrgIdentifier(
        scheme=SCHEME_CIN,
        entity_class=ENTITY_CLASS_LEGAL_ENTITY,
        original=original,
        normalized=candidate,
        state_code=roc_state,
    )


def _validate_llpin(original: str, candidate: str) -> ValidatedOrgIdentifier:
    match = _LLPIN_RE.match(candidate)
    if match is None:
        raise _malformed(
            SCHEME_LLPIN,
            original,
            f"'{original}' is not a well-formed LLPIN (expected 3 letters + 4 digits, e.g. AAB-1234)",
        )
    # Canonical LLPIN: the 7-char letters+digits form with any hyphen removed (already done by
    # _canonicalize). No jurisdiction code is embedded, so state_code stays empty.
    return ValidatedOrgIdentifier(
        scheme=SCHEME_LLPIN,
        entity_class=ENTITY_CLASS_LEGAL_ENTITY,
        original=original,
        normalized=f"{match.group(1)}{match.group(2)}",
    )


def _validate_gstin(original: str, candidate: str) -> ValidatedOrgIdentifier:
    match = _GSTIN_RE.match(candidate)
    if match is None:
        raise _malformed(
            SCHEME_GSTIN,
            original,
            f"'{original}' is not a well-formed GSTIN (expected 15 chars: "
            "2-digit state + 10-char PAN + entity char + 'Z' + checksum)",
        )
    gst_state_code = match.group(1)
    if not is_valid_gst_state_code(gst_state_code):
        raise _malformed(
            SCHEME_GSTIN,
            original,
            f"GSTIN state code '{gst_state_code}' is not a recognized Indian GST state code",
        )
    if _gstin_check_digit(candidate[:14]) != candidate[14]:
        # Structurally shaped like a GSTIN but the built-in check digit does not verify.
        raise NormalizationError(
            REASON_IDENTIFIER_CHECKSUM_FAILED,
            f"'{original}' has an invalid GSTIN check digit",
            detail=original,
        )
    # The embedded PAN (match.group(2)) is validated for shape above and then deliberately dropped —
    # it is never surfaced, so no personal identifier is extracted from the GSTIN.
    return ValidatedOrgIdentifier(
        scheme=SCHEME_GSTIN,
        entity_class=ENTITY_CLASS_ESTABLISHMENT,
        original=original,
        normalized=candidate,
        state_code=gst_state_code,
    )
