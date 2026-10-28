"""Unicode/IDN-aware normalization + typed organization-identifier validation for identity resolution.

A pure, deterministic, network-free helper family. It derives repeatable search / clustering forms
from a raw multilingual company name or domain WITHOUT discarding the original (DCA-064), and
validates typed Indian organization identifiers (CIN / LLPIN / GSTIN) while keeping their type
(DCA-065). It records nothing and merges nothing — a shared normalized key is a hint, and resolving
identity stays a later capability.

Public surface:

- :func:`normalize_company_name` / :class:`NormalizedName` / :func:`share_match_key` (DCA-064)
- :func:`normalize_domain` / :class:`NormalizedDomain` / :func:`share_host` (DCA-064)
- :func:`validate_org_identifier` / :class:`ValidatedOrgIdentifier` / :func:`entity_class_for` +
  the ``SCHEME_*`` / ``ENTITY_CLASS_*`` constants (DCA-065)
- :func:`normalize_phone` / :class:`NormalizedPhone` / :func:`share_e164` + the ``STATUS_*`` /
  ``UNCERTAINTY_*`` constants (DCA-066)
- :class:`NormalizationError` + the stable refusal codes.
"""

from __future__ import annotations

from app.normalization.domains import NormalizedDomain, normalize_domain, share_host
from app.normalization.errors import (
    REASON_EMPTY_DOMAIN,
    REASON_EMPTY_IDENTIFIER,
    REASON_EMPTY_NAME,
    REASON_EMPTY_PHONE,
    REASON_IDENTIFIER_CHECKSUM_FAILED,
    REASON_INVALID_DOMAIN,
    REASON_INVALID_PHONE,
    REASON_MALFORMED_IDENTIFIER,
    REASON_UNKNOWN_IDENTIFIER_SCHEME,
    REASON_UNNORMALIZABLE_NAME,
    REASON_UNPARSEABLE_PHONE,
    NormalizationError,
)
from app.normalization.names import NormalizedName, normalize_company_name, share_match_key
from app.normalization.phones import (
    STATUS_NORMALIZED,
    STATUS_UNRESOLVED,
    UNCERTAINTY_NO_COUNTRY,
    UNCERTAINTY_UNSUPPORTED_COUNTRY,
    NormalizedPhone,
    normalize_phone,
    share_e164,
)
from app.normalization.org_identifiers import (
    ENTITY_CLASS_ESTABLISHMENT,
    ENTITY_CLASS_LEGAL_ENTITY,
    SCHEME_CIN,
    SCHEME_GSTIN,
    SCHEME_LLPIN,
    SUPPORTED_SCHEMES,
    ValidatedOrgIdentifier,
    entity_class_for,
    normalize_scheme,
    validate_org_identifier,
)

__all__ = [
    "ENTITY_CLASS_ESTABLISHMENT",
    "ENTITY_CLASS_LEGAL_ENTITY",
    "NormalizationError",
    "NormalizedDomain",
    "NormalizedName",
    "NormalizedPhone",
    "REASON_EMPTY_DOMAIN",
    "REASON_EMPTY_IDENTIFIER",
    "REASON_EMPTY_NAME",
    "REASON_EMPTY_PHONE",
    "REASON_IDENTIFIER_CHECKSUM_FAILED",
    "REASON_INVALID_DOMAIN",
    "REASON_INVALID_PHONE",
    "REASON_MALFORMED_IDENTIFIER",
    "REASON_UNKNOWN_IDENTIFIER_SCHEME",
    "REASON_UNNORMALIZABLE_NAME",
    "REASON_UNPARSEABLE_PHONE",
    "SCHEME_CIN",
    "SCHEME_GSTIN",
    "SCHEME_LLPIN",
    "STATUS_NORMALIZED",
    "STATUS_UNRESOLVED",
    "SUPPORTED_SCHEMES",
    "UNCERTAINTY_NO_COUNTRY",
    "UNCERTAINTY_UNSUPPORTED_COUNTRY",
    "ValidatedOrgIdentifier",
    "entity_class_for",
    "normalize_company_name",
    "normalize_domain",
    "normalize_phone",
    "normalize_scheme",
    "share_e164",
    "share_host",
    "share_match_key",
    "validate_org_identifier",
]
