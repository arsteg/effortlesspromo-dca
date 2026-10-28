"""DCA-064 — Unicode/IDN-aware company-name & domain normalization.

Pure, deterministic, network-free behavior: repeatable forms, original multilingual text preserved,
transliteration as a search aid only, no automatic merge of similar names / shared domains, and a
reproducible denial that changes nothing (the module has no state to change).
"""

from __future__ import annotations

import pytest

from app.normalization import (
    REASON_EMPTY_DOMAIN,
    REASON_EMPTY_NAME,
    REASON_INVALID_DOMAIN,
    REASON_UNNORMALIZABLE_NAME,
    NormalizationError,
    normalize_company_name,
    normalize_domain,
    share_host,
    share_match_key,
)

# --------------------------------------------------------------------------------------------------
# Company names
# --------------------------------------------------------------------------------------------------


def test_name_preserves_original_multilingual_text() -> None:
    # A Hindi (Devanagari) trade name — the original is kept verbatim, script untouched.
    raw = "भारत टेक्सटाइल्स प्राइवेट लिमिटेड"
    result = normalize_company_name(raw)
    assert result.original == raw
    assert result.display == raw  # already NFC + single-spaced
    assert result.match_key == raw.casefold()
    assert result.identity_proof is False


def test_name_normalization_is_repeatable() -> None:
    raw = "  Café   Résumé  Solutions  Pvt Ltd  "
    first = normalize_company_name(raw)
    second = normalize_company_name(raw)
    assert first == second  # frozen dataclass — same input, same forms
    # Display keeps the diacritics and the same script; whitespace is collapsed.
    assert first.display == "Café Résumé Solutions Pvt Ltd"
    # Match key case-folds but KEEPS the diacritics (script preserved, no transliteration).
    assert first.match_key == "café résumé solutions pvt ltd"


def test_name_transliteration_is_ascii_search_aid_only() -> None:
    result = normalize_company_name("Café Résumé Solutions Pvt Ltd")
    # The ASCII fold is a search aid — the diacritics are dropped here, NOT in display/match_key.
    assert result.transliteration == "cafe resume solutions pvt ltd"
    assert result.transliteration != result.match_key
    assert result.identity_proof is False  # transliteration is never proof of identity


def test_name_transliteration_may_be_empty_for_non_latin_scripts() -> None:
    # Pure Devanagari folds to an empty ASCII aid — and that is fine: the original is what counts.
    result = normalize_company_name("भारत टेक्सटाइल्स")
    assert result.transliteration == ""
    assert result.original == "भारत टेक्सटाइल्स"  # original still preserved


def test_name_fullwidth_compatibility_folds_in_match_key() -> None:
    # Full-width Latin (NFKC compatibility) maps to ASCII in the match key, but display stays as-is.
    result = normalize_company_name("ＡＣＭＥ")
    assert result.match_key == "acme"


def test_similar_names_are_not_automatically_merged() -> None:
    # "Acme" and "Acme Ltd" are DIFFERENT names — distinct match keys, never merged.
    a = normalize_company_name("Acme Industries")
    b = normalize_company_name("Acme Industries Ltd")
    assert a.match_key != b.match_key
    assert share_match_key(a, b) is False


def test_shared_match_key_is_a_hint_not_an_identity() -> None:
    # Case/width/spacing-only differences collide on the match key — but that is only a search hint.
    a = normalize_company_name("ACME   Industries")
    b = normalize_company_name("acme industries")
    assert a.match_key == b.match_key
    assert share_match_key(a, b) is True
    # The hint is explicitly NOT an identity assertion.
    assert a.identity_proof is False and b.identity_proof is False
    # The originals are retained distinctly — nothing was merged or rewritten.
    assert a.original == "ACME   Industries" and b.original == "acme industries"


@pytest.mark.parametrize("raw", ["", "   ", "\t\n"])
def test_empty_name_is_refused(raw: str) -> None:
    with pytest.raises(NormalizationError) as exc:
        normalize_company_name(raw)
    assert exc.value.code == REASON_EMPTY_NAME


def test_name_with_only_control_characters_is_refused() -> None:
    # Zero-width + control characters only — nothing normalizable survives.
    with pytest.raises(NormalizationError) as exc:
        normalize_company_name("​‌﻿")
    assert exc.value.code == REASON_UNNORMALIZABLE_NAME


def test_name_denial_is_reproducible_and_stateless() -> None:
    # The failure case repeats identically — the module holds no state to change.
    with pytest.raises(NormalizationError) as first:
        normalize_company_name("")
    with pytest.raises(NormalizationError) as second:
        normalize_company_name("")
    assert first.value.code == second.value.code == REASON_EMPTY_NAME


# --------------------------------------------------------------------------------------------------
# Domains
# --------------------------------------------------------------------------------------------------


def test_idn_domain_normalizes_to_ascii_and_keeps_unicode() -> None:
    # A Hindi IDN (.भारत) — ASCII A-label for matching, U-label for display, original preserved.
    raw = "कंपनी.भारत"
    result = normalize_domain(raw)
    assert result.original == raw
    assert result.ascii == "xn--i1b1b4ch5i.xn--h2brj9c"
    assert result.unicode == "कंपनी.भारत"
    assert result.normalized == result.ascii
    assert result.identity_proof is False


def test_domain_normalization_is_repeatable_and_strips_url_noise() -> None:
    first = normalize_domain("HTTPS://User@EXAMPLE.COM:8080/path?q=1#frag")
    second = normalize_domain("HTTPS://User@EXAMPLE.COM:8080/path?q=1#frag")
    assert first == second
    assert first.ascii == "example.com"
    assert first.unicode == "example.com"


def test_domain_strips_mailto_and_email_local_part() -> None:
    assert normalize_domain("mailto:sales@Acme.in").ascii == "acme.in"
    assert normalize_domain("sales@acme.in").ascii == "acme.in"


def test_domain_strips_trailing_root_dot() -> None:
    assert normalize_domain("acme.in.").ascii == "acme.in"


def test_shared_host_is_not_merged_for_www() -> None:
    # www. is deliberately NOT stripped — treating www.acme.in and acme.in as the same company is an
    # identity decision, not a normalization side effect.
    bare = normalize_domain("acme.in")
    www = normalize_domain("www.acme.in")
    assert bare.ascii != www.ascii
    assert share_host(bare, www) is False


def test_same_host_is_a_hint_not_identity() -> None:
    a = normalize_domain("https://acme.in/contact")
    b = normalize_domain("ACME.in")
    assert share_host(a, b) is True
    assert a.identity_proof is False and b.identity_proof is False


@pytest.mark.parametrize("raw", ["", "   ", "https://", "mailto:"])
def test_empty_domain_is_refused(raw: str) -> None:
    with pytest.raises(NormalizationError) as exc:
        normalize_domain(raw)
    assert exc.value.code == REASON_EMPTY_DOMAIN


@pytest.mark.parametrize("raw", ["localhost", "acme", "acme..in", ".in", "a_b.in", "xn--.in"])
def test_invalid_domain_is_refused(raw: str) -> None:
    with pytest.raises(NormalizationError) as exc:
        normalize_domain(raw)
    assert exc.value.code == REASON_INVALID_DOMAIN


def test_domain_denial_is_reproducible_and_stateless() -> None:
    with pytest.raises(NormalizationError) as first:
        normalize_domain("localhost")
    with pytest.raises(NormalizationError) as second:
        normalize_domain("localhost")
    assert first.value.code == second.value.code == REASON_INVALID_DOMAIN
