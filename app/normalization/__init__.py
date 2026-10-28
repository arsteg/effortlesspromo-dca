"""DCA-064 — Unicode/IDN-aware company-name & domain normalization.

A pure, deterministic, network-free helper for identity resolution: it derives repeatable search /
clustering forms from a raw multilingual company name or domain WITHOUT discarding the original, and
treats transliteration as a search aid, never as proof of identity. It records nothing and merges
nothing — a shared normalized key is a hint, and resolving identity stays a later capability.

Public surface:

- :func:`normalize_company_name` / :class:`NormalizedName` / :func:`share_match_key`
- :func:`normalize_domain` / :class:`NormalizedDomain` / :func:`share_host`
- :class:`NormalizationError` + the stable refusal codes.
"""

from __future__ import annotations

from app.normalization.domains import NormalizedDomain, normalize_domain, share_host
from app.normalization.errors import (
    REASON_EMPTY_DOMAIN,
    REASON_EMPTY_NAME,
    REASON_INVALID_DOMAIN,
    REASON_UNNORMALIZABLE_NAME,
    NormalizationError,
)
from app.normalization.names import NormalizedName, normalize_company_name, share_match_key

__all__ = [
    "NormalizationError",
    "NormalizedDomain",
    "NormalizedName",
    "REASON_EMPTY_DOMAIN",
    "REASON_EMPTY_NAME",
    "REASON_INVALID_DOMAIN",
    "REASON_UNNORMALIZABLE_NAME",
    "normalize_company_name",
    "normalize_domain",
    "share_host",
    "share_match_key",
]
