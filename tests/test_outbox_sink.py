"""DCA-027 — transactional outbox sink.

Two layers, matching the DCA-026 audit-sink suite:

- **Always-on unit tests** (in-memory SQLite) pin the sink's contract: it stages (never commits) a
  PENDING outbox row with a STABLE, deterministic event id, records only opaque references, refuses
  raw secrets/contact data with no row staged, and the ``(business_id, event_id)`` uniqueness
  constraint rejects a duplicate logical intent. (A unique constraint is enforced by SQLite too, so
  the duplicate-intent *denial* is exercised here; the transaction-rollback atomicity below is not.)
- **Atomicity on a disposable PostgreSQL** (``DCA_TEST_DATABASE_URL``) proves the headline acceptance
  criterion: a local change and its outbox intent share one transaction, so rolling back the local
  change also removes its outbox intent. Transactions are not exercised on SQLite — this layer skips
  without a Postgres URL.

Spin up a throwaway Postgres, e.g.:
    docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55473:5432 postgres:16-alpine
    export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55473/dca"
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
    OutboxMessage,
    OutboxStatus,
    ProspectCandidate,
    outbox_event_id,
)
from app.identity.tenant_context import TenantContext
from app.outbox import OutboxPayloadError, OutboxSink

A = "00000000-0000-4000-8000-00000000000a"
B = "00000000-0000-4000-8000-00000000000b"
SUBJECT = "00000000-0000-4000-9000-0000000a11ce"


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


def test_enqueue_stages_pending_without_committing(session_factory):
    factory = session_factory
    session = factory()
    try:
        sink = OutboxSink.for_context(session, _ctx(A))
        row = sink.enqueue(
            event_type="candidate.accepted",
            dedup_key="cand-123:accept",
            payload={"candidateId": "cand-123", "runId": "run-9"},
        )
        # Staged (pending), NOT yet persisted: a second session sees nothing until the caller commits.
        assert row in session.new
        with factory() as other:
            assert other.scalars(select(OutboxMessage)).all() == []
        session.commit()
    finally:
        session.close()

    with factory() as check:
        rows = check.scalars(select(OutboxMessage)).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.business_id == A  # scope from the context, not a caller argument
        assert row.event_type == "candidate.accepted"
        assert row.dedup_key == "cand-123:accept"
        assert row.status == OutboxStatus.PENDING.value
        assert row.delivered_at is None
        assert row.version == 1
        assert row.payload == {"candidateId": "cand-123", "runId": "run-9"}
        # Event id is the STABLE derivation of the logical intent, not the random row id.
        assert row.event_id == outbox_event_id(A, "candidate.accepted", "cand-123:accept")
        assert row.event_id != row.id


def test_event_id_is_stable_and_scoped_per_business():
    # Deterministic for the same intent; different per business even with identical type+key.
    a1 = outbox_event_id(A, "candidate.accepted", "cand-1:accept")
    a2 = outbox_event_id(A, "candidate.accepted", "cand-1:accept")
    b1 = outbox_event_id(B, "candidate.accepted", "cand-1:accept")
    assert a1 == a2
    assert a1 != b1


def test_duplicate_logical_intent_is_rejected(session_factory):
    """The headline constraint: the same (business, event_type, dedup_key) cannot be staged twice."""
    factory = session_factory
    with factory() as session:
        OutboxSink.for_context(session, _ctx(A)).enqueue(
            event_type="candidate.accepted", dedup_key="cand-1:accept"
        )
        session.commit()

    # A second, independent unit of work for the SAME logical intent violates (business_id, event_id).
    with factory() as session:
        OutboxSink.for_context(session, _ctx(A)).enqueue(
            event_type="candidate.accepted", dedup_key="cand-1:accept", payload={"attempt": 2}
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    # Exactly one row survived — the duplicate changed no state.
    with factory() as check:
        rows = check.scalars(select(OutboxMessage)).all()
        assert len(rows) == 1
        assert (
            rows[0].payload == {}
        )  # the first (payload-less) row, not the rejected second attempt


def test_same_intent_in_two_businesses_is_allowed(session_factory):
    # The constraint is PER business: two tenants may legitimately hold the same logical intent.
    factory = session_factory
    with factory() as session:
        OutboxSink.for_context(session, _ctx(A)).enqueue(
            event_type="candidate.accepted", dedup_key="cand-1:accept"
        )
        OutboxSink.for_context(session, _ctx(B)).enqueue(
            event_type="candidate.accepted", dedup_key="cand-1:accept"
        )
        session.commit()  # no collision across tenants
    with factory() as check:
        assert len(check.scalars(select(OutboxMessage)).all()) == 2


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
def test_enqueue_refuses_raw_contact_or_secret(session_factory, payload):
    factory = session_factory
    session = factory()
    try:
        sink = OutboxSink.for_context(session, _ctx(A))
        with pytest.raises(OutboxPayloadError):
            sink.enqueue(
                event_type="candidate.accepted", dedup_key="cand-1:accept", payload=payload
            )
        # Refused BEFORE staging: nothing pending.
        assert list(session.new) == []
        session.commit()
    finally:
        session.close()
    with factory() as check:
        assert check.scalars(select(OutboxMessage)).all() == []


def test_enqueue_refuses_contact_data_in_type_or_key(session_factory):
    session = session_factory()
    try:
        sink = OutboxSink.for_context(session, _ctx(A))
        with pytest.raises(OutboxPayloadError):
            sink.enqueue(event_type="candidate.accepted", dedup_key="founder@acme.co.in")
        assert list(session.new) == []
    finally:
        session.close()


def test_enqueue_requires_scope_type_and_key(session_factory):
    session = session_factory()
    try:
        with pytest.raises(ValueError):
            OutboxSink(session).enqueue(event_type="x", dedup_key="k")  # no scope bound
        with pytest.raises(ValueError):
            OutboxSink(session, business_id=A).enqueue(event_type="  ", dedup_key="k")
        with pytest.raises(ValueError):
            OutboxSink(session, business_id=A).enqueue(event_type="x", dedup_key="  ")
        with pytest.raises(ValueError):
            OutboxSink(session, business_id=A).enqueue(event_type="x", dedup_key="k", version=0)
        assert list(session.new) == []
    finally:
        session.close()


def test_mark_delivered_transitions_status(session_factory):
    factory = session_factory
    with factory() as session:
        row = OutboxSink.for_context(session, _ctx(A)).enqueue(
            event_type="candidate.accepted", dedup_key="cand-1:accept"
        )
        session.commit()
        row_id = row.id

    with factory() as session:
        sink = OutboxSink.for_context(session, _ctx(A))
        msg = session.get(OutboxMessage, row_id)
        sink.mark_delivered(msg)
        session.commit()

    with factory() as check:
        msg = check.get(OutboxMessage, row_id)
        assert msg.status == OutboxStatus.DELIVERED.value
        assert msg.delivered_at is not None


# --------------------------------------------------------------------------------------------------
# Atomicity on disposable PostgreSQL — the headline acceptance criterion
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)
SCHEMA = "dca_outbox_test"


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


@requires_pg
def test_rolling_back_the_local_change_removes_its_outbox_intent(pg_factory):
    factory = pg_factory
    candidate_id = _seed_candidate(factory)

    # A unit of work that mutates the candidate AND stages its outbox intent, then fails before commit.
    session = factory()
    try:
        cand = session.get(ProspectCandidate, candidate_id)
        cand.status = "accepted"
        OutboxSink.for_context(session, _ctx(A)).enqueue(
            event_type="candidate.accepted",
            dedup_key=f"{candidate_id}:accept",
            payload={"candidateId": candidate_id},
        )
        raise RuntimeError("boom: downstream step fails after staging both")
    except RuntimeError:
        session.rollback()
    finally:
        session.close()

    with factory() as check:
        cand = check.get(ProspectCandidate, candidate_id)
        assert cand.status == "suggested"  # local change rolled back
        assert check.scalars(select(OutboxMessage)).all() == []  # outbox intent rolled back too


@requires_pg
def test_successful_transaction_commits_both(pg_factory):
    factory = pg_factory
    candidate_id = _seed_candidate(factory)

    with factory() as session:
        cand = session.get(ProspectCandidate, candidate_id)
        cand.status = "accepted"
        OutboxSink.for_context(session, _ctx(A)).enqueue(
            event_type="candidate.accepted",
            dedup_key=f"{candidate_id}:accept",
            payload={"candidateId": candidate_id},
        )
        session.commit()

    with factory() as check:
        cand = check.get(ProspectCandidate, candidate_id)
        assert cand.status == "accepted"
        rows = check.scalars(select(OutboxMessage)).all()
        assert len(rows) == 1
        assert rows[0].event_type == "candidate.accepted"
        assert rows[0].status == OutboxStatus.PENDING.value
        assert rows[0].payload == {"candidateId": candidate_id}
        assert rows[0].business_id == A


@requires_pg
def test_duplicate_intent_rejected_on_postgres(pg_factory):
    factory = pg_factory
    candidate_id = _seed_candidate(factory)

    with factory() as session:
        OutboxSink.for_context(session, _ctx(A)).enqueue(
            event_type="candidate.accepted", dedup_key=f"{candidate_id}:accept"
        )
        session.commit()

    with factory() as session:
        OutboxSink.for_context(session, _ctx(A)).enqueue(
            event_type="candidate.accepted", dedup_key=f"{candidate_id}:accept"
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    with factory() as check:
        assert len(check.scalars(select(OutboxMessage)).all()) == 1
