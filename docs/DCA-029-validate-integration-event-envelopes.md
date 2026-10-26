# DCA-029 — Validate integration event envelopes

**Status:** implemented. Adds a single, **versioned Pydantic envelope** for every integration event
this service exchanges over the DCA-027 outbox / DCA-028 inbox messaging path, plus the one sanctioned
entry point that **validates an inbound event before a receiver touches any state** and **rejects a
malformed or incompatible event without echoing its payload**. Pure validation only — no DB write, no
transport, no other service, no UI, no AI-service internals.

## What it does

`app/integration/envelope.py` defines `IntegrationEventEnvelope` (Pydantic v2, `frozen=True`,
`extra="forbid"`) and `parse_envelope(raw)` — the sanctioned validator a receiver calls **first**,
before it opens a session. The envelope is deliberately thin and minimized:

| Field | Meaning |
|---|---|
| `schema_version` | the envelope-contract version; only `SUPPORTED_ENVELOPE_VERSIONS` (`{1}`) are accepted |
| `event_id` | the event's own id (the de-dup handle); opaque |
| `business_id` | the tenant scope the event belongs to (never cross-tenant); opaque |
| `event_type` | a controlled event-type code, e.g. `candidate.accepted`; opaque |
| `aggregate_version` | monotonic version of the emitting aggregate (`≥ 1`), so a consumer can order / detect stale replays |
| `occurred_at` | when the event logically happened |
| `payload` | **minimized** map of opaque references (ids / row keys) only — screened free of secrets/contact data, capped at `MAX_PAYLOAD_BYTES` (8192) |

`new_envelope(...)` mints a current-version envelope on the producer side (same payload screen), so
producer and consumer share one contract.

### Reject unknown/unsupported versions first

`parse_envelope` runs the `schema_version` gate **before any other validation**: a missing/non-integer
version or a version not in `SUPPORTED_ENVELOPE_VERSIONS` is turned away with
`UnsupportedEnvelopeVersionError`, so an event minted against a contract this build does not understand
is never mis-read. Evolving the contract adds a version to the set (and any needed translation) — it
never silently reinterprets an unknown version. The model itself re-checks the version
(`field_validator`), so even direct construction cannot mint an unsupported version.

### Rejected before mutation

`parse_envelope` is a **pure function**: it opens no session and writes nothing, so a rejection happens
strictly before any state change. A guarded receiver validates first and only then mutates — a
malformed / future-version event can never reach its unit of work. The Postgres test below proves this
end to end against the DCA-028 inbox receipt.

### No sensitive payload echoed

Every rejection raises an `EnvelopeError` with a **shape-only** message — the version, the offending
field *locations*, or a size — never the submitted values:

- the payload and free-text ids are screened for secrets/contact data (reusing the single DCA-012
  redaction policy via `assert_opaque_text` / `assert_opaque_references`) **before** the model is built,
  so a non-opaque value is refused with a leak-*kind* message and never flows into a Pydantic error that
  would embed it;
- Pydantic's own `ValidationError` (which embeds the raw input) is caught and summarized to `loc`/`msg`
  pairs with the inputs stripped;
- an oversized payload is reported by size only.

## Tests

`tests/test_integration_envelope.py`, two layers:

```
# always-on unit tests (no database) — the envelope contract + every rejection + no-echo:
pytest tests/test_integration_envelope.py

# "rejected before mutation" on a disposable PostgreSQL (DCA-028 receipt untouched by rejections):
docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55474:5432 postgres:16-alpine
export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55474/dca"
pytest tests/test_integration_envelope.py
```

The unit layer pins: a valid current-version event is accepted; a future/unknown/non-integer version is
rejected; structural faults (empty id/scope/type, `aggregate_version < 1`, bad timestamp, non-map
payload, unknown field, missing field) are rejected; a non-opaque or oversized payload is rejected; and
**no rejection message contains the raw secret/contact value**. The Postgres layer runs a guarded
receiver and asserts that after a valid event plus a future-version, a malformed and a leaky event, the
`inbox_receipts` table holds exactly one row — only the valid event. No migration is needed (no schema
change).

Quality gate (all clean): `ruff check .` · `ruff format --check .` · `mypy` · `pytest`.

## Manual check

```
python scripts/manual_check.py DCA-029
```

For synthetic Business A, a guarded receiver validates each envelope then records a DCA-028 inbox
receipt only on success: (1) a valid current-version event is recorded; (2) a future-version event, (3)
a malformed event (`event_id=""`, `aggregate_version=0`), and (4) a non-opaque payload (raw contact)
are each rejected before any mutation. It prints the persisted `inbox_receipts` rows (expect exactly one
— the valid event), each rejection message, and a check that the raw contact value never appears in any
rejection message. Local synthetic SQLite only; no AI/paid action; the ephemeral DB is cleaned up.

## Boundary

Inside the DCA only: new `app/integration/` package (`envelope.py` + `__init__.py`), focused tests, this
doc, and the DCA-029 manual-check scenario. No model/migration (pure validation, no schema change), no
other service, no UI, no AI-service internals. The envelope is **not** wired into the outbox/inbox sinks
or any transport — that remains a separate, disabled capability. It is a ready, independently reviewable
validation capability. Future capabilities stay disabled.
