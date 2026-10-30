# DCA-099 — Persist durable job requests

Stage: Durable Python execution · key `job-store` · source sections 2, 3, 8.

## What this adds

A DCA-owned **durable job-request store**: a tenant-scoped table (`durable_job_requests`) and a single
sanctioned writer (`app/jobs/store.py`) that persists the *intent* to do a unit of background work so
that intent survives a restart, is deduplicated by logical intent, and can be reviewed/audited.

This story persists job requests **only**. It does **not** execute them — rows are created `QUEUED`
and nothing advances them. A worker / claim / retry loop is a separate, currently-disabled future
capability; the `status`, `attempt_count`, `max_attempts`, `available_at` and `last_error` columns are
bookkeeping scaffolding for it and are never driven here.

Enqueuing work grants nothing and sends nothing: the DCA has no send path, and acceptance of a prospect
is not recipient consent.

## Design

- **The row** (`DurableJobRequest`, `app/db/models.py`) is tenant-owned (`business_id` + DCA-018 RLS),
  carrying: `schema_version` (the payload contract version), `job_type`, `idempotency_key`, an opaque
  `payload_ref`, a one-way `payload_digest`, small non-identifying `payload_metadata`, `status`, and the
  attempt/availability bookkeeping.
- **Deduplicated by logical intent.** `(business_id, job_type, idempotency_key)` is unique
  (`uq_durable_job_request`). The writer relies on that constraint as the serialisation point:
  enqueuing the same logical work twice collapses to exactly ONE row (`created=False` on the replay).
  Verified under real contention on PostgreSQL (two concurrent enqueues → one job).
- **No raw PII on the row.** The job stores only a pointer (`payload_ref`) + a one-way digest of the
  payload — never the payload itself. The digest (derived from `schema_version` + `payload_ref` +
  sorted metadata) lets a replay confirm the SAME work; a *different* payload under a reused key is a
  `JobRequestConflict`, rejected before any write. The store additionally **refuses** an enqueue whose
  reference or string metadata contains an email/phone by shape (reusing the `app/core/logging`
  redactor's patterns), so contact data can never enter the durable queue.
- **Fail-closed, atomic with the caller.** The store stages + flushes into the caller's session and
  never commits — the job commits in the same unit of work as any audit entry. A conflict or a PII
  refusal raises before/without a surviving write, so a denial leaves no state and reproduces exactly.
- **Tenant-scoped.** Every read is filtered to the bound `business_id`; a key another business recorded
  is invisible, and two businesses may reuse the same key as two independent jobs.

## Files

- `app/db/models.py` — `JobRequestStatus` enum + `DurableJobRequest` model.
- `app/jobs/__init__.py`, `app/jobs/store.py` — the store, enqueue contract, digest helper, exceptions.
- `migrations/versions/0037_durable_job_requests.py` — dual-path migration (no-op on a fresh DB; creates
  the table + its tenant-isolation RLS policy on a DB migrated before it existed), matching 0005..0036.
- `tests/test_job_request_store.py` — focused tests (SQLite logic + disposable PostgreSQL dedup /
  conflict / concurrency).
- `tests/test_rls.py`, `tests/test_migrations.py` — table-set assertions + RLS/dual-path coverage for
  the new table.
- `scripts/manual_check.py` — the `DCA-099` inspectable scenario.

## Verify

```
pip install -r requirements-dev.txt
pytest tests/test_job_request_store.py            # SQLite logic (PG tests skip without a URL)
python scripts/manual_check.py DCA-099            # inspectable synthetic scenario

# Locking/RLS/transaction behaviour on a disposable Postgres (NOT SQLite):
docker run --rm -d --name dca99pg -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55499:5432 postgres:16-alpine
export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55499/dca"
pytest tests/test_job_request_store.py tests/test_rls.py tests/test_migrations.py
docker stop dca99pg
```

Quality gate: `ruff check .` · `ruff format --check .` · `mypy` · `pytest`.

## Boundary

Inside the DCA boundary only (its own DB, migration, store, tests, docs). No AI-service, Web, Admin or
.NET changes; no model execution, no send/upload path, no network. Future execution capabilities stay
disabled.
