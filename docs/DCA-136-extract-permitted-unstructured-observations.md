# DCA-136 — Extract permitted unstructured observations

**Stage:** Existing AI-service integration · **Status:** Ready for manual review
**Key:** `ai-extract` · source sections 4, 8, 9

## What this delivers

One end-to-end AI capability — the **AI counterpart of the deterministic DCA-124 website extractor**:
turn **already-fetched, APPROVED + entitled source content** into permitted **organization field
observations**, through the accepted DCA AI chokepoint, with every extracted observation grounded in a
citation the DCA independently re-validates before it is stored. The observations are recorded as
ordinary `FieldObservation`s (provenanced, citing captured evidence); nothing is auto-applied beyond
being an observation.

## Capability placement (the boundary)

A **new capability** was added to the existing `effortlesspromo-ai` gateway (the only place model
providers/routing live) — not a second gateway, not a provider SDK in the DCA. The DCA requests it
through the already-accepted facade and owns the resulting business-state writes.

- **effortlesspromo-ai** — new `unstructured_observation_extraction` capability (schema + deterministic
  stub + prompt/provider registration + a `rule_check`). Input: the permitted organization-field
  allowlist + evidence refs (trusted) + the fetched content body (untrusted). Output: `observations[]`
  (each `{field, value, citationRef, citedText, inferenceLabel, confidence}`), `unknowns[]`,
  `confidence`. `accepts_untrusted=True`, `always_human_review=True`. The `rule_check` rejects any
  observation on a field **outside** `permittedFields` (so a personal/contact field can never be
  returned) — the load-bearing bound at the gateway.
- **effortlesspromo-dca** — the capability is added to the DCA request allowlist + the per-capability
  context-minimizer allowlist, and an extractor composes the full accepted AI seam and owns the writes.

## How it works (DCA side)

`app/sources/unstructured_extract.py` → `UnstructuredObservationExtractor.extract(...)`:

1. **Capability/transport gate (DCA-128, fail-closed)** — disabled → refusal, stages nothing, **no
   model call**.
2. **Subject + idempotency** — the organization observation must exist in THIS business; a
   `(source, subject, content_ref)` already extracted is a deterministic no-op (no second model call).
3. **Rights FIRST (DCA-048)** — the permitted fields are resolved through the sanctioned
   `SourcePreflight` gate (`COLLECT` for the purpose). An **unentitled** source yields no permitted
   field → refusal, stages nothing, **never calls the model** (no spend).
4. **DCA-132 untrusted framing** — subject kind + permitted-field allowlist + content ref go to the
   trusted context; the content **body** rides ONLY the untrusted channel. DCA-131 minimises the
   trusted context before egress.
5. **DCA-132 boundary** — a rejected gateway output stores nothing.
6. **DCA-133 independent grounding** — each model observation is re-validated against the content the
   DCA retrieved AND the permitted-field allowlist. The gateway's "valid" does **not** authorize a
   write — only an observation whose citation is confirmed grounded and whose field is permitted is
   kept. Invented / hallucinated / unrelated / **non-permitted (incl. personal/contact)** /
   expired-rights / unknown observations are **dropped** (recorded, never written).
7. **DCA-062 / DCA-061 writers** — the content is captured as a license-aware `EvidenceObject`; each
   grounded observation becomes a provenanced `FieldObservation` citing that evidence. An
   `UnstructuredObservationExtraction` provenance row records the run (idempotency handle + counts:
   grounded fields recorded vs model observations dropped).

Organization-only: the subject is fixed to an organization observation and the permitted fields are the
source's organization-field allowlist, so there is no path to a contact/personal field.

## Changed files

**effortlesspromo-ai (existing AI service):**
- `app/gateway/registry.py` — new `_UNSTRUCTURED_OBSERVATION_EXTRACTION` capability + its
  `_unstructured_observation_rule` (+ in `_DEFAULTS`)
- `app/gateway/prompts.py`, `app/gateway/providers.py` — register the capability (parity)
- `app/gateway/backend.py` — deterministic, evidence-grounded stub (`_extract_unstructured_observations`)
- `tests/test_unstructured_observation_extraction_capability.py` — gateway capability contract tests

**effortlesspromo-dca:**
- `app/sources/unstructured_extract.py` — the AI extractor (new)
- `app/db/models.py` — `UnstructuredObservationExtraction` provenance/dedup table (new)
- `migrations/versions/0055_unstructured_observations.py` — dual-path create + RLS (new)
- `app/ai/contract.py` — `unstructured_observation_extraction` added to `KNOWN_CAPABILITIES`
- `app/ai/context_minimizer.py` — per-capability trusted-context allowlist entry
- `tests/test_unstructured_extract.py` — focused service tests (new)
- `tests/test_migrations.py`, `tests/test_rls.py` — new table added to the covered sets + a 0055 branch test
- `scripts/manual_check.py` — `DCA-136` inspectable scenario
- `docs/DCA-136-extract-permitted-unstructured-observations.md` — this doc

## Migration note

Adds `unstructured_observation_extractions` (tenant-owned, RLS). Revision `0055_unstructured_observations`
(revises `0054_discovery_plans`), dual-path: a fresh DB gets the table from the metadata baseline (0001)
+ RLS from 0002; a DB that predates it gets the table + its tenant-isolation policy created here. No
other schema change; no `.NET` changes; no frontend changes.

## Verification

- effortlesspromo-dca: `.venv/Scripts/python.exe -m pytest tests/test_unstructured_extract.py -q` and
  the full suite `.venv/Scripts/python.exe -m pytest -q`.
- effortlesspromo-ai: `AI_BACKEND=stub .venv/Scripts/python.exe -m pytest -q` (the capability contract
  + registration-parity tests).
- Postgres-backed migration/RLS branches run under `DCA_TEST_DATABASE_URL` (disposable Postgres); they
  skip without it.
- Manual scenario: `.venv/Scripts/python.exe scripts/manual_check.py DCA-136` — prints the authorised
  content, the per-observation recorded/dropped disposition with reasons, the stored field observations
  + cited evidence + provenance row, the idempotent replay and the unentitled-source denial. Local
  synthetic + fixture transport only: no network, no model, **no spend**.

## Boundary confirmation

Stayed strictly within DCA-136: one capability, end to end. No provider SDK/key or second gateway in the
DCA; model execution stays in `effortlesspromo-ai`; tenant isolation, consent/suppression, source rights,
idempotency and budgets are untouched; live/paid paths remain disabled (fixture transport). No DCA-137
work.
