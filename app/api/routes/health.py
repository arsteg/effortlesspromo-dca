"""Liveness/readiness — unauthenticated (no business data), same split as the other services."""

from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import text

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
def live() -> dict:
    return {"status": "ok"}


@router.get("/ready")
def ready(request: Request) -> dict:
    with request.app.state.engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"status": "ready"}
