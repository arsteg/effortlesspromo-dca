# DCA-100 — Register typed Python job handlers

Stage: Durable Python execution · key `job-registry` · source sections 2, 3, 8.

## What this adds

A DCA-owned **typed job-handler registry** (`app/jobs/handlers.py`): an explicit registry that binds a
durable job's `(job_type, schema_version)` to a typed Python handler whose payload is a Pydantic model.
It is the gate between a persisted `DurableJobRequest` (DCA-099 — a pointer + digest of intent) and the
eventual execution of that intent: before any work could run, the payload is validated against its
handler's schema, and an unknown job type (or unknown version of a known type) fails safely.

This story **registers + validates only**. It does **not** execute jobs — there is no worker loop, and a
handler's `run()` is the typed contract a later (currently-disabled) worker capability will drive. No path
here invokes it. Registering a handler grants nothing and sends nothing: the DCA has no send path, and
acceptance of a prospect is not recipient consent.

## Design

- **Explicit, one-to-one registration.** `JobHandlerRegistry.register(handler)` binds a handler only by an
  outright call — no import-time autodiscovery. A handler missing a `job_type` / Pydantic `payload_model`
  is refused (`JobHandlerError`); a second handler for an already-bound `(job_type, schema_version)` is a
  `DuplicateHandlerError`.
- **Version support.** A handler declares `schema_version` (matching the `DurableJobRequest.schema_version`
  column). A new payload contract is a NEW handler at a NEW version, never an in-place edit — so a row
  enqueued under v1 and one under v2 resolve to different handlers. `known_versions(job_type)` enumerates
  them.
- **Unknown job types fail safely.** `resolve(job_type, version)` raises `UnknownJobTypeError` when nothing
  is registered for the type at all, and `UnsupportedSchemaVersionError` when the type is known but not at
  that version (distinct, so a caller can tell "unrecognised work" from "a known job at a contract version
  this build doesn't understand"). It never returns `None` and never guesses a nearby version.
- **Invalid payloads never reach execution.** The ONLY way to obtain a runnable `ValidatedJob`
  (handler + validated payload) is `prepare(...)`, which resolves the handler and validates the raw payload
  against its Pydantic model FIRST. A payload that fails validation raises `JobPayloadValidationError`
  before any `ValidatedJob` is produced, so a future worker only ever receives conforming payloads.
- **PII-safe failures.** A validation error carries a `PayloadFieldError` list — field location + stable
  Pydantic error type only, never the submitted value (a payload may reference tenant data). Same
  discipline as the DCA-011 error envelope.
- **No state, fail-closed.** The registry is pure in-memory and touches no database, so every rejection
  changes nothing and reproduces exactly. `build_default_registry()` is deliberately **empty** — real
  job-type handlers are each their own future story (payload schema + rights/rulepack review), and future
  execution stays disabled. An empty registry still fails closed: every type resolves to
  `UnknownJobTypeError`, so nothing can run.

## Files

- `app/jobs/handlers.py` — the registry, `JobHandler` base, `ValidatedJob`/`HandlerDescriptor`, exceptions,
  and `build_default_registry()`.
- `app/jobs/__init__.py` — re-exports the registry surface alongside the DCA-099 store.
- `tests/test_job_handler_registry.py` — focused tests (always-on registry logic + an end-to-end
  compose-with-DCA-099 check on a disposable PostgreSQL).
- `scripts/manual_check.py` — the `DCA-100` inspectable scenario.

No migration is needed: the registry is pure Python and adds no table or column.

## Verify

```
pip install -r requirements-dev.txt
pytest tests/test_job_handler_registry.py        # always-on registry logic (PG test skips without a URL)
python scripts/manual_check.py DCA-100           # inspectable synthetic scenario

# End-to-end compose with the DCA-099 store on a disposable Postgres (NOT SQLite):
docker run --rm -d --name dca100pg -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55499:5432 postgres:16-alpine
export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55499/dca"
pytest tests/test_job_handler_registry.py
docker stop dca100pg
```

Quality gate: `ruff check .` · `ruff format --check .` · `mypy` · `pytest`.

## Boundary

Inside the DCA boundary only (its own registry, tests, docs). No AI-service, Web, Admin or .NET changes;
no model execution, no send/upload path, no network, no DB write. Future job execution stays disabled.
