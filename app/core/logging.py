"""DCA-012 — structured, correlation-stamped application logging with central redaction.

Two pieces, one policy:

- **Structured correlation logging.** Every log record is emitted as a single JSON line carrying a
  ``correlationId`` taken from the per-request context (set by :class:`CorrelationIdMiddleware`, which
  reuses the inbound ``X-Correlation-Id`` / ``X-Request-Id`` or mints one — the same id the DCA-011
  error envelope returns, so a log line and an error response can be joined).
- **Central redaction.** One :class:`Redactor` scrubs *secrets* (registered token values) and
  *personal contact fields* (emails, phone numbers) out of every record — the rendered message, any
  structured ``extra`` fields, and the formatted traceback — before it is ever written. Redaction is
  applied in the logging layer, not at each call site, so no future ``log.info(...)`` can leak by
  forgetting to scrub first.

This module adds NO metrics (out of scope for DCA-012) and performs no I/O beyond writing log lines.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import uuid
from contextvars import ContextVar, Token
from typing import IO, Any

# Correlation id header names — the canonical source other modules import (avoids an errors<->logging
# import cycle). ``X-Correlation-Id`` is preferred; ``X-Request-Id`` is the inbound id the .NET API /
# effortlesspromo-ai already use, so a trace spans services.
CORRELATION_HEADER = "X-Correlation-Id"
REQUEST_ID_HEADER = "X-Request-Id"

_correlation_id: ContextVar[str | None] = ContextVar("dca_correlation_id", default=None)


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def set_correlation_id(cid: str) -> Token[str | None]:
    return _correlation_id.set(cid)


def reset_correlation_id(token: Token[str | None]) -> None:
    _correlation_id.reset(token)


def new_correlation_id() -> str:
    return uuid.uuid4().hex


# --------------------------------------------------------------------------------------------------
# Redaction policy
# --------------------------------------------------------------------------------------------------

REDACTED = "[redacted]"
EMAIL_MASK = "[redacted-email]"
PHONE_MASK = "[redacted-phone]"

# Structured-field keys whose VALUE is always a secret or personal contact, redacted whole regardless
# of its text (so e.g. a short token that the pattern scan would miss is still removed).
SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "auth",
        "token",
        "service_auth_token",
        "x_service_auth",
        "x-service-auth",
        "ai_service_token",
        "api_key",
        "apikey",
        "secret",
        "password",
        "passwd",
        "pwd",
        "contact",
        "email",
        "phone",
        "mobile",
        "msisdn",
    }
)

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Broad candidate for a phone-like run of digits/separators; confirmed only when it carries enough
# digits (>=10) so ISO dates / short ids are not mistaken for phone numbers.
_PHONE_CANDIDATE_RE = re.compile(r"(?<!\w)\+?\d[\d\s().\-]{7,}\d(?!\w)")
_MIN_PHONE_DIGITS = 10


def _mask_phone(match: re.Match[str]) -> str:
    text = match.group()
    digits = sum(ch.isdigit() for ch in text)
    return PHONE_MASK if digits >= _MIN_PHONE_DIGITS else text


class Redactor:
    """Central, process-wide scrubber. Registered secret values are matched literally; emails and
    phone numbers are matched by shape. Over-redaction is deliberately preferred to a leak."""

    # A registered secret shorter than this is too ambiguous to match literally (it would blanket
    # false-positive on ordinary words); such secrets are only ever removed via SENSITIVE_KEYS.
    _MIN_SECRET_LEN = 6

    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        self._secrets: set[str] = set()
        for secret in secrets:
            self.register_secret(secret)

    def register_secret(self, value: str | None) -> None:
        text = (value or "").strip()
        if len(text) >= self._MIN_SECRET_LEN:
            self._secrets.add(text)

    def redact_text(self, text: str) -> str:
        if not text:
            return text
        # Longest secrets first so a secret that contains another is not partially unmasked.
        for secret in sorted(self._secrets, key=len, reverse=True):
            if secret in text:
                text = text.replace(secret, REDACTED)
        text = _EMAIL_RE.sub(EMAIL_MASK, text)
        text = _PHONE_CANDIDATE_RE.sub(_mask_phone, text)
        return text

    def redact_value(self, key: str, value: Any) -> Any:
        """Redact a structured field by key (sensitive keys dropped whole) then by content."""
        if key.lower() in SENSITIVE_KEYS:
            return REDACTED
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, dict):
            return {k: self.redact_value(str(k), v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact_value(key, v) for v in value]
        return value


# --------------------------------------------------------------------------------------------------
# Formatter + filter
# --------------------------------------------------------------------------------------------------

# Attributes LogRecord sets itself; everything else on a record is caller-supplied ``extra`` and is
# emitted (redacted) as a structured field.
_RESERVED = frozenset(
    vars(logging.makeLogRecord({})).keys() | {"message", "asctime", "correlation_id", "taskName"}
)


class StructuredFormatter(logging.Formatter):
    """Render one redacted JSON object per record. Message, extras, and traceback all pass the
    redactor; the traceback is kept (operators need it) but scrubbed, never dropped."""

    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "correlationId": getattr(record, "correlation_id", None) or "-",
            "message": self._redactor.redact_text(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = self._redactor.redact_value(key, value)
        if record.exc_info:
            payload["exception"] = (
                record.exc_info[0].__name__ if record.exc_info[0] else "Exception"
            )
            payload["traceback"] = self._redactor.redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, ensure_ascii=False)


class CorrelationFilter(logging.Filter):
    """Stamp each record with the current request's correlation id (``-`` outside a request)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "correlation_id"):
            record.correlation_id = get_correlation_id() or "-"
        return True


# --------------------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------------------

_LOGGER_NAME = "dca"
_HANDLER_TAG = "_dca_handler"
_default_redactor = Redactor()


def get_redactor() -> Redactor:
    """The process-wide redactor (also usable directly, e.g. to scrub before a non-log sink)."""
    return _default_redactor


def configure_logging(
    settings: Any | None = None,
    *,
    stream: IO[str] | None = None,
    level: str | int = "INFO",
) -> Redactor:
    """Install the structured, redacting handler on the ``dca`` logger tree. Idempotent: a repeat
    call (e.g. another ``create_app`` in tests) replaces the handler rather than stacking them.

    Secret *values* from settings are registered with the redactor so their literals never appear in
    a log line. Nothing here reads a secret from anywhere but the ``Settings`` object it is handed.
    """
    redactor = _default_redactor
    if settings is not None:
        redactor.register_secret(getattr(settings, "service_auth_token", None))
        redactor.register_secret(getattr(settings, "ai_service_token", None))
        redactor.register_secret(getattr(settings, "auth_token_signing_secret", None))

    logger = logging.getLogger(_LOGGER_NAME)
    # Drop any handler a previous configure_logging installed so handlers never accumulate.
    for stale in [h for h in logger.handlers if getattr(h, _HANDLER_TAG, False)]:
        logger.removeHandler(stale)

    handler: logging.Handler = logging.StreamHandler(stream or sys.stderr)
    setattr(handler, _HANDLER_TAG, True)
    handler.setFormatter(StructuredFormatter(redactor))
    handler.addFilter(CorrelationFilter())
    logger.addHandler(handler)
    logger.setLevel(level)
    # Own handler only: don't double-emit through the root logger (which may be unredacted).
    logger.propagate = False
    return redactor


# --------------------------------------------------------------------------------------------------
# Correlation middleware (pure ASGI, so the contextvar reaches endpoints and exception handlers)
# --------------------------------------------------------------------------------------------------


class CorrelationIdMiddleware:
    """Resolve one correlation id per request, publish it to the logging contextvar and to
    ``scope['state']`` (where the DCA-011 error handlers read it), and echo it on the response.

    Implemented as raw ASGI rather than ``BaseHTTPMiddleware`` on purpose: a contextvar set inside a
    ``BaseHTTPMiddleware`` does not propagate into the endpoint's task, so request logs would lose the
    id. A raw ASGI middleware runs in the same task, so every log emitted while handling the request
    carries the correlation id.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])
        }
        inbound = (
            headers.get(CORRELATION_HEADER.lower()) or headers.get(REQUEST_ID_HEADER.lower()) or ""
        ).strip()
        cid = inbound or new_correlation_id()
        scope.setdefault("state", {})["correlation_id"] = cid
        token = set_correlation_id(cid)

        async def send_wrapper(message: dict) -> None:
            if message["type"] == "http.response.start":
                raw_headers = message.setdefault("headers", [])
                key = CORRELATION_HEADER.encode("latin-1")
                if not any(existing.lower() == key.lower() for existing, _ in raw_headers):
                    raw_headers.append((key, cid.encode("latin-1")))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            reset_correlation_id(token)
