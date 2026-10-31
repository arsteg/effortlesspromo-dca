# DCA-137 — Suggest ambiguous identity matches

Stage 12 (existing AI-service integration). **Ready for manual review** — not committed, no human
acceptance marked.

## What this delivers

ONE end-to-end AI capability: suggest **ambiguous identity matches** through the accepted DCA AI
chokepoint, to **feed — never replace** — the deterministic DCA-067/068 matchers and the
human-authorized, review-only DCA-071 merge.

The AI proposes candidate **pairs** of this business's organization observations that *might* be the
same real-world entity. Every proposal is re-vetted DCA-side with the pure DCA-067 matcher over the two
**stored** observations and kept only as a **review-only suggestion** — it can **never execute a merge**.

## The load-bearing guarantees (acceptance criteria)

- **An AI suggestion can never execute a merge or inspect unentitled records.** The service writes only
  `IdentityMatchSuggestion` review proposals (`status='proposed'`); it creates no `IdentityMerge`,
  collapses no observations, and sends nothing. Recording a merge stays the deliberate, human-authorized
  DCA-071 action, which itself refuses an AI/automation actor outright
  (`identity_merge_ai_actor_forbidden`). Only caller-supplied candidate ids that resolve to an
  observation **in this business** are used; a proposed pair referencing any other id is dropped
  DCA-side, and the gateway capability's own `rule_check` rejects it too.
- **A strong deterministic refutation always wins.** For every proposed pair the DCA re-runs
  `match_organizations` over the stored observations and keeps the suggestion only when the conclusion is
  `MATCH` or an ambiguous `REVIEW`. A `NO_MATCH` (a **conflicting typed registration identifier**) or an
  `INSUFFICIENT` pair is dropped (recorded in `excluded`), never stored — an AI suggestion can never
  override a deterministic `NO_MATCH`.
- **No permission is ever transferred; tenant-scoped; idempotent.** Surfacing a suggestion touches no
  `ConsentReceipt`/`ProcessingRestriction`. Every read/write is confined to the one `business_id`. A pair
  already suggested (in either order) returns the existing row — no duplicate
  (`(business_id, subject_type, subject_a_id, subject_b_id)` unique, canonical pair order).
- **New capability code lives in effortlesspromo-ai; DCA owns the resulting business-state writes.** The
  gateway capability (schema + deterministic stub + the `rule_check` bound) is in effortlesspromo-ai; the
  DCA owns the `identity_match_suggestions` writes and re-vets everything before storing.

Live/paid execution stays disabled by default (the fixture transport is dev/test-only; the live gateway
is used only when `ai_enabled` is configured).

## Changed / added files

### effortlesspromo-dca (DCA)
- `app/db/models.py` — new `IdentityMatchSuggestionStatus` enum + `IdentityMatchSuggestion`
  (TenantOwned/RLS) table.
- `migrations/versions/0056_identity_match_suggestions.py` — dual-path create + RLS for the table
  (fresh DBs get it from the 0001 metadata baseline + 0002 RLS; pre-existing DBs get it here).
- `app/services/identity_match_suggester.py` — the capability integration: `suggest_identity_matches()`
  + `IdentityMatchSuggestionStore` (re-vet via DCA-067, keep review-only suggestions, exclude refutations).
- `app/ai/contract.py` — add `ambiguous_identity_match_suggestion` to the DCA capability allowlist.
- `app/ai/context_minimizer.py` — add the capability's trusted-context allowlist (`subjectType`,
  `candidates` — company-fact descriptors only).
- `tests/test_identity_match_suggester.py` — focused tests (see below).
- `tests/test_rls.py`, `tests/test_migrations.py` — add `identity_match_suggestions` to the tenant-owned
  expected sets + a 0056 create/RLS migration test.
- `scripts/manual_check.py` — register the `DCA-137` inspectable scenario.
- `docs/DCA-137-suggest-ambiguous-identity-matches.md` — this file.

### effortlesspromo-ai (existing AI service)
- `app/gateway/registry.py` — new `ambiguous_identity_match_suggestion` `Capability` (input/output schema
  + `rule_check` that rejects any suggestion referencing an un-authorized id or a self-pair) + added to
  `_DEFAULTS`.
- `app/gateway/backend.py` — deterministic network-free stub `_suggest_identity_matches`.
- `app/gateway/prompts.py`, `app/gateway/providers.py` — register the capability's prompt + provider chain.
- `tests/test_ambiguous_identity_match_capability.py` — gateway capability contract test.

## Tests

- DCA focused: `.venv/Scripts/python.exe -m pytest tests/test_identity_match_suggester.py -q`
- DCA full suite: `.venv/Scripts/python.exe -m pytest -q`
- AI service: `cd ../effortlesspromo-ai && .venv/Scripts/python.exe -m pytest -q` (`AI_BACKEND=stub`)

## Manual check

```text
python scripts/manual_check.py DCA-137
```

Seeds synthetic Business A with an **ambiguous same-name pair** and a **same-name pair with conflicting
CINs**, feeds both as confident AI proposals through the fixture transport, and prints the deterministic
re-match verdict per proposal: the ambiguous pair becomes a REVIEW suggestion; the conflicting-CIN pair
is DROPPED (`no_match`) — an AI suggestion cannot override it. It then shows that **no merge was
executed** and that the DCA-071 merge **refuses an AI actor** (`identity_merge_ai_actor_forbidden`).
Local synthetic only — no network, no model, no spend.

## Boundary confirmation

Stayed strictly inside DCA-137: Python DCA + one narrowly-scoped capability in the existing
effortlesspromo-ai registry; no provider SDK/key/second gateway; no .NET; no frontend; no DCA-138 work.
