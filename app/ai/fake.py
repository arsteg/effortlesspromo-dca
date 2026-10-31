"""DCA-128 — deterministic, in-memory AI-gateway fixture transport + the facade builders.

The fixture transport is network-free and does NO model/paid work: it synthesises a gateway envelope
that echoes the request's ``jobId``/``capability`` and returns a stub, obviously-synthetic result. It
exists so the DCA AI contract is reviewable and testable locally without ever calling effortlesspromo-ai
(and so without any provider/model spend). It mirrors the gateway's envelope shape (``app/gateway/
envelope.py`` in effortlesspromo-ai) closely enough for :func:`~app.ai.contract.parse_envelope`, and it
can be told to produce the acceptance-criteria FAILURE shapes — a mismatched ``jobId``/``capability``, an
incompatible (missing-field) body, or a body-level ``status=failed`` — so the fail-closed behaviour is
exercised deterministically.

The real transport is ``app/clients/ai_client.py`` (the HTTP client to the gateway). Selection is
config-driven and fail-safe: when the gateway is configured (``settings.ai_enabled`` — off by default)
the live client is used; otherwise the fixture transport is used in dev/test and REFUSED in production
(mirroring the identity/delivery fakes and the seed's dev-only stance) so a synthetic answer can never
stand in for a real one in prod.
"""

from __future__ import annotations

from typing import Any

from app.clients.ai_client import AiClient

SYNTHETIC_VERSION = "fake-ai-gateway-v1"


class AiContractDisabled(RuntimeError):
    """Raised when an AI-gateway transport is requested where none is wired (production, AI off)."""


class FakeAiGatewayTransport:
    """A deterministic :class:`~app.ai.contract.AiGatewayTransport`. Pure: no network, no model, no spend.

    Returns a synthetic completed envelope that echoes the call's ``jobId``/``capability`` so the facade
    can parse and verify it. Knobs drive the acceptance-criteria failure shapes without a wall clock or a
    socket:

    - ``mismatch_job_id`` — reply with a DIFFERENT ``jobId`` (a mismatched response → fail closed).
    - ``mismatch_capability`` — reply with a DIFFERENT ``capability`` (mismatched response → fail closed).
    - ``drop_fields`` — omit required envelope fields (an incompatible schema → fail closed).
    - ``fail`` — reply with a body-level ``status=failed`` + error (a legitimate outcome, returned as data).
    - ``structured_output`` — the ``structuredOutput`` to return on a completed envelope (default: an
      obviously-synthetic stub). A capability-specific contract test (e.g. DCA-134 business-profile
      drafting) supplies a realistic, schema-shaped payload so the DCA consumer is exercised against the
      actual output shape rather than an empty stub — the gateway still owns the real schema/validation.
    - ``validation`` — envelope validation/taint/injection/human-review flags to merge in, so a test can
      drive the DCA boundary's hold/reject dispositions deterministically without a real gateway.

    ``enabled`` defaults True so dev/test can drive the facade; ``synthetic`` is read by the facade to
    flag every result as a fixture answer.
    """

    synthetic = True

    def __init__(
        self,
        *,
        enabled: bool = True,
        mismatch_job_id: bool = False,
        mismatch_capability: bool = False,
        drop_fields: tuple[str, ...] = (),
        fail: bool = False,
        version: str = SYNTHETIC_VERSION,
        structured_output: dict[str, Any] | None = None,
        validation: dict[str, Any] | None = None,
    ) -> None:
        self._enabled = enabled
        self._mismatch_job_id = mismatch_job_id
        self._mismatch_capability = mismatch_capability
        self._drop_fields = drop_fields
        self._fail = fail
        self._version = version
        self._structured_output = structured_output
        self._validation = validation or {}
        self.calls: list[dict[str, Any]] = []  # every call recorded for inspection (never a send)

    @property
    def enabled(self) -> bool:
        return self._enabled

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
        # A RECORDING fake (DCA-131): it captures the exact ``input_context`` / ``untrusted`` it was handed
        # so a test can inspect the outgoing envelope and prove no forbidden field was ever egressed. This
        # stands in for a recording fake AI HTTP service placed in front of the gateway.
        self.calls.append(
            {
                "capability": capability,
                "business_id": business_id,
                "job_id": job_id,
                "request_id": request_id,
                "input_context": dict(input_context or {}),
                "untrusted": list(untrusted or []),
                "dry_run": dry_run,
            }
        )
        envelope: dict[str, Any] = {
            "jobId": "platform:not-dca" if self._mismatch_job_id else job_id,
            "capability": "some_other_capability" if self._mismatch_capability else capability,
            "status": "failed" if self._fail else "completed",
            "structuredOutput": None
            if self._fail
            else (
                self._structured_output
                if self._structured_output is not None
                else {"synthetic": True, "capability": capability}
            ),
            "confidence": None if self._fail else 0.99,
            "modelProvider": self._version,
            "modelId": "stub-model",
            "promptVersion": prompt_version or "stub-v1",
            "tokenUsage": {"promptTokens": 0, "completionTokens": 0, "totalTokens": 0},
            # A dry run projects cost with no spend; a real run here is still free (synthetic).
            "costEstimate": {
                "amount": 0.0,
                "currency": "USD",
                "estimated": dry_run,
                "pricingUnavailable": False,
            },
            "validationStatus": "failed" if self._fail else "valid",
            "validationReport": {"status": "failed" if self._fail else "passed"},
            "humanReviewRequired": False,
            "taint": False,
            "injectionSuspected": False,
        }
        if self._fail:
            envelope["error"] = {
                "code": "synthetic_failure",
                "message": "fixture transport failure",
                "retryable": False,
            }
        # Merge any test-supplied validation/taint/human-review flags so the DCA boundary's hold/reject
        # dispositions can be driven deterministically (e.g. untrusted evidence → humanReviewRequired).
        envelope.update(self._validation)
        for field in self._drop_fields:
            envelope.pop(field, None)
        return envelope


def build_ai_gateway_transport(settings: Any) -> FakeAiGatewayTransport | AiClient:
    """The AI-gateway transport for this deployment — live when configured, fixture in dev/test.

    When the gateway is configured (``settings.ai_enabled``, off by default), the live HTTP client is
    returned (it still 401s if its token is wrong — fail-closed). When it is NOT configured, production
    gets NO transport (``AiContractDisabled``) rather than a silent synthetic stub; dev/test gets the
    deterministic fixture transport so the contract is reviewable without any network or spend.
    """
    if getattr(settings, "ai_enabled", False):
        return AiClient(
            getattr(settings, "ai_base_url", None),
            getattr(settings, "ai_service_token", None),
            timeout_seconds=float(getattr(settings, "ai_timeout_seconds", 100.0)),
            connect_timeout_seconds=float(getattr(settings, "ai_connect_timeout_seconds", 5.0)),
        )
    if getattr(settings, "is_production", False):
        raise AiContractDisabled(
            "no AI gateway is configured in production and the synthetic fixture transport is "
            "dev/test-only: set EFFORTLESSPROMO_DCA_AI_BASE_URL/AI_SERVICE_TOKEN or keep AI disabled"
        )
    return FakeAiGatewayTransport()


def build_ai_capability_facade(settings: Any) -> Any:
    """Build the single DCA AI-capability facade over the deployment's transport.

    Returned loosely typed (``Any``) to avoid importing the facade at module import where it is not
    needed; the concrete type is :class:`~app.ai.contract.AiCapabilityFacade`.
    """
    from app.ai.contract import AiCapabilityFacade

    return AiCapabilityFacade(build_ai_gateway_transport(settings))
