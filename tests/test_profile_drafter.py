"""DCA-134 — draft a business profile with evidence (the AI-capability integration).

Two layers, matching the DCA-036/133 suites:

- **Service unit tests** (in-memory SQLite + the deterministic fixture transport) pin the drafting
  logic: grounded claims become a draft; ungrounded model output (invented / hallucinated / unrelated /
  unknown) is DROPPED via the independent DCA-133 grounding gate and changes nothing; the draft carries
  its uncertainty + references + the dropped record; it is a DRAFT and is never auto-confirmed; a
  rejected gateway output or a disabled capability stages NO draft; and everything is tenant-scoped.
- **HTTP tests** (``TestClient``, facade overridden with the fixture transport) prove the same over the
  real service API, plus retry-idempotency and the fail-closed 503 when AI is disabled.

No AI, no network, no model, no spend — the fixture transport stands in for the gateway (the real
gateway capability schema is pinned by effortlesspromo-ai's own contract test, not here).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.ai import AiCapabilityFacade, FakeAiGatewayTransport
from app.ai.citation_validator import (
    REASON_CITATION_NOT_IN_SOURCE,
    REASON_FIELD_NOT_PERMITTED,
    REASON_UNKNOWN_FACT,
)
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import ProfileStatus
from app.main import create_app
from app.services.profile_drafter import (
    CAPABILITY,
    REASON_CAPABILITY_DISABLED,
    REASON_OUTPUT_REJECTED,
    RetrievedEvidence,
    draft_profile_from_evidence,
)
from app.services.profile_store import ProfileStore
from tests.conftest import AUTH, make_settings

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"

# One authorised page the DCA retrieved — the ONLY material a claim may cite.
PAGE = (
    "About Acme Teas Pvt Ltd.\n"
    "We sell small-batch Indian teas sourced direct from growers.\n"
    "Industry: retail.\n"
    "FSSAI licensed and GST registered in Karnataka."
)

EVIDENCE = [
    RetrievedEvidence(
        ref="ev-1",
        content=PAGE,
        permitted_fields=frozenset({"value_proposition", "business_type", "products"}),
        rights_valid=True,
        provenance="website:acme-about",
    )
]

# A realistic model draft (the shape the gateway's business_profile_draft capability returns): two
# genuinely grounded claims, one hallucinated (span absent), one unrelated (field not permitted) and
# one unknown (null value). Only the grounded two may survive the DCA-133 grounding gate.
MODEL_OUTPUT = {
    "claims": [
        {
            "field": "value_proposition",
            "value": "small-batch Indian teas",
            "citationRef": "ev-1",
            "citedText": "We sell small-batch Indian teas sourced direct from growers.",
            "inferenceLabel": "observed",
            "confidence": 0.86,
        },
        {
            "field": "business_type",
            "value": "retail",
            "citationRef": "ev-1",
            "citedText": "Industry: retail.",
            "inferenceLabel": "observed",
            "confidence": 0.8,
        },
        {
            "field": "products",  # HALLUCINATED: this span never appears in the page
            "value": "rockets",
            "citationRef": "ev-1",
            "citedText": "We sell rockets to the moon.",
            "inferenceLabel": "observed",
            "confidence": 0.9,
        },
        {
            "field": "cin",  # UNRELATED: the source is not permitted to supply `cin`
            "value": "U17110KA2010PTC000000",
            "citationRef": "ev-1",
            "citedText": "Industry: retail.",
            "inferenceLabel": "observed",
            "confidence": 0.7,
        },
        {
            "field": "gstin",  # UNKNOWN: the model could not find a value — it stays null, dropped
            "value": None,
            "citationRef": "ev-1",
            "citedText": "GST registered in Karnataka.",
            "inferenceLabel": "observed",
            "confidence": 0.3,
        },
    ],
    "unknowns": ["languages"],
    "confidence": 0.78,
}


def _facade(structured_output=MODEL_OUTPUT, validation=None, enabled=True) -> AiCapabilityFacade:
    """A facade over the deterministic fixture transport returning a crafted draft (no network/spend).
    Untrusted evidence taints the real gateway, so the default fixture flags humanReviewRequired/taint."""
    if validation is None:
        validation = {"humanReviewRequired": True, "taint": True}
    return AiCapabilityFacade(
        FakeAiGatewayTransport(
            enabled=enabled, structured_output=structured_output, validation=validation
        )
    )


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def _draft(factory, business_id, facade, **overrides):
    with factory() as session:
        result = draft_profile_from_evidence(
            session,
            facade,
            business_id,
            business_name=overrides.pop("business_name", "Acme Teas"),
            evidence=overrides.pop("evidence", EVIDENCE),
            invocation_key=overrides.pop("invocation_key", f"draft:{business_id}:1"),
            draft_fields=overrides.pop(
                "draft_fields", ["value_proposition", "business_type", "products", "languages"]
            ),
            **overrides,
        )
        session.commit()
        return result


# --------------------------------------------------------------------------------------------------
# Service unit tests
# --------------------------------------------------------------------------------------------------


def test_grounded_claims_become_a_draft_with_uncertainty_and_references(session_factory):
    result = _draft(session_factory, BIZ_A, _facade())
    assert result.produced
    draft = result.draft
    assert draft.status == ProfileStatus.DRAFT.value
    # The two grounded claims landed on the canonical aspects.
    assert draft.value_proposition == "small-batch Indian teas"
    assert draft.business_type == "retail"
    # Provenance retains uncertainty (overall + per-claim confidence) and the references (citations).
    assert draft.provenance["overall_confidence"] == 0.78
    refs = draft.provenance["references"]
    assert {r["field"] for r in refs} == {"value_proposition", "business_type"}
    assert all(r["citationRef"] == "ev-1" for r in refs)
    assert draft.source == "business_profile_draft"


def test_ungrounded_model_output_is_dropped_and_changes_nothing(session_factory):
    result = _draft(session_factory, BIZ_A, _facade())
    # Only the two genuinely grounded claims survived; the rest were dropped by the grounding gate.
    assert {f.field_key for f in result.grounded} == {"value_proposition", "business_type"}
    dropped = {v.fact.field_key: v.reason for v in result.dropped}
    assert dropped["products"] == REASON_CITATION_NOT_IN_SOURCE  # hallucinated span
    assert dropped["cin"] == REASON_FIELD_NOT_PERMITTED  # unrelated field
    assert dropped["gstin"] == REASON_UNKNOWN_FACT  # null value stays null
    # The hallucinated 'products' never reached the draft; it is an honest unknown instead.
    assert result.draft.products == []
    assert "products" in result.draft.unknowns
    # The dropped record is retained on the draft for review (names + reasons, never a write).
    assert {d["field"] for d in result.draft.provenance["dropped"]} == {"products", "cin", "gstin"}


def test_draft_is_never_auto_applied(session_factory):
    result = _draft(session_factory, BIZ_A, _facade())
    assert result.draft.status == ProfileStatus.DRAFT.value
    # Untrusted evidence → held for review; a drafted profile is never silently confirmed.
    assert result.draft.provenance["held_for_review"] is True
    with session_factory() as session:
        # No confirmed profile was created by drafting.
        assert ProfileStore(session, BIZ_A).confirmed() is None


def test_rejected_gateway_output_stages_no_draft(session_factory):
    rejecting = _facade(
        validation={"validationStatus": "failed", "validationReport": {"status": "failed"}}
    )
    result = _draft(session_factory, BIZ_A, rejecting)
    assert result.draft is None
    assert result.refused_reason == REASON_OUTPUT_REJECTED
    with session_factory() as session:
        assert ProfileStore(session, BIZ_A).versions() == []  # nothing staged


def test_disabled_capability_stages_no_draft(session_factory):
    # No facade at all (AI disabled in prod) — fail closed.
    assert _draft(session_factory, BIZ_A, None).refused_reason == REASON_CAPABILITY_DISABLED
    # A wired-but-disabled transport — equally fail closed.
    result = _draft(session_factory, BIZ_A, _facade(enabled=False))
    assert result.refused_reason == REASON_CAPABILITY_DISABLED
    with session_factory() as session:
        assert ProfileStore(session, BIZ_A).versions() == []


def test_drafting_is_tenant_scoped(session_factory):
    _draft(session_factory, BIZ_A, _facade())
    with session_factory() as session:
        # A's draft is confined to A; B's store never sees it.
        assert [d.version for d in ProfileStore(session, BIZ_A).versions()] == [1]
        assert ProfileStore(session, BIZ_B).versions() == []


def test_evidence_bodies_never_enter_the_trusted_context(session_factory):
    transport = FakeAiGatewayTransport(structured_output=MODEL_OUTPUT, validation={"taint": True})
    facade = AiCapabilityFacade(transport)
    _draft(session_factory, BIZ_A, facade)
    call = transport.calls[0]
    assert call["capability"] == CAPABILITY
    # The evidence body rides ONLY the untrusted channel; the trusted context carries just identity/refs.
    assert "small-batch Indian teas" not in repr(call["input_context"])
    assert call["input_context"].get("evidenceRefs") == ["ev-1"]
    assert call["untrusted"] == [{"source": "website:acme-about", "content": PAGE}]


# --------------------------------------------------------------------------------------------------
# HTTP tests — the real service API
# --------------------------------------------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    app = create_app(make_settings())
    with TestClient(app, raise_server_exceptions=False) as test_client:
        # Override the default fixture facade with one returning a crafted, realistic draft.
        test_client.app.state.ai_capability_facade = _facade()
        for biz in (BIZ_A, BIZ_B):
            test_client.post("/v1/tenants", json={"businessId": biz}, headers=AUTH)
        yield test_client


def _body(**overrides) -> dict:
    body = {
        "businessName": "Acme Teas",
        "evidence": [
            {
                "ref": "ev-1",
                "content": PAGE,
                "permittedFields": ["value_proposition", "business_type", "products"],
                "rightsValid": True,
                "provenance": "website:acme-about",
            }
        ],
        "draftFields": ["value_proposition", "business_type", "products", "languages"],
    }
    body.update(overrides)
    return body


def test_http_draft_from_evidence_returns_grounded_draft(client):
    resp = client.post(
        f"/v1/tenants/{BIZ_A}/profile/draft-from-evidence", json=_body(), headers=AUTH
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "draft"
    assert body["businessType"] == "retail"
    grounding = body["grounding"]
    assert {g["field"] for g in grounding["grounded"]} == {"value_proposition", "business_type"}
    assert {d["field"] for d in grounding["dropped"]} == {"products", "cin", "gstin"}
    assert grounding["heldForReview"] is True
    # Drafting created no confirmed profile.
    confirmed = client.get(f"/v1/tenants/{BIZ_A}/profile/confirmed", headers=AUTH).json()
    assert confirmed["confirmed"] is False


def test_http_draft_is_idempotent_on_retry(client):
    from app.idempotency import IDEMPOTENCY_HEADER

    keyed = {**AUTH, IDEMPOTENCY_HEADER: "draft-ev-key-1"}
    first = client.post(
        f"/v1/tenants/{BIZ_A}/profile/draft-from-evidence", json=_body(), headers=keyed
    )
    replay = client.post(
        f"/v1/tenants/{BIZ_A}/profile/draft-from-evidence", json=_body(), headers=keyed
    )
    assert first.status_code == 201
    assert replay.json() == first.json()
    assert replay.headers.get("Idempotent-Replay") == "true"
    # The retry created no second version.
    listed = client.get(f"/v1/tenants/{BIZ_A}/profile/drafts", headers=AUTH).json()
    assert [row["version"] for row in listed] == [1]


def test_http_fails_closed_when_ai_disabled(client):
    client.app.state.ai_capability_facade = None  # AI disabled (production, no gateway)
    resp = client.post(
        f"/v1/tenants/{BIZ_A}/profile/draft-from-evidence", json=_body(), headers=AUTH
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["message"] == REASON_CAPABILITY_DISABLED
    # No draft was staged.
    assert client.get(f"/v1/tenants/{BIZ_A}/profile/drafts", headers=AUTH).json() == []


def test_http_unregistered_tenant_is_404(client):
    resp = client.post(
        "/v1/tenants/99999999-9999-9999-9999-999999999999/profile/draft-from-evidence",
        json=_body(),
        headers=AUTH,
    )
    assert resp.status_code == 404
