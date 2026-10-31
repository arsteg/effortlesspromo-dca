# DCA-134 — Draft a business profile with evidence

**Stage:** Existing AI-service integration · **Status:** Ready for manual review
**Key:** `ai-profile` · source sections 4, 8, 9

## What this delivers

One end-to-end AI capability: turn **authorised website / Brand-KB evidence** into a reviewable
**business-profile draft**, through the accepted DCA AI chokepoint, with every drafted claim grounded
in a citation the DCA can independently verify. The output stays a **draft** carrying its uncertainty
and references; nothing is auto-confirmed and no targeting/spend is approved.

## Capability placement (the boundary)

A **new capability** was added to the existing `effortlesspromo-ai` gateway (the only place model
providers/routing live) — not a second gateway, not a provider SDK in the DCA. The DCA requests it
through the already-accepted facade and owns the resulting business-state write.

- **effortlesspromo-ai** — new `business_profile_draft` capability (schema + deterministic stub +
  prompt/provider registration). Input: business identity (trusted) + evidence bodies (untrusted).
  Output: `claims[]` (each `{field, value, citationRef, citedText, inferenceLabel, confidence}`),
  `unknowns[]`, `confidence`. Advisory (`SKIP` fallback), `accepts_untrusted=True`,
  `always_human_review=True` — a drafted profile is never auto-applied.
- **effortlesspromo-dca** — the capability is added to the DCA request allowlist + the per-capability
  context-minimizer allowlist, and a drafting service composes the full accepted AI seam.

## How it works (DCA side)

`app/services/profile_drafter.py` → `draft_profile_from_evidence(...)`:

1. **DCA-132 untrusted framing** — business identity goes to the trusted context; evidence **bodies**
   go ONLY to the untrusted channel (`build_untrusted_request`), so injected page instructions arrive
   as data, never instructions.
2. **DCA-128 facade** — the one `AiCapabilityFacade` runs `business_profile_draft` over the deployment
   transport (the dev/test fixture today; the live gateway only when configured). DCA-131 minimises the
   trusted context before egress.
3. **DCA-132 boundary** — the gateway's returned validation/taint/injection/human-review signals are
   honoured: a **rejected** output stores nothing; otherwise the draft is staged but marked
   held-for-review.
4. **DCA-133 independent grounding** — each model claim is re-validated against the evidence the DCA
   actually retrieved. The gateway's own "valid" verdict does **not** authorize a write — only a claim
   whose citation is confirmed grounded is kept. Invented / hallucinated / unrelated / expired-rights /
   unknown claims are **dropped** with a stable reason and change nothing.
5. **DCA-036 draft store** — grounded claims are staged as a NEW `BusinessProfileDraft` **version**
   (`status='draft'`, never confirmed) via the only sanctioned writer. Uncertainty (overall + per-claim
   confidence), references (grounded citations) and the dropped record are kept in `provenance`.

### HTTP contract

`POST /v1/tenants/{business_id}/profile/draft-from-evidence` (X-Service-Auth, retry-safe via
`Idempotency-Key`). Body: `{businessName, domain?, evidence:[{ref, content, permittedFields,
rightsValid, provenance?}], draftFields?}`. Returns the draft view + a `grounding` report
(`grounded`, `dropped`, `heldForReview`, `overallConfidence`). Fail-closed: capability/transport
disabled → **503**; rejected gateway output → **502** — both stage nothing.

## Changed files

**effortlesspromo-ai (existing AI service):**
- `app/gateway/registry.py` — new `_BUSINESS_PROFILE_DRAFT` capability (+ in `_DEFAULTS`)
- `app/gateway/prompts.py`, `app/gateway/providers.py` — register the capability (parity)
- `app/gateway/backend.py` — deterministic, evidence-grounded stub (`_draft_business_profile`)
- `tests/test_business_profile_draft_capability.py` — gateway capability contract tests

**effortlesspromo-dca:**
- `app/ai/contract.py` — `business_profile_draft` added to `KNOWN_CAPABILITIES`
- `app/ai/context_minimizer.py` — per-capability trusted-context allowlist entry
- `app/ai/fake.py` — fixture transport gains optional `structured_output` / `validation` knobs
- `app/services/profile_drafter.py` — the drafting service (new)
- `app/api/routes/profiles.py` — the `draft-from-evidence` route (new)
- `app/main.py` — wires `app.state.ai_capability_facade` (None when AI disabled → fail-closed)
- `tests/test_profile_drafter.py` — focused service + HTTP tests
- `scripts/manual_check.py` — `DCA-134` inspectable scenario
- `docs/DCA-134-draft-a-business-profile-with-evidence.md` — this doc

No migration: the existing `BusinessProfileDraft` columns (incl. the `provenance` JSON) carry the
grounded references, dropped record and uncertainty. No `.NET` changes; no frontend changes.

## Verification

- effortlesspromo-dca: `.venv/Scripts/python.exe -m pytest tests/test_profile_drafter.py -q` and the
  full suite `.venv/Scripts/python.exe -m pytest -q`.
- effortlesspromo-ai: `AI_BACKEND=stub .venv/Scripts/python.exe -m pytest -q` (the capability contract
  + registration-parity tests).
- Manual scenario: `.venv/Scripts/python.exe scripts/manual_check.py DCA-134` — prints the authorised
  evidence, the per-claim grounded/dropped disposition, the stored draft (grounded fields, unknowns,
  uncertainty, references) and the two fail-closed denials. Local synthetic + fixture transport only:
  no network, no model, **no spend**.

## Boundary confirmation

Stayed strictly within DCA-134: one capability, end to end. No provider SDK/key or second gateway in
the DCA; model execution stays in `effortlesspromo-ai`; tenant isolation, consent/suppression,
idempotency and budgets are untouched; live/paid paths remain disabled (fixture transport). No DCA-135
work.
