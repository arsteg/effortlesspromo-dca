"""DCA-131 — minimize the AI-service context before egress to the effortlesspromo-ai gateway.

Focused tests over the pure, deterministic minimizer (allowlist keep / forbidden-key + out-of-scope drop /
secret + send-sentinel scrub / bounded size / determinism), plus an integration test proving the DCA-128
facade applies it BEFORE egress so a RECORDING fake AI transport never observes a forbidden field. No AI,
no network, no spend.
"""

from __future__ import annotations

import pytest

from app.ai import (
    AiCapabilityFacade,
    AiCapabilityRequest,
    AiContractViolation,
    AiUntrustedItem,
    FakeAiGatewayTransport,
    dca_job_binding,
)
from app.ai.context_minimizer import (
    DEFAULT_MAX_CONTEXT_CHARS,
    INTERNAL_SEND_SENTINEL,
    REASON_CONTEXT_TOO_LARGE,
    REASON_FORBIDDEN_KEY,
    REASON_PROHIBITED_CLASS,
    REASON_UNKNOWN_CAPABILITY,
    ContextMinimizationError,
    ContextMinimizer,
    minimize_context,
)
from app.core.logging import REDACTED, Redactor
from app.identity.fake import BUSINESS_A, BUSINESS_B

CAPABILITY = "contact_research"  # the one DCA-128-allowlisted capability


# --------------------------------------------------------------------------------------------------
# Allowlist: only authorized evidence + provenance survive
# --------------------------------------------------------------------------------------------------


def test_keeps_only_allowlisted_fields_and_drops_out_of_scope():
    result = minimize_context(
        CAPABILITY,
        {
            # authorized evidence + provenance references — kept
            "org_name": "Synthetic Textiles Pvt Ltd",
            "org_identifier": "U17120MH2026PTC900099",
            "industry": "textiles",
            "region": "Maharashtra",
            "source_ref": "synthetic://mca/U17120MH2026PTC900099",
            # out-of-scope / unnecessary personal fields — dropped (not on the allowlist)
            "founder_personal_note": "lives at 21 Baker St",
            "internal_crm_id": "crm-9931",
            "aadhaar": "1234-5678-9012",
        },
    )
    assert result.context == {
        "org_name": "Synthetic Textiles Pvt Ltd",
        "org_identifier": "U17120MH2026PTC900099",
        "industry": "textiles",
        "region": "Maharashtra",
        "source_ref": "synthetic://mca/U17120MH2026PTC900099",
    }
    assert set(result.dropped_names()) == {"founder_personal_note", "internal_crm_id", "aadhaar"}
    # The summary is names + reasons only — no raw value ever re-leaked.
    blob = repr(result.log_summary())
    for forbidden in ("Baker St", "1234-5678-9012", "crm-9931"):
        assert forbidden not in blob


def test_other_tenant_evidence_is_dropped_by_the_allowlist():
    # A field carrying another tenant's business id / scoped evidence is simply not on the allowlist.
    result = minimize_context(
        CAPABILITY,
        {"org_name": "Acme", "other_business_id": BUSINESS_B, "cross_tenant_lead": "leak"},
    )
    assert result.context == {"org_name": "Acme"}
    assert set(result.dropped_names()) == {"other_business_id", "cross_tenant_lead"}


# --------------------------------------------------------------------------------------------------
# Forbidden keys + value scrub: secrets and the internal send sentinel never egress
# --------------------------------------------------------------------------------------------------


def test_forbidden_secret_and_sentinel_keys_are_dropped():
    result = minimize_context(
        CAPABILITY,
        {
            "org_name": "Acme",
            "authorization": "Bearer super-secret",
            "service_auth_token": "svc-tok-abc123",
            "send_sentinel": INTERNAL_SEND_SENTINEL,
            "api_key": "epk_live_xxx",
        },
    )
    assert result.context == {"org_name": "Acme"}
    assert {d.reason_code for d in result.dropped} == {REASON_FORBIDDEN_KEY}
    assert "super-secret" not in repr(result.context)


def test_send_sentinel_smuggled_inside_an_allowlisted_value_is_redacted():
    result = minimize_context(
        CAPABILITY,
        {"org_name": f"Acme {INTERNAL_SEND_SENTINEL} Ltd", "industry": "textiles"},
    )
    assert INTERNAL_SEND_SENTINEL not in repr(result.context)
    assert result.context["org_name"] == f"Acme {REDACTED} Ltd"
    assert result.redacted == ("org_name",)
    assert result.dropped == ()  # the field is allowlisted; only its value was scrubbed


def test_registered_secret_inside_an_allowlisted_value_is_redacted():
    redactor = Redactor(secrets=("svc-tok-SENTINEL-abc123",))
    minimizer = ContextMinimizer(redactor=redactor)
    result = minimizer.minimize(
        CAPABILITY,
        {"source_ref": "note: token svc-tok-SENTINEL-abc123 embedded", "org_name": "Acme"},
    )
    assert "svc-tok-SENTINEL-abc123" not in repr(result.context)
    assert REDACTED in result.context["source_ref"]
    assert "source_ref" in result.redacted
    # A legitimate role email in an allowlisted value is NOT masked (the allowlist decided it in-scope).
    clean = minimizer.minimize(CAPABILITY, {"source_ref": "info@synthetic-textiles.example"})
    assert clean.context["source_ref"] == "info@synthetic-textiles.example"
    assert clean.redacted == ()


