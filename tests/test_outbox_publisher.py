"""DCA-034 — idempotent outbox publisher.

Two layers, matching the DCA-027 sink suite:

- **Always-on unit tests** (in-memory SQLite) drive the REAL platform transport against the DCA-033
  fake contract server (no network). They pin the publisher contract: a 2xx acknowledgment marks the
  row DELIVERED keyed by its stable event id; a lost acknowledgment (timeout / unreachable / 5xx /
  429) leaves the row PENDING with the SAME event id and never a fresh key; a 4xx is a surfaced
  denial that also changes no state; scope is never crossed; and the capability is disabled (no
  publisher built) until it is configured.
- **Lost-ack-then-retry on a disposable PostgreSQL** (``DCA_TEST_DATABASE_URL``) proves the headline
  acceptance criterion against real transactions: publish once with a lost acknowledgment, then retry
  — the row keeps its identity throughout and the fake host sees ONE logical event (same
  Idempotency-Key both times). Transactions are not exercised on SQLite — this layer skips without a
  Postgres URL.

Spin up a throwaway Postgres, e.g.:
    docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55474:5432 postgres:16-alpine
    export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55474/dca"
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker

from app.clients.platform_transport import PlatformHttpTransport
from app.core.config import Settings, validate_required
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import OutboxMessage, OutboxStatus
from app.identity.tenant_context import TenantContext
from app.outbox import (
    OutboxPublisher,
    OutboxSink,
    PublishOutcome,
    build_outbox_publisher,
)
from app.outbox.publisher import OutboxPublisherDisabled
from app.testing import FixtureOutcome, build_fake_contract_server

A = "00000000-0000-4000-8000-00000000000a"
B = "00000000-0000-4000-8000-00000000000b"
SUBJECT = "00000000-0000-4000-9000-0000000a11ce"

PLATFORM_BASE = "https://platform.internal/svc"
PLATFORM_CRED = "scoped-platform-credential-SENTINEL-xyz"  # synthetic, local-only
PUBLISH_PATH = "/outbox/events"
MOUNT = "/svc"  # the base URL's path prefix the fake server strips before matching


def _ctx(business_id: str) -> TenantContext:
    return TenantContext(
        subject=SUBJECT, business_id=business_id, roles=("owner",), membership_version="v1"
    )


def _settings(**overrides) -> Settings:
    base = {
        "environment": "testing",
        "database_url": "sqlite+pysqlite:///:memory:",
        "service_auth_token": "test-service-token",
        "platform_base_url": PLATFORM_BASE,
        "platform_service_credential": PLATFORM_CRED,
        "platform_outbox_publish_path": PUBLISH_PATH,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


def _idem_of(call) -> str | None:
    """Case-insensitive lookup of the Idempotency-Key the fake server recorded for a call."""
    for name, value in call.headers.items():
        if name.lower() == "idempotency-key":
            return value
    return None


# --------------------------------------------------------------------------------------------------
# Fixtures — SQLite session factory + a fake-server-backed publisher
# --------------------------------------------------------------------------------------------------


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


@pytest.fixture
def server():
    return build_fake_contract_server(_settings(), base_path=MOUNT)


@pytest.fixture
def publisher(session_factory, server) -> OutboxPublisher:
    built = build_outbox_publisher(_settings(), session_factory, transport=server.as_transport())
    assert built is not None  # configured in this profile
    return built


def _stage(session_factory, business_id: str, dedup_key: str, **kw) -> str:
    """Stage + commit one PENDING outbox message; return its stable event id."""
    with session_factory() as session:
        row = OutboxSink.for_context(session, _ctx(business_id)).enqueue(
            event_type=kw.pop("event_type", "candidate.accepted"), dedup_key=dedup_key, **kw
        )
        session.commit()
        return row.event_id


# --------------------------------------------------------------------------------------------------
# Happy path — a 2xx acknowledgment delivers, keyed by the stable event id
# --------------------------------------------------------------------------------------------------


def test_publish_success_marks_delivered_keyed_by_stable_event_id(
    session_factory, server, publisher
):
    event_id = _stage(session_factory, A, "cand-1:accept", payload={"candidateId": "cand-1"})
    server.set_route("POST", PUBLISH_PATH, FixtureOutcome.response(202, json={"accepted": True}))

    results = publisher.publish_pending(A)

    assert len(results) == 1
    res = results[0]
    assert res.outcome is PublishOutcome.DELIVERED
    assert res.delivered and not res.retryable
    # The idempotency key IS the stable event id (not the random row id).
    assert res.idempotency_key == event_id
    assert _idem_of(server.last_call) == event_id
    # The row flipped PENDING -> DELIVERED with an acknowledgment timestamp.
    with session_factory() as check:
        row = check.scalars(select(OutboxMessage)).one()
        assert row.status == OutboxStatus.DELIVERED.value
        assert row.delivered_at is not None
        assert row.event_id == event_id  # identity unchanged


def test_publish_sends_the_versioned_envelope(session_factory, server, publisher):
    event_id = _stage(session_factory, A, "cand-7:accept", payload={"candidateId": "cand-7"})
    server.set_route("POST", PUBLISH_PATH, FixtureOutcome.response(200))

    publisher.publish_pending(A, correlation_id="trace-xyz")

    import json

    body = json.loads(server.last_call.text)
    assert body["schema_version"] == 1
    assert body["event_id"] == event_id
    assert body["business_id"] == A
    assert body["event_type"] == "candidate.accepted"
    assert body["payload"] == {"candidateId": "cand-7"}
    assert server.last_call.correlation_id == "trace-xyz"


# --------------------------------------------------------------------------------------------------
# The headline criterion — a lost acknowledgment preserves a retryable event with the SAME identity
# --------------------------------------------------------------------------------------------------


def test_timeout_leaves_pending_same_identity_then_retry_delivers(
    session_factory, server, publisher
):
    event_id = _stage(session_factory, A, "cand-2:accept", payload={"candidateId": "cand-2"})

    # 1) First attempt: the acknowledgment is lost (transport timeout).
    server.set_route("POST", PUBLISH_PATH, FixtureOutcome.timeout())
    first = publisher.publish_pending(A)
    assert first[0].outcome is PublishOutcome.RETRY_TIMEOUT
    assert first[0].retryable
    assert first[0].idempotency_key == event_id
    key_attempt_1 = _idem_of(server.last_call)

    # The event is untouched: still PENDING, no delivered_at, SAME event id — never a fresh key.
    with session_factory() as check:
        row = check.scalars(select(OutboxMessage)).one()
        assert row.status == OutboxStatus.PENDING.value
        assert row.delivered_at is None
        assert row.event_id == event_id

    # 2) Retry: the host is reachable now and acknowledges.
    server.set_route("POST", PUBLISH_PATH, FixtureOutcome.response(200, json={"ok": True}))
    second = publisher.publish_pending(A)
    assert second[0].outcome is PublishOutcome.DELIVERED
    key_attempt_2 = _idem_of(server.last_call)

    # BOTH attempts carried the SAME idempotency key — the fake host records ONE logical event.
    assert key_attempt_1 == key_attempt_2 == event_id
    assert server.call_count == 2  # two HTTP attempts...
    posts = [c for c in server.calls if c.method == "POST"]
    assert {_idem_of(c) for c in posts} == {event_id}  # ...one logical event

    with session_factory() as check:
        row = check.scalars(select(OutboxMessage)).one()
        assert row.status == OutboxStatus.DELIVERED.value
        assert row.event_id == event_id


def test_unreachable_is_retryable_and_changes_no_state(session_factory, server, publisher):
    _stage(session_factory, A, "cand-3:accept")
    server.set_route("POST", PUBLISH_PATH, FixtureOutcome.unreachable())

    results = publisher.publish_pending(A)

    assert results[0].outcome is PublishOutcome.RETRY_UNREACHABLE
    assert results[0].retryable
    with session_factory() as check:
        row = check.scalars(select(OutboxMessage)).one()
        assert row.status == OutboxStatus.PENDING.value
        assert row.delivered_at is None


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (500, PublishOutcome.RETRY_SERVER_ERROR),
        (503, PublishOutcome.RETRY_SERVER_ERROR),
        (429, PublishOutcome.RETRY_RATE_LIMITED),
    ],
)
def test_retryable_http_responses_keep_pending(session_factory, server, publisher, status, outcome):
    _stage(session_factory, A, f"cand-{status}:accept")
    server.set_route("POST", PUBLISH_PATH, FixtureOutcome.response(status))

    results = publisher.publish_pending(A)

    assert results[0].outcome is outcome
    assert results[0].retryable
    assert results[0].status_code == status
    with session_factory() as check:
        assert check.scalars(select(OutboxMessage)).one().status == OutboxStatus.PENDING.value


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409])
def test_definitive_denial_is_surfaced_and_changes_no_state(
    session_factory, server, publisher, status
):
    _stage(session_factory, A, f"cand-deny-{status}:accept")
    server.set_route("POST", PUBLISH_PATH, FixtureOutcome.response(status, json={"error": "nope"}))

    results = publisher.publish_pending(A)

    assert results[0].outcome is PublishOutcome.DENIED
    assert not results[0].retryable and not results[0].delivered
    assert results[0].status_code == status
    # A denial still never flips state: the row stays PENDING with its identity intact.
    with session_factory() as check:
        row = check.scalars(select(OutboxMessage)).one()
        assert row.status == OutboxStatus.PENDING.value
        assert row.delivered_at is None


# --------------------------------------------------------------------------------------------------
# Scope, batching, selection
# --------------------------------------------------------------------------------------------------


def test_publish_is_scoped_to_one_business(session_factory, server, publisher):
    _stage(session_factory, A, "a:accept")
    b_event = _stage(session_factory, B, "b:accept")
    server.set_route("POST", PUBLISH_PATH, FixtureOutcome.response(200))

    results = publisher.publish_pending(A)

    assert len(results) == 1  # only A's message was drained
    assert all(_idem_of(c) != b_event for c in server.calls)
    with session_factory() as check:
        rows = {r.business_id: r.status for r in check.scalars(select(OutboxMessage)).all()}
        assert rows[A] == OutboxStatus.DELIVERED.value
        assert rows[B] == OutboxStatus.PENDING.value  # B untouched


def test_only_pending_messages_are_published(session_factory, server, publisher):
    _stage(session_factory, A, "one:accept")
    server.set_route("POST", PUBLISH_PATH, FixtureOutcome.response(200))
    publisher.publish_pending(A)  # delivers it

    # A second pass finds nothing PENDING and attempts no call.
    calls_before = server.call_count
    again = publisher.publish_pending(A)
    assert again == []
    assert server.call_count == calls_before


def test_batch_size_caps_a_pass(session_factory, server):
    for i in range(5):
        _stage(session_factory, A, f"c{i}:accept")
    server.set_route("POST", PUBLISH_PATH, FixtureOutcome.response(200))
    publisher = build_outbox_publisher(
        _settings(outbox_publish_batch_size=2), session_factory, transport=server.as_transport()
    )
    assert publisher is not None

    first = publisher.publish_pending(A)
    assert len(first) == 2  # capped
    with session_factory() as check:
        delivered = [
            r
            for r in check.scalars(select(OutboxMessage)).all()
            if r.status == OutboxStatus.DELIVERED.value
        ]
        assert len(delivered) == 2


def test_publish_message_by_id(session_factory, server, publisher):
    with session_factory() as session:
        row = OutboxSink.for_context(session, _ctx(A)).enqueue(
            event_type="candidate.accepted", dedup_key="by-id:accept"
        )
        session.commit()
        row_id, event_id = row.id, row.event_id
    server.set_route("POST", PUBLISH_PATH, FixtureOutcome.response(200))

    res = publisher.publish_message(row_id, A)
    assert res is not None and res.outcome is PublishOutcome.DELIVERED
    assert _idem_of(server.last_call) == event_id
    # Wrong tenant / unknown id => nothing attempted, nothing changed.
    assert publisher.publish_message(row_id, B) is None


def test_publish_requires_a_business_scope(publisher):
    with pytest.raises(ValueError):
        publisher.publish_pending("  ")


# --------------------------------------------------------------------------------------------------
# Disabled-by-default denial — a missing relay contract stays disabled (fail-closed)
# --------------------------------------------------------------------------------------------------


def test_publisher_disabled_without_a_relay_path(session_factory):
    s = _settings(platform_outbox_publish_path=None)
    assert s.outbox_publisher_configured is False
    assert build_outbox_publisher(s, session_factory) is None


def test_publisher_disabled_without_the_platform_transport(session_factory):
    s = _settings(platform_base_url=None, platform_service_credential=None)
    # No transport => the publisher cannot be configured even with a path present.
    assert s.outbox_publisher_configured is False
    assert build_outbox_publisher(s, session_factory) is None


def test_constructor_fails_closed_on_empty_path(session_factory, server):
    transport = PlatformHttpTransport(PLATFORM_BASE, PLATFORM_CRED, transport=server.as_transport())
    with pytest.raises(OutboxPublisherDisabled):
        OutboxPublisher(transport, publish_path="  ", session_factory=session_factory)


def test_relay_path_without_transport_is_rejected_at_boot():
    s = _settings(platform_base_url=None, platform_service_credential=None)
    with pytest.raises(RuntimeError, match="outbox relay path is set"):
        validate_required(s)


def test_nonpositive_batch_size_is_rejected_at_boot():
    with pytest.raises(RuntimeError, match="OUTBOX_PUBLISH_BATCH_SIZE must be positive"):
        validate_required(_settings(outbox_publish_batch_size=0))


# --------------------------------------------------------------------------------------------------
# Lost-ack-then-retry on disposable PostgreSQL — the headline criterion over real transactions
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)
SCHEMA = "dca_outbox_publisher_test"


@pytest.fixture
def pg_factory():
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


@requires_pg
def test_lost_ack_then_retry_is_one_logical_event_on_postgres(pg_factory, server):
    factory = pg_factory
    event_id = _stage(factory, A, "cand-pg:accept", payload={"candidateId": "cand-pg"})
    publisher = build_outbox_publisher(
        _settings(database_url=PG_URL), factory, transport=server.as_transport()
    )
    assert publisher is not None

    # Lost acknowledgment first.
    server.set_route("POST", PUBLISH_PATH, FixtureOutcome.timeout())
    first = publisher.publish_pending(A)
    assert first[0].outcome is PublishOutcome.RETRY_TIMEOUT
    with factory() as check:
        row = check.scalars(select(OutboxMessage)).one()
        assert row.status == OutboxStatus.PENDING.value  # retryable, unchanged
        assert row.event_id == event_id

    # Retry succeeds; identity is preserved and the host saw one idempotency key.
    server.set_route("POST", PUBLISH_PATH, FixtureOutcome.response(200))
    second = publisher.publish_pending(A)
    assert second[0].outcome is PublishOutcome.DELIVERED
    posts = [c for c in server.calls if c.method == "POST"]
    assert {_idem_of(c) for c in posts} == {event_id}
    with factory() as check:
        row = check.scalars(select(OutboxMessage)).one()
        assert row.status == OutboxStatus.DELIVERED.value
        assert row.event_id == event_id
