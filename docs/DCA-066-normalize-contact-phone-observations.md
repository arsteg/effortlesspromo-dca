# DCA-066 — Normalize contact phone observations

Stage: Evidence and identity resolution · key `normalize-phone` · source sections 6, 7.

## What this adds

A pure, deterministic, network-free normalizer for **contact phone observations** — the phone sibling
of DCA-064 (names/domains) and DCA-065 (typed org identifiers). It turns a raw, human-entered phone
value into a canonical **E.164** form, but **only when supported country evidence is available**, and
WITHOUT discarding the original. It records nothing, contacts no one, and merges nothing — resolving
identity and acquisition stay disabled future capabilities.

- `app/normalization/phones.py` — `normalize_phone` / `NormalizedPhone` / `share_e164` + the
  `STATUS_*` and `UNCERTAINTY_*` constants.
- `app/normalization/errors.py` — three new stable refusal codes on the existing `NormalizationError`.
- `app/normalization/__init__.py` — the DCA-066 surface added to the package exports.

No DB table, no migration, no HTTP route, no AI capability, no acquisition/send path. Pure Python
(it reuses `app/geography/india.py`'s `normalize_country` / `INDIA_COUNTRY` for the India-only plan).
**No .NET / UI / AI-service changes. No new dependency** (no `phonenumbers`; the Indian plan is a
small, bounded, auditable rule set — deterministic exactly like the compliance rulepacks).

## Supported country evidence (bounded, India-only)

E.164 is produced **only** when one of these is present, and the single supported numbering plan is
India (country calling code `91`, a 10-digit national significant number whose leading digit is 1–9,
which admits every mobile `6–9` and landline area code e.g. Delhi `11`):

- an explicit international prefix embedded in the value — `+91 …` or the IDD `0091 …` (the number
  declares its own country); or
- an explicitly declared `country` argument that resolves to India (`IN` / `IND` / `INDIA`).

An embedded international prefix **always wins** over a conflicting `country` argument.

`NormalizedPhone` carries `original` (verbatim), `status` (`normalized` | `unresolved`), `e164`
(`+91XXXXXXXXXX` or `None`), `country` (`IN` or `""`), `country_calling_code`, `national_number`,
`uncertainty`, and `jurisdiction_proof` — which is **always `False`**.

## Acceptance criteria → evidence

- **An ambiguous number remains unresolved; `+91` alone does not establish recipient jurisdiction or
  consent.** A national-format value with no declared country is returned `UNRESOLVED` with
  `uncertainty == no_country_evidence` (the raw preserved, `e164 is None`) — never guessed. An
  international value for a country this service carries no plan for is `UNRESOLVED` with
  `unsupported_country`. And even when a `+91` value IS formatted to E.164, that is a **formatting**
  fact only: `jurisdiction_proof` stays `False` (jurisdiction resolution is `app/geography/resolver.py`'s
  job, where `+91` is an explicitly insufficient weak signal) and nothing is contacted — acceptance of
  a prospect is never recipient consent.
  (`test_missing_country_national_number_is_unresolved`,
  `test_international_unsupported_country_is_unresolved_not_denied`,
  `test_declared_foreign_country_national_number_is_unresolved`,
  `test_plus91_does_not_prove_jurisdiction_or_consent`,
  `test_declared_in_normalized_number_still_proves_no_jurisdiction`.)
- **E.164 only with supported country evidence; original preserved; repeatable.** `+91` / `0091` /
  declared-`IN` values with a valid NSN normalize to `+919876543210`; common national forms (trunk `0`,
  un-prefixed `91`, punctuation noise) all reduce to the same E.164; a Delhi landline (leading NSN
  digit `1`) is accepted; the same input always yields the same frozen result; `original` is kept
  verbatim on both normalize and unresolve. `share_e164` is a search HINT (same reachable number across
  spellings), never a merge/consent/jurisdiction decision.
  (`test_explicit_plus91_normalizes_to_e164`, `test_idd_00_91_prefix_normalizes_to_e164`,
  `test_declared_country_in_normalizes_national_number`, `test_declared_in_accepts_common_national_forms`,
  `test_indian_landline_leading_digit_one_is_valid`, `test_india_variant_india_token_resolves_country`,
  `test_embedded_plus91_overrides_a_conflicting_declared_country`, `test_normalization_is_repeatable`,
  `test_original_is_preserved_verbatim_on_normalize_and_unresolve`,
  `test_share_e164_is_a_hint_not_consent_or_identity`.)
- **Reproduce the result and relevant denial without unintended state changes.** A blank value
  (`empty_phone`), a value with no dialable digit (`unparseable_phone`), and a value that DOES carry
  supported country evidence yet is malformed for the Indian plan (`invalid_phone`) are each refused
  with a stable code; the module is pure, so the refusal repeats identically and changes nothing. (An
  ambiguous number with NO country evidence is NOT a denial — it is `UNRESOLVED`.)
  (`test_blank_phone_is_refused`, `test_value_with_no_dialable_digit_is_refused`,
  `test_malformed_plus91_number_is_refused`, `test_malformed_declared_in_number_is_refused`,
  `test_denial_is_reproducible_and_stateless`.)

## Tests / quality

- `tests/test_phone_normalization.py` — 33 focused tests (happy path, unresolved/ambiguous, denials,
  repeatability, `share_e164` hint).
- Pure-Python unit behavior: there is no DB / RLS / transaction in this story, so no PostgreSQL
  fixture is required here (the disposable-Postgres rule applies to DB-touching behavior). Full suite
  re-run green (`899 passed, 60 skipped` — the skips are the Postgres-Docker-gated async/RLS suites).
- Gate on the touched files:
  `ruff check app/normalization scripts/manual_check.py tests/test_phone_normalization.py`,
  `ruff format --check …`, `mypy app/normalization`, `pytest` all green.

## Manual check

```text
python scripts/manual_check.py DCA-066
```

Runs explicit-country (`+91` and declared-`IN`), missing-country and invalid fixture numbers through
the pure normalizer and prints each value's status, derived E.164 / country / NSN, named uncertainty
and the always-`False` `jurisdiction_proof`. Shows E.164 is produced only with supported country
evidence; that an ambiguous number stays `UNRESOLVED` with the raw preserved; that `+91` establishes
no jurisdiction/consent; and that malformed / blank / no-digit values are refused reproducibly with no
state change. Local synthetic only; no AI/paid/network/DB write.