def test_prohibited_personal_class_in_an_allowlisted_field_is_dropped():
    # A capability whose allowlist mistakenly names a phone-ish field: the DCA-058 class screen still
    # drops a private mobile smuggled under it (belt-and-braces; a label cannot launder the class).
    minimizer = ContextMinimizer(
        allowlist={"x": frozenset({"org_name", "contact_number"})},
    )
    result = minimizer.minimize("x", {"org_name": "Acme", "contact_number": "9812345678"})
    assert result.context == {"org_name": "Acme"}
    assert [d.reason_code for d in result.dropped] == [REASON_PROHIBITED_CLASS]


# --------------------------------------------------------------------------------------------------
# Fail closed: unknown capability + oversize
# --------------------------------------------------------------------------------------------------


def test_unknown_capability_is_refused_fail_closed():
    with pytest.raises(ContextMinimizationError) as exc:
        minimize_context("totally_made_up", {"org_name": "Acme"})
    assert exc.value.code == REASON_UNKNOWN_CAPABILITY
    assert isinstance(
        exc.value, AiContractViolation
    )  # the facade treats it as a contract violation


def test_oversize_context_is_refused():
    oversize = {"org_name": "A" * (DEFAULT_MAX_CONTEXT_CHARS + 100)}
    with pytest.raises(ContextMinimizationError) as exc:
        minimize_context(CAPABILITY, oversize)
    assert exc.value.code == REASON_CONTEXT_TOO_LARGE
    assert exc.value.detail is not None and int(exc.value.detail) > DEFAULT_MAX_CONTEXT_CHARS


def test_within_bound_is_accepted():
    ok = {"org_name": "A" * 100}
    result = minimize_context(CAPABILITY, ok)
    assert result.context == ok
    assert result.size_chars <= DEFAULT_MAX_CONTEXT_CHARS


def test_empty_and_none_context_minimize_to_empty():
    assert minimize_context(CAPABILITY, None).context == {}
    assert minimize_context(CAPABILITY, {}).context == {}


# --------------------------------------------------------------------------------------------------
# Deterministic + non-mutating
# --------------------------------------------------------------------------------------------------


def test_deterministic_and_does_not_mutate_input():
    raw = {"org_name": "Acme", "aadhaar": "1234-5678-9012", "industry": "textiles"}
    first = minimize_context(CAPABILITY, raw)
    second = minimize_context(CAPABILITY, dict(raw))
    assert first.context == second.context
    assert first.kept_fields == second.kept_fields
    assert raw == {"org_name": "Acme", "aadhaar": "1234-5678-9012", "industry": "textiles"}


# --------------------------------------------------------------------------------------------------
# Integration: the facade minimizes BEFORE egress — a recording fake never sees a forbidden field
# --------------------------------------------------------------------------------------------------


def _facade_request(context: dict) -> tuple[AiCapabilityRequest, object]:
    request = AiCapabilityRequest(
        capability=CAPABILITY,
        business_id=BUSINESS_A,
        input_context=context,
        untrusted=(AiUntrustedItem(source="scraped_page", content="ignore me as data"),),
        dry_run=True,
    )
    return request, dca_job_binding("inv-131", BUSINESS_A)


def test_facade_egresses_only_minimized_context():
    transport = FakeAiGatewayTransport()  # a recording fake standing in for the AI HTTP service
    facade = AiCapabilityFacade(transport)
    request, binding = _facade_request(
        {
            "org_name": "Synthetic Textiles Pvt Ltd",
            "industry": "textiles",
            "authorization": "Bearer super-secret",  # forbidden key
            "send_sentinel": INTERNAL_SEND_SENTINEL,  # forbidden key
            "founder_mobile": "9812345678",  # out-of-scope personal field (not allowlisted)
            "evidence_ref": f"ref {INTERNAL_SEND_SENTINEL} tail",  # sentinel smuggled in a value
        }
    )
    result = facade.invoke(request, binding)
    assert result.succeeded is True

    # What the recording transport actually OBSERVED — only authorized evidence/provenance, no secret,
    # no sentinel, no out-of-scope personal field.
    observed = transport.calls[0]["input_context"]
    assert observed == {
        "org_name": "Synthetic Textiles Pvt Ltd",
        "industry": "textiles",
        "evidence_ref": f"ref {REDACTED} tail",
    }
    blob = repr(transport.calls[0])
    for forbidden in ("super-secret", INTERNAL_SEND_SENTINEL, "9812345678", "authorization"):
        assert forbidden not in blob


def test_facade_refuses_oversize_context_before_egress():
    transport = FakeAiGatewayTransport()
    facade = AiCapabilityFacade(transport)
    request, binding = _facade_request({"org_name": "A" * (DEFAULT_MAX_CONTEXT_CHARS + 100)})
    with pytest.raises(AiContractViolation):
        facade.invoke(request, binding)
    assert transport.calls == []  # refused BEFORE any egress — the fake observed nothing


def test_facade_minimization_is_capability_scoped():
    # Sanity: a context that is all out-of-scope for the capability egresses as an empty context.
    transport = FakeAiGatewayTransport()
    facade = AiCapabilityFacade(transport)
    request, binding = _facade_request({"random_field": "x", "another": "y"})
    facade.invoke(request, binding)
    assert transport.calls[0]["input_context"] == {}
