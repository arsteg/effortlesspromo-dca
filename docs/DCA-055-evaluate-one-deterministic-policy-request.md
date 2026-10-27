# DCA-055 — Evaluate one deterministic policy request

Stage: Source rights and policy foundation · key: `policy-engine`

## What this adds

A pure, deterministic Python **policy evaluator** — the "policy ENGINE that would CONSULT an in-force
pack" that DCA-052/053 kept describing as a disabled future capability. This story builds *only that
one read-only evaluation function* so it can be reviewed and accepted independently. It is wired into
**no** send, acquisition, worker or HTTP path — nothing calls it except its own tests and the
manual-check scenario. Expressing a decision enforces nothing and enables nothing.

- `app/compliance/policy_engine.py`
  - `decide(request, pack, *, now)` — the pure core: given a `PolicyRequest` and an already-resolved
    `InForcePack | None`, returns a `PolicyEvaluation`. No DB, no network, **no model** — reproducible.
  - `PolicyEvaluator(session)` — a thin, read-only orchestrator: resolves the highest in-force pack
    from the DCA-052 rulepack store + DCA-053 approval lifecycle, then calls `decide`. Never commits.

## Decisions (`PolicyDecision`)

| Decision | When |
|---|---|
| `NOT_APPLICABLE` | the request involves no personal data (the regime does not reach it) — decided first |
| `ALLOW` | an in-force rule explicitly permits it **and** no in-force rule forbids it |
| `DENY` | an in-force rule forbids it, **or** the fail-closed default (no in-force pack, unresolved/unsupported jurisdiction, unknown action/channel, or no granting rule) |
| `REVIEW` | a governing rule plausibly applies but a required condition fact is missing — defer to a human, never guess ALLOW |

Resolution is **deny-dominant** (strictest wins): a single applicable `deny` from any authority blocks
the action. Authority precedence `law > provider > product` is reported as the *decisive authority* and
orders the evidence; it never turns a deny into an allow — law is the ceiling, provider/product
defaults only narrow it.

## Acceptance criteria — how this meets them

- **A missing pack or an unresolved jurisdiction can never yield ALLOW; the LLM is never consulted.**
  `pack=None` → `DENY no_in_force_pack`; an empty (ambiguous) or non-`IN` jurisdiction →
  `DENY unresolved_jurisdiction` / `unsupported_jurisdiction`. The module imports no AI client
  (`llm_consulted` is always `False`). Unknown action/channel → `DENY unsupported_action|channel`
  (default unknown scope to no personal-data permission).
- **Reproduce the result and the relevant denial without unintended state changes.** `decide` is a pure
  function over a frozen request/pack; the evaluator only reads. The manual-check shows the three
  evaluations leave the registry row counts unchanged.

## Focused verification

- `tests/test_policy_engine.py` — the four decisions, the fail-closed invariants (missing pack,
  unresolved/unsupported jurisdiction, unknown scope, no-matching-rule default-deny), deny-dominance,
  rule-window expiry, `channel=any` matching, reproducibility, and the DB-backed `PolicyEvaluator`
  resolve→decide path. The DB path runs on **in-memory SQLite** for the store logic and on a
  **disposable PostgreSQL** (`DCA_TEST_DATABASE_URL`) for the real transaction/read path (skips when no
  URL is set). Verified green on `postgres:16-alpine`.
- Quality gate: `ruff check .` ✓, `ruff format --check` ✓ (changed files), `mypy` adds no new error
  (the 14 pre-existing baseline errors in `policy_rulepacks.py`/`manual_check.py` are untouched and
  out of scope), `pytest` → 686 passed / 49 skipped.

## Developer manual test

Local synthetic, disposable DB, AI disabled:

```bash
cd effortlesspromo-dca
python scripts/manual_check.py DCA-055
```

Shows the actual decision / reasons / evidence / versions for the allowed, denied, missing-pack and
ambiguous-country inputs, and that evaluating writes nothing (row counts unchanged). For the real
Postgres read path:

```bash
docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55432:5432 postgres:16-alpine
DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55432/dca" pytest tests/test_policy_engine.py
```

## Boundary confirmation

Inside the Python-DCA boundary only: `app/compliance/policy_engine.py` (new), `tests/test_policy_engine.py`
(new), one new manual-check scenario in `scripts/manual_check.py`, and this doc. No AI-service, Web,
Admin or .NET change. No migration (no new table — the evaluator only reads existing DCA-052/053 rows).
No source or channel enabled; no rulepack authored or promoted; no model call; no prospect messaged.
Future capabilities stay disabled.

## Traceability

Source sections: 5, 6; key: `policy-engine`. This story's stop rule overrides the reference prompt's
broad continue-working instructions.
