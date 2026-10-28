"""Stable refusal codes for DCA-064 name/domain normalization.

Normalization is a pure, side-effect-free read: it never touches the database, so a refusal changes
no state. It raises :class:`NormalizationError` with one of the stable codes below when the input
cannot be turned into a meaningful normalized value (an empty/blank value, or a string that holds no
normalizable content / is not a well-formed domain). The ``detail`` names the offending value when
useful — never anything secret, since the inputs here are public company names and domains.
"""

from __future__ import annotations

# A company name with no usable content after trimming.
REASON_EMPTY_NAME = "empty_name"
# A company name that normalizes to nothing (e.g. only punctuation/control characters).
REASON_UNNORMALIZABLE_NAME = "unnormalizable_name"
# A domain with no usable content after trimming/parsing.
REASON_EMPTY_DOMAIN = "empty_domain"
# A string that is not a well-formed (IDN-aware) domain — too few labels, empty label, or an
# IDNA-invalid label. A denial here is reproducible and changes nothing.
REASON_INVALID_DOMAIN = "invalid_domain"

# -- DCA-065 typed Indian organization-identifier validation (CIN / LLPIN / GSTIN) ------------------
# A typed organization identifier supplied with no usable content after trimming.
REASON_EMPTY_IDENTIFIER = "empty_identifier"
# The declared identifier scheme is not one this capability validates (not cin / llpin / gstin).
REASON_UNKNOWN_IDENTIFIER_SCHEME = "unknown_identifier_scheme"
# The value does not match the declared scheme's structural format (wrong length / character shape /
# an unrecognized embedded jurisdiction code). A GSTIN supplied as a CIN is refused here, never
# silently accepted as the other type.
REASON_MALFORMED_IDENTIFIER = "malformed_identifier"
# The value is structurally shaped like the scheme but fails the scheme's built-in check digit (GSTIN).
REASON_IDENTIFIER_CHECKSUM_FAILED = "identifier_checksum_failed"


class NormalizationError(Exception):
    """A normalization request was refused with a stable code. Pure — raising it mutates no state, so
    the denial is perfectly reproducible. ``detail`` names the offending value when useful."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail
