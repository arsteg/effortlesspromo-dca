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
from app.db.base import Base, make_engine, make_session_factory


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    validate_required(settings)

    engine = make_engine(settings.database_url or "")
    session_factory = make_session_factory(engine)
    if settings.auto_create_schema:
        Base.metadata.create_all(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task: asyncio.Task | None = None
        if settings.worker_enabled:
            from app.workers.scheduler import worker_loop

            task = asyncio.create_task(worker_loop(session_factory, settings))
        yield
        if task is not None:
            task.cancel()

    app = FastAPI(title="EffortlessPromo Data Concierge Agent", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.include_router(health.router)
    app.include_router(tenants.router)
    return app
