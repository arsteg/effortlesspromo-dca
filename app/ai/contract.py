"""DCA-128 — the single DCA AI-capability facade + its typed request/response boundary.

Key: ``ai-facade`` · source sections 4, 8, 9. Stage: existing AI-service integration.

All DCA model work goes through ONE chokepoint — :class:`AiCapabilityFacade`. The facade is a *client
contract*, not a second capability registry or gateway: the existing effortlesspromo-ai gateway owns
every provider, model-routing, prompt and output-validation decision (ADR-0004 / the DCA boundary).
The facade only (a) gates which capabilities the DCA may REQUEST, (b) marshals a DCA-owned typed
request onto the verified gateway envelope, (c) delegates to the HTTP transport, and (d) parses the
envelope back into a DCA-owned typed result — failing CLOSED on anything it cannot vouch for.

What is reused verbatim from the recorded contract (docs/DCA-001-integration-contracts.md §2), never
re-invented here:

- **Envelope semantics** — ``jobId``, ``capability``, ``businessId``, ``inputContext``, ``untrusted``
  and ``dryRun`` on the way in; ``status`` / ``structuredOutput`` / ``confidence`` / validation /
  human-review / token-usage / cost metadata on the way back (``envelope.py`` in effortlesspromo-ai).
  The DCA models mirror only the fields the DCA consumes; the gateway stays the schema authority.
- **Job-ID allocation (B2).** A DCA invocation has NO .NET ``AIJob`` row, so a DCA job id lives in its
  OWN namespace: the ``dca:<AiInvocation id>`` string minted by ``app/services/ai_invoker.py``. The
  facade REFUSES any job id outside that namespace (:data:`DCA_JOB_ID_PREFIX`) so it can never collide
  with or imitate a platform-owned AIJob id — *existing service job ownership is respected*.
- **Tenant binding (B3).** The gateway's ``X-Service-Auth`` authenticates the *service*, not a tenant;
  ``businessId`` is caller-asserted. The facade therefore requires the binding's ``business_id`` to
  match the request's, so a caller can only ever invoke for the tenant it has itself authorized — the
  facade never widens tenant scope.
- **Idempotency (B4).** The deterministic ``X-Request-Id`` ( = the ``dca:<id>`` request id) is carried
  on the binding so a retry replays the SAME id and the gateway serves it from its cache (no double
  spend). Durable spend/replay records are the DCA's own ``AiInvocation`` rows, not the AI-side cache.

Fail-closed contract checks (acceptance criteria):

- an UNKNOWN capability (not in the DCA allowlist) is refused before any call — :class:`AiCapabilityDisabled`;
- a job id OUTSIDE the DCA namespace, or a tenant mismatch, is refused — :class:`AiContractViolation`;
- a response whose ``jobId``/``capability`` does NOT match the request, or whose shape is incompatible
  (missing required fields, unknown status), is refused — :class:`AiContractViolation`.

Future capabilities stay DISABLED: the allowlist is deliberately narrow and live execution is gated on
AI being configured at all (off by default). Extending the contract (new capability, new envelope
field the DCA relies on) is an explicit later AI-capability story — documented as a blocker, never
smuggled in here. See ``app/ai/fake.py`` for the deterministic fixture transport and the builders.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

# The DCA-owned job-id / request-id namespace (B2). Every DCA invocation id is minted as
# ``dca:<AiInvocation id>`` by app/services/ai_invoker.py; the facade refuses anything else so a DCA
# job can never be mistaken for, or collide with, a platform-owned AIJob id.
DCA_JOB_ID_PREFIX = "dca:"

# The capabilities the DCA facade is permitted to REQUEST from the gateway. This is a fail-closed
# allowlist, NOT a capability registry: it carries no schema, prompt, or routing — those live in the
# effortlesspromo-ai gateway. Only a key listed here may be requested; everything else fails closed as
# unknown. It is intentionally narrow (future capabilities stay disabled): the entries are the ones a
# through-DCA-134 accepted story already references — the licensed-provider organization fetch
# (``contact_research``, app/sources/licensed_provider.py, whose live paid call is itself disabled) and
# the DCA-134 business-profile drafting capability (``business_profile_draft``,
# app/services/profile_drafter.py — a draft-only, evidence-grounded capability) and the DCA-135 bounded
# discovery-plan capability (``discovery_plan``, app/services/discovery_planner.py — a proposal-only
# planning capability over the business's own confirmed ICP + approved sources). A later AI-capability
# story adds its reviewed key here; listing a key only permits REQUESTING it and never enables a
# live/paid call (that stays gated on the transport being configured — off by default).
# The DCA-136 permitted unstructured-observation extraction capability
# (``unstructured_observation_extraction``, app/sources/unstructured_extract.py — the AI counterpart of
# the deterministic DCA-124 website extractor: permitted organization fields extracted from
# already-fetched approved content, each citation-grounded before it becomes a field observation) is
# added here too. Listing a key only permits REQUESTING it and never enables a live/paid call.
KNOWN_CAPABILITIES: frozenset[str] = frozenset(
    {
        "contact_research",
        "business_profile_draft",
        "discovery_plan",
        "unstructured_observation_extraction",
    }
)

# Terminal envelope statuses the DCA understands. Anything else is an incompatible contract extension.
_COMPLETED = "completed"
_FAILED = "failed"
_KNOWN_STATUSES: frozenset[str] = frozenset({_COMPLETED, _FAILED})


class AiCapabilityDisabled(RuntimeError):
    """Raised BEFORE any call when a capability may not be requested (unknown key, or AI off)."""


class AiContractViolation(RuntimeError):
    """Raised when a binding or a gateway response violates the DCA contract — always fail-closed.

    Covers a non-DCA job id, a tenant mismatch, and a response whose job/capability does not match the
    request or whose shape is incompatible with the envelope the DCA verified (B2/§2.2).
    """


class AiUntrustedItem(BaseModel):
    """One piece of external/untrusted content, kept out of the instruction path (AI-032).

    Mirrors the gateway's ``UntrustedItem``: provenance + raw external text that the gateway isolates
    and sanitises before any model sees it. The DCA never places untrusted text into ``input_context``
    as if it were trusted instruction data.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(min_length=1)
    content: str


