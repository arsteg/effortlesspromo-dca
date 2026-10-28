# DCA-064 — Normalize company names and domains

Stage: Evidence and identity resolution · key `normalize` · source sections 6, 7.

## What this adds

A pure, deterministic, network-free normalizer for **company / trade names** and **domains**, used by
identity resolution as a repeatable **search aid**. It derives canonical forms WITHOUT discarding the
original multilingual text, and it merges nothing — resolving identity stays a disabled future
capability.

- `app/normalization/names.py` — `normalize_company_name` / `NormalizedName` / `share_match_key`.
- `app/normalization/domains.py` — `normalize_domain` / `NormalizedDomain` / `share_host`.
- `app/normalization/errors.py` — `NormalizationError` + stable refusal codes.
- `app/normalization/__init__.py` — the public surface.

No DB table, no migration, no HTTP route, no AI capability, no acquisition/send path. Pure Python
(like `app/geography/india.py`). **No .NET / UI / AI-service changes.** `idna==3.20` (already a
transitive dependency) is pinned explicitly in `requirements.txt` / `pyproject.toml` for the IDN work.

## Derived forms

**Company name** (`NormalizedName`):

- `original` — the value exactly as supplied; multilingual text preserved verbatim.
- `display` — NFC, control/format chars dropped, whitespace collapsed; SAME script.
- `match_key` — the repeatable search/clustering key: NFKC (compatibility) + Unicode case-fold +
  whitespace collapse, **script preserved** (it never transliterates).
- `transliteration` — an ASCII fold (NFKD, combining marks dropped, non-ASCII discarded). A **search
  aid only**; partial or empty for non-Latin scripts. `identity_proof` is always `False`.

**Domain** (`NormalizedDomain`):

- `original` — as supplied.
- `ascii` / `normalized` — the canonical IDNA/UTS-46 A-label host (Punycode for non-ASCII labels),
  lower-cased, with scheme / userinfo / path / query / fragment / port / trailing dot stripped.
- `unicode` — the human-readable U-label. `identity_proof` is always `False`.

`www.` is deliberately **not** stripped: treating `www.acme.in` and `acme.in` as the same company is
an identity decision for a later story, not a normalization side effect.

## Acceptance criteria → evidence

- **Repeatable + original multilingual text preserved** — same input → same frozen result; `original`
  carries Hindi/diacritic text through unchanged. (`test_name_normalization_is_repeatable`,
  `test_name_preserves_original_multilingual_text`, `test_idn_domain_normalizes_to_ascii_and_keeps_unicode`,
  `test_domain_normalization_is_repeatable_and_strips_url_noise`.)
- **Transliteration is a search aid, not identity proof** — ASCII fold differs from the match key and
  may be empty for Devanagari; `identity_proof` is always `False`.
  (`test_name_transliteration_is_ascii_search_aid_only`,
  `test_name_transliteration_may_be_empty_for_non_latin_scripts`.)
- **Similar names / shared / www-vs-apex domains are NOT auto-merged** — `Acme` vs `Acme Ltd` and
  `acme.in` vs `www.acme.in` get distinct keys; a shared key is a hint, never a merge.
  (`test_similar_names_are_not_automatically_merged`, `test_shared_match_key_is_a_hint_not_an_identity`,
  `test_shared_host_is_not_merged_for_www`, `test_same_host_is_a_hint_not_identity`.)
- **Failure case + reproducible denial with no state change** — blank / control-only names and
  blank / malformed IDN domains are refused with stable codes (`empty_name`, `unnormalizable_name`,
  `empty_domain`, `invalid_domain`); the module is pure so the refusal repeats identically.
  (`test_empty_name_is_refused`, `test_name_with_only_control_characters_is_refused`,
  `test_empty_domain_is_refused`, `test_invalid_domain_is_refused`,
  `test_name_denial_is_reproducible_and_stateless`, `test_domain_denial_is_reproducible_and_stateless`.)

## Tests / quality

- `tests/test_normalization.py` — 29 focused tests (names + domains, happy path + denial).
- Pure-Python unit behavior: there is no DB / RLS / transaction in this story, so no PostgreSQL
  fixture is required here (the disposable-Postgres rule applies to DB-touching behavior).
- Gate on the touched files: `ruff check`, `ruff format --check`, `mypy app/normalization`, `pytest`
  all green. (Repo-wide `mypy` / `ruff` have pre-existing findings in unrelated files; this story
  adds none.)

## Manual check

```text
python scripts/manual_check.py DCA-064
```

Runs Hindi/English trade-name + IDN fixtures through the normalizer and prints originals, normalized
values, the ASCII search aid, and the IDN A-label/U-label; shows two synthetic Businesses A/B
normalizing the same value to the same key yet keeping separate identities (merges nothing); and shows
a reproducible invalid-domain denial with no state change. Local synthetic only; no AI/paid/network.
