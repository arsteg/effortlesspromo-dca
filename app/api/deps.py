"""Request-scoped dependencies. The session factories live on app.state (wired in main.create_app).

Two flavours:
- ``get_session`` — the existing sync session (SQLite-friendly dev/test surface).
- ``get_async_session`` — the DCA-006 async session: exactly one session per request, wrapped in an
  explicit transaction scope that commits on success and rolls back on failure before the connection
  returns to the pool. Available only when an async (PostgreSQL) engine is configured.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db.base import transactional_session


def get_session(request: Request) -> Iterator[Session]:
    session: Session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


async def get_async_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory = getattr(request.app.state, "async_session_factory", None)
    if factory is None:
        # Fail-closed: no async datastore configured (e.g. SQLite dev) => the capability is disabled,
        # never silently served against the wrong engine.
        raise HTTPException(status_code=503, detail="async database not configured")
    async with transactional_session(factory) as session:
        yield session
