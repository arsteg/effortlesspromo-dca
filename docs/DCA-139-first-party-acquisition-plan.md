# DCA-139 — Draft a first-party acquisition plan

**Stage:** Existing AI-service integration · **Status:** Ready for manual review · **Key:** `ai-acquisition`

## What this delivers

One end-to-end AI capability: DRAFT a **first-party acquisition plan** — a bounded PROPOSAL of
**compliant first-party permission-capture mechanisms** (forms / QR / opt-in — the stage-15 first-party
capture mechanisms) grounded in the business's confirmed India ICP + its own profile, produced through
the one DCA AI chokepoint.

It is **not** an outbound-to-strangers plan and **not** a send. It is a draft proposal: nothing is
published, no contact is fabricated, no message is sent, and it is never auto-approved.

## Boundary (what changed, by component)

### effortlesspromo-ai (the existing AI service — narrowly extended)
A single new gateway capability, registered the same way as the DCA-134..138 capabilities — no gateway
mechanics touched, no provider SDK/key added.

- `app/gateway/registry.py` — new `first_party_acquisition_plan` capability (`Capability` dataclass:
  input/output schema, `accepts_untrusted=False`, `always_human_review=True`) + its load-bearing
  deterministic `rule_check` (`_first_party_acquisition_plan_rule`). Added to `_DEFAULTS`.
- `app/gateway/providers.py`, `app/gateway/prompts.py` — seed the capability's provider chain + active
  prompt (registration parity).
- `app/gateway/backend.py` — deterministic `StubModelBackend` handler (`_plan_first_party_acquisition`)
  dispatched for the capability (network-free, no spend).
- `tests/test_first_party_acquisition_plan_capability.py` — contract tests pinning the REAL capability
  (schema + the `rule_check` bound), not a permissive fake.

**The gateway `rule_check` bound (load-bearing).** The model output is rejected if it:
- proposes a `method` outside the DCA-declared `permittedMethods` allowlist (no non-first-party method);
- proposes an outbound-to-strangers / purchased-list / scraped / cold-outreach tactic (scanned in text);
- promises **contacts** or a ready-to-message list (a B2C zero-data plan fabricates none) — a forbidden
  output/suggestion key;
- exceeds `maxSuggestions`.

### effortlesspromo-dca (owns the business-state write)
- `app/services/acquisition_planner.py` — the service. Grounds in the confirmed ICP (DCA-040) + the
  latest profile draft (DCA-036, localisation only); declares the bounded `FIRST_PARTY_METHODS`
  allowlist + cap; invokes via the DCA-128 facade; honours the gateway validation verdict; then
  **re-enforces every bound DCA-side** (clamp/drop any non-first-party method, outbound tactic, or
  fabricated-contact suggestion, then cap) before staging a `status='draft'` `AcquisitionPlan`.
- `app/db/models.py` — new `AcquisitionPlan` tenant-owned model. `(business_id, icp_version)` unique =
  the idempotency key (a re-run for the same confirmed ICP returns the existing plan).
- `migrations/versions/0058_acquisition_plans.py` — dual-path create + RLS policy/grant (same guard
  style as 0057). Next after 0057; rev id ≤ 32 chars.
- `app/ai/contract.py` — `first_party_acquisition_plan` added to `KNOWN_CAPABILITIES` (fail-closed
  allowlist).
- `app/ai/context_minimizer.py` — the capability's trusted-context allowlist (ICP + profile summary +
  `permittedMethods` + `maxSuggestions` only; no PII, no external content).
- `tests/test_acquisition_planner.py` — focused service tests (grounded/bounded plan; non-first-party /
  outbound / fabricated-contact / over-cap exclusions; zero-data plan fabricates no contacts;
  idempotency; disabled/rejected/no-ICP denials stage nothing; tenant isolation).
- `tests/test_migrations.py`, `tests/test_rls.py` — `acquisition_plans` added to the covered-table sets
  + a 0058 create-branch migration test.
- `scripts/manual_check.py` — `@register("DCA-139", …)` inspectable scenario.

**No .NET, no Web, no Admin changes.** No provider infrastructure, no second gateway, no live/paid call,
no send.

## Acceptance criteria → evidence

- *B2C zero-data plans do not fabricate contacts or promise a ready-to-message list.* →
  `test_zero_data_plan_fabricates_no_contacts`, `test_fabricated_contacts_suggestion_is_excluded`, and
  the gateway rule_check `test_model_cannot_fabricate_a_contact_list`.
- *Contract tests cover the real gateway capability schema and DCA boundary, not just a permissive
  fake; new capability code lives in effortlesspromo-ai and DCA owns the business-state writes.* → the
  AI `test_first_party_acquisition_plan_capability.py` pins the registry/schema/rule_check; the DCA
  service owns the `AcquisitionPlan` write.
- *Reproduce the result and relevant denial without unintended state changes.* → denial tests assert
  `AcquisitionPlan` count stays 0; manual check prints kept/excluded + `executed/published/sent/
  autoApproved/fabricatedContacts = False`.

## Run it

```bash
# effortlesspromo-dca
.venv/Scripts/python.exe -m pytest tests/test_acquisition_planner.py -q     # focused
.venv/Scripts/python.exe -m pytest -q                                       # full suite
.venv/Scripts/python.exe scripts/manual_check.py DCA-139                     # inspectable scenario
# Postgres migration/RLS branches (disposable PG):
#   set DCA_TEST_DATABASE_URL=postgresql+psycopg://postgres:dca@localhost:55432/dca

# effortlesspromo-ai
AI_BACKEND=stub .venv/Scripts/python.exe -m pytest tests/test_first_party_acquisition_plan_capability.py -q
```

## Notes / blockers

- Live model execution stays disabled (`ai_enabled=False`): the fixture transport is used in dev/test
  and refused in production. No paid/live call, no send.
- The Postgres-backed migration/RLS tests skip without `DCA_TEST_DATABASE_URL` (disposable Postgres);
  the always-on covered-table unit tests run everywhere.
- Promotion of a draft plan to an accepted/executed plan, and any actual first-party capture surface
  (forms/QR), remain later stories (stage 15+) — kept disabled here.
