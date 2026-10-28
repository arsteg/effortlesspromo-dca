"""DCA-064 — IDN-aware domain normalization (a repeatable search aid, never an identity).

:func:`normalize_domain` turns a raw domain / URL / email-ish host into a canonical form — WITHOUT
discarding the original. Like :mod:`app.normalization.names` it is pure, network-free, deterministic
Python: no DNS lookup, no WHOIS, no model. It only canonicalizes the STRING; it asserts nothing about
who owns the domain.

What it produces (:class:`NormalizedDomain`):

- ``original`` — the value exactly as supplied.
- ``ascii`` — the canonical ASCII A-label (IDNA/UTS-46, Punycode for non-ASCII labels), lower-cased,
  with scheme / userinfo / path / query / fragment / port / trailing dot stripped. This is the
  repeatable match key (``normalized`` is an alias for it).
- ``unicode`` — the human-readable U-label (the Unicode display form of the same host).
- ``normalized`` — alias of ``ascii``: the key other code should compare on.

The load-bearing guarantees (the DCA-064 acceptance criteria):

- **Repeatable + original preserved.** The same input always yields the same canonical host, and
  ``original`` is kept verbatim.
- **No automatic merge.** A shared ``ascii`` host is a search/clustering HINT, never an automatic
  merge or an identity assertion — this module records and merges nothing, and ``www.`` is NOT
  stripped (``www.acme.in`` and ``acme.in`` stay distinct hosts; treating them as "the same company"
  is an identity decision for a later story, not a normalization side effect). :func:`share_host`
  exposes the hint with that contract spelled out.
- **A denial changes nothing.** An empty value, or a string that is not a well-formed IDN domain (too
  few labels, an empty label, or an IDNA-invalid label), is refused with a stable code; the function
  is pure, so the refusal is perfectly reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass

import idna

from app.normalization.errors import (
    REASON_EMPTY_DOMAIN,
    REASON_INVALID_DOMAIN,
    NormalizationError,
)


@dataclass(frozen=True)
class NormalizedDomain:
    """The derived forms of one domain. ``original`` is preserved verbatim; ``normalized`` aliases
    ``ascii`` (the canonical A-label host). ``identity_proof`` is always ``False``: a shared host is a
    search aid, never a basis for automatically merging two observations."""

    original: str
    ascii: str
    unicode: str
    normalized: str
    identity_proof: bool = False


def _extract_host(raw: str) -> str:
    """Pull the bare host out of a raw domain / URL / ``user@host`` string (no network access).

    Strips a scheme, userinfo, path / query / fragment, a trailing ``:port`` and a trailing dot.
    """
    host = raw.strip()
    # Scheme (http://, https://, mailto:, etc.) — take what follows "://" or a leading "mailto:".
    if "://" in host:
        host = host.split("://", 1)[1]
    if host.lower().startswith("mailto:"):
        host = host[len("mailto:") :]
    # Userinfo / local-part — keep what follows the last "@" (handles user:pass@host and emails).
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    # Path / query / fragment — keep only the authority up to the first delimiter.
    for sep in ("/", "?", "#"):
        if sep in host:
            host = host.split(sep, 1)[0]
    # Port — strip a trailing ":digits" (IPv6 bracket hosts are not expected for company domains).
    if ":" in host:
        head, _, tail = host.rpartition(":")
        if head and tail.isdigit():
            host = head
    # A single trailing dot (the DNS root) is not part of the registrable name.
    host = host.strip().rstrip(".")
    return host


def normalize_domain(raw: str) -> NormalizedDomain:
    """Normalize ``raw`` into a :class:`NormalizedDomain`, preserving the original.

    Deterministic and repeatable. Raises :class:`NormalizationError` (``empty_domain`` /
    ``invalid_domain``) — mutating nothing — when the value is blank or not a well-formed IDN domain.
    """
    original = raw if raw is not None else ""
    host = _extract_host(original)
    if not host:
        raise NormalizationError(REASON_EMPTY_DOMAIN, "a domain is required", detail=original)

    # A registrable domain needs at least two non-empty labels (a bare label like "localhost" or a
    # dangling dot is not a company domain here).
    labels = host.split(".")
    if len(labels) < 2 or any(label == "" for label in labels):
        raise NormalizationError(
            REASON_INVALID_DOMAIN,
            f"'{host}' is not a well-formed domain (needs at least two non-empty labels)",
            detail=original,
        )

    try:
        # UTS-46 maps/lower-cases and validates; non-transitional keeps ß / final-sigma as-is.
        ascii_host = idna.encode(host, uts46=True, transitional=False).decode("ascii")
        unicode_host = idna.decode(ascii_host)
    except idna.IDNAError as exc:
        raise NormalizationError(
            REASON_INVALID_DOMAIN,
            f"'{host}' is not a valid internationalized domain: {exc}",
            detail=original,
        ) from exc

    return NormalizedDomain(
        original=original,
        ascii=ascii_host,
        unicode=unicode_host,
        normalized=ascii_host,
    )


def share_host(a: NormalizedDomain, b: NormalizedDomain) -> bool:
    """Whether two normalized domains resolve to the same canonical host.

    A search/clustering HINT, not an identity decision: a ``True`` here means "same host string",
    never "same company" (distinct businesses can share a host; ``www.`` is deliberately not merged).
    """
    return bool(a.ascii) and a.ascii == b.ascii
