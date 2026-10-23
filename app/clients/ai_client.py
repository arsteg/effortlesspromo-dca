"""HTTP client for the existing effortlesspromo-ai gateway — the ONLY model-execution path.

Implements the contract recorded in docs/DCA-001-integration-contracts.md §2:
- ``POST {base}/v1/{capability}`` with ``X-Service-Auth`` + deterministic ``X-Request-Id``;
- a body-level failure is HTTP 200 with ``status=failed`` (returned to the caller as data);
- transport errors / HTTP >= 500 raise :class:`AiServiceUnavailable` so the caller retries with
  the SAME request id and is served from the AI-side idempotency cache (no double spend);
- ``dryRun=True`` projects cost with no provider call (AI-025).

No provider SDKs, keys, or model routing live here (DCA boundary): the gateway owns all of that.
"""

from __future__ import annotations

from typing import Any

import httpx


class AiServiceUnavailable(Exception):
    """Transient: the AI service was unreachable, timed out, or returned a 5xx."""


class AiDisabled(Exception):
    """The AI integration is not configured (no base URL/token) — AI-dependent features are off."""


class AiClient:
    def __init__(
        self,
        base_url: str | None,
        service_token: str | None,
        timeout_seconds: float = 100.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = (base_url or "").rstrip("/")
        self._token = service_token or ""
        self._client = httpx.Client(timeout=timeout_seconds, transport=transport)

    @property
    def enabled(self) -> bool:
        return bool(self._base_url and self._token)

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

        payload: dict[str, Any] = {
            "jobId": job_id,
            "businessId": business_id,
            "correlationId": correlation_id,
            "inputContext": input_context or {},
            "modelHint": model_hint,
            "promptVersion": prompt_version,
            "untrusted": untrusted or [],
            "dryRun": dry_run,
        }
        headers = {"X-Service-Auth": self._token, "X-Request-Id": request_id}
        url = f"{self._base_url}/v1/{capability}"
        try:
            response = self._client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:  # connect/timeout/protocol — retryable with same request id
            raise AiServiceUnavailable(f"AI service unreachable: {exc}") from exc
        if response.status_code >= 500:
            raise AiServiceUnavailable(f"AI service returned {response.status_code}")
        if response.status_code >= 400:
            # 4xx is a caller bug (bad capability/auth) — not retryable; surface loudly.
            raise RuntimeError(f"AI service rejected the call: {response.status_code} {response.text}")
        return response.json()

    def close(self) -> None:
        self._client.close()
