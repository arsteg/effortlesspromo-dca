"""DCA-006 — isolated async database sessions.

Transaction / isolation / rollback behaviour is verified against a **disposable PostgreSQL**
(`DCA_TEST_DATABASE_URL`), never SQLite — SQLite's transaction/locking semantics would not prove the
acceptance criteria. Those tests skip when no Postgres URL is supplied so the default network-free
suite still runs; the wiring-guard tests below always run.

Spin up a throwaway Postgres, e.g.:
    docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55432:5432 postgres:16-alpine
    export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55432/dca"
"""

from __future__ import annotations

import asyncio
import os
import sys
import types
import uuid

import pytest
from fastapi import HTTPException

# psycopg3's async driver cannot run on Windows' default ProactorEventLoop; the real deployment is
# Linux (where this is a non-issue), so for local Windows test runs we pin the selector loop policy.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.api.deps import get_async_session
from app.db.base import (
    Base,
    is_async_capable,
    make_async_engine,
    make_async_session_factory,
    make_engine,
    transactional_session,
)
from app.db.models import TenantAgent

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)


# --- always-on wiring guards (no database needed) ---


def test_is_async_capable_only_for_postgres():
    assert is_async_capable("postgresql+psycopg://u:p@h/db") is True
    assert is_async_capable("postgresql://u:p@h/db") is True
    assert is_async_capable("sqlite+pysqlite:///:memory:") is False


def test_async_dependency_fail_closed_when_not_configured():
    # SQLite dev/test has no async engine => the async dependency is disabled, never served wrong.
    req = types.SimpleNamespace(
        app=types.SimpleNamespace(state=types.SimpleNamespace(async_session_factory=None))
    )

    async def drive():
        gen = get_async_session(req)
        with pytest.raises(HTTPException) as exc:
            await gen.__anext__()
        assert exc.value.status_code == 503

    asyncio.run(drive())


# --- Postgres-backed transaction/isolation behaviour ---


@pytest.fixture
def pg_factory():
    # Tables created via the sync engine against the same DB (no Alembic yet); async engine reads/writes.
    sync_engine = make_engine(PG_URL)
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()
    async_engine = make_async_engine(PG_URL)
    factory = make_async_session_factory(async_engine)
    yield factory
    asyncio.run(async_engine.dispose())


@requires_pg
def test_sessions_are_not_shared_between_concurrent_tasks(pg_factory):
    seen_ids: list[int] = []

    async def unit():
        async with transactional_session(pg_factory) as session:
            seen_ids.append(id(session))
            await asyncio.sleep(0.02)  # force genuine interleaving
            return id(session)

    async def run():
        return await asyncio.gather(unit(), unit(), unit())

    ids = asyncio.run(run())
    assert len(set(ids)) == 3  # each task got its own session instance
    assert len(set(seen_ids)) == 3


@requires_pg
def test_two_concurrent_transactions_one_fails_other_commits(pg_factory):
    good_id = f"ok-{uuid.uuid4().hex[:16]}"  # business_id is String(36)
    bad_id = f"fail-{uuid.uuid4().hex[:16]}"

    async def commit_one():
        async with transactional_session(pg_factory) as session:
            session.add(TenantAgent(business_id=good_id, country="IN", enabled=False))

    async def fail_one():
        async with transactional_session(pg_factory) as session:
            session.add(TenantAgent(business_id=bad_id, country="IN", enabled=False))
            await asyncio.sleep(0.02)
            raise ValueError("synthetic failure after write")

    async def run():
        results = await asyncio.gather(commit_one(), fail_one(), return_exceptions=True)
        # The failing unit surfaces its error; the other is unaffected.
        assert isinstance(results[1], ValueError)
        # Fresh session on a returned-to-pool connection: committed row present, rolled-back absent.
        from sqlalchemy import select

        async with transactional_session(pg_factory) as session:
            rows = (await session.execute(select(TenantAgent.business_id))).scalars().all()
        assert good_id in rows
        assert bad_id not in rows

    asyncio.run(run())


@requires_pg
def test_async_dependency_rolls_back_on_endpoint_error(pg_factory):
    biz = f"dep-{uuid.uuid4().hex[:16]}"  # business_id is String(36)
    req = types.SimpleNamespace(
        app=types.SimpleNamespace(state=types.SimpleNamespace(async_session_factory=pg_factory))
    )

    async def run():
        gen = get_async_session(req)
        session = await gen.__anext__()
        session.add(TenantAgent(business_id=biz, country="IN", enabled=False))
        # Simulate FastAPI propagating an endpoint exception into the dependency's teardown.
        with pytest.raises(ValueError):
            await gen.athrow(ValueError("boom"))
        # The write must have rolled back before the connection was returned.
        from sqlalchemy import select

        async with transactional_session(pg_factory) as session2:
            rows = (await session2.execute(select(TenantAgent.business_id))).scalars().all()
        assert biz not in rows

    asyncio.run(run())
