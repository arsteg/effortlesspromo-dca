"""DCA application factory — fail-fast config, own DB, optional worker loop.

Run: ``uvicorn app.asgi:app --reload --port 8100`` (the AI service conventionally uses 8000).
The factory lives here import-side-effect-free so tests build apps with explicit Settings.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import health, tenants
from app.core.config import Settings, get_settings, validate_required
from app.core.errors import install_error_handling
from app.core.logging import CorrelationIdMiddleware, configure_logging
from app.db.base import (
    Base,
    is_async_capable,
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    validate_required(settings)

    # DCA-012: structured, correlation-stamped logging with central redaction. Register this
    # instance's secret values so their literals can never surface in a log line.
    configure_logging(settings)

    database_url = settings.database_url or ""
    engine = make_engine(database_url)
    session_factory = make_session_factory(engine)
    if settings.auto_create_schema:
        Base.metadata.create_all(engine)

    # DCA-006: an async session factory, one-session-per-request/task, for the DCA-owned PostgreSQL.
    # SQLite dev/test has no first-class async driver here, so it stays on the sync path (None).
    async_engine = make_async_engine(database_url) if is_async_capable(database_url) else None
    async_session_factory = (
        make_async_session_factory(async_engine) if async_engine is not None else None
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task: asyncio.Task | None = None
        if settings.worker_enabled:
            from app.workers.scheduler import worker_loop

            task = asyncio.create_task(worker_loop(session_factory, settings))
        yield
        if task is not None:
            task.cancel()
        if async_engine is not None:
            await async_engine.dispose()

    app = FastAPI(title="EffortlessPromo Data Concierge Agent", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.async_engine = async_engine
    app.state.async_session_factory = async_session_factory
    # DCA-012: resolve/publish the per-request correlation id (reaches endpoints, handlers, logs).
    app.add_middleware(CorrelationIdMiddleware)
    # DCA-011: one standardized error envelope + correlation id for every error response.
    install_error_handling(app)
    app.include_router(health.router)
    app.include_router(tenants.router)
    return app
