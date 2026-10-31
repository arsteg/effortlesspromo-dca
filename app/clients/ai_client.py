"""DCA-129 — the authenticated DCA HTTP adapter to the existing effortlesspromo-ai gateway.

This is the ONE model-execution path for the DCA (ADR-0004 / the DCA boundary): the gateway owns every
provider, model-routing, prompt, and output-validation decision; this adapter only *calls* it over
authenticated HTTP. **No provider SDKs, no provider keys, no model routing, and no fallback model
execution live here** — and none ever may. It is the live transport the DCA-128 facade delegates to
(``app/ai/contract.py``); the deterministic review stand-in is ``app/ai/fake.py``.

Implements the contract recorded in ``docs/DCA-001-integration-contracts.md`` §2 and the DCA-129
hardening surface (mirroring the DCA-032 platform transport so the two adapters are hardened the same
way):

- **Allowlisted service URL.** Every call is pinned to the single configured gateway origin
  (scheme + host + port) under the fixed ``/v1/<capability>`` path. The only caller-controlled segment
  is the capability key, which is validated as a single safe path segment — an absolute/protocol-
  relative value, a ``/`` or ``..`` traversal, or whitespace is **refused before any socket is opened**
  (:class:`AiEndpointNotAllowed`), and redirects are **not** followed, so a call can never leave the
  configured gateway.
- **Verified service-auth mechanism.** The shared secret is sent as ``X-Service-Auth`` and the adapter
  is fail-closed: with no base URL or token it is :attr:`enabled` ``False`` and every ``invoke`` raises
  :class:`AiDisabled` rather than calling unauthenticated. A wrong token surfaces as the gateway's own
  401 (see :class:`AiServiceRejected`).
- **Tenant-bound invocation context.** ``businessId`` is carried on every request body; the caller is
  responsible for only ever asserting a tenant it has itself authorized (B3 — the gateway authenticates
  the *service*, not a tenant).
- **Correlation ids.** Every request carries ``X-Correlation-Id`` (reusing the current request's
  correlation id, or a minted one) so a trace spans services, alongside the deterministic ``X-Request-Id``
  that is the gateway's idempotency key (B4) — these are **distinct**: the request id must stay the stable
  ``dca:<id>`` so a retry replays it, while the correlation id is the per-exchange trace id.
- **Explicit deadlines.** Separate connect and overall timeouts are always set — never an unbounded wait.
- **Redacted errors.** The service token is registered with the central DCA-012 redactor at
  construction, and no raised error ever echoes the gateway response body or any credential — a rejection
  carries only its stable status code.

Failure semantics (acceptance criteria — *fail safely*):

- a body-level failure is HTTP 200 with ``status=failed`` and is returned to the caller **as data**;
- a 4xx (bad capability / unauthorized) is a non-retryable caller error → :class:`AiServiceRejected`
  (redacted — status code only), so unauthorized calls fail safely without a blind retry;
- a transport failure where the request was **not** submitted (connect failure/timeout, pool timeout)
  or an HTTP 5xx → :class:`AiServiceUnavailable`: safe to retry fresh;
- a **read/write timeout** — where the request may already have reached the gateway and been processed
  — → :class:`AiServiceTimeout`: the outcome is *uncertain* (possibly submitted). The caller must not
  blindly treat it as "never happened"; recovery replays the SAME deterministic ``X-Request-Id`` so the
  gateway's idempotency cache returns the original result rather than re-executing/double-spending
  (``app/services/ai_invoker.py`` records such a call as ``uncertain``).
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.logging import (
    CORRELATION_HEADER,
    REQUEST_ID_HEADER,
    get_correlation_id,
    get_redactor,
    new_correlation_id,
)

# The service-auth header the gateway verifies (``X-Service-Auth`` via hmac.compare_digest, fail-closed).
SERVICE_AUTH_HEADER = "X-Service-Auth"


class AiServiceUnavailable(Exception):
    """Transient and **not submitted** (or a definite 5xx): the gateway was unreachable, the
    connection/pool timed out, or it returned a 5xx. Safe to retry fresh — the call did not spend."""


class AiServiceTimeout(AiServiceUnavailable):
    """A read/write timeout where the request may ALREADY have reached the gateway (possibly submitted).

    A subclass of :class:`AiServiceUnavailable` so existing ``except AiServiceUnavailable`` handlers
    still catch it, but it marks the outcome as *uncertain*: the gateway may have processed (and
    charged) the call even though no response arrived. Recovery must replay the SAME deterministic
    ``X-Request-Id`` so the gateway's idempotency cache serves the original result — never a blind
    re-execution (the caller records it as ``uncertain``, not ``unavailable``)."""


class AiDisabled(Exception):
    """The AI integration is not configured (no base URL/token) — AI-dependent features are off
    (fail-closed). The adapter never calls the gateway unauthenticated."""


class AiEndpointNotAllowed(Exception):
    """A capability key would resolve outside the allowlisted ``/v1/<capability>`` path on the
    configured gateway origin. Raised **before** any network I/O, so the refusal changes nothing."""


class AiServiceRejected(RuntimeError):
    """The gateway rejected the call with a 4xx (bad capability, unauthorized, …) — a caller error,
    not retryable. Carries only the stable :attr:`status_code`; the message never echoes the response
    body or any credential (redacted errors)."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"AI service rejected the call ({status_code})")


