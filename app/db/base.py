"""Engine/session plumbing for the DCA-owned database.

The DCA never writes to any other service's database (polyrepo rule, ADR-0005 / DCA boundary) —
``database_url`` must point at a database owned by this service. SQLite works for dev/test;
PostgreSQL (``postgresql+psycopg://``) for real deployments.
"""

from __future__ import annotations

from sqlalchemy import create_engine
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
