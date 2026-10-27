"""DCA-035 — the HTTP seam for request idempotency.

Wires :class:`~app.idempotency.store.IdempotencyStore` into the service's write routes with three
small pieces:

- :func:`idempotency` — a FastAPI dependency. It reads the ``Idempotency-Key`` header and the request
  body, resolves the tenant scope (path ``business_id`` → body ``businessId`` → a platform sentinel),
  and *before the handler runs* decides the request's fate: a byte-identical replay raises
  :class:`IdempotencyReplay` (the stored response is returned verbatim, no handler, no state change);
  a same-key/different-body reuse raises ``409``. A new request returns an :class:`IdempotencyContext`
  the handler uses to record its outcome.
- :class:`IdempotencyContext` + :func:`commit_idempotent` — the handler builds its result, then calls
  :func:`commit_idempotent`, which stages the idempotency record and commits it in the SAME unit of
  work as the business change (atomic). If a concurrent duplicate won the unique-constraint race, the
  commit fails, this request rolls back its own duplicate change, and the winner's stored result is
  replayed — so the effect is applied exactly once.
- :func:`install_idempotency` — registers the :class:`IdempotencyReplay` → response handler.

A request with no ``Idempotency-Key`` is served normally (the context is a no-op): idempotency is an
opt-in protection a client requests per write, exactly like the platform's ``Idempotency-Key`` (BR-124).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.core.errors import resolve_correlation_id
from app.core.logging import CORRELATION_HEADER
from app.idempotency.store import (
    IDEMPOTENCY_HEADER,
    IdempotencyConflict,
    IdempotencyStore,
    request_digest,
)

# A stable reason code for a key reused with a different body (part of the caller-facing contract).
REASON_KEY_REUSE = "idempotency_key_reuse"
# The scope used for a write that names no business (there is none on this surface today, but it keeps
# the store's non-null scope honest rather than inventing a tenant). A real business is always used by
# the current write routes.
PLATFORM_SCOPE = "__platform__"


class IdempotencyReplay(Exception):
    """A prior identical request was found — return its stored response verbatim (no handler runs).

    Carries the stored status + body so the registered handler can reproduce the first call's result
    exactly, without the endpoint executing (and therefore without any second state change).
    """

    def __init__(self, status_code: int, body: dict) -> None:
        super().__init__("idempotent replay")
        self.status_code = status_code
        self.body = body


def _resolve_scope(request: Request, body: bytes) -> str:
    """The tenant scope for this write: the path ``business_id`` if present, else a ``businessId``
    field in the JSON body (the register route carries it there), else the platform sentinel."""
    path_business = request.path_params.get("business_id")
    if path_business:
        return str(path_business)
    if body:
        try:
            parsed = json.loads(body)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            candidate = parsed.get("businessId")
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return PLATFORM_SCOPE


def _route_template(request: Request) -> str:
    """The route template (e.g. ``/v1/tenants/{business_id}/enable``) when the router exposes it, so
    the key is scoped to the logical route rather than a business-specific path; else the raw path."""
    route = request.scope.get("route")
    template = getattr(route, "path", None)
    return template or request.url.path


@dataclass
class IdempotencyContext:
    """What a write handler receives: the bound store (or ``None`` when no key was supplied) plus the
    resolved method/route/key/digest for this request. ``active`` is true only when a key was given."""

    store: IdempotencyStore | None
    method: str
    route: str
    key: str | None
    digest: str

    @property
    def active(self) -> bool:
        return self.store is not None and bool(self.key)

    def _stage(self, response_body: dict, status_code: int) -> None:
        if not self.active:
            return
        assert self.store is not None and self.key is not None
        self.store.remember(
            method=self.method,
            route=self.route,
            key=self.key,
            request_digest=self.digest,
            response_status=status_code,
            response_body=response_body,
        )

    def _reconcile_after_race(self) -> None:
        """A commit raced a concurrent duplicate and failed. Re-read the now-present record and either
        replay it (same body) or reject the reuse (different body)."""
        if not self.active:
            return
        assert self.store is not None and self.key is not None
        try:
            existing = self.store.reconcile(
                method=self.method, route=self.route, key=self.key, digest=self.digest
            )
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=REASON_KEY_REUSE) from exc
        if existing is not None:
            raise IdempotencyReplay(existing.response_status, dict(existing.response_body or {}))


async def idempotency(
    request: Request, session: Annotated[Session, Depends(get_session)]
) -> IdempotencyContext:
    """Resolve this request's idempotency context, replaying or rejecting a known key before the
    handler runs. Shares the handler's session (FastAPI caches ``get_session`` per request), so the
    record the handler stages commits atomically with its change."""
    key = (request.headers.get(IDEMPOTENCY_HEADER) or "").strip() or None
    body = await request.body()
    digest = request_digest(body)
    method = request.method.upper()
    route = _route_template(request)

    if key is None:
        # No key supplied: serve normally, record nothing (opt-in protection, like BR-124).
        return IdempotencyContext(store=None, method=method, route=route, key=None, digest=digest)

    store = IdempotencyStore.for_scope(session, _resolve_scope(request, body))
    try:
        existing = store.reconcile(method=method, route=route, key=key, digest=digest)
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=REASON_KEY_REUSE) from exc
    if existing is not None:
        raise IdempotencyReplay(existing.response_status, dict(existing.response_body or {}))
    return IdempotencyContext(store=store, method=method, route=route, key=key, digest=digest)


def commit_idempotent(
    session: Session, ctx: IdempotencyContext, result: dict, *, status_code: int = 200
) -> dict:
    """Stage the idempotency record (if a key was supplied) and commit it atomically with the handler's
    change. On the unique-constraint race the duplicate change is rolled back and the winner's stored
    result is replayed. Returns ``result`` so a handler can ``return commit_idempotent(...)``."""
    ctx._stage(result, status_code)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        ctx._reconcile_after_race()
        # Not an idempotency collision (no matching record appeared) — surface the real failure.
        raise
    return result


def install_idempotency(app: FastAPI) -> None:
    """Register the replay handler: an :class:`IdempotencyReplay` returns the stored response verbatim,
    carrying the request's correlation id like every other response in the DCA-011 envelope."""

    @app.exception_handler(IdempotencyReplay)
    async def _replay_handler(request: Request, exc: IdempotencyReplay) -> JSONResponse:
        cid = resolve_correlation_id(request)
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.body,
            headers={CORRELATION_HEADER: cid, "Idempotent-Replay": "true"},
        )
