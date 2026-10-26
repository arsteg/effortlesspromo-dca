"""DCA-011 — one standardized error envelope for every error this service returns.

Every error response shares a single shape::

    {"error": {"code": "<stable_reason_code>", "message": "<safe message>", "correlationId": "<id>"}}

(Validation failures add a PII-safe ``fields`` list: location + issue only, never the submitted value.)

Guarantees the handlers below enforce:
- **Stable reason code** — derived from the HTTP status (``not_found``, ``conflict``,
  ``validation_failed``, ``internal_error`` …), so a caller branches on a constant, not on prose.
  Mirrors the ``{"error": {...}}`` wrapper the sibling effortlesspromo-ai service already returns.
- **Safe message** — deliberate rejections (``HTTPException``) keep their own short detail string
  (route authors only ever put status words / field names there, never a value or personal data);
  unexpected failures return a single fixed generic message.
- **Correlation id** — echoed in the body and the ``X-Correlation-Id`` response header, reused from
  the inbound ``X-Correlation-Id`` / ``X-Request-Id`` header when present (so a trace spans services)
  or freshly minted otherwise.
- **No leakage on the unexpected path** — the catch-all handler never puts a stack trace, exception
  text, or request data in the response; the traceback is logged server-side under the correlation
  id for operators instead.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import (
    CORRELATION_HEADER,
    REQUEST_ID_HEADER,
    new_correlation_id,
    set_correlation_id,
)

log = logging.getLogger("dca.api")

# The only message a client ever sees for an *unexpected* failure. Fixed, carries nothing.
INTERNAL_ERROR_MESSAGE = "internal server error"


class ErrorCode:
    """Stable reason codes. Part of the surface callers depend on — treat as constants, not text."""

    BAD_REQUEST = "bad_request"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    CONFLICT = "conflict"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    UNPROCESSABLE = "unprocessable_entity"
    VALIDATION_FAILED = "validation_failed"
    RATE_LIMITED = "rate_limited"
    SERVICE_UNAVAILABLE = "service_unavailable"
    INTERNAL_ERROR = "internal_error"


_STATUS_TO_CODE: dict[int, str] = {
    400: ErrorCode.BAD_REQUEST,
    401: ErrorCode.UNAUTHORIZED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    409: ErrorCode.CONFLICT,
    413: ErrorCode.PAYLOAD_TOO_LARGE,
    422: ErrorCode.UNPROCESSABLE,
    429: ErrorCode.RATE_LIMITED,
    503: ErrorCode.SERVICE_UNAVAILABLE,
}

_DEFAULT_MESSAGES: dict[int, str] = {
    400: "bad request",
    401: "unauthorized",
    403: "forbidden",
    404: "not found",
    405: "method not allowed",
    409: "conflict",
    413: "payload too large",
    422: "unprocessable entity",
    429: "too many requests",
    503: "service unavailable",
}


def code_for_status(status_code: int) -> str:
    """Map an HTTP status to its stable reason code; 5xx falls back to the generic server code."""
    if status_code in _STATUS_TO_CODE:
        return _STATUS_TO_CODE[status_code]
    return ErrorCode.INTERNAL_ERROR if status_code >= 500 else ErrorCode.BAD_REQUEST


def resolve_correlation_id(request: Request) -> str:
    """The request's correlation id: normally already set on ``request.state`` by
    :class:`~app.core.logging.CorrelationIdMiddleware`; fall back to the inbound header (or a fresh
    id) if an error is raised before that middleware ran. Always (re)publishes it to the logging
    contextvar so the record this handler emits carries the id even on the 500 path, where the
    outermost server-error middleware runs after the correlation middleware has reset the contextvar.
    """
    existing = getattr(request.state, "correlation_id", None)
    if existing:
        set_correlation_id(existing)
        return existing
    inbound = (
        request.headers.get(CORRELATION_HEADER) or request.headers.get(REQUEST_ID_HEADER) or ""
    ).strip()
    cid = inbound or new_correlation_id()
    request.state.correlation_id = cid
    set_correlation_id(cid)
    return cid


def _envelope(status_code: int, code: str, message: str, cid: str, **extra: object) -> JSONResponse:
    error: dict[str, object] = {"code": code, "message": message, "correlationId": cid}
    error.update(extra)
    return JSONResponse(
        status_code=status_code,
        content={"error": error},
        headers={CORRELATION_HEADER: cid},
    )


def install_error_handling(app: FastAPI) -> None:
    """Wire the three exception handlers onto the envelope. The per-request correlation id is
    published by :class:`~app.core.logging.CorrelationIdMiddleware` (added in ``create_app``)."""

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        # A deliberate, controlled rejection: keep the route's own detail (safe by construction) as
        # the message. Preserve any headers the framework attached (e.g. Allow on a 405).
        cid = resolve_correlation_id(request)
        detail = exc.detail
        message = detail if isinstance(detail, str) and detail.strip() else None
        if message is None:
            message = _DEFAULT_MESSAGES.get(exc.status_code, "error")
        response = _envelope(exc.status_code, code_for_status(exc.status_code), message, cid)
        for key, value in (getattr(exc, "headers", None) or {}).items():
            response.headers[key] = value
        return response

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Expose only the field location and the constraint message — never the submitted ``input``
        # value or ``ctx`` (those can carry personal data). This is the one place the default
        # FastAPI 422 body would otherwise echo the request payload back to the caller.
        cid = resolve_correlation_id(request)
        fields = [
            {
                "field": ".".join(str(part) for part in err.get("loc", ())),
                "issue": str(err.get("msg", "invalid")),
            }
            for err in exc.errors()
        ]
        return _envelope(
            422, ErrorCode.VALIDATION_FAILED, "request validation failed", cid, fields=fields
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        # The only path that must never leak: log the full traceback server-side under the
        # correlation id for operators, return a fixed generic message with no exception text.
        cid = resolve_correlation_id(request)
        log.error(
            "unhandled error [%s] on %s %s: %s",
            cid,
            request.method,
            request.url.path,
            type(exc).__name__,
            exc_info=exc,
        )
        return _envelope(500, ErrorCode.INTERNAL_ERROR, INTERNAL_ERROR_MESSAGE, cid)
