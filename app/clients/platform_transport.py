"""DCA-032 — the platform HTTP transport (the *mechanism* for calling the .NET platform API).

This is a hardened, generic HTTP transport and **nothing more**. It has no knowledge of suppression,
CRM, entitlement, delivery, or any other platform capability — those are **business adapters** that
live separately and stay **disabled** until DCA-001 blocker **B1** (a scoped service-to-service
contract into the .NET API) is designed and accepted. Keeping the transport free of business logic is
deliberate: one reviewable mechanism, many adapters later.

What the transport guarantees (the DCA-032 acceptance surface):

- **Allowlisted base URL.** Every call is pinned to the single configured platform origin
  (scheme + host + port) and its base path. A path that would resolve *outside* that endpoint — an
  absolute URL, a protocol-relative ``//other-host`` reference, or a ``..`` traversal escaping the
  base path — is **refused before any socket is opened** (:class:`PlatformEndpointNotAllowed`), so a
  misdirected call can never leave the box. There is no off-allowlist escape via a redirect either:
  redirects are **not** followed.
- **Scoped credentials, never leaked.** A scoped credential is mandatory (fail-closed: no credential
  ⇒ the transport refuses to construct). The credential is registered with the central DCA-012
  redactor at construction, so its literal can never appear in a log line, and it is never placed in
  an exception message or ``repr``.
- **Explicit timeouts.** Separate connect and overall timeouts are always set — no unbounded wait.
- **Correlation ids.** Every request carries ``X-Correlation-Id`` + ``X-Request-Id``, reusing the
  current request's correlation id when the caller does not supply one, so a trace spans services.
- **Safe, stable error mapping.** Transport failures map to a small, stable classification:
  **every** timeout (connect/read/write/pool) classifies identically as ``timeout``; other network
  failures as ``unreachable``. HTTP responses map to a stable :class:`ResponseClass`. The mapping is
  by exception/status *shape*, not by prose, so a caller branches on a constant.

No provider SDKs/keys, no model routing, no second AI gateway, and no writes to another service's
database live here (DCA boundary). This module performs I/O only against the one allowlisted origin.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Self

import httpx

from app.core.logging import (
    CORRELATION_HEADER,
    REQUEST_ID_HEADER,
    get_correlation_id,
    get_redactor,
    new_correlation_id,
)

log = logging.getLogger("dca.platform")

# Response bodies are stored only up to this many characters — a defensive cap so a pathological
# platform response can never balloon a transport result (the structured ``json`` is kept in full).
_MAX_TEXT = 2000


class TransportErrorKind(str, Enum):
    """Stable classification of a *transport* failure (no HTTP response was received).

    The two values are intentionally coarse and shape-derived: a caller recovers from a ``timeout``
    (retry with the same correlation id) differently from a hard ``unreachable``, but never needs to
    know whether a timeout happened in the connect or the read phase — so both classify identically.
    """

    TIMEOUT = "timeout"
    UNREACHABLE = "unreachable"


class ResponseClass(str, Enum):
    """Stable classification of a received HTTP response, derived from its status code."""

    SUCCESS = "success"  # 2xx
    UNAUTHORIZED = "unauthorized"  # 401
    FORBIDDEN = "forbidden"  # 403
    RATE_LIMITED = "rate_limited"  # 429
    CLIENT_ERROR = "client_error"  # other 3xx/4xx (redirects are not followed)
    SERVER_ERROR = "server_error"  # 5xx


def classify_status(status_code: int) -> ResponseClass:
    """Map an HTTP status to its stable response class."""
    if 200 <= status_code < 300:
        return ResponseClass.SUCCESS
    if status_code == 401:
        return ResponseClass.UNAUTHORIZED
    if status_code == 403:
        return ResponseClass.FORBIDDEN
    if status_code == 429:
        return ResponseClass.RATE_LIMITED
    if 500 <= status_code < 600:
        return ResponseClass.SERVER_ERROR
    return ResponseClass.CLIENT_ERROR


def classify_exception(exc: httpx.HTTPError) -> TransportErrorKind:
    """Map an httpx transport error to a stable kind.

    ``httpx.TimeoutException`` is the common base of ``ConnectTimeout``/``ReadTimeout``/
    ``WriteTimeout``/``PoolTimeout`` — classifying on the base makes the timeout verdict identical
    regardless of which phase timed out. Everything else is an ``unreachable`` network failure.
    """
    if isinstance(exc, httpx.TimeoutException):
        return TransportErrorKind.TIMEOUT
    return TransportErrorKind.UNREACHABLE


class PlatformTransportDisabled(Exception):
    """The platform transport is not configured (no base URL / no scoped credential) — the whole
    capability is off (fail-closed). Mirrors :class:`~app.clients.ai_client.AiDisabled`."""


class PlatformEndpointNotAllowed(Exception):
    """A requested path resolves outside the configured platform endpoint. Raised **before** any
    network I/O, so the refusal is reproducible and changes nothing."""


class PlatformTransportError(Exception):
    """A transport-level failure (no HTTP response). Carries a stable :attr:`kind`; the message is
    deliberately free of any request detail or credential — only the classification."""

    def __init__(self, kind: TransportErrorKind, message: str) -> None:
        self.kind = kind
        super().__init__(f"{kind.value}: {message}")


@dataclass(frozen=True)
class TransportResult:
    """A received HTTP response, classified. Returned faithfully to the caller (an adapter) — the
    transport does not interpret the body; it only classifies the outcome and echoes the correlation
    id used for the exchange."""

    status_code: int
    response_class: ResponseClass
    correlation_id: str
    url: str
    json: Any | None = None
    text: str = ""

    @property
    def is_success(self) -> bool:
        return self.response_class is ResponseClass.SUCCESS


class PlatformHttpTransport:
    """A single-origin, credential-scoped, timeout-bounded HTTP transport to the platform API.

    Construct once per process (or per configured credential) and reuse. Fail-closed: a missing base
    URL or credential raises :class:`PlatformTransportDisabled` rather than building a useless client.
    """

    def __init__(
        self,
        base_url: str | None,
        credential: str | None,
        *,
        credential_header: str = "Authorization",
        credential_scheme: str = "Bearer",
        connect_timeout_seconds: float = 5.0,
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        base = (base_url or "").strip()
        if not base:
            raise PlatformTransportDisabled("platform base URL is not configured")
        self._credential = (credential or "").strip()
        if not self._credential:
            # Scoped credentials are mandatory: an allowlisted endpoint with no credential would
            # call the platform unauthenticated. Fail-closed in every environment.
            raise PlatformTransportDisabled("platform scoped credential is not configured")
        if connect_timeout_seconds <= 0 or timeout_seconds <= 0:
            raise ValueError("platform transport timeouts must be positive")

        self._base = httpx.URL(base)
        if self._base.scheme not in ("http", "https"):
            raise ValueError(f"platform base URL scheme must be http(s): {self._base.scheme!r}")
        # Pre-computed allowlist anchors: the exact origin and the base path prefix every call must
        # stay within.
        self._base_str = str(self._base).rstrip("/")
        self._base_path = self._base.path.rstrip("/")

        self._credential_header = credential_header
        scheme = (credential_scheme or "").strip()
        self._credential_value = f"{scheme} {self._credential}" if scheme else self._credential

        # Register the credential literal with the process-wide redactor so it can never surface in a
        # log line (DCA-012), even if some future call site logs carelessly.
        get_redactor().register_secret(self._credential)

        # Separate connect vs overall timeout — never an unbounded wait. Redirects are NOT followed,
        # so a 3xx can never bounce a call to an off-allowlist host.
        timeout = httpx.Timeout(timeout_seconds, connect=connect_timeout_seconds)
        self._client = httpx.Client(timeout=timeout, transport=transport, follow_redirects=False)

    # -- allowlist ---------------------------------------------------------------------------------

    def _resolve_url(self, path: str) -> httpx.URL:
        """Resolve a caller-supplied *relative* path against the allowlisted base, refusing anything
        that could point off the configured origin. Pure/no-I/O: raising here changes nothing."""
        raw = (path or "").strip()
        # Reject escapes before we build anything: an absolute URL (``scheme://``), a protocol-
        # relative reference (``//host``), or a parent-traversal segment that could climb above the
        # base path. These are the only ways a "relative" path could leave the allowlisted origin.
        if "://" in raw or raw.startswith("//"):
            raise PlatformEndpointNotAllowed(
                "absolute or protocol-relative URLs are not allowed; pass a path under the base URL"
            )
        normalized = raw if raw.startswith("/") else "/" + raw
        if any(segment == ".." for segment in normalized.split("/")):
            raise PlatformEndpointNotAllowed("path traversal ('..') is not allowed")

        url = httpx.URL(self._base_str + normalized)
        # Belt-and-suspenders: the resolved URL must share the base origin and stay under the base
        # path. (Given the rejections above this always holds; the explicit check means a future
        # change can never silently widen the allowlist.)
        if (
            url.scheme != self._base.scheme
            or url.host != self._base.host
            or url.port != self._base.port
            or not url.path.startswith(self._base_path)
        ):
            raise PlatformEndpointNotAllowed(
                "resolved URL falls outside the configured platform endpoint"
            )
        return url

    # -- request -----------------------------------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
        correlation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> TransportResult:
        """Perform one request against the allowlisted platform endpoint.

        Raises :class:`PlatformEndpointNotAllowed` (before any I/O) if ``path`` escapes the endpoint,
        and :class:`PlatformTransportError` on a transport failure (no HTTP response). A received HTTP
        response — including 4xx/5xx — is returned as a classified :class:`TransportResult`.
        """
        url = self._resolve_url(path)  # may raise; no socket opened yet
        cid = (correlation_id or "").strip() or get_correlation_id() or new_correlation_id()

        headers = {
            self._credential_header: self._credential_value,
            CORRELATION_HEADER: cid,
            REQUEST_ID_HEADER: cid,
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        try:
            response = self._client.request(
                method.upper(), url, json=json, params=params, headers=headers
            )
        except httpx.HTTPError as exc:
            kind = classify_exception(exc)
            # Log the path (not headers/credential) + the stable kind. The redactor scrubs the line
            # regardless; the message we raise carries only the classification, never request detail.
            log.warning(
                "platform transport %s %s failed: %s",
                method.upper(),
                url.path,
                kind.value,
                extra={"correlation_id": cid},
            )
            raise PlatformTransportError(kind, "platform request failed") from exc

        response_class = classify_status(response.status_code)
        body: Any | None = None
        try:
            body = response.json()
        except ValueError:
            body = None
        log.info(
            "platform transport %s %s -> %s (%s)",
            method.upper(),
            url.path,
            response.status_code,
            response_class.value,
            extra={"correlation_id": cid},
        )
        return TransportResult(
            status_code=response.status_code,
            response_class=response_class,
            correlation_id=cid,
            url=str(url),
            json=body,
            text=response.text[:_MAX_TEXT],
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:  # never leak the credential, even in a debug repr
        return f"PlatformHttpTransport(base_url={self._base_str!r})"


def build_platform_transport(
    settings: Any, *, transport: httpx.BaseTransport | None = None
) -> PlatformHttpTransport | None:
    """Build the transport from settings, or ``None`` when the capability is not configured.

    Returning ``None`` (rather than raising) mirrors the identity/entitlement factories: the platform
    transport is an off-by-default capability, so an unconfigured deployment simply has no transport
    and any adapter built on it stays disabled (B1). A half-configured deployment (base URL without a
    credential) is already rejected at boot by ``validate_required``.
    """
    if not getattr(settings, "platform_transport_configured", False):
        return None
    return PlatformHttpTransport(
        settings.platform_base_url,
        settings.platform_service_credential,
        credential_header=settings.platform_credential_header,
        credential_scheme=settings.platform_credential_scheme,
        connect_timeout_seconds=settings.platform_connect_timeout_seconds,
        timeout_seconds=settings.platform_timeout_seconds,
        transport=transport,
    )
