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
- :class:`NormalizationError` + the stable refusal codes.
"""

from __future__ import annotations

from app.normalization.domains import NormalizedDomain, normalize_domain, share_host
from app.normalization.errors import (
    REASON_EMPTY_DOMAIN,
    REASON_EMPTY_IDENTIFIER,
    REASON_EMPTY_NAME,
    REASON_IDENTIFIER_CHECKSUM_FAILED,
    REASON_INVALID_DOMAIN,
    REASON_MALFORMED_IDENTIFIER,
    REASON_UNKNOWN_IDENTIFIER_SCHEME,
    REASON_UNNORMALIZABLE_NAME,
    NormalizationError,
)
from app.normalization.names import NormalizedName, normalize_company_name, share_match_key
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
    "REASON_EMPTY_DOMAIN",
    "REASON_EMPTY_IDENTIFIER",
    "REASON_EMPTY_NAME",
    "REASON_IDENTIFIER_CHECKSUM_FAILED",
    "REASON_INVALID_DOMAIN",
    "REASON_MALFORMED_IDENTIFIER",
    "REASON_UNKNOWN_IDENTIFIER_SCHEME",
    "REASON_UNNORMALIZABLE_NAME",
    "SCHEME_CIN",
    "SCHEME_GSTIN",
    "SCHEME_LLPIN",
    "SUPPORTED_SCHEMES",
    "ValidatedOrgIdentifier",
    "entity_class_for",
    "normalize_company_name",
    "normalize_domain",
    "normalize_scheme",
    "share_host",
    "share_match_key",
    "validate_org_identifier",
]
