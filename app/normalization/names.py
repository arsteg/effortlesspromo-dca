"""DCA-064 — Unicode-aware company-name normalization (a repeatable search aid, never an identity).

:func:`normalize_company_name` turns a raw, possibly multilingual company / trade name into a small
bundle of derived forms — WITHOUT ever discarding the original. It is deliberately pure, network-free,
deterministic Python (like :mod:`app.geography.india` and the compliance rulepacks): identity-resolution
signals must be reproducible and auditable, and a model is never consulted.

What it produces (:class:`NormalizedName`):

- ``original`` — the value exactly as supplied, byte-for-byte. The multilingual text is preserved;
  normalization is additive, it never overwrites what the source gave us.
- ``display`` — a tidy, SAME-SCRIPT display form: Unicode NFC, control characters dropped, surrounding
  and internal whitespace collapsed. A Hindi name stays Hindi; a name with diacritics keeps them.
- ``match_key`` — the repeatable search/clustering key: NFKC (compatibility) + Unicode case-fold +
  whitespace collapse, script PRESERVED. Two spellings that differ only in case/width/spacing collide
  here; genuinely different names (``Acme`` vs ``Acme Ltd``) do NOT.
- ``transliteration`` — an ASCII fold (NFKD, combining marks dropped, non-ASCII discarded). This is a
  **search aid only** — for a non-Latin script it is often partial or empty, and it is NEVER treated
  as proof of identity. ``identity_proof`` is always ``False`` to make that explicit in the contract.

The load-bearing guarantees (the DCA-064 acceptance criteria):

- **Repeatable + original preserved.** The same input always yields the same forms, and ``original``
  carries the multilingual text through unchanged.
- **No automatic merge.** A shared ``match_key`` (or a shared transliteration) is a HINT for search and
  candidate clustering, never an automatic merge or an identity assertion — this module records
  nothing and merges nothing. :func:`share_match_key` exposes the hint with that contract spelled out.
- **A denial changes nothing.** An empty/blank name, or one that holds no normalizable content (only
  punctuation / control characters), is refused with a stable code; nothing is stored (the function is
  pure), so the refusal is perfectly reproducible.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from app.normalization.errors import (
    REASON_EMPTY_NAME,
    REASON_UNNORMALIZABLE_NAME,
    NormalizationError,
)

# Collapses any run of Unicode whitespace to a single ASCII space.
_WHITESPACE_RUN = re.compile(r"\s+")


@dataclass(frozen=True)
class NormalizedName:
    """The derived forms of one company / trade name. ``original`` is always preserved verbatim.

    ``identity_proof`` is always ``False``: the derived forms (``match_key``, ``transliteration``) are
    search/clustering aids, never a basis for automatically merging two observations.
    """

    original: str
    display: str
    match_key: str
    transliteration: str
    identity_proof: bool = False


def _strip_controls(value: str) -> str:
    """Drop Unicode control / format characters (category ``C*``) but keep every letter, mark, digit,
    punctuation and symbol — so multilingual content survives while invisible junk is removed."""
    return "".join(ch for ch in value if not unicodedata.category(ch).startswith("C"))


def _transliterate_ascii(value: str) -> str:
    """A best-effort ASCII fold: NFKD, drop combining marks, discard anything still non-ASCII.

    For Latin-with-diacritics this yields the plain-ASCII spelling (``Café`` -> ``cafe``); for a
    non-Latin script (Devanagari, etc.) it is partial or empty. It is a SEARCH AID, nothing more.
    """
    decomposed = unicodedata.normalize("NFKD", value)
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    ascii_only = without_marks.encode("ascii", "ignore").decode("ascii")
    return _WHITESPACE_RUN.sub(" ", ascii_only).strip().casefold()


def normalize_company_name(raw: str) -> NormalizedName:
    """Normalize ``raw`` into a :class:`NormalizedName`, preserving the original multilingual text.

    Deterministic and repeatable: the same input always yields the same forms. Raises
    :class:`NormalizationError` (``empty_name`` / ``unnormalizable_name``) — mutating nothing — when
    the value is blank or holds no normalizable content.
    """
    original = raw if raw is not None else ""
    if not original.strip():
        raise NormalizationError(REASON_EMPTY_NAME, "a company name is required", detail=original)

    # Display form: NFC (compose, same script), strip control/format chars, collapse whitespace.
    cleaned = _strip_controls(original)
    display = _WHITESPACE_RUN.sub(" ", unicodedata.normalize("NFC", cleaned)).strip()
    if not display:
        # Held only control/format characters — nothing normalizable survived.
        raise NormalizationError(
            REASON_UNNORMALIZABLE_NAME,
            "the company name has no normalizable content",
            detail=original,
        )

    # Match key: NFKC (compatibility — e.g. full-width -> ASCII) + case-fold, script preserved,
    # whitespace collapsed. Repeatable and script-faithful; it never transliterates.
    match_key = _WHITESPACE_RUN.sub(" ", unicodedata.normalize("NFKC", display).casefold()).strip()

    return NormalizedName(
        original=original,
        display=display,
        match_key=match_key,
        transliteration=_transliterate_ascii(display),
    )


def share_match_key(a: NormalizedName, b: NormalizedName) -> bool:
    """Whether two normalized names share a (non-empty) match key.

    This is a SEARCH/CLUSTERING HINT, not an identity decision: a ``True`` here means "worth showing
    as a possible match", never "merge these". Callers must still resolve identity with real evidence.
    """
    return bool(a.match_key) and a.match_key == b.match_key
