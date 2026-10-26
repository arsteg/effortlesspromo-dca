# DCA-026 — Persist transactional audit events

**Status:** implemented. Adds a DCA audit sink that records a tenant-scoped mutation — actor, scope,
action, reason and opaque references — as a durable row staged **inside the caller's transaction**.
No other service changed; no UI; no AI-service internals. Future consumers (e.g. an integration
outbox) are a separate story and stay out of scope.

## What it does

`AuditSink` (`app/audit/sink.py`) is the single sanctioned writer of the new `audit_events` table
(`app/db/models.py`). `sink.record(...)` only `session.add(...)`s the row — it never commits,
flushes, or opens a transaction of its own. The caller commits the business change and its audit
entry together, so the two share one unit of work. This mirrors the platform's atomic-audit rule
(SEC-001 / BR-122) and its `AuditGuard` interceptor.

Columns (opaque only — never personal data):

| Column | Meaning |
|---|---|
| `business_id` | WHERE — the tenant scope (from `TenantOwned`; stamped from the verified context, never a caller argument) |
| `actor_id`, `actor_type` | WHO — an opaque subject/service id + its kind (`service` / `user` / `system` / `platform_support`) |
| `action` | WHAT — a controlled action code, e.g. `candidate.accept` |
| `reason` | WHY — a reason code / short server-side explanation |
| `references` | opaque references (ids / row keys) — JSON, screened free of secrets/contact data |
| `correlation_id` | trace-join to the DCA-012 structured logs |

### No raw secrets or contact data

Every payload is screened by `assert_opaque_references` **before** anything is staged. A reference
(or the `actor_id` / `reason`) carrying an email, a phone number, a registered secret, or a sensitive
key (`email` / `phone` / `contact` / `token` / …) is refused with `AuditPayloadError` and no row is
created. Detection reuses the single DCA-012 redaction policy (`app.core.logging.get_redactor`) so it
can never drift from redaction; canonical UUIDs are recognised as opaque ids and are not mistaken for
phone numbers.

### Tenant isolation

`audit_events` is a `TenantOwned` table, so it is automatically in `tenant_owned_tables()` and is
covered by the DCA-018 row-level-security policy exactly like the other tenant data tables — one
tenant can never read another's audit trail. Migration `0005_audit_events` adds the table + RLS
policy for databases that predate it (on a fresh DB the metadata baseline + 0002 already cover it;
0005 is a guarded no-op there).

## Atomicity (the headline acceptance criterion)

A failed transaction leaves **neither** the business change nor its audit event:

- `tests/test_audit_sink.py::test_failed_transaction_commits_neither_change_nor_audit` — accept a
  candidate + record its audit, then fail before commit → rollback → candidate still `suggested`,
  zero audit rows.
- `...::test_successful_transaction_commits_both` — the same unit of work committed → candidate
  `accepted` and exactly one audit row.
- `...::test_refused_audit_leaves_the_transaction_clean` — a refused (raw-contact) payload raises
  before staging; the caller may still commit the business change deliberately, and no audit row
  exists.

These transaction tests run on a **disposable PostgreSQL** (`DCA_TEST_DATABASE_URL`) and skip when
none is supplied; the sink-contract and redaction-denial unit tests always run on in-memory SQLite.

## Tests

```
# unit + denial (always):
pytest tests/test_audit_sink.py

# transaction atomicity + the 0005 create branch (disposable Postgres):
docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55471:5432 postgres:16-alpine
export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55471/dca"
pytest tests/test_audit_sink.py tests/test_migrations.py tests/test_rls.py
```

## Manual check

```
python scripts/manual_check.py DCA-026
```

Seeds two synthetic candidates for Business A, then: (1) commits an audited accept, (2) rolls back an
audited accept (simulated downstream failure), (3) refuses an audit carrying a raw email. It prints
the actual candidate statuses and the persisted `audit_events` rows — expect candidate #1 `accepted`,
candidate #2 `suggested` (change discarded), exactly one audit row, and the raw-contact write
refused. Local synthetic SQLite only; no AI/paid action; the ephemeral DB is cleaned up.

## Boundary

Inside the DCA only: new model + migration, `app/audit/`, focused tests, this doc, and the DCA-026
manual-check scenario. The sink is not yet wired into the HTTP mutation routes (that attribution — a
real forwarded actor — belongs with a consumer story); it is a ready, independently reviewable
capability. Future capabilities stay disabled.
