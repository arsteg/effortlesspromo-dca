"""DCA-035 — the request-idempotency store.

Two layers, matching the DCA-027 outbox / DCA-028 inbox suites:

- **Always-on unit tests** (in-memory SQLite) pin the store's contract: a digest is a stable
  fingerprint of the body; ``reconcile`` returns ``None`` for a new request, the stored record for a
  byte-identical replay, and raises :class:`IdempotencyConflict` for a same-key/different-body reuse;
  the ``(business_id, method, route, key)`` uniqueness constraint rejects a duplicate; and a key never
  collides across tenants or routes. (A unique constraint is enforced by SQLite too, so the
  duplicate-key *denial* is exercised here; the transaction-rollback atomicity below is not.)
- **Atomicity on a disposable PostgreSQL** (``DCA_TEST_DATABASE_URL``) proves the write and its
  idempotency record share one transaction (rolling back the change removes its record) and that a
  concurrent duplicate loses the unique-constraint race — the lock behind a safe retry.

Spin up a throwaway Postgres, e.g.:
    docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55475:5432 postgres:16-alpine
    export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55475/dca"
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db.base import Base, make_engine, make_session_factory
from app.db.models import DiscoveryRun, IdempotencyRecord, ProspectCandidate
from app.idempotency import IdempotencyConflict, IdempotencyStore, request_digest

A = "00000000-0000-4000-8000-00000000000a"
B = "00000000-0000-4000-8000-00000000000b"
ROUTE = "/v1/tenants/{business_id}/config"


# --------------------------------------------------------------------------------------------------
# Always-on unit tests (in-memory SQLite) — the store contract
# --------------------------------------------------------------------------------------------------


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def test_request_digest_is_stable_and_body_sensitive():
    assert request_digest(b'{"a":1}') == request_digest(b'{"a":1}')  # same bytes => same digest
    assert request_digest(b'{"a":1}') != request_digest(
        b'{"a":2}'
    )  # any change => different digest
    assert request_digest(b"") == request_digest(None)  # empty/absent body hashes deterministically


def test_reconcile_new_request_returns_none(session_factory):
    with session_factory() as session:
        store = IdempotencyStore.for_scope(session, A)
        assert store.reconcile(method="PUT", route=ROUTE, key="k1", digest="d1") is None


def test_same_key_same_body_replays_the_stored_result(session_factory):
    factory = session_factory
    digest = request_digest(b'{"dataMode":"my_data_only"}')
    with factory() as session:
        IdempotencyStore.for_scope(session, A).remember(
            method="PUT",
            route=ROUTE,
            key="k1",
            request_digest=digest,
            response_status=200,
            response_body={"businessId": A, "dataMode": "my_data_only"},
        )
        session.commit()

    with factory() as session:
        record = IdempotencyStore.for_scope(session, A).reconcile(
            method="PUT", route=ROUTE, key="k1", digest=digest
        )
        assert record is not None
        assert record.response_status == 200
        assert record.response_body == {"businessId": A, "dataMode": "my_data_only"}


def test_same_key_different_body_is_rejected(session_factory):
    factory = session_factory
    with factory() as session:
        IdempotencyStore.for_scope(session, A).remember(
            method="PUT",
            route=ROUTE,
            key="k1",
            request_digest=request_digest(b'{"dataMode":"my_data_only"}'),
            response_status=200,
            response_body={"ok": True},
        )
        session.commit()

    with factory() as session:
        store = IdempotencyStore.for_scope(session, A)
        with pytest.raises(IdempotencyConflict):
            store.reconcile(
                method="PUT",
                route=ROUTE,
                key="k1",
                digest=request_digest(b'{"dataMode":"suggest_platform_data"}'),
            )


def test_same_key_does_not_collide_across_tenants(session_factory):
    factory = session_factory
    with factory() as session:
        IdempotencyStore.for_scope(session, A).remember(
            method="PUT", route=ROUTE, key="shared", request_digest="da", response_status=200
        )
        IdempotencyStore.for_scope(session, B).remember(
            method="PUT", route=ROUTE, key="shared", request_digest="db", response_status=201
        )
        session.commit()  # both survive — the key is per-tenant, no collision

    with factory() as session:
        a = IdempotencyStore.for_scope(session, A).lookup(method="PUT", route=ROUTE, key="shared")
        b = IdempotencyStore.for_scope(session, B).lookup(method="PUT", route=ROUTE, key="shared")
        assert a is not None and b is not None
        assert a.response_status == 200 and b.response_status == 201
        # B's reconcile with B's digest replays B's row; A's digest would be a conflict FOR B.
        assert (
            IdempotencyStore.for_scope(session, B).reconcile(
                method="PUT", route=ROUTE, key="shared", digest="db"
            )
            is b
        )


def test_same_key_does_not_collide_across_routes(session_factory):
    factory = session_factory
    with factory() as session:
        store = IdempotencyStore.for_scope(session, A)
        store.remember(method="POST", route="/r1", key="k", request_digest="d", response_status=200)
        store.remember(method="POST", route="/r2", key="k", request_digest="d", response_status=200)
        store.remember(method="PUT", route="/r1", key="k", request_digest="d", response_status=200)
        session.commit()  # same key, three different (method, route) scopes — all allowed

    with factory() as session:
        assert session.query(IdempotencyRecord).count() == 3


def test_duplicate_key_same_scope_is_constrained(session_factory):
    factory = session_factory
    with factory() as session:
        IdempotencyStore.for_scope(session, A).remember(
            method="PUT", route=ROUTE, key="k1", request_digest="d", response_status=200
        )
        session.commit()
    with factory() as session:
        IdempotencyStore.for_scope(session, A).remember(
            method="PUT", route=ROUTE, key="k1", request_digest="d", response_status=200
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
    with factory() as check:
        assert check.query(IdempotencyRecord).count() == 1


def test_remember_requires_scope_method_route_key_and_digest(session_factory):
    with session_factory() as session:
        with pytest.raises(ValueError):
            IdempotencyStore(session).remember(  # no scope bound
                method="PUT", route=ROUTE, key="k", request_digest="d", response_status=200
            )
        store = IdempotencyStore.for_scope(session, A)
        with pytest.raises(ValueError):
            store.remember(
                method="  ", route=ROUTE, key="k", request_digest="d", response_status=200
            )
        with pytest.raises(ValueError):
            store.remember(method="PUT", route="  ", key="k", request_digest="d", response_status=1)
        with pytest.raises(ValueError):
            store.remember(
                method="PUT", route=ROUTE, key=" ", request_digest="d", response_status=1
            )
        with pytest.raises(ValueError):
            store.remember(
                method="PUT", route=ROUTE, key="k", request_digest=" ", response_status=1
            )
        assert list(session.new) == []


# --------------------------------------------------------------------------------------------------
# Atomicity on disposable PostgreSQL — the write and its record share one transaction
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)
SCHEMA = "dca_idem_test"


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
def test_rolling_back_the_change_removes_its_idempotency_record(pg_factory):
    factory = pg_factory
    candidate_id = _seed_candidate(factory)

    # A unit of work that accepts the candidate AND records its idempotency outcome, then fails.
    session = factory()
    try:
        cand = session.get(ProspectCandidate, candidate_id)
        cand.status = "accepted"
        IdempotencyStore.for_scope(session, A).remember(
            method="POST",
            route="/v1/tenants/{business_id}/candidates/{candidate_id}/accept",
            key="accept-1",
            request_digest=request_digest(b""),
            response_status=200,
            response_body={"id": candidate_id, "status": "accepted"},
        )
        raise RuntimeError("boom: downstream step fails after staging both")
    except RuntimeError:
        session.rollback()
    finally:
        session.close()

    with factory() as check:
        cand = check.get(ProspectCandidate, candidate_id)
        assert cand.status == "suggested"  # change rolled back
        assert check.scalars(select(IdempotencyRecord)).all() == []  # record rolled back with it


@requires_pg
def test_concurrent_duplicate_loses_the_unique_constraint_race(pg_factory):
    factory = pg_factory
    candidate_id = _seed_candidate(factory)

    # First writer commits the accept + its record.
    with factory() as first:
        cand = first.get(ProspectCandidate, candidate_id)
        cand.status = "accepted"
        IdempotencyStore.for_scope(first, A).remember(
            method="POST",
            route="/accept",
            key="accept-1",
            request_digest=request_digest(b""),
            response_status=200,
            response_body={"status": "accepted"},
        )
        first.commit()

    # A duplicate retry stages the SAME key; the unique constraint rejects it at commit, so its own
    # (duplicate) change rolls back and the winner's result can be replayed instead.
    with factory() as second:
        cand = second.get(ProspectCandidate, candidate_id)
        cand.status = "accepted"  # the duplicate's would-be change
        IdempotencyStore.for_scope(second, A).remember(
            method="POST",
            route="/accept",
            key="accept-1",
            request_digest=request_digest(b""),
            response_status=200,
            response_body={"status": "accepted"},
        )
        with pytest.raises(IntegrityError):
            second.commit()
        second.rollback()
        replay = IdempotencyStore.for_scope(second, A).reconcile(
            method="POST", route="/accept", key="accept-1", digest=request_digest(b"")
        )
        assert replay is not None and replay.response_body == {"status": "accepted"}

    with factory() as check:
        assert len(check.scalars(select(IdempotencyRecord)).all()) == 1  # exactly one record
