"""Engine/session plumbing for the DCA-owned database.

The DCA never writes to any other service's database (polyrepo rule, ADR-0005 / DCA boundary) —
``database_url`` must point at a database owned by this service. SQLite works for dev/test;
PostgreSQL (``postgresql+psycopg://``) for real deployments.

Two engines coexist deliberately:
- the **sync** engine (``make_engine`` / ``make_session_factory``) backs the existing SQLite-friendly
  dev/test surface;
- the **async** engine (``make_async_engine`` / ``make_async_session_factory``) is the DCA-006
  per-request/per-task session for the DCA-owned PostgreSQL. It reuses the psycopg3 driver (the
  ``postgres`` extra) — SQLAlchemy drives it in async mode, no second driver needed.

Async is only wired for PostgreSQL (``is_async_capable``); SQLite has no first-class async driver
here and dev/test stay on the sync path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str):
    kwargs: dict = {}
    if database_url.startswith("sqlite"):
        # One shared in-memory DB across threads (TestClient + worker) in dev/test.
        kwargs = {"connect_args": {"check_same_thread": False}}
        if ":memory:" in database_url:
            kwargs["poolclass"] = StaticPool
    return create_engine(database_url, **kwargs)


def make_session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def is_async_capable(database_url: str) -> bool:
    """Only PostgreSQL gets an async engine in this service (the DCA-owned datastore)."""
    return database_url.startswith("postgresql")


def _async_url(database_url: str) -> str:
    """Normalise a PostgreSQL URL onto the async-capable psycopg3 driver.

    An explicit ``postgresql+<driver>://`` is honoured as-is; a bare ``postgresql://`` is pinned to
    ``+psycopg`` so it never silently loads a sync-only driver (e.g. psycopg2) under an async engine.
    """
    if database_url.startswith("postgresql+"):
        return database_url
    if database_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + database_url[len("postgresql://") :]
    return database_url


def make_async_engine(database_url: str) -> AsyncEngine:
    # pool_pre_ping keeps long-lived worker connections healthy across idle gaps.
    return create_async_engine(_async_url(database_url), pool_pre_ping=True)


def make_async_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def transactional_session(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """One session, one explicit transaction scope.

    Each entry builds its OWN session (never shared between concurrent tasks). The body commits on
    success; on any exception the work is rolled back **before** the connection is closed/returned to
    the pool, so a failed unit of work can never leave a half-applied transaction on a pooled
    connection (DCA-006 acceptance).
    """
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
