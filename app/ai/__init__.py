"""DCA-128 — the single DCA AI-capability facade (the ``ai-facade`` seam).

All DCA model work goes through ONE chokepoint — :class:`~app.ai.contract.AiCapabilityFacade` — which
delegates to the existing effortlesspromo-ai gateway over authenticated HTTP. This package defines only
that client contract: DCA-owned typed request/response boundary models that mirror the verified gateway
envelope, the DCA-side capability allowlist (fail-closed on unknown), the DCA-owned job-id namespace and
tenant-binding rules (B2/B3), the transport ``Protocol`` (satisfied by ``app/clients/ai_client.py``), a
parser that fails closed on a mismatched/incompatible response, and a deterministic fixture transport for
local tests (no network, no model, no spend; refused in production). It is NOT a second capability
registry or gateway — the effortlesspromo-ai service owns every provider/model/prompt/validation
decision. Future capabilities stay disabled; extending the contract is an explicit later story.
"""

from __future__ import annotations

from app.ai.contract import (
    DCA_JOB_ID_PREFIX,
    KNOWN_CAPABILITIES,
    AiCapabilityDisabled,
    AiCapabilityFacade,
    AiCapabilityRequest,
    AiCapabilityResult,
    AiContractViolation,
    AiCost,
    AiError,
    AiGatewayTransport,
    AiJobBinding,
    AiTokenUsage,
    AiUntrustedItem,
    AiValidation,
    dca_job_binding,
    parse_envelope,
)
from app.ai.fake import (
    SYNTHETIC_VERSION,
    AiContractDisabled,
    FakeAiGatewayTransport,
    build_ai_capability_facade,
    build_ai_gateway_transport,
)

__all__ = [
    "DCA_JOB_ID_PREFIX",
    "KNOWN_CAPABILITIES",
    "SYNTHETIC_VERSION",
    "AiCapabilityDisabled",
    "AiCapabilityFacade",
    "AiCapabilityRequest",
    "AiCapabilityResult",
    "AiContractDisabled",
    "AiContractViolation",
    "AiCost",
    "AiError",
    "AiGatewayTransport",
    "AiJobBinding",
    "AiTokenUsage",
    "AiUntrustedItem",
    "AiValidation",
    "FakeAiGatewayTransport",
    "build_ai_capability_facade",
    "build_ai_gateway_transport",
    "dca_job_binding",
    "parse_envelope",
]