class AiCapabilityRequest(BaseModel):
    """A DCA-owned request to run one gateway capability. Capability-specific shape lives in context.

    ``input_context`` is the MINIMISED, capability-scoped input (never secrets, never raw untrusted
    text). ``untrusted`` carries external text role-separated. ``dry_run`` projects cost with no
    provider spend (AI-025). The job id / request id / tenant binding is NOT on the request — it is an
    :class:`AiJobBinding` the caller allocates from the DCA-owned namespace and passes alongside, so
    the authorized allocation (B2) and tenant binding (B3) are explicit, not invented inline.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    capability: str = Field(min_length=1)
    business_id: str = Field(min_length=1)
    input_context: dict[str, Any] = Field(default_factory=dict)
    untrusted: tuple[AiUntrustedItem, ...] = ()
    model_hint: str | None = None
    prompt_version: str | None = None
    dry_run: bool = False
    correlation_id: str | None = None


class AiJobBinding(BaseModel):
    """The DCA-owned job/request identity + tenant a request is bound to (B2/B3/B4).

    ``job_id`` and ``request_id`` MUST both live in the DCA namespace (``dca:<AiInvocation id>``); the
    facade refuses anything else. ``request_id`` is the deterministic ``X-Request-Id`` so a retry
    replays the same id (B4). ``business_id`` is the tenant the caller has itself authorized; it must
    equal the request's ``business_id``. Use :func:`dca_job_binding` to build one from an invocation id.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    business_id: str = Field(min_length=1)

    @property
    def is_dca_owned(self) -> bool:
        """Both ids live in the DCA namespace — never a platform-owned AIJob id (B2)."""
        return self.job_id.startswith(DCA_JOB_ID_PREFIX) and self.request_id.startswith(
            DCA_JOB_ID_PREFIX
        )


def dca_job_binding(invocation_id: str, business_id: str) -> AiJobBinding:
    """Build a binding from a DCA ``AiInvocation`` id, mirroring ai_invoker's ``dca:<id>`` convention.

    This does NOT invent a new id scheme — it reuses the one the durable recorder
    (``app/services/ai_invoker.py``) already mints, so the facade and the durable row agree. The caller
    is responsible for having persisted the invocation row (the durable spend/replay record, B4).
    """
    token = f"{DCA_JOB_ID_PREFIX}{invocation_id}"
    return AiJobBinding(job_id=token, request_id=token, business_id=business_id)


class AiTokenUsage(BaseModel):
    model_config = ConfigDict(frozen=True)

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class AiCost(BaseModel):
    """What the gateway reported a call cost (or projected, for a dry run)."""

    model_config = ConfigDict(frozen=True)

    amount: float = 0.0
    currency: str = "USD"
    estimated: bool = False  # a dry-run projection, not an actual charge (AI-025)
    pricing_unavailable: bool = False


class AiValidation(BaseModel):
    """The gateway's output-validation / safety metadata — inspected, never second-guessed here.

    The 6-screen output-validation pipeline is the gateway's (AI-033). The DCA only reads the verdict
    so a consumer can honour human-review / taint / injection signals. ``status`` is the envelope's
    ``validationStatus`` (valid | low_confidence | invalid_schema | failed).
    """

    model_config = ConfigDict(frozen=True)

    status: str = "valid"
    report_status: str | None = None  # validationReport.status (passed | failed)
    failed_screen: str | None = None
    human_review_required: bool = False
    taint: bool = False
    injection_suspected: bool = False


