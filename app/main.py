"""DCA application factory — fail-fast config, own DB, optional worker loop.

Run: ``uvicorn app.asgi:app --reload --port 8100`` (the AI service conventionally uses 8000).
The factory lives here import-side-effect-free so tests build apps with explicit Settings.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.idempotency import install_idempotency
from app.api.routes import (
    auth,
    authz,
    budget,
    deletion_propagation,
    grievances,
    health,
    icp,
    privacy_erasure,
    privacy_scope,
    profiles,
    reacquisition,
    records,
    service_integration,
    targeting,
    tenants,
)
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
from app.identity import IdentityContractDisabled, build_identity_client
from app.identity.contract import IdentityClient
from app.identity.service_credentials import build_service_credential_verifier
from app.identity.tokens import build_token_verifier
from app.integration.deletion_fake import build_deletion_destination_client
from app.services.entitlement_gate import default_entitlement_client
from app.services.profile_importer import default_platform_profile_client


def _resolve_identity_client(settings: Settings) -> IdentityClient | None:
    """The identity client for this deployment, or ``None`` when the contract is disabled.

    DCA-016 resolves tenant scope through this client. Only the dev/test fake exists today; the live
    platform adapter (B1) is unbuilt, so production has no client and tenant resolution fails closed
    (every scoped call 403) rather than silently trusting a header.
    """
    try:
        return build_identity_client(settings)
    except IdentityContractDisabled:
        return None


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
    # DCA-015: the end-user token verifier (None when the contract is disabled — fail-closed).
    app.state.token_verifier = build_token_verifier(settings)
    # DCA-016: the identity client used to verify membership before resolving a tenant scope
    # (None when the contract is disabled — tenant resolution then fails closed).
    app.state.identity_client = _resolve_identity_client(settings)
    # DCA-020: the scoped service-credential verifier (None when disabled — fail-closed).
    app.state.service_credential_verifier = build_service_credential_verifier(settings)
    # DCA-023: the entitlement client used to gate chargeable operations (None when the contract is
    # disabled — the gate then fails closed).
    app.state.entitlement_client = default_entitlement_client(settings)
    # DCA-037: the platform-profile client used to import an authorized profile snapshot into a
    # provenance-bearing draft (None when the contract is disabled — the import then fails closed).
    app.state.platform_profile_client = default_platform_profile_client(settings)
    # DCA-085: the external-deletion destination client used to propagate an erasure outward (None
    # when the contract is disabled — production, until the live adapter ships — so propagation fails
    # closed rather than acknowledging against a synthetic destination).
    app.state.deletion_destination_client = build_deletion_destination_client(settings)
    # DCA-012: resolve/publish the per-request correlation id (reaches endpoints, handlers, logs).
    app.add_middleware(CorrelationIdMiddleware)
    # DCA-011: one standardized error envelope + correlation id for every error response.
    install_error_handling(app)
    # DCA-035: replay a known idempotent request's stored response verbatim (registered after the
    # envelope handlers so the specific IdempotencyReplay type wins over the catch-all).
    install_idempotency(app)
    app.include_router(health.router)
    app.include_router(tenants.router)
    app.include_router(profiles.router)
    app.include_router(icp.router)
    app.include_router(records.router)
    app.include_router(targeting.router)
    app.include_router(auth.router)
    app.include_router(authz.router)
    app.include_router(budget.router)
    app.include_router(grievances.router)
    app.include_router(privacy_scope.router)
    app.include_router(privacy_erasure.router)
    app.include_router(deletion_propagation.router)
    app.include_router(reacquisition.router)
    app.include_router(service_integration.router)
    return app
