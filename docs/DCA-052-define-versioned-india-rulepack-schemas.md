# DCA-052 — Define versioned India rulepack schemas

Stage: Source rights and policy foundation · key: `policy-schema`

## What this adds

Versioned, validated **India compliance policy rulepack** schemas — the deterministic, auditable data
structure a FUTURE (disabled) policy engine would consult to decide whether an acquisition/contact
**action** over a **channel** is permitted, under explicit **conditions**, backed by **evidence**,
within an **effective-date** window, and labelled by its **authority** (the distinction between a
*law*, a *provider* and a *product* default). Each save is a new **version**; the pack is India-only.

This is distinct from the lightweight `app/compliance/rulepacks.py` **acquisition-screening** pack
(which answers "may this candidate be acquired at all"). This story is the richer, versioned,
reviewable **policy document**.

**This story defines and validates the schema. It enforces nothing and activates nothing.**

A `CompliancePolicyRulepack` row (one version of the India pack) carries:

- **jurisdiction** — always `IN` (the store refuses any other; the service is India-only).
- **version** — monotonic per jurisdiction, insert-only (a prior version is never overwritten).
- **rules** — the validated, normalized list. Each rule has:
  - **effect** — `allow` | `deny` (`RuleEffect`).
  - **action** — `acquire` | `store` | `enrich` | `profile` | `contact` (`RuleAction`).
  - **channel** — `any` | `email` | `sms` | `whatsapp` | `voice` | `postal` (`RuleChannel`).
  - **authority** — `law` | `provider` | `product` (`RuleAuthority`) — WHY the rule exists and which
    outranks which (precedence `law > provider > product`).
  - **conditions** — a structured object over a recognized vocabulary (`consent`, `trust_tier`,
    `contact_kind`, `time_window`, `max_frequency_per_day`, `registration_required`); an unknown key
    or a bad value is **malformed**.
  - **evidence** — `{"reference", "kind", "uri"}`; a rule **cannot exist without a non-empty
    `reference`** (it must cite why).
  - **effective_from / expires_at** — the rule's EXPLICIT validity window (optional; coherent if both
    are given).
- **evidence** (pack-level) — optional `{"reference", "kind", "uri"}` the policy itself rests on.
- **effective_from / expires_at** (pack-level) — the pack's explicit validity window.
- **status** — always `draft` (recording rules puts nothing in force).

### The guarantees (acceptance criteria)

- **Malformed or contradictory rules cannot be saved.** `CompliancePolicyRulepackStore.add_version`
  validates the WHOLE pack FIRST, so a refusal raises `PolicyRulepackValidationError` with a stable
  code **before any row is staged** — a non-India jurisdiction (`invalid_jurisdiction`), an empty pack
  (`no_rules`), an unknown action/channel/effect/authority (`unknown_*`), a rule missing evidence
  (`missing_rule_evidence`), a malformed condition (`invalid_condition`), an incoherent validity
  window (`invalid_window`), or two **contradictory** rules (`contradictory_rules`). A refusal leaves
  no row and no partial state.
- **A contradiction is caught, not stored.** Two rules that scope the identical
  `(action, channel, authority, conditions)` with OPPOSITE effects over an OVERLAPPING validity window
  cannot both be true, so the pack is refused. Opposite effects across DIFFERENT authorities are **not**
  a contradiction — that is precedence (`law` allow vs `product` deny is resolvable by a reader).
  Opposite effects on NON-overlapping windows are also allowed.
- **No rule becomes live just by existing.** Every stored pack is `status='draft'`; `is_active` is
  always `False`. Promotion to `active` — and the policy-engine capability it would pair with — stay
  DISABLED future stories.
- **Reproduce the result and the relevant denial without unintended state changes.** The complete
  fixture validates to one draft row; the contradictory and malformed fixtures are refused and stage
  nothing (verifiable: the row count is unchanged by the rejections).

## Where it lives

- Schema + validation + the one writer: `app/compliance/policy_rulepacks.py`
  (`PolicyRuleInput`, `RulepackInput`, `CompliancePolicyRulepackStore`, stable `REASON_*` codes).
- Model + enums: `app/db/models.py` (`CompliancePolicyRulepack`, `RuleEffect`, `RuleAuthority`,
  `RuleAction`, `RuleChannel`, `PolicyRulepackStatus`).
- Migration: `migrations/versions/0021_compliance_policy_rulepacks.py` (control-plane table; **not**
  tenant-RLS — a shared policy registry, like `sources`/`source_terms`/`source_term_approvals`).

## Boundary

Platform **control-plane** data — a shared policy registry, never tenant-scoped, outside tenant RLS.
It holds only declarative policy metadata: no prospect contact data, no secrets. No UI, no AI-service
change, no `.NET` change, no second AI gateway. Reusing/activating a rulepack (a policy engine) is a
separate, later story and stays disabled here.

## Developer manual test

Disposable PostgreSQL covers the schema/transaction paths; the store logic (control-plane, no RLS) is
unit-tested on in-memory SQLite — the same split every source-rights story uses.

```bash
# unit: valid / missing-effective-dates / contradictory / malformed fixtures (SQLite)
pytest tests/test_compliance_policy_rulepacks.py -q

# schema + migration + RLS-exclusion on a disposable PostgreSQL
docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55452:5432 postgres:16-alpine
export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55452/dca"
pytest tests/test_migrations.py tests/test_rls.py -q

# inspectable scenario (prints real rows/decisions, not PASS/FAIL)
python scripts/manual_check.py DCA-052
# …or against the disposable Postgres (control-plane transactions):
python scripts/manual_check.py DCA-052 \
  --database-url "postgresql+psycopg://postgres:dca@localhost:55452/dca"
```

`manual_check.py DCA-052` validates a complete India fixture (→ one DRAFT row, never live), a fixture
with no effective dates (→ a valid, unbounded version), a contradictory fixture (→ refused
`contradictory_rules`, no row) and two malformed fixtures (non-India → `invalid_jurisdiction`; a rule
with no evidence → `missing_rule_evidence`, no row), and confirms the row count is unchanged by the
rejections.