class AiClient:
    def __init__(
        self,
        base_url: str | None,
        service_token: str | None,
        timeout_seconds: float = 100.0,
        *,
        connect_timeout_seconds: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = (base_url or "").rstrip("/")
        self._token = service_token or ""
        # Register the service token with the process-wide redactor so its literal can never surface in
        # a log line or a traceback, even if some future call site logs carelessly (DCA-012).
        if self._token:
            get_redactor().register_secret(self._token)

        # Pin the allowlisted origin. A configured base URL must be http(s); an empty one leaves the
        # adapter disabled (fail-closed) rather than building a useless client.
        self._base: httpx.URL | None = None
        if self._base_url:
            if connect_timeout_seconds <= 0 or timeout_seconds <= 0:
                raise ValueError("AI client timeouts must be positive")
            self._base = httpx.URL(self._base_url)
            if self._base.scheme not in ("http", "https"):
                raise ValueError(f"AI base URL scheme must be http(s): {self._base.scheme!r}")

        # Separate connect vs overall deadline — never an unbounded wait. Redirects are NOT followed,
        # so a 3xx can never bounce a call to an off-allowlist host.
        timeout = httpx.Timeout(timeout_seconds, connect=connect_timeout_seconds)
        self._client = httpx.Client(timeout=timeout, transport=transport, follow_redirects=False)

    @property
    def enabled(self) -> bool:
        return bool(self._base_url and self._token)

    def _resolve_url(self, capability: str) -> httpx.URL:
        """Resolve the single allowlisted ``/v1/<capability>`` URL, refusing any capability key that
        could escape the configured origin. Pure/no-I/O: raising here changes nothing."""
        cap = (capability or "").strip()
        # The capability is the ONLY caller-controlled path segment. It must be a single safe segment:
        # reject an absolute/protocol-relative value, any path separator or parent-traversal, and an
        # empty value — these are the only ways it could redirect the call off the pinned path.
        if (
            not cap
            or cap != capability
            or any(ch.isspace() for ch in cap)
            or "/" in cap
            or "\\" in cap
            or "://" in cap
            or ".." in cap
        ):
            raise AiEndpointNotAllowed(
                "capability is not a single safe path segment (no '/', '\\', '..', scheme or spaces)"
            )
        assert self._base is not None  # guaranteed: enabled() is checked before resolving
        url = httpx.URL(f"{self._base_url}/v1/{cap}")
        # Belt-and-suspenders: the resolved URL must share the pinned origin and stay under ``/v1``.
        if (
            url.scheme != self._base.scheme
            or url.host != self._base.host
            or url.port != self._base.port
            or not url.path.startswith("/v1/")
        ):
            raise AiEndpointNotAllowed(
                "resolved URL falls outside the configured AI gateway endpoint"
            )
        return url

    def invoke(
        self,
        capability: str,
        *,
        business_id: str,
        job_id: str,
        request_id: str,
        input_context: dict[str, Any] | None = None,
        untrusted: list[dict[str, str]] | None = None,
        model_hint: str | None = None,
        prompt_version: str | None = None,
        correlation_id: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise AiDisabled("ai_base_url / ai_service_token not configured")

        url = self._resolve_url(capability)  # may raise AiEndpointNotAllowed; no socket opened yet
        # The correlation id (trace) is distinct from the request id (idempotency key): reuse the
        # current request's correlation id, or mint one, but NEVER let it displace the deterministic
        # X-Request-Id that a retry must replay (B4).
        cid = (correlation_id or "").strip() or get_correlation_id() or new_correlation_id()

        payload: dict[str, Any] = {
            "jobId": job_id,
            "businessId": business_id,
            "correlationId": cid,
            "inputContext": input_context or {},
            "modelHint": model_hint,
            "promptVersion": prompt_version,
            "untrusted": untrusted or [],
            "dryRun": dry_run,
        }
        headers = {
            SERVICE_AUTH_HEADER: self._token,
            REQUEST_ID_HEADER: request_id,
            CORRELATION_HEADER: cid,
        }
        try:
            response = self._client.post(url, json=payload, headers=headers)
        except (httpx.ReadTimeout, httpx.WriteTimeout) as exc:
            # The request was (likely) already sent — the gateway may have processed and charged it.
            # Uncertain: the caller must reconcile via a same-request-id replay, never a blind retry.
            raise AiServiceTimeout(
                "AI service timed out after the request may have been submitted"
            ) from exc
        except httpx.HTTPError as exc:
            # Connect failure/timeout, pool timeout, protocol error — the request was NOT submitted.
            # Safe to retry fresh. The message carries no request detail or credential (redacted).
            raise AiServiceUnavailable("AI service unreachable") from exc
        if response.status_code >= 500:
            raise AiServiceUnavailable(f"AI service returned {response.status_code}")
        if response.status_code >= 400:
            # 4xx is a caller bug (bad capability/auth) — not retryable. Surface the status code only;
            # never the response body (it could echo submitted context or a credential).
            raise AiServiceRejected(response.status_code)
        return response.json()

    def close(self) -> None:
        self._client.close()

    def __repr__(self) -> str:  # never leak the service token, even in a debug repr
        return f"AiClient(base_url={self._base_url!r})"