class AiError(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    message: str = ""
    retryable: bool = False


class AiCapabilityResult(BaseModel):
    """A DCA-owned, typed view of one gateway invocation's outcome.

    Carries only the fields the DCA consumes from the envelope. ``synthetic`` marks a result produced
    by the dev/test fixture transport so a stub answer can never be mistaken for a real one.
    """

    model_config = ConfigDict(frozen=True)

    job_id: str
    capability: str
    business_id: str
    status: str
    structured_output: dict[str, Any] | None = None
    confidence: float | None = None
    model_provider: str | None = None
    model_id: str | None = None
    prompt_version: str | None = None
    usage: AiTokenUsage = Field(default_factory=AiTokenUsage)
    cost: AiCost = Field(default_factory=AiCost)
    validation: AiValidation = Field(default_factory=AiValidation)
    human_review_required: bool = False
    error: AiError | None = None
    synthetic: bool = False

    @property
    def succeeded(self) -> bool:
        return self.status == _COMPLETED


def _require(envelope: dict[str, Any], key: str) -> Any:
    if key not in envelope:
        raise AiContractViolation(f"gateway response is missing required field '{key}'")
    return envelope[key]


def parse_envelope(
    request: AiCapabilityRequest,
    binding: AiJobBinding,
    envelope: dict[str, Any],
    *,
    synthetic: bool = False,
) -> AiCapabilityResult:
    """Parse a raw gateway envelope into a typed result, failing CLOSED on any contract violation.

    Enforces the acceptance criteria on the RESPONSE side: the envelope must carry the required fields
    (incompatible schema → violation), its ``jobId`` must equal the bound job id and its ``capability``
    the requested capability (a mismatched response → violation), and its ``status`` must be a status
    the DCA understands. Only then is the (possibly ``status=failed``) envelope surfaced as data — a
    body-level ``failed`` is a legitimate outcome, not a parse violation.
    """
    if not isinstance(envelope, dict):
        raise AiContractViolation("gateway response is not a JSON object")

    job_id = _require(envelope, "jobId")
    capability = _require(envelope, "capability")
    status = _require(envelope, "status")

    if job_id != binding.job_id:
        raise AiContractViolation(
            f"response jobId '{job_id}' does not match the bound job id '{binding.job_id}'"
        )
    if capability != request.capability:
        raise AiContractViolation(
            f"response capability '{capability}' does not match the requested "
            f"capability '{request.capability}'"
        )
    if status not in _KNOWN_STATUSES:
        raise AiContractViolation(f"response status '{status}' is not a known envelope status")

    usage_raw = envelope.get("tokenUsage") or {}
    cost_raw = envelope.get("costEstimate") or {}
    report_raw = envelope.get("validationReport") or {}
    error_raw = envelope.get("error")

    validation = AiValidation(
        status=str(envelope.get("validationStatus") or "valid"),
        report_status=report_raw.get("status") if isinstance(report_raw, dict) else None,
        failed_screen=report_raw.get("failedScreen") if isinstance(report_raw, dict) else None,
        human_review_required=bool(envelope.get("humanReviewRequired", False)),
        taint=bool(envelope.get("taint", False)),
        injection_suspected=bool(envelope.get("injectionSuspected", False)),
    )

    error = None
    if isinstance(error_raw, dict) and error_raw.get("code"):
        error = AiError(
            code=str(error_raw.get("code")),
            message=str(error_raw.get("message") or ""),
            retryable=bool(error_raw.get("retryable", False)),
        )

    return AiCapabilityResult(
        job_id=job_id,
        capability=capability,
        business_id=binding.business_id,
        status=status,
        structured_output=envelope.get("structuredOutput"),
        confidence=envelope.get("confidence"),
        model_provider=envelope.get("modelProvider"),
        model_id=envelope.get("modelId"),
        prompt_version=envelope.get("promptVersion"),
        usage=AiTokenUsage(
            prompt_tokens=int(usage_raw.get("promptTokens", 0))
            if isinstance(usage_raw, dict)
            else 0,
            completion_tokens=(
                int(usage_raw.get("completionTokens", 0)) if isinstance(usage_raw, dict) else 0
            ),
            total_tokens=int(usage_raw.get("totalTokens", 0)) if isinstance(usage_raw, dict) else 0,
        ),
        cost=AiCost(
            amount=float(cost_raw.get("amount", 0.0)) if isinstance(cost_raw, dict) else 0.0,
            currency=str(cost_raw.get("currency") or "USD")
            if isinstance(cost_raw, dict)
            else "USD",
            estimated=bool(cost_raw.get("estimated", False))
            if isinstance(cost_raw, dict)
            else False,
            pricing_unavailable=(
                bool(cost_raw.get("pricingUnavailable", False))
                if isinstance(cost_raw, dict)
                else False
            ),
        ),
        validation=validation,
        human_review_required=validation.human_review_required,
        error=error,
        synthetic=synthetic,
    )


@runtime_checkable
class AiGatewayTransport(Protocol):
    """The HTTP transport seam the facade delegates to (structurally satisfied by ``AiClient``).

    Synchronous, matching the existing service-to-service clients. The live adapter is
    ``app/clients/ai_client.py``; the dev/test fixture is ``app/ai/fake.py``. ``enabled`` reports
    whether the transport is actually wired (base URL + token); an unwired transport invokes nothing.
    """

    @property
    def enabled(self) -> bool: ...

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
    ) -> dict[str, Any]: ...


