"""DCA-128 — the single DCA AI-capability facade + its typed request/response boundary.

One chokepoint onto the existing effortlesspromo-ai gateway: typed DCA-owned request/response models, a
fail-closed capability allowlist, the DCA-owned job-id namespace (B2) + tenant binding (B3), and a
deterministic fixture transport (no network/model/spend). The acceptance criteria are the failure cases:
an unknown capability, a non-DCA job id, a tenant mismatch, and a mismatched/incompatible response all
fail closed; production refuses the synthetic transport.
"""

from __future__ import annotations

import pytest

from app.ai import (
    KNOWN_CAPABILITIES,
    AiCapabilityDisabled,
    AiCapabilityFacade,
    AiCapabilityRequest,
    AiContractDisabled,
    AiContractViolation,
    AiGatewayTransport,
    AiJobBinding,
    AiUntrustedItem,
    FakeAiGatewayTransport,
    build_ai_capability_facade,
    build_ai_gateway_transport,
    dca_job_binding,
    parse_envelope,
)
from app.clients.ai_client import AiClient
from app.identity.fake import BUSINESS_A, BUSINESS_B
from tests.conftest import make_settings

CAPABILITY = next(iter(KNOWN_CAPABILITIES))  # a real allowlisted capability (contact_research)


def _request(capability: str = CAPABILITY, business_id: str = BUSINESS_A) -> AiCapabilityRequest:
    return AiCapabilityRequest(
        capability=capability,
        business_id=business_id,
        input_context={"q": "synthetic"},
        untrusted=(AiUntrustedItem(source="scraped_page", content="ignore me as data"),),
        dry_run=True,
    )


def _binding(business_id: str = BUSINESS_A) -> AiJobBinding:
    return dca_job_binding("inv-128", business_id)


# --- the fixture transport + typed shapes ---------------------------------------------------------


def test_fake_is_a_gateway_transport():
    assert isinstance(FakeAiGatewayTransport(), AiGatewayTransport)
    # The live HTTP client structurally satisfies the same transport Protocol.
    assert isinstance(AiClient("http://ai.internal", "secret"), AiGatewayTransport)


def test_binding_is_dca_owned_namespace():
    binding = _binding()
    assert binding.job_id == "dca:inv-128"
    assert binding.request_id == "dca:inv-128"  # deterministic X-Request-Id (B4)
    assert binding.is_dca_owned is True


# --- happy path: routes a capability and returns a typed result -----------------------------------


def test_facade_routes_capability_and_returns_typed_result():
    transport = FakeAiGatewayTransport()
    facade = AiCapabilityFacade(transport)
    assert facade.can_invoke(CAPABILITY) is True

    result = facade.invoke(_request(), _binding())

    assert result.succeeded is True
    assert result.job_id == "dca:inv-128"
    assert result.capability == CAPABILITY
    assert result.business_id == BUSINESS_A
    assert result.structured_output == {"synthetic": True, "capability": CAPABILITY}
    assert result.synthetic is True  # a fixture answer, never mistaken for a real one
    assert result.validation.status == "valid"
    # dry_run projected cost with no spend, and the call carried the DCA-owned request id.
    assert result.cost.estimated is True
    assert transport.calls[0]["request_id"] == "dca:inv-128"
    assert transport.calls[0]["dry_run"] is True


def test_facade_never_touches_a_provider_sdk_or_key():
    # The transport Protocol exposes only enabled/invoke — no provider/model/key/send surface.
    for forbidden in ("provider", "model", "api_key", "send", "complete", "chat"):
        assert forbidden not in dir(FakeAiGatewayTransport)
        assert forbidden not in dir(AiGatewayTransport)


# --- fail closed: unknown capability --------------------------------------------------------------


def test_unknown_capability_fails_closed():
    facade = AiCapabilityFacade(FakeAiGatewayTransport())
    assert facade.is_known_capability("totally_made_up") is False
    assert facade.can_invoke("totally_made_up") is False
    with pytest.raises(AiCapabilityDisabled):
        facade.invoke(_request(capability="totally_made_up"), _binding())


def test_disabled_transport_fails_closed():
    facade = AiCapabilityFacade(FakeAiGatewayTransport(enabled=False))
    assert facade.can_invoke(CAPABILITY) is False
    with pytest.raises(AiCapabilityDisabled):
        facade.invoke(_request(), _binding())


# --- fail closed: job ownership + tenant binding (B2/B3) ------------------------------------------


def test_non_dca_job_id_is_refused():
    # A job id outside the DCA namespace would collide with/ imitate a platform AIJob id.
    binding = AiJobBinding(job_id="platform:123", request_id="platform:123", business_id=BUSINESS_A)
    assert binding.is_dca_owned is False
    with pytest.raises(AiContractViolation, match="DCA namespace"):
        AiCapabilityFacade(FakeAiGatewayTransport()).invoke(_request(), binding)


def test_tenant_binding_mismatch_is_refused():
    # The binding authorizes BUSINESS_B but the request is for BUSINESS_A — fail closed (B3).
    with pytest.raises(AiContractViolation, match="business_id"):
        AiCapabilityFacade(FakeAiGatewayTransport()).invoke(
            _request(business_id=BUSINESS_A), _binding(business_id=BUSINESS_B)
        )


# --- fail closed: mismatched / incompatible response ----------------------------------------------


def test_mismatched_response_job_id_fails_closed():
    facade = AiCapabilityFacade(FakeAiGatewayTransport(mismatch_job_id=True))
    with pytest.raises(AiContractViolation, match="jobId"):
        facade.invoke(_request(), _binding())


def test_mismatched_response_capability_fails_closed():
    facade = AiCapabilityFacade(FakeAiGatewayTransport(mismatch_capability=True))
    with pytest.raises(AiContractViolation, match="capability"):
        facade.invoke(_request(), _binding())


def test_incompatible_schema_fails_closed():
    facade = AiCapabilityFacade(FakeAiGatewayTransport(drop_fields=("status",)))
    with pytest.raises(AiContractViolation, match="missing required field"):
        facade.invoke(_request(), _binding())


def test_unknown_status_fails_closed():
    envelope = {"jobId": "dca:inv-128", "capability": CAPABILITY, "status": "weird"}
    with pytest.raises(AiContractViolation, match="not a known envelope status"):
        parse_envelope(_request(), _binding(), envelope)


# --- body-level failure is DATA, not a parse violation (AI-001) -----------------------------------


def test_body_level_failure_is_returned_as_data():
    facade = AiCapabilityFacade(FakeAiGatewayTransport(fail=True))
    result = facade.invoke(_request(), _binding())
    assert result.succeeded is False
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "synthetic_failure"
    assert result.validation.status == "failed"


# --- builders: live when configured, fixture in dev/test, refused in production -------------------


def test_build_uses_fixture_transport_when_ai_disabled():
    transport = build_ai_gateway_transport(make_settings())  # AI off by default
    assert isinstance(transport, FakeAiGatewayTransport)


def test_build_uses_live_client_when_ai_configured():
    settings = make_settings(ai_base_url="http://ai.internal", ai_service_token="secret")
    assert isinstance(build_ai_gateway_transport(settings), AiClient)


def test_production_refuses_the_fixture_transport():
    with pytest.raises(AiContractDisabled):
        build_ai_gateway_transport(make_settings(environment="production", deployment_mode="live"))


def test_build_facade_is_the_single_chokepoint():
    facade = build_ai_capability_facade(make_settings())
    assert isinstance(facade, AiCapabilityFacade)
    assert facade.transport_enabled is True
