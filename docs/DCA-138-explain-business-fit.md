# DCA-138 — Explain business fit

Stage 12 (existing AI-service integration). **Ready for manual review** — not committed, no human
acceptance marked.

## What this delivers

ONE end-to-end AI capability: **explain business fit** through the accepted DCA AI chokepoint — a
grounded, evidence-cited explanation of **why a prospect fits (or does not fit) the business's confirmed
ICP**. It is **advisory** and can **never change channel eligibility**; eligibility stays a separate axis
(the DCA-073 quality-summary separation, now applied to the AI explanation).

The AI produces, per criterion, an **assessment** (`met` | `unmet` | `partial`) that **cites one
supported fact** (its evidence ref + the exact span) and is labelled `inferred` (a fit verdict is a
derived judgement, not a quoted value), plus an advisory overall `fitBand` + rationale. The DCA
independently re-grounds every citation and owns the resulting `fit_explanations` business-state writes.

## The load-bearing guarantees (acceptance criteria)

- **Fit is strictly separate from eligibility — a high fit can never change a channel restriction.**
  The service carries the caller's pre-decided DCA-055 permission outcome through **verbatim** on the
  stored explanation's separate `eligibility` / `eligibility_reason` fields. It is never derived from the
  fit and **never sent to the model**, so a top `high` fit leaves a `blocked` eligibility exactly
  `blocked`. (Manual check: `prospect-blocked` is `fit_band=high` yet `eligibility='blocked'`.)
- **Only grounded, cited claims survive; the gateway's "valid" does not authorize a write.** Each
  model-proposed assessment is re-validated against the supported facts the DCA actually holds
  (`app/ai/citation_validator.py`, DCA-133): an assessment citing a fact ref the DCA did not supply, a
  field that fact is not about, or a span not present in the fact, is **dropped** (recorded in `dropped`),
  never stored.
- **Sensitive inferred traits are never a scoring input.** Supported facts are screened through the
  DCA-058 field-minimization screen before any egress; a prohibited personal class (children/sensitive,
  private mobile, unnecessary identifier) is **excluded from the model context entirely** — it can never
  be cited or scored on. The gateway capability's own `rule_check` and the DCA re-enforce that a cited
  field must be permitted.
- **New capability code lives in effortlesspromo-ai; DCA owns the resulting business-state writes.** The
  gateway capability (schema + deterministic stub + the `rule_check` bound) is in effortlesspromo-ai; the
  DCA owns the `fit_explanations` writes and re-grounds everything before storing.
- **Advisory, not auto-acted, tenant-scoped, idempotent.** The explanation grants no acquisition/sending
  authority and triggers nothing. Every read/write is confined to one `business_id`. An explanation
  already stored for the subject returns the existing row — no duplicate
  (`(business_id, subject_type, subject_ref)` unique).

Live/paid execution stays disabled by default (the fixture transport is dev/test-only; the live gateway
is used only when `ai_enabled` is configured).

## The gateway `rule_check` bound (effortlesspromo-ai)

The `business_fit_explanation` capability's own screen (the real gateway, not a permissive fake) fails an
output that:

1. carries any eligibility/permission/consent/suppression field (fit can never grant or revoke a
   permission — `eligibility` key on the output, or such a token as an assessment criterion/field);
2. cites a fact ref outside the supplied `supportedFacts` (no invented evidence); or
3. cites a field outside `permittedFields` (a sensitive/excluded trait can never be a scoring input).

## Changed / added files

**effortlesspromo-ai (existing AI service — one narrowly-scoped capability):**
- `app/gateway/registry.py` — new `business_fit_explanation` capability (input/output schema,
  `always_human_review`, `data_categories`) + `_business_fit_explanation_rule` (the eligibility-separation
  + grounding bound); added to `_DEFAULTS`.
- `app/gateway/backend.py` — deterministic `_explain_business_fit` stub + dispatch.
- `app/gateway/prompts.py`, `app/gateway/providers.py` — register the capability key (prompt + provider
  chain parity).
- `tests/test_business_fit_explanation_capability.py` — contract tests (registry/schema parity, grounded
  output, determinism, and the three `rule_check` rejections).

**effortlesspromo-dca:**
- `app/ai/contract.py` — add `business_fit_explanation` to `KNOWN_CAPABILITIES` (the DCA allowlist).
- `app/ai/context_minimizer.py` — add the capability's trusted-context allowlist (prospectRef / icp /
  supportedFacts / permittedFields — no eligibility, no external content).
- `app/db/models.py` — new `FitBand` enum + `FitExplanation` tenant-owned, RLS table.
- `migrations/versions/0057_fit_explanations.py` — dual-path create + RLS policy (after 0056).
- `app/services/fit_explainer.py` — the service: capability gate → idempotency → DCA-058 sensitive
  exclusion → ICP + supported-facts context → facade invoke → gateway-verdict gate → DCA-133 citation
  re-grounding → carry eligibility verbatim → stage `FitExplanation`.
- `tests/test_fit_explainer.py` — focused tests (grounded explanation; high fit does not unblock; ungrounded
  dropped; sensitive excluded before egress; tenant isolation; idempotent; fail-closed denials).
- `tests/test_migrations.py`, `tests/test_rls.py` — add `fit_explanations` to the expected/owned-tables
  sets + a 0057 create-branch migration test.
- `scripts/manual_check.py` — `DCA-138` inspectable scenario.

## Manual test

```text
.venv/Scripts/python.exe -m pytest tests/test_fit_explainer.py -q      # focused (DCA)
.venv/Scripts/python.exe -m pytest -q                                  # full DCA suite
.venv/Scripts/python.exe scripts/manual_check.py DCA-138               # inspectable scenario
# effortlesspromo-ai:
AI_BACKEND=stub .venv/Scripts/python.exe -m pytest tests/test_business_fit_explanation_capability.py -q
```

The manual check scores a fitting prospect (`fit_band=high`), a non-fitting prospect (`fit_band=low`),
and a **high-fit but channel-blocked** prospect, and prints the grounded assessments + the carried-through
eligibility — showing a high fit changed no eligibility and a sensitive trait was excluded before egress.
Live model/paid execution is disabled (`ai_enabled=False`); the fixture transport makes the capability
reviewable with no network or spend.
