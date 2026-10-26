# DCA-027 — Persist transactional outbox messages

**Status:** implemented. Adds a DCA transactional **outbox** — a tenant-scoped, versioned integration
*intent* staged **inside the caller's transaction**, with a stable event id and a `pending`/`delivered`
status. It persists the record only; it sends **no remote HTTP calls** (the relay/consumer is a
separate, still-disabled story). No other service changed; no UI; no AI-service internals.

## What it does

`OutboxSink` (`app/outbox/sink.py`) is the single sanctioned writer of the new `outbox_messages` table
(`app/db/models.py`). `sink.enqueue(...)` only `session.add(...)`s a row — it never commits, flushes,
or opens a transaction of its own, and it does no I/O. The caller commits the local change and its
outbox intent together, so the two share one unit of work. This mirrors DCA-026's audit sink and the
platform's atomic-audit rule (SEC-001 / BR-122).

Columns (opaque only — never personal data):

| Column | Meaning |
|---|---|
| `business_id` | the tenant scope (from `TenantOwned`; stamped from the verified context, never a caller argument) |
| `event_id` | **stable, deterministic** id for the logical intent — `uuid5(business_id, event_type, dedup_key)`; re-staging the same intent re-derives the same id |
| `event_type` | a controlled event-type code, e.g. `candidate.accepted` |
| `dedup_key` | the caller's logical-intent key (e.g. a row id + action), opaque |
| `version` | the payload-contract **version** (default `1`), so a later consumer can evolve the envelope |
| `payload` | opaque references (ids / row keys) — JSON, screened free of secrets/contact data |
| `status` | `pending` → `delivered`; rows are created `pending`, and nothing transitions here |
| `created_at`, `delivered_at` | staged timestamps; `delivered_at` stays null in this story |

### Duplicate logical intents are constrained

`(business_id, event_id)` is unique (`tenant_unique("event_id", name="uq_outbox_event")`). Because
`event_id` is derived deterministically from `(business_id, event_type, dedup_key)`, a duplicate
logical intent re-derives the same id and is **rejected by the database** (an `IntegrityError` at
commit) rather than silently doubled. The constraint is *per business*, so two tenants may legitimately
hold the same logical intent without colliding.

### No raw secrets or contact data

Every `event_type` / `dedup_key` / `payload` value is screened **before** anything is staged, reusing
the single DCA-012 redaction policy via the audit screens (`assert_opaque_text`,
`assert_opaque_references`). A value carrying an email, a phone number, a registered secret, or a
sensitive key is refused with `OutboxPayloadError` and no row is created. Candidate acceptance is not
recipient consent, and the outbox holds no contact data.

### Tenant isolation

`outbox_messages` is a `TenantOwned` table, so it is automatically in `tenant_owned_tables()` and is
covered by the DCA-018 row-level-security policy exactly like the other tenant data tables — one tenant
can never read another's outbox. Migration `0006_outbox_messages` adds the table + RLS policy for
databases that predate it (on a fresh DB the metadata baseline + 0002 already cover it; 0006 is a
guarded no-op there — same dual-path style as 0005).

## Atomicity (the headline acceptance criterion)

Rolling back a local change also removes its outbox intent:

- `tests/test_outbox_sink.py::test_rolling_back_the_local_change_removes_its_outbox_intent` — accept a
  candidate + stage its intent, then fail before commit → rollback → candidate still `suggested`, zero
  outbox rows.
- `...::test_successful_transaction_commits_both` — the same unit of work committed → candidate
  `accepted` and exactly one `pending` outbox row.
- `...::test_duplicate_intent_rejected_on_postgres` / `...::test_duplicate_logical_intent_is_rejected`
  — a second stage of the same logical intent is rejected by the uniqueness constraint; one row stands.

The transaction-atomicity tests run on a **disposable PostgreSQL** (`DCA_TEST_DATABASE_URL`) and skip
when none is supplied; the sink-contract, dedup-constraint and redaction-denial unit tests always run
(a unique constraint is enforced on SQLite too, so the duplicate-intent *denial* is exercised there).

## Tests

```
# unit + dedup-denial (always, in-memory SQLite):
pytest tests/test_outbox_sink.py

# transaction atomicity + the 0006 create branch + RLS coverage (disposable Postgres):
docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55473:5432 postgres:16-alpine
export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55473/dca"
pytest tests/test_outbox_sink.py tests/test_migrations.py tests/test_rls.py
```

Quality gate (all clean): `ruff check .` · `ruff format --check .` · `mypy` · `pytest`.

## Manual check

```
python scripts/manual_check.py DCA-027
```

Seeds two synthetic candidates for Business A, then: (1) commits an accept + its outbox intent, (2)
rolls back an accept + intent (simulated downstream failure), (3) re-stages candidate #1's same logical
intent (rejected by the uniqueness constraint), (4) refuses an intent carrying a raw email. It prints
the actual candidate statuses and the persisted `outbox_messages` rows — expect candidate #1 `accepted`
and candidate #2 `suggested` (change discarded), exactly one `pending` outbox row, the duplicate intent
refused, and the raw-contact payload refused. Local synthetic SQLite only; no AI/paid action, no remote
delivery; the ephemeral DB is cleaned up.

## Boundary

Inside the DCA only: new model + `uq_outbox_event` constraint, migration `0006_outbox_messages`,
`app/outbox/`, one shared `assert_opaque_text` helper added to `app/audit/`, focused tests, the two
tenant-owned-table assertions updated (`tests/test_rls.py`, `tests/test_migrations.py`), this doc, and
the DCA-027 manual-check scenario. The outbox is **not** wired into the HTTP mutation routes and has
**no relay/delivery path** — that is a separate, disabled story. It is a ready, independently reviewable
persistence capability. Future capabilities stay disabled.
