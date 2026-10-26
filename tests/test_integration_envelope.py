"""DCA-029 — versioned integration-event envelopes.

Two layers:

- **Always-on unit tests** pin the envelope contract: it carries event id, business scope, aggregate
  version, timestamp and a minimized payload; it accepts a valid current-version event; and it
  rejects — without echoing the submitted payload — a malformed event, a future/unknown version, a
  non-opaque (secret/contact) payload, and an oversized payload. Rejection is a pure function (no
  session, no mutation), so these need no database.
- **"Rejected before mutation" on a disposable PostgreSQL** (``DCA_TEST_DATABASE_URL``) exercises the
  acceptance criterion end to end: a guarded receiver validates the envelope first and only then
  records a DCA-028 inbox receipt, so a malformed / future-version event leaves no row. Transactions
  are not exercised on SQLite — this layer skips without a Postgres URL.

Spin up a throwaway Postgres, e.g.:
    docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55474:5432 postgres:16-alpine
    export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55474/dca"
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import InboxReceipt
from app.identity.tenant_context import TenantContext
from app.inbox import InboxSink
from app.integration import (
    CURRENT_ENVELOPE_VERSION,
    MAX_PAYLOAD_BYTES,
    SUPPORTED_ENVELOPE_VERSIONS,
    EnvelopeError,
    IntegrationEventEnvelope,
    MalformedEnvelopeError,
    UnsupportedEnvelopeVersionError,
    new_envelope,
    parse_envelope,
)

A = "00000000-0000-4000-8000-00000000000a"
SUBJECT = "00000000-0000-4000-9000-0000000a11ce"
OCCURRED = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

# A raw (sentinel) secret and contact value — must NEVER appear in any rejection message.
SECRET_EMAIL = "founder@acme.co.in"
SECRET_PHONE = "+91 98765 43210"


def _valid_raw(**overrides: object) -> dict:
    base: dict = {
        "schema_version": CURRENT_ENVELOPE_VERSION,
        "event_id": "evt-123",
        "business_id": A,
        "event_type": "candidate.accepted",
        "aggregate_version": 3,
        "occurred_at": OCCURRED.isoformat(),
        "payload": {"candidateId": "cand-9", "runId": "run-1"},
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------------------------------
# Always-on unit tests — the envelope contract
# --------------------------------------------------------------------------------------------------


def test_valid_current_version_event_is_accepted():
    env = parse_envelope(_valid_raw())
    assert isinstance(env, IntegrationEventEnvelope)
    assert env.schema_version == CURRENT_ENVELOPE_VERSION
    assert env.event_id == "evt-123"
    assert env.business_id == A
    assert env.event_type == "candidate.accepted"
    assert env.aggregate_version == 3
    assert env.occurred_at == OCCURRED
    assert env.payload == {"candidateId": "cand-9", "runId": "run-1"}


def test_envelope_is_frozen_and_closed():
    env = parse_envelope(_valid_raw())
    with pytest.raises(ValidationError):  # frozen model: no mutation after construction
        env.event_id = "other"  # type: ignore[misc]
    # Unknown fields are rejected (extra="forbid") — an event can only carry the agreed envelope.
    with pytest.raises(MalformedEnvelopeError):
        parse_envelope(_valid_raw(unexpected="x"))


def test_new_envelope_producer_helper_stamps_current_version():
    env = new_envelope(
        event_id="evt-1",
        business_id=A,
        event_type="candidate.accepted",
        aggregate_version=1,
        occurred_at=OCCURRED,
        payload={"candidateId": "cand-1"},
    )
    assert env.schema_version == CURRENT_ENVELOPE_VERSION
    # Producer and consumer share one contract: a minted envelope re-parses cleanly.
    assert parse_envelope(env.model_dump(mode="json")) == env


@pytest.mark.parametrize("bad_version", [2, 99, 0, -1])
def test_future_or_unknown_version_is_rejected(bad_version):
    with pytest.raises(UnsupportedEnvelopeVersionError) as exc:
        parse_envelope(_valid_raw(schema_version=bad_version))
    assert str(bad_version) in str(exc.value)
    assert sorted(SUPPORTED_ENVELOPE_VERSIONS) == [CURRENT_ENVELOPE_VERSION]


@pytest.mark.parametrize("bad", [None, "1", 1.0, True, {"v": 1}])
def test_missing_or_nonint_version_is_rejected(bad):
    raw = _valid_raw()
    raw["schema_version"] = bad
    with pytest.raises(UnsupportedEnvelopeVersionError):
        parse_envelope(raw)


def test_version_gate_runs_before_structural_validation():
    # Even with an otherwise-broken body, the version is the first thing rejected.
    with pytest.raises(UnsupportedEnvelopeVersionError):
        parse_envelope({"schema_version": 999})


@pytest.mark.parametrize(
    "overrides",
    [
        {"event_id": ""},  # empty id
        {"business_id": ""},  # empty scope
        {"event_type": ""},  # empty type
        {"aggregate_version": 0},  # must be >= 1
        {"aggregate_version": -2},
        {"aggregate_version": "three"},  # wrong type
        {"occurred_at": "not-a-timestamp"},
        {"payload": ["not", "a", "map"]},  # payload must be a mapping
    ],
)
def test_structurally_malformed_event_is_rejected(overrides):
    raw = _valid_raw(**overrides)
    with pytest.raises(MalformedEnvelopeError):
        parse_envelope(raw)


def test_missing_required_field_is_rejected():
    raw = _valid_raw()
    del raw["event_id"]
    with pytest.raises(MalformedEnvelopeError) as exc:
        parse_envelope(raw)
    assert "event_id" in str(exc.value)  # names the field location, nothing sensitive


def test_non_dict_input_is_rejected():
    for bad in [None, "x", 7, ["a"]]:
        with pytest.raises(MalformedEnvelopeError):
            parse_envelope(bad)


@pytest.mark.parametrize(
    "payload",
    [
        {"owner": SECRET_EMAIL},  # email by content
        {"note": f"call {SECRET_PHONE} today"},  # phone by content
        {"email": "x"},  # sensitive key
        {"contact": {"phone": "x"}},  # nested sensitive key
        {"meta": ["ok", f"reach me at {SECRET_EMAIL}"]},  # email nested in a list
    ],
)
def test_non_opaque_payload_is_rejected(payload):
    with pytest.raises(MalformedEnvelopeError):
        parse_envelope(_valid_raw(payload=payload))


def test_rejection_never_echoes_sensitive_payload():
    # The headline: a refused event's message must not contain the raw secret/contact value.
    cases = [
        _valid_raw(payload={"owner": SECRET_EMAIL}),
        _valid_raw(payload={"note": f"ping {SECRET_PHONE}"}),
        _valid_raw(event_id=SECRET_EMAIL),  # sensitive value in a structural field
    ]
    for raw in cases:
        with pytest.raises(EnvelopeError) as exc:
            parse_envelope(raw)
        message = str(exc.value)
        assert SECRET_EMAIL not in message
        assert SECRET_PHONE not in message
        assert "acme" not in message
        assert "98765" not in message


def test_oversized_payload_is_rejected_without_echo():
    big = {"ref": "x" * (MAX_PAYLOAD_BYTES + 100)}  # opaque but far over the minimized cap
    with pytest.raises(MalformedEnvelopeError) as exc:
        parse_envelope(_valid_raw(payload=big))
    assert "x" * 50 not in str(exc.value)  # the payload itself is not echoed
    assert str(MAX_PAYLOAD_BYTES) in str(exc.value)


def test_payload_defaults_to_empty_map():
    raw = _valid_raw()
    del raw["payload"]
    env = parse_envelope(raw)
    assert env.payload == {}


def test_direct_construction_still_enforces_supported_version():
    # Defense in depth: bypassing parse_envelope cannot mint an unsupported version.
    with pytest.raises(ValidationError):
        IntegrationEventEnvelope(
            schema_version=999,
            event_id="e",
            business_id=A,
            event_type="t",
            aggregate_version=1,
            occurred_at=OCCURRED,
        )


# --------------------------------------------------------------------------------------------------
# "Rejected before mutation" on a disposable PostgreSQL — the acceptance criterion
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)
SCHEMA = "dca_envelope_test"


def _ctx(business_id: str) -> TenantContext:
    return TenantContext(
        subject=SUBJECT, business_id=business_id, roles=("owner",), membership_version="v1"
    )


@pytest.fixture
def pg_factory():
    """A sync session factory over a fresh schema on the disposable Postgres (superuser connection)."""
    admin = create_engine(PG_URL)
    with admin.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE'))
        conn.execute(text(f'CREATE SCHEMA "{SCHEMA}"'))
    admin.dispose()

    engine = create_engine(PG_URL)

    @event.listens_for(engine, "connect")
    def _pin_search_path(dbapi_conn, _record):  # pragma: no cover - trivial connect hook
        cur = dbapi_conn.cursor()
        cur.execute(f'SET search_path TO "{SCHEMA}"')
        cur.close()

    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        engine.dispose()
        admin = create_engine(PG_URL)
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE'))
        admin.dispose()


def _guarded_receive(factory, raw: object) -> bool:
    """A receiver that VALIDATES the envelope before it mutates: it records a DCA-028 inbox receipt
    only for a well-formed, supported event. Returns True iff a receipt was committed."""
    env = parse_envelope(raw)  # raises BEFORE any session is opened
    with factory() as session:
        InboxSink.for_context(session, _ctx(env.business_id)).register(
            event_source="effortlesspromo-api",
            event_id=env.event_id,
            event_type=env.event_type,
            payload=env.payload,
        )
        session.commit()
    return True


@requires_pg
def test_valid_event_is_recorded_but_rejected_events_leave_no_row(pg_factory):
    factory = pg_factory

    # A valid event is validated then recorded.
    assert _guarded_receive(factory, _valid_raw(event_id="evt-ok")) is True

    # A future-version event and a malformed event are each rejected before the receiver mutates.
    with pytest.raises(UnsupportedEnvelopeVersionError):
        _guarded_receive(factory, _valid_raw(event_id="evt-future", schema_version=2))
    with pytest.raises(MalformedEnvelopeError):
        _guarded_receive(factory, _valid_raw(event_id="", aggregate_version=0))
    with pytest.raises(MalformedEnvelopeError):
        _guarded_receive(factory, _valid_raw(event_id="evt-leak", payload={"owner": SECRET_EMAIL}))

    # Exactly one receipt exists — only the valid event — and it never carried contact data.
    with factory() as check:
        rows = check.scalars(select(InboxReceipt)).all()
        assert len(rows) == 1
        assert rows[0].event_id == "evt-ok"
        assert SECRET_EMAIL not in str(rows[0].payload)