class AiCapabilityFacade:
    """The ONE chokepoint through which the DCA requests model work from the effortlesspromo-ai gateway.

    It does NOT route models, hold keys, or validate output — those are the gateway's. It gates the
    capability (allowlist, fail-closed), enforces the DCA-owned job-id namespace and tenant binding,
    delegates to the transport with the deterministic ``X-Request-Id``, and parses the envelope into a
    typed result (fail-closed on a mismatched/incompatible response).

    The allowlist is injectable so a test can exercise a capability deterministically; it defaults to
    the narrow module allowlist (future capabilities stay disabled). ``synthetic`` is derived from the
    transport so results from the fixture transport are flagged.
    """

    def __init__(
        self,
        transport: AiGatewayTransport,
        *,
        allowed_capabilities: frozenset[str] = KNOWN_CAPABILITIES,
        minimizer: Any | None = None,
    ) -> None:
        self._transport = transport
        self._allowed = allowed_capabilities
        # DCA-131 — minimize the context per capability BEFORE egress. Imported lazily to avoid an
        # import cycle (the minimizer imports this module's AiContractViolation). Defaults to the
        # process-wide minimizer over the module allowlist.
        if minimizer is None:
            from app.ai.context_minimizer import default_context_minimizer

            minimizer = default_context_minimizer()
        self._minimizer = minimizer

    @property
    def transport_enabled(self) -> bool:
        return self._transport.enabled

    def is_known_capability(self, capability: str) -> bool:
        return capability in self._allowed

    def can_invoke(self, capability: str) -> bool:
        """A capability may be requested only when it is allowlisted AND the transport is wired."""
        return self.is_known_capability(capability) and self._transport.enabled

    def invoke(self, request: AiCapabilityRequest, binding: AiJobBinding) -> AiCapabilityResult:
        """Request one capability and return a typed result. Fail-closed at every gate.

        Order: unknown capability → transport disabled → non-DCA job id → tenant mismatch → delegate →
        parse (job/capability/schema mismatch). A body-level ``status=failed`` is returned as data (the
        gateway mirrors failures as HTTP 200 with ``status=failed``); only a transport/5xx error
        propagates (as :class:`~app.clients.ai_client.AiServiceUnavailable`) for a same-id retry.
        """
        if not self.is_known_capability(request.capability):
            raise AiCapabilityDisabled(
                f"capability '{request.capability}' is not in the DCA allowlist "
                "(unknown/disabled capability — fail closed)"
            )
        if not self._transport.enabled:
            raise AiCapabilityDisabled(
                "the AI gateway transport is not configured — AI-dependent work is disabled"
            )
        if not binding.is_dca_owned:
            raise AiContractViolation(
                "job/request id is outside the DCA namespace "
                f"('{DCA_JOB_ID_PREFIX}<id>' required; existing-service job ownership is respected)"
            )
        if binding.business_id != request.business_id:
            raise AiContractViolation(
                "binding business_id does not match the request business_id "
                "(a caller may only invoke for the tenant it authorized)"
            )

        # DCA-131 — minimize the context to this capability's authorized fields BEFORE egress. Strips
        # secrets, the internal send sentinel and out-of-scope PII, and bounds total size; an
        # un-minimizable/oversize context raises AiContractViolation (fail closed, no call made). The DCA
        # performs this even though the gateway ALSO runs privacy screens — a recording fake in front of
        # the gateway never observes a forbidden field because the DCA never sends one.
        minimized = self._minimizer.minimize(request.capability, request.input_context)

        envelope = self._transport.invoke(
            request.capability,
            business_id=request.business_id,
            job_id=binding.job_id,
            request_id=binding.request_id,
            input_context=minimized.context,
            untrusted=[{"source": u.source, "content": u.content} for u in request.untrusted],
            model_hint=request.model_hint,
            prompt_version=request.prompt_version,
            correlation_id=request.correlation_id,
            dry_run=request.dry_run,
        )
        synthetic = getattr(self._transport, "synthetic", False)
        return parse_envelope(request, binding, envelope, synthetic=bool(synthetic))
