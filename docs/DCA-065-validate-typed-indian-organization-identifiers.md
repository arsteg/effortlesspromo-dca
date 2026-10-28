# DCA-065 — Validate typed Indian organization identifiers

Stage: Evidence and identity resolution · key `normalize-org-id` · source sections 6, 7.

## What this adds

A pure, deterministic, network-free validator for **typed** Indian organization identifiers — CIN,
LLPIN and GSTIN — that keeps each identifier's **type** and its original evidence. The caller states
the scheme; the validator enforces the structure (and, for GSTIN, the built-in check digit) and
derives the legal-entity-vs-establishment class deterministically from the scheme. It validates the
STRING only; it asserts nothing about whether the organization exists (no registry lookup). It
records and merges nothing — mapping a validated id onto a stored observation stays DCA-059's job and
acquisition stays a disabled future capability.

- `app/normalization/org_identifiers.py` — `validate_org_identifier` / `ValidatedOrgIdentifier` /
  `entity_class_for` / `normalize_scheme` + the `SCHEME_*` and `ENTITY_CLASS_*` constants.
- `app/normalization/errors.py` — four new stable refusal codes on the existing `NormalizationError`.
- `app/normalization/__init__.py` — the DCA-065 surface added to the package exports.

No DB table, no migration, no HTTP route, no AI capability, no acquisition/send path. Pure Python
(like `app/geography/india.py`, whose `normalize_state_ut` / `is_valid_gst_state_code` it reuses for
the embedded jurisdiction codes). **No .NET / UI / AI-service changes.** No new dependency.

## Supported typed schemes

| scheme | shape | entity class | embedded jurisdiction (`state_code`) |
|---|---|---|---|
| `cin`   | `L/U` + 5 digits + 2-letter state + 4-digit year + 3-char class + 6 digits (21) | `legal_entity` | 2-letter ISO ROC state |
| `llpin` | 3 letters + 4 digits (hyphen optional, e.g. `AAB-1234`) | `legal_entity` | — (none) |
| `gstin` | 2-digit state + 10-char PAN + entity char + `Z` + checksum (15) | `establishment` | 2-digit GST state code |

`ValidatedOrgIdentifier` carries `scheme`, `entity_class`, `original` (verbatim evidence),
`normalized` (upper-cased, separators removed) and the non-personal `state_code`. `validated` means
"well-formed and (GSTIN) checksum-correct", **not** "exists / is active".

LLPIN is validation-only here — it is not yet a storage scheme in `OrgExternalScheme` (DCA-059), so
no store accepts it; adding that is a later, separate change.

## Acceptance criteria → evidence

- **Type + original evidence retained; establishment never conflated with legal entity.** A GSTIN
  resolves to `entity_class == establishment`; CIN/LLPIN to `legal_entity`; the class is derived from
  the scheme, never guessed. Two GSTINs of one legal entity (same PAN, two states) stay distinct
  establishments and are never equated to the CIN. A GSTIN value declared as `cin` is **refused**
  (`malformed_identifier`), never coerced into the legal-entity type.
  (`test_cin_is_a_legal_entity_identifier`, `test_llpin_is_a_legal_entity_identifier_and_canonicalizes_hyphen`,
  `test_gstin_is_an_establishment_identifier_not_the_legal_entity`, `test_entity_class_is_derived_from_scheme`,
  `test_two_establishments_of_one_legal_entity_stay_distinct`, `test_gstin_value_declared_as_cin_is_refused_not_coerced`.)
- **No embedded personal identifiers extracted.** The PAN embedded in a GSTIN is validated for shape
  but never surfaced, stored or returned; only whole-id forms + the non-personal state code are
  exposed. (`test_embedded_pan_is_never_extracted_from_gstin`.)
- **Reproducible + original preserved.** Same input → same frozen result; the original is kept
  verbatim even when the canonical form upper-cases / strips separators.
  (`test_validation_is_repeatable`, `test_canonicalizes_case_and_separators_but_keeps_original`,
  `test_real_world_valid_gstins_pass_the_checksum`.)
- **Failure case + reproducible denial with no state change.** Unknown scheme, blank value, malformed
  value (wrong length/shape, unrecognized embedded state), and a failed GSTIN check digit are each
  refused with a stable code (`unknown_identifier_scheme`, `empty_identifier`, `malformed_identifier`,
  `identifier_checksum_failed`); the module is pure, so the refusal repeats identically.
  (`test_unknown_scheme_is_refused`, `test_empty_identifier_is_refused`, `test_malformed_identifier_is_refused`,
  `test_gstin_with_unrecognized_state_code_is_malformed`, `test_gstin_with_bad_check_digit_is_refused`,
  `test_denial_is_reproducible_and_stateless`.)

## Tests / quality

- `tests/test_org_identifiers.py` — 17 focused test functions (happy path, legal-entity-vs-establishment,
  no-PAN-extraction, denials). GSTIN fixtures are checksum-valid; `27AAPFU0939F1ZV` and
  `29AAGCB7383J1Z4` are real-world valid GSTINs anchoring the standard check-digit algorithm.
- Pure-Python unit behavior: no DB / RLS / transaction in this story, so no PostgreSQL fixture is
  required here (the disposable-Postgres rule applies to DB-touching behavior). Full suite re-run green.
- Gate on the touched files: `ruff check app/normalization scripts/manual_check.py tests/test_org_identifiers.py`,
  `ruff format --check`, `mypy app/normalization`, `pytest` all green. (Repo-wide `mypy`/`ruff` have
  pre-existing findings in unrelated files; this story adds none.)

## Manual check

```text
python scripts/manual_check.py DCA-065
```

Runs checksum-valid CIN/LLPIN/GSTIN fixtures (including two GSTINs of one legal entity across two
states) through the validator and prints each identifier's declared type, derived entity class,
preserved original and canonical form; shows a GSTIN is an establishment reference never conflated
with the legal-entity CIN/LLPIN; shows the embedded PAN is never a result field; and shows malformed
/ bad-checksum / mis-typed / unknown-scheme denials reproduced with no state change. Local synthetic
only; no AI/paid/network.
