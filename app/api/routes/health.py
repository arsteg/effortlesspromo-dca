"""Liveness/readiness — unauthenticated (no business data), same split as the other services.

Liveness (``/health/live``) proves the process is up and must never depend on the database or any
integration. Readiness (``/health/ready``, DCA-013) proves the required local dependencies — the
DCA-owned database — are healthy; it returns 503 (and ``status: not_ready``) when they are not, so an
orchestrator drains this instance without killing a process that is merely waiting on its DB.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from app.core.readiness import build_readiness

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
def live() -> dict:
    return {"status": "ok"}


@router.get("/ready")
def ready(request: Request, response: Response) -> dict:
    report, is_ready = build_readiness(request.app.state.settings, request.app.state.engine)
    if not is_ready:
        response.status_code = 503
    return report
