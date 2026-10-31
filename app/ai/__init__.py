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
from app.ai.bound_invocation import (
    BoundInvocationActor,
    BoundInvocationAuthorizationError,
    BoundInvocationError,
    BoundInvocationRequest,
    BoundInvocationResult,
    BoundModelInvoker,
)
from app.ai.citation_validator import (
    LABEL_INFERRED,
    LABEL_OBSERVED,
    REASON_CITATION_NOT_IN_SOURCE,
    REASON_FIELD_NOT_PERMITTED,
    REASON_INVALID_INFERENCE_LABEL,
    REASON_MISSING_CITATION,
    REASON_SOURCE_RIGHTS_EXPIRED,
    REASON_UNKNOWN_EVIDENCE,
    REASON_UNKNOWN_FACT,
    REASON_VALUE_NOT_SUPPORTED,
    CitationValidationResult,
    CitedFact,
    CitedSource,
    FactVerdict,
    validate_citations,
    validate_fact_citation,
)
from app.ai.context_minimizer import (
    CAPABILITY_CONTEXT_ALLOWLIST,
    DEFAULT_MAX_CONTEXT_CHARS,
    FORBIDDEN_CONTEXT_KEYS,
    INTERNAL_SEND_SENTINEL,
    ContextMinimizationError,
    ContextMinimizer,
    FieldDrop,
    MinimizedContext,
    default_context_minimizer,
    minimize_context,
)
from app.ai.fake import (
    SYNTHETIC_VERSION,
    AiContractDisabled,
    FakeAiGatewayTransport,
    build_ai_capability_facade,
    build_ai_gateway_transport,
)
from app.ai.untrusted_framer import (
    DEFAULT_MAX_UNTRUSTED_CHARS,
    REASON_MISSING_PROVENANCE,
    REASON_UNTRUSTED_TOO_LARGE,
    ExternalContent,
    UntrustedFramingError,
    UntrustedOutcome,
    build_untrusted_request,
    evaluate_untrusted_result,
    frame_external_content,
)

__all__ = [
    "CAPABILITY_CONTEXT_ALLOWLIST",
    "DCA_JOB_ID_PREFIX",
    "DEFAULT_MAX_CONTEXT_CHARS",
    "DEFAULT_MAX_UNTRUSTED_CHARS",
    "FORBIDDEN_CONTEXT_KEYS",
    "INTERNAL_SEND_SENTINEL",
    "KNOWN_CAPABILITIES",
    "LABEL_INFERRED",
    "LABEL_OBSERVED",
    "REASON_CITATION_NOT_IN_SOURCE",
    "REASON_FIELD_NOT_PERMITTED",
    "REASON_INVALID_INFERENCE_LABEL",
    "REASON_MISSING_CITATION",
    "REASON_MISSING_PROVENANCE",
    "REASON_SOURCE_RIGHTS_EXPIRED",
    "REASON_UNKNOWN_EVIDENCE",
    "REASON_UNKNOWN_FACT",
    "REASON_UNTRUSTED_TOO_LARGE",
    "REASON_VALUE_NOT_SUPPORTED",
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
    "BoundInvocationActor",
    "BoundInvocationAuthorizationError",
    "BoundInvocationError",
    "BoundInvocationRequest",
    "BoundInvocationResult",
    "BoundModelInvoker",
    "CitationValidationResult",
    "CitedFact",
    "CitedSource",
    "ContextMinimizationError",
    "ContextMinimizer",
    "ExternalContent",
    "FactVerdict",
    "FakeAiGatewayTransport",
    "FieldDrop",
    "MinimizedContext",
    "UntrustedFramingError",
    "UntrustedOutcome",
    "build_ai_capability_facade",
    "build_ai_gateway_transport",
    "build_untrusted_request",
    "dca_job_binding",
    "default_context_minimizer",
    "evaluate_untrusted_result",
    "frame_external_content",
    "minimize_context",
    "parse_envelope",
    "validate_citations",
    "validate_fact_citation",
]
