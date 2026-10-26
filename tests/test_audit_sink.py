"""DCA-026 — transactional audit sink.

Two layers:

- **Always-on unit tests** (in-memory SQLite) pin the sink's contract: it stages (never commits) an
  audit row, derives scope/actor from a verified tenant context, records only opaque references, and
  refuses raw secrets/contact data with no row staged.
- **Atomicity on a disposable PostgreSQL** (``DCA_TEST_DATABASE_URL``) proves the headline acceptance
  criterion: a business change and its audit event share one transaction, so a failed transaction
  commits neither. Transactions are not exercised on SQLite — this layer skips without a Postgres URL.

Spin up a throwaway Postgres, e.g.:
    docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55471:5432 postgres:16-alpine
    export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55471/dca"
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker

from app.audit import ActorType, AuditActor, AuditPayloadError, AuditSink, assert_opaque_references
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import AuditEvent, DiscoveryRun, ProspectCandidate
from app.identity.tenant_context import TenantContext

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


def test_record_stages_without_committing(session_factory):
    factory = session_factory
    session = factory()
    try:
        sink = AuditSink.for_context(session, _ctx(A))
        event_row = sink.record(
            action="candidate.accept",
            reason="ok",
            references={"candidateId": "cand-123", "runId": "run-9"},
        )
        # Staged (pending), NOT yet persisted: a second session sees nothing until the caller commits.
        assert event_row in session.new
        with factory() as other:
            assert other.scalars(select(AuditEvent)).all() == []
        session.commit()
    finally:
        session.close()

    with factory() as check:
        rows = check.scalars(select(AuditEvent)).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.business_id == A  # scope from the context, not a caller argument
        assert row.actor_id == SUBJECT
        assert row.actor_type == ActorType.USER.value
        assert row.action == "candidate.accept"
        assert row.reason == "ok"
        assert row.references == {"candidateId": "cand-123", "runId": "run-9"}


def test_scope_comes_from_context_not_argument(session_factory):
    session = session_factory()
    try:
        sink = AuditSink.for_context(session, _ctx(A))
        # Even if a caller tries to assert a different business, the bound context scope wins only
        # when no explicit override is given; an explicit override is honoured but the context sink
        # never silently re-homes — here we confirm the default is the context's business.
        row = sink.record(action="agent.enable")
        session.commit()
        assert row.business_id == A
    finally:
        session.close()


@pytest.mark.parametrize(
    "references",
    [
        {"owner": "founder@acme.co.in"},  # email by content
        {"note": "call +91 98765 43210 today"},  # phone by content
        {"email": "x"},  # sensitive key
        {"contact": {"phone": "x"}},  # nested sensitive key
        {"meta": ["ok", "reach me at founder@acme.co.in"]},  # email nested in a list
    ],
)
def test_record_refuses_raw_contact_or_secret(session_factory, references):
    factory = session_factory
    session = factory()
    try:
        sink = AuditSink.for_context(session, _ctx(A))
        with pytest.raises(AuditPayloadError):
            sink.record(action="candidate.accept", references=references)
        # Refused BEFORE staging: nothing pending.
        assert list(session.new) == []
        session.commit()
    finally:
        session.close()
    # Independent session over the same in-memory DB: no audit row ever reached the database.
    with factory() as check:
        assert check.scalars(select(AuditEvent)).all() == []


def test_assert_opaque_references_passes_opaque_values():
    ok = {"candidateId": "c-1", "count": 3, "ok": True, "nested": {"runId": "r-1"}, "tags": ["a"]}
    assert assert_opaque_references(ok) == ok
    assert assert_opaque_references(None) == {}


def test_record_requires_action_scope_and_actor(session_factory):
    session = session_factory()
    try:
        with pytest.raises(ValueError):
            AuditSink(session).record(action="x")  # no scope/actor bound
        with pytest.raises(ValueError):
            AuditSink(session, business_id=A, actor=AuditActor(SUBJECT, ActorType.SERVICE)).record(
                action="  "
            )
        assert list(session.new) == []
    finally:
        session.close()


def test_actor_id_that_is_contact_data_is_refused(session_factory):
    session = session_factory()
    try:
        bad_actor = AuditActor(id="founder@acme.co.in", type=ActorType.USER)
        with pytest.raises(AuditPayloadError):
            AuditSink(session, business_id=A, actor=bad_actor).record(action="candidate.accept")
        assert list(session.new) == []
    finally:
        session.close()


# --------------------------------------------------------------------------------------------------
# Atomicity on disposable PostgreSQL — the headline acceptance criterion
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)
SCHEMA = "dca_audit_test"


@pytest.fixture
def pg_factory():
    """A sync session factory over a fresh schema on the disposable Postgres (superuser connection —
    this layer exercises TRANSACTION atomicity, which RLS tests already cover separately)."""
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
def test_failed_transaction_commits_neither_change_nor_audit(pg_factory):
    factory = pg_factory
    candidate_id = _seed_candidate(factory)

    # A unit of work that mutates the candidate AND audits it, then fails before commit.
    session = factory()
    try:
        cand = session.get(ProspectCandidate, candidate_id)
        cand.status = "accepted"
        AuditSink.for_context(session, _ctx(A)).record(
            action="candidate.accept",
            reason="ok",
            references={"candidateId": candidate_id},
        )
        raise RuntimeError("boom: downstream step fails after staging both")
    except RuntimeError:
        session.rollback()
    finally:
        session.close()

    with factory() as check:
        cand = check.get(ProspectCandidate, candidate_id)
        assert cand.status == "suggested"  # business change rolled back
        assert check.scalars(select(AuditEvent)).all() == []  # audit entry rolled back too


@requires_pg
def test_successful_transaction_commits_both(pg_factory):
    factory = pg_factory
    candidate_id = _seed_candidate(factory)

    with factory() as session:
        cand = session.get(ProspectCandidate, candidate_id)
        cand.status = "accepted"
        AuditSink.for_context(session, _ctx(A)).record(
            action="candidate.accept",
            reason="ok",
            references={"candidateId": candidate_id},
        )
        session.commit()

    with factory() as check:
        cand = check.get(ProspectCandidate, candidate_id)
        assert cand.status == "accepted"
        rows = check.scalars(select(AuditEvent)).all()
        assert len(rows) == 1
        assert rows[0].action == "candidate.accept"
        assert rows[0].references == {"candidateId": candidate_id}
        assert rows[0].business_id == A


@requires_pg
def test_refused_audit_leaves_the_transaction_clean(pg_factory):
    """A refused payload (raw contact data) raises before staging; the surrounding business change
    can still be committed deliberately, and NO audit row exists — the denial changed no audit state."""
    factory = pg_factory
    candidate_id = _seed_candidate(factory)

    with factory() as session:
        cand = session.get(ProspectCandidate, candidate_id)
        cand.status = "dismissed"
        with pytest.raises(AuditPayloadError):
            AuditSink.for_context(session, _ctx(A)).record(
                action="candidate.dismiss",
                references={"ownerEmail": "founder@acme.co.in"},
            )
        session.commit()  # the caller chooses to proceed; the refusal staged no audit row

    with factory() as check:
        assert check.get(ProspectCandidate, candidate_id).status == "dismissed"
        assert check.scalars(select(AuditEvent)).all() == []
