# DCA-056 — Select rules by effective date

Stage: Source rights and policy foundation · key: `policy-effective`

## Prerequisite (verified before starting)

- Git history shows the accepted sequence through **DCA-055** (`bb27e88 DCA-055-evaluate-one-deterministic-policy-request`).
- The policy foundation exists end to end: `app/compliance/policy_rulepacks.py` (DCA-052, per-rule
  `effective_from`/`expires_at`), `app/services/policy_approval.py` (DCA-053 lifecycle),
  `app/compliance/policy_engine.py` (DCA-055 evaluator). DCA-054's artifact records that DPDP
  commencement is **per-provision/staged** — store an effective date *per obligation*, never a single
  `dpdp_enabled` flag. A repo-wide search confirms **no `dpdp_enabled`** exists anywhere.

## What this adds

Two capabilities on top of the DCA-055 evaluator — **nothing is wired into any send/acquisition/worker/
HTTP path**; only the tests and the manual-check scenario call it. No DB schema/migration is needed: the
per-rule effective window already exists (DCA-052) and the snapshot is an in-memory, reproducible
artifact on the evaluation result, so evaluating still changes no state.

### 1. Obligation-specific effective-date selection
Each rule carries its OWN validity window — that is where an obligation's per-provision commencement
date lands (DCA-054 §1). A **boundary timestamp selects the correct rule**, one obligation at a time;
there is **no universal `dpdp_enabled` shortcut**.

- `obligation_key(rule)` — the stable identity of the obligation a rule implements (its cited evidence
  `reference`, falling back to its scope). Selection is a per-obligation question.
- `select_effective_rules(rules, *, now)` — the rules in force at `now`, each chosen purely by its own
  half-open `[effective_from, expires_at)` window. A not-yet-commenced or lapsed obligation is simply
  not selected.
- Behaviour: the SAME consented request **DENIES** just before an obligation commences
  (`no_matching_rule` — a default-deny, *not* `denied_by_rule`) and **ALLOWs** at/after it; two
  obligations with different effective dates switch on **independently**.

### 2. Immutable evaluation-time rule snapshot
Every pack-backed decision carries a frozen, content-fingerprinted snapshot of the rules that were in
force at that instant.

- `RuleSnapshot` (frozen) — one rule's obligation key, effect/scope/authority, canonical
  `conditions_json` (a string, so the whole thing is immutable), its window, and `time_eligible`.
- `EvaluationSnapshot` (frozen) — `evaluated_at`, rulepack version / approval, engine version, a
  **tuple** of `RuleSnapshot`, and a sha256 `fingerprint` over the canonical content.
- `replay(request, snapshot)` — reproduces the identical decision **from the snapshot alone** (no DB, no
  live pack, no state change), even after the live rulepack has been superseded, expired or changed.

## Acceptance criteria — how this meets them

- **Boundary timestamps select the correct rule; no universal `dpdp_enabled` shortcut.** Selection is
  driven solely by each rule's own effective window keyed per obligation
  (`select_effective_rules`/`obligation_key`); before-vs-at-vs-after a commencement boundary flips DENY→
  ALLOW, and two obligations commence independently. No `dpdp_enabled` flag exists anywhere.
- **Reproduce the result and the relevant denial without unintended state changes or later-story work.**
  `replay` re-derives both an ALLOW and its relevant pre-commencement DENY from the immutable snapshot,
  with the same fingerprint, even after the live pack changes — and evaluating writes nothing (row
  counts unchanged). No schema/migration, no enablement, no new wiring.

## Tests

`tests/test_policy_effective_date.py` (12 tests) — boundary flips (commencement + expiry), independent
obligation commencement (no universal flag), `select_effective_rules`/`obligation_key`, snapshot capture
+ immutability (`FrozenInstanceError`), fingerprint stability across a boundary, `replay`
reproducibility (incl. after a superseded live pack) for both the ALLOW and the relevant denial. The
DB-backed boundary path runs on **disposable PostgreSQL** (`DCA_TEST_DATABASE_URL`, `postgresql+psycopg`)
and skips when unset — SQLite is used only for the store logic, never the real read path.

## Developer manual test

```bash
cd effortlesspromo-dca
python scripts/manual_check.py DCA-056
```

Records a DCA-052 India rulepack whose only rule **commences 2026-11-01**, promotes it in force
(DCA-053), then evaluates the SAME consented request just **before / at / after** the boundary — showing
`deny (no_matching_rule) → allow → allow` — and `replay()`s the immutable snapshots to reproduce the
ALLOW and the denial even after the live pack is superseded by a deny, with the registry row counts
shown unchanged. Local synthetic only; AI disabled; scoped cleanup.

Focused + regression run (disposable Postgres):

```bash
export DCA_TEST_DATABASE_URL="postgresql+psycopg://<user>:<pw>@localhost:5432/<db>"
pytest tests/test_policy_effective_date.py -q      # 12 passed
pytest -q                                          # full suite: 747 passed
ruff check . && mypy                               # no NEW findings (pre-existing baseline unchanged)
```

## Boundary confirmation

Work stayed inside the DCA boundary: `app/compliance/policy_engine.py` (logic), `scripts/manual_check.py`
(scenario), `tests/test_policy_effective_date.py`, and this doc. No AI-service, Web, Admin, schema,
migration or config change. No source/channel enabled, no model call, no prospect messaged; the
evaluator remains wired into no pipeline.

## Traceability

Source sections: 5, 6; key: `policy-effective`. This story's stop rule overrides the reference prompt's
broad continue-working instructions.
