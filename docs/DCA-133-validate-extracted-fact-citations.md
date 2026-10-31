# DCA-133 — Validate extracted fact citations

Stage: Existing AI-service integration · Key: `ai-grounding` · Source sections 4, 8, 9.

## What this story adds

A pure, deterministic **citation validator** at the DCA grounding seam
(`app/ai/citation_validator.py`). Before any AI-extracted fact may become a `FieldObservation`, every
fact must **cite supporting material the DCA itself retrieved** — and that citation is checked here,
independently, against the actual retrieved evidence content and the source's permitted fields. A fact
whose citation is invented, hallucinated, unrelated, or rests on expired source rights is **rejected**
(dropped, reason recorded, nothing written). Only a genuinely supported fact passes. This is the DCA
anti-hallucination gate for AI extraction.

It mirrors the intent of the effortlesspromo-ai gateway's `claim_grounding` screen
(`gateway/validation.py`) — "a claim is grounded only if its text is covered by the fact set; no
override path; a failure is a hard block" — applied the other way round: the gateway screens generated
**copy** against approved facts, while this screens extracted **facts** against the DCA-retrieved
**source**.

**The gateway's own validation verdict cannot authorize an observation write.** Even an envelope the
gateway marked `valid` must pass this grounding gate before a fact may be written — a gateway "valid"
says the output was well-formed and safe, not that each extracted fact is actually supported by evidence
this tenant was entitled to collect. The DCA owns that second decision because only the DCA holds the
retrieved evidence and the source-rights state.

## Load-bearing guarantees (acceptance criteria)

Per fact, checks run in a fixed order and stop at the first failure, which names a stable reason code:

1. **Unknown fact stays null** — a null/blank value is dropped (`unknown_fact_null`), never guessed.
2. **Valid inference label** — `observed` | `inferred` only; else `invalid_inference_label`.
3. **Citation mandatory** — both a cited evidence ref and a cited span are required; else
   `citation_missing` (an unprovenanced value is never promoted).
4. **Cited evidence must be one the DCA actually retrieved** — an unknown ref is an invented citation
   (`citation_evidence_unknown`).
5. **Expired source rights ground nothing** — `rights_valid=False` → `source_rights_expired`.
6. **Only a permitted field** — a field outside the source's allowlist is unrelated
   (`field_not_permitted`).
7. **Cited span must appear in the source content** — a quote absent from the retrieved content is
   hallucinated (`citation_not_in_source`).
8. **Observed value supported by its own cited span** — for an `observed` fact the value must appear in
   the cited span (`value_not_supported`). An `inferred` fact skips this one check (its value is
   derived) but still must be labelled inferred and its cited basis must appear in the source — an
   inference is never dressed up as an observed fact.

Matching is a case-insensitive / whitespace-normalised substring check (the same crude, deterministic
entailment proxy the gateway's grounding screen uses). Every fact gets a `FactVerdict`
(`accepted`, `reason`, `grounded_ref`, original `fact`); only `CitationValidationResult.accepted` facts
are grounded and may be promoted by the caller. A rejection changes nothing downstream — the validator
performs no observation write itself; the caller owns the unit of work.

## Boundary

- Python DCA only (`apps/effortlesspromo-dca`). **No change to effortlesspromo-ai**, no new AI
  capability, no provider SDK/key, no second gateway, no `.NET`.
- **No database change / no migration** — the validator is a pure, in-memory screen (no I/O, no DB, no
  model, no mutation of its input).
- Live/paid stays disabled: this validator does no network/model work; the AI gateway transport remains
  off by default (`ai_enabled=False`) and the synthetic fixture transport is refused in production.

## Files

- `app/ai/citation_validator.py` — the validator: `CitedSource`, `CitedFact`, `FactVerdict`,
  `CitationValidationResult`, `validate_fact_citation`, `validate_citations`, the `REASON_*` codes and
  the `LABEL_OBSERVED` / `LABEL_INFERRED` vocabulary.
- `app/ai/__init__.py` — re-exports the new public names.
- `tests/test_ai_citation_validation.py` — focused tests.
- `scripts/manual_check.py` — `DCA-133` inspectable scenario.

## Verify

```bash
# focused
.venv/Scripts/python.exe -m pytest tests/test_ai_citation_validation.py -q
# full suite (exit code 0 is authoritative)
.venv/Scripts/python.exe -m pytest -q
# quality gate
.venv/Scripts/python.exe -m ruff check app/ai/citation_validator.py tests/test_ai_citation_validation.py scripts/manual_check.py
.venv/Scripts/python.exe -m ruff format --check app/ai/citation_validator.py tests/test_ai_citation_validation.py
.venv/Scripts/python.exe -m mypy app/ai/citation_validator.py
# inspectable scenario (prints grounded-accepted vs ungrounded-dropped facts + reasons)
.venv/Scripts/python.exe scripts/manual_check.py DCA-133
```

The manual check validates a batch of synthetic AI-extracted facts against one live DCA-retrieved page
and one expired-rights page, then prints, per fact, the actual disposition (ACCEPTED / DROPPED + reason)
— a grounded observed fact and a labelled inferred fact are accepted; an invented citation, a
hallucinated quote, an unrelated/non-permitted field, expired rights, a missing citation, and an
unknown null-value fact are each dropped. Local synthetic only; no network, no model, no spend.
