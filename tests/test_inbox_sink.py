"""DCA-028 — inbound-event deduplication (inbox / idempotent receiver) sink.

Two layers, matching the DCA-027 outbox-sink suite:

- **Always-on unit tests** (in-memory SQLite) pin the sink's contract: it stages (never commits) a
  PROCESSED receipt keyed by the external ``(event_source, event_id)``, records only opaque
  references, refuses raw secrets/contact data with no row staged, exposes a tenant-scoped fast-path
  dedup check, and the ``(business_id, event_source, event_id)`` uniqueness constraint rejects a
  replay. (A unique constraint is enforced by SQLite too, so the replay *denial* is exercised here;
  the transaction-rollback atomicity below is not.)
- **Transactional handler completion on a disposable PostgreSQL** (``DCA_TEST_DATABASE_URL``) proves
  the headline acceptance criterion: a replay cannot apply the local change twice, and failed
  processing rolls back cleanly so the event can be retried safely. Transactions are not exercised on
  SQLite — this layer skips without a Postgres URL.

Spin up a throwaway Postgres, e.g.:
    docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55474:5432 postgres:16-alpine
    export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55474/dca"
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db.base import Base, make_engine, make_session_factory
from app.db.models import (
    DiscoveryRun,
    InboxReceipt,
    InboxStatus,
    ProspectCandidate,
)
from app.identity.tenant_context import TenantContext
from app.inbox import InboxPayloadError, InboxSink

A = "00000000-0000-4000-8000-00000000000a"
B = "00000000-0000-4000-8000-00000000000b"
SUBJECT = "00000000-0000-4000-9000-0000000a11ce"
SOURCE = "effortlesspromo-api"


def _ctx(business_id: str) -> TenantContext:
    return TenantContext(
        subject=SUBJECT, business_id=business_id, roles=("owner",), membership_version="v1"
    )


# --------------------------------------------------------------------------------------------------
# Always-on unit tests (in-memory SQLite) — the sink contract
# --------------------------------------------------------------------------------------------------


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def test_register_stages_processed_without_committing(session_factory):
    factory = session_factory
    session = factory()
    try:
        sink = InboxSink.for_context(session, _ctx(A))
        row = sink.register(
            event_source=SOURCE,
            event_id="evt-123",
            event_type="candidate.enriched",
            payload={"candidateId": "cand-123"},
        )
        # Staged (processed), NOT yet persisted: a second session sees nothing until the caller commits.
        assert row in session.new
        with factory() as other:
            assert other.scalars(select(InboxReceipt)).all() == []
        session.commit()
    finally:
        session.close()

    with factory() as check:
        rows = check.scalars(select(InboxReceipt)).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.business_id == A  # scope from the context, not a caller argument
        assert row.event_source == SOURCE
        assert row.event_id == "evt-123"
        assert row.event_type == "candidate.enriched"
        assert row.status == InboxStatus.PROCESSED.value
        assert row.processed_at is not None
        assert row.received_at is not None
        assert row.payload == {"candidateId": "cand-123"}


def test_already_processed_is_tenant_scoped_fast_path(session_factory):
    factory = session_factory
    with factory() as session:
        InboxSink.for_context(session, _ctx(A)).register(event_source=SOURCE, event_id="evt-1")
        session.commit()

    with factory() as session:
        # A's receipt is visible to A, not to B; an unseen event returns None.
        sink_a = InboxSink.for_context(session, _ctx(A))
        sink_b = InboxSink.for_context(session, _ctx(B))
        assert sink_a.already_processed(event_source=SOURCE, event_id="evt-1") is not None
        assert sink_a.already_processed(event_source=SOURCE, event_id="evt-2") is None
        assert sink_b.already_processed(event_source=SOURCE, event_id="evt-1") is None


def test_replay_is_rejected_by_uniqueness(session_factory):
    """The headline constraint: the same (business, source, id) cannot be recorded twice."""
    factory = session_factory
    with factory() as session:
        InboxSink.for_context(session, _ctx(A)).register(event_source=SOURCE, event_id="evt-1")
        session.commit()

    # A second, independent unit of work for the SAME event violates (business_id, source, id).
    with factory() as session:
        InboxSink.for_context(session, _ctx(A)).register(
            event_source=SOURCE, event_id="evt-1", payload={"attempt": 2}
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    # Exactly one receipt survived — the replay changed no state.
    with factory() as check:
        rows = check.scalars(select(InboxReceipt)).all()
        assert len(rows) == 1
        assert rows[0].payload == {}  # the first receipt, not the rejected replay


def test_same_event_in_two_businesses_is_allowed(session_factory):
    # The constraint is PER business: two tenants may legitimately receive the same source + id.
    factory = session_factory
    with factory() as session:
        InboxSink.for_context(session, _ctx(A)).register(event_source=SOURCE, event_id="evt-1")
        InboxSink.for_context(session, _ctx(B)).register(event_source=SOURCE, event_id="evt-1")
        session.commit()  # no collision across tenants
    with factory() as check:
        assert len(check.scalars(select(InboxReceipt)).all()) == 2


@pytest.mark.parametrize(
    "payload",
    [
        {"owner": "founder@acme.co.in"},  # email by content
        {"note": "call +91 98765 43210 today"},  # phone by content
        {"email": "x"},  # sensitive key
        {"contact": {"phone": "x"}},  # nested sensitive key
        {"meta": ["ok", "reach me at founder@acme.co.in"]},  # email nested in a list
    ],
)
def test_register_refuses_raw_contact_or_secret(session_factory, payload):
    factory = session_factory
    session = factory()
    try:
        sink = InboxSink.for_context(session, _ctx(A))
        with pytest.raises(InboxPayloadError):
            sink.register(event_source=SOURCE, event_id="evt-1", payload=payload)
        # Refused BEFORE staging: nothing pending.
        assert list(session.new) == []
        session.commit()
    finally:
        session.close()
    with factory() as check:
        assert check.scalars(select(InboxReceipt)).all() == []


def test_register_refuses_contact_data_in_source_id_or_type(session_factory):
    session = session_factory()
    try:
        sink = InboxSink.for_context(session, _ctx(A))
        with pytest.raises(InboxPayloadError):
            sink.register(event_source=SOURCE, event_id="founder@acme.co.in")
        with pytest.raises(InboxPayloadError):
            sink.register(event_source="founder@acme.co.in", event_id="evt-1")
        with pytest.raises(InboxPayloadError):
            sink.register(
                event_source=SOURCE, event_id="evt-1", event_type="notify founder@acme.co.in"
            )
        assert list(session.new) == []
    finally:
        session.close()


def test_register_requires_scope_source_and_id(session_factory):
    session = session_factory()
    try:
        with pytest.raises(ValueError):
            InboxSink(session).register(event_source=SOURCE, event_id="evt-1")  # no scope bound
        with pytest.raises(ValueError):
            InboxSink(session, business_id=A).register(event_source="  ", event_id="evt-1")
        with pytest.raises(ValueError):
            InboxSink(session, business_id=A).register(event_source=SOURCE, event_id="  ")
        with pytest.raises(ValueError):
            InboxSink(session, business_id=A).already_processed(event_source=SOURCE, event_id="  ")
        assert list(session.new) == []
    finally:
        session.close()


# --------------------------------------------------------------------------------------------------
# Transactional handler completion on disposable PostgreSQL — the headline acceptance criterion
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)
SCHEMA = "dca_inbox_test"


@pytest.fixture
def pg_factory():
    """A sync session factory over a fresh schema on the disposable Postgres (superuser connection —
    this layer exercises TRANSACTION atomicity, which the RLS tests already cover separately)."""
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


def _seed_candidate(factory) -> str:
    with factory() as session:
        run = DiscoveryRun(business_id=A, status="completed")
        session.add(run)
        session.flush()
        cand = ProspectCandidate(
            business_id=A,
            run_id=run.id,
            org_name="Synthetic Org",
            org_identifier="SYN-1",
            source_key="synthetic",
            source_ref="synthetic://1",
            trust_tier="official_registry",
            status="suggested",
        )
        session.add(cand)
        session.commit()
        return cand.id


def _handle_event(factory, candidate_id: str, event_id: str, *, fail: bool = False) -> bool:
    """One idempotent-receiver unit of work: skip if already handled, else mutate + record the
    receipt and commit both together. Returns True if the local change was applied this call."""
    with factory() as session:
        sink = InboxSink.for_context(session, _ctx(A))
        if sink.already_processed(event_source=SOURCE, event_id=event_id) is not None:
            return False  # replay fast-path: do no handler work
        cand = session.get(ProspectCandidate, candidate_id)
        cand.status = "accepted"
        sink.register(event_source=SOURCE, event_id=event_id, payload={"candidateId": candidate_id})
        if fail:
            session.rollback()  # simulate a downstream failure after staging both
            return False
        session.commit()
        return True


@requires_pg
def test_replay_does_not_apply_the_local_change_twice(pg_factory):
    factory = pg_factory
    candidate_id = _seed_candidate(factory)

    # First delivery handles the event; the second (a replay) is short-circuited by the fast path.
    assert _handle_event(factory, candidate_id, "evt-1") is True
    assert _handle_event(factory, candidate_id, "evt-1") is False

    with factory() as check:
        cand = check.get(ProspectCandidate, candidate_id)
        assert cand.status == "accepted"
        receipts = check.scalars(select(InboxReceipt)).all()
        assert len(receipts) == 1  # exactly one receipt — the event was applied exactly once
        assert receipts[0].status == InboxStatus.PROCESSED.value


@requires_pg
def test_replay_without_fast_path_is_rejected_at_commit(pg_factory):
    """Even if a caller skips the fast-path check and re-registers, the unique constraint rejects the
    replay at commit and the rolled-back transaction leaves the local change applied exactly once."""
    factory = pg_factory
    candidate_id = _seed_candidate(factory)
    assert _handle_event(factory, candidate_id, "evt-1") is True

    with factory() as session:
        cand = session.get(ProspectCandidate, candidate_id)
        cand.status = "re-accepted"  # a second application attempt
        InboxSink.for_context(session, _ctx(A)).register(event_source=SOURCE, event_id="evt-1")
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    with factory() as check:
        cand = check.get(ProspectCandidate, candidate_id)
        assert cand.status == "accepted"  # the second application rolled back
        assert len(check.scalars(select(InboxReceipt)).all()) == 1


@requires_pg
def test_failed_processing_can_retry_safely(pg_factory):
    factory = pg_factory
    candidate_id = _seed_candidate(factory)

    # A failed attempt rolls back BOTH the local change and the receipt — nothing is left behind.
    assert _handle_event(factory, candidate_id, "evt-1", fail=True) is False
    with factory() as check:
        cand = check.get(ProspectCandidate, candidate_id)
        assert cand.status == "suggested"  # local change rolled back
        assert check.scalars(select(InboxReceipt)).all() == []  # receipt rolled back too

    # The retry sees no prior receipt and applies the change cleanly, exactly once.
    assert _handle_event(factory, candidate_id, "evt-1") is True
    with factory() as check:
        cand = check.get(ProspectCandidate, candidate_id)
        assert cand.status == "accepted"
        assert len(check.scalars(select(InboxReceipt)).all()) == 1
