"""DCA-136 — extract permitted unstructured observations (the AI counterpart of DCA-124).

Pins the load-bearing behaviours of :mod:`app.sources.unstructured_extract` — the organization-only AI
extractor that turns already-fetched, APPROVED + entitled source CONTENT into permitted organization
:class:`~app.db.models.FieldObservation` rows through the one DCA AI chokepoint, each citation-grounded
before it is stored:

1. **Permitted, grounded observations are recorded with provenance + a cited evidence object.** An
   approved+entitled source records the grounded permitted fields as observed :class:`FieldObservation`
   rows (carrying the source + a provenance_ref that cites the captured content evidence), captures the
   content as an :class:`EvidenceObject`, and records an :class:`UnstructuredObservationExtraction` row.
2. **Ungrounded / hallucinated output is DROPPED and changes nothing.** A model observation whose cited
   span is absent from the content is dropped by the independent DCA-133 grounding gate (recorded,
   never written).
3. **Personal / non-permitted data is excluded.** A model observation on a field outside the source's
   permitted allowlist (e.g. a contact email) is dropped as not-permitted — no contact field is ever
   written; a null value is dropped as an unknown.
4. **An unentitled source extracts nothing — and never calls the model.** With no entitlement, no field
   is permitted: the extraction refuses, stages nothing, and makes NO gateway call (no spend).
5. **A rejected gateway output / a disabled capability stage nothing.**
6. **Idempotent re-extraction.** Re-running the same content is a deterministic no-op (skipped), with no
   second model call and no duplicate observations.
7. **Tenant isolation.** Two businesses extracting the same content get independent, business-scoped
   observations; neither sees the other's extraction provenance.

No AI, no network, no model, no spend — the deterministic fixture transport stands in for the gateway
(the real gateway capability schema + its rule_check bound are pinned by effortlesspromo-ai's own
contract test). Unit-tested on in-memory SQLite; the RLS/migration paths for the table are covered by
``tests/test_rls.py`` and ``tests/test_migrations.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.ai import AiCapabilityFacade, FakeAiGatewayTransport
from app.ai.citation_validator import (
    REASON_CITATION_NOT_IN_SOURCE,
    REASON_FIELD_NOT_PERMITTED,
    REASON_UNKNOWN_FACT,
)
from app.audit.sink import ActorType, AuditActor
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import (
    AuditEvent,
    BusinessSourceEntitlement,
    ContactPoint,
    EvidenceObject,
    FieldObservation,
    PersonObservation,
    Source,
    SourceAccessMethod,
    SourceApprovalStatus,
    SourceKind,
    SourceStatus,
    SourceTerms,
    SourceTermsApproval,
    SourceTermsStatus,
    UnstructuredObservationExtraction,
)
from app.services.org_observation import ObservationInput, OrgObservationStore
from app.sources.unstructured_extract import (
    CAPABILITY,
    REASON_CAPABILITY_DISABLED,
    REASON_OUTPUT_REJECTED,
    REASON_SOURCE_NOT_ENTITLED,
    REASON_SUBJECT_NOT_FOUND,
    FetchedContent,
    UnstructuredExtractError,
    UnstructuredExtractRequest,
    UnstructuredObservationExtractor,
)

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
ACTOR = AuditActor(id="00000000-0000-4000-9000-0000000a0136", type=ActorType.USER)

SOURCE_KEY = "acme_profile_doc"
PURPOSE = "prospect_enrichment"
CONTENT_REF = "doc://acme/profile-2026"
PERMITTED = ["legal_name", "industry", "description", "cin", "gstin"]

# A synthetic APPROVED content body: permitted company facts, PLUS a personal contact. The company facts
# are recorded; the contact (a non-permitted field) is dropped; a hallucinated span grounds nothing.
CONTENT_BODY = (
    "About Synthetic Textiles Pvt Ltd.\n"
    "Legal name: Synthetic Textiles Pvt Ltd.\n"
    "Industry: Textile Manufacturing.\n"
    "Description: A synthetic textiles manufacturer based in Maharashtra.\n"
    "Contact our founder at founder@synthetic.example or +919876543210.\n"
)
CONTENT = FetchedContent(
    content_ref=CONTENT_REF,
    body=CONTENT_BODY,
    content_media_type="text/plain",
    content_kind="business_profile_doc",
    provenance="source:acme-profile",
)

# A realistic model output: three genuinely grounded permitted observations, one hallucinated (span
# absent), one on a NON-permitted personal field (email), and one unknown (null value). Only the
# grounded three may survive the DCA-133 grounding gate.
MODEL_OUTPUT = {
    "observations": [
        {
            "field": "legal_name",
            "value": "Synthetic Textiles Pvt Ltd",
            "citationRef": CONTENT_REF,
            "citedText": "Legal name: Synthetic Textiles Pvt Ltd.",
            "inferenceLabel": "observed",
            "confidence": 0.88,
        },
        {
            "field": "industry",
            "value": "Textile Manufacturing",
            "citationRef": CONTENT_REF,
            "citedText": "Industry: Textile Manufacturing.",
            "inferenceLabel": "observed",
            "confidence": 0.84,
        },
        {
            "field": "description",
            "value": "A synthetic textiles manufacturer based in Maharashtra",
            "citationRef": CONTENT_REF,
            "citedText": "Description: A synthetic textiles manufacturer based in Maharashtra.",
            "inferenceLabel": "observed",
            "confidence": 0.8,
        },
        {
            "field": "cin",
            "value": "U17120MH2026PTC900099",
            "citationRef": CONTENT_REF,
            "citedText": "CIN: U17120MH2026PTC900099.",  # HALLUCINATED: this span is NOT in the content
            "inferenceLabel": "observed",
            "confidence": 0.7,
        },
        {
            "field": "email",
            "value": "founder@synthetic.example",
            "citationRef": CONTENT_REF,
            "citedText": "Contact our founder at founder@synthetic.example or +919876543210.",
            "inferenceLabel": "observed",
            "confidence": 0.9,
        },  # NON-PERMITTED personal field
        {
            "field": "gstin",
            "value": None,
            "citationRef": CONTENT_REF,
            "citedText": "GST registered in Maharashtra.",
            "inferenceLabel": "observed",
            "confidence": 0.3,
        },  # UNKNOWN: null stays null, dropped
    ],
    "unknowns": [],
    "confidence": 0.82,
}


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def _transport(structured_output=MODEL_OUTPUT, validation=None, enabled=True):
    """The deterministic fixture transport returning a crafted model output (no network/model/spend).
    The fetched content is untrusted, so the default fixture flags humanReviewRequired/taint."""
    if validation is None:
        validation = {"humanReviewRequired": True, "taint": True}
    return FakeAiGatewayTransport(
        enabled=enabled, structured_output=structured_output, validation=validation
    )


def _facade(transport=None) -> AiCapabilityFacade:
    return AiCapabilityFacade(transport if transport is not None else _transport())


def _seed_catalog(session, *, source_key: str = SOURCE_KEY) -> None:
    """Catalogue + record terms + approve the source ONCE (the ``sources`` catalog is a global
    control-plane registry, not tenant-scoped). Enabling is synthetic (the running service never promotes
    a source to ENABLED — a disabled future capability)."""
    session.add(
        Source(
            key=source_key,
            name=source_key,
            kind=SourceKind.COMMERCIAL.value,
            trust_tier="business_document",
            capabilities=["org_discovery"],
            fields=list(PERMITTED),
            access_method=SourceAccessMethod.PORTAL_LOOKUP.value,
            india_coverage={"nationwide": True, "states": []},
            status=SourceStatus.ENABLED.value,
            enabled=True,
            permissions=[],
        )
    )
    session.add(
        SourceTerms(
            source_key=source_key,
            version=1,
            operation_rights={"collect": True, "store": True},
            field_rights={},
            purposes=[PURPOSE],
            contract_evidence={"reference": "lic-1", "kind": "licence", "uri": ""},
            status=SourceTermsStatus.ACTIVE.value,
        )
    )
    session.add(
        SourceTermsApproval(
            source_key=source_key,
            terms_version=1,
            reviewer_id="op-1",
            reviewer_type="platform_operator",
            review_evidence={"reference": "rev-1", "kind": "review", "uri": ""},
            reviewed_at=NOW - timedelta(days=1),
            effective_from=NOW - timedelta(days=1),
            expires_at=NOW + timedelta(days=365),
            approved_operation_rights={"collect": True, "store": True},
            approved_field_rights={},
            status=SourceApprovalStatus.ACTIVE.value,
        )
    )


def _entitle(session, business_id: str, *, source_key: str = SOURCE_KEY) -> None:
    """Grant ``business_id`` the DCA-047 entitlement to COLLECT/store the permitted fields for PURPOSE."""
    session.add(
        BusinessSourceEntitlement(
            business_id=business_id,
            source_key=source_key,
            version=1,
            enabled=True,
            operation_rights={"collect": True, "store": True},
            field_rights={},
            purposes=[PURPOSE],
            approved_terms_version=1,
            actor_id="seed",
            actor_type="user",
        )
    )


def _seed_approved_entitled_source(
    session, business_id: str, *, source_key: str = SOURCE_KEY
) -> None:
    """Seed the full DCA-044..048 rights chain so ``source_key`` is approved+entitled for COLLECT on the
    permitted organization fields for ``PURPOSE`` (for single-business tests)."""
    _seed_catalog(session, source_key=source_key)
    _entitle(session, business_id, source_key=source_key)


def _seed_subject(session, business_id: str, *, name: str = "Synthetic Textiles Pvt Ltd") -> str:
    obs = OrgObservationStore(session, business_id).create(
        ObservationInput(entity_type="legal_entity", display_name=name), actor=ACTOR, now=NOW
    )
    session.flush()
    return obs.id


def _request(subject_id: str, **overrides) -> UnstructuredExtractRequest:
    base = {
        "subject_observation_id": subject_id,
        "source_key": SOURCE_KEY,
        "source_version": 1,
        "purpose": PURPOSE,
        "invocation_key": f"extract:{subject_id}:1",
        "scope": "MH",
    }
    base.update(overrides)
    return UnstructuredExtractRequest(**base)


# --- 1/2/3) grounded recorded; ungrounded/personal/unknown dropped ---------------------------------


def test_grounded_permitted_observations_recorded_citing_evidence(session_factory):
    with session_factory() as session:
        _seed_approved_entitled_source(session, BIZ_A)
        subject_id = _seed_subject(session, BIZ_A)
        session.commit()

    with session_factory() as session:
        result = UnstructuredObservationExtractor(session, BIZ_A).extract(
            _facade(), _request(subject_id), CONTENT, ACTOR, now=NOW
        )
        assert result.produced
        assert {o.field_key for o in result.recorded} == {"legal_name", "industry", "description"}
        assert all(o.inference_label == "observed" for o in result.recorded)
        assert result.fields_recorded == 3
        # Untrusted content → held for review (never silently auto-applied beyond being observations).
        assert result.outcome is not None and result.outcome.held_for_review is True
        assert result.evidence_id is not None
        session.commit()

    with session_factory() as session:
        fields = session.query(FieldObservation).all()
        assert {f.field_key for f in fields} == {"legal_name", "industry", "description"}
        assert all(f.subject_id == subject_id for f in fields)
        assert all(f.source_key == SOURCE_KEY and f.source_version == 1 for f in fields)
        assert all(f.scope == "MH" for f in fields)
        assert all("#evidence=" in f.provenance_ref for f in fields)
        # the recorded values are the grounded ones (never fabricated)
        by_key = {f.field_key: f.field_value for f in fields}
        assert by_key["industry"] == "Textile Manufacturing"

        # The content is captured as the evidence the observations cite; NO person/contact rows exist.
        evidence = session.query(EvidenceObject).one()
        assert evidence.content_ref == CONTENT_REF
        assert evidence.subject_id == subject_id
        assert all(f.provenance_ref.endswith(evidence.id) for f in fields)
        assert session.query(PersonObservation).count() == 0
        assert session.query(ContactPoint).count() == 0

        # One run-level provenance row with the idempotency handle + the dropped witness.
        prov = UnstructuredObservationExtractor(session, BIZ_A).extractions_for(
            SOURCE_KEY, subject_id
        )
        assert len(prov) == 1
        assert prov[0].content_ref == CONTENT_REF
        assert prov[0].evidence_id == evidence.id
        assert prov[0].fields_recorded == 3
        assert prov[0].observations_dropped == 3  # hallucinated + non-permitted + unknown


def test_ungrounded_personal_and_unknown_observations_are_dropped(session_factory):
    with session_factory() as session:
        _seed_approved_entitled_source(session, BIZ_A)
        subject_id = _seed_subject(session, BIZ_A)
        session.commit()

    with session_factory() as session:
        result = UnstructuredObservationExtractor(session, BIZ_A).extract(
            _facade(), _request(subject_id), CONTENT, ACTOR, now=NOW
        )
        dropped = {v.fact.field_key: v.reason for v in result.dropped}
        assert dropped["cin"] == REASON_CITATION_NOT_IN_SOURCE  # hallucinated span
        assert (
            dropped["email"] == REASON_FIELD_NOT_PERMITTED
        )  # personal/non-permitted field excluded
        assert dropped["gstin"] == REASON_UNKNOWN_FACT  # null value stays null
        session.commit()

    with session_factory() as session:
        # The personal email never became an observation (no contact field, no person/contact row).
        values = {f.field_value for f in session.query(FieldObservation).all()}
        assert "founder@synthetic.example" not in values
        assert (
            session.query(FieldObservation).filter(FieldObservation.field_key == "email").count()
            == 0
        )


# --- 4) an unentitled source extracts nothing AND never calls the model ----------------------------


def test_unentitled_source_extracts_nothing_and_makes_no_gateway_call(session_factory):
    with session_factory() as session:
        # Source + subject exist, but NO rights chain (no terms/approval/entitlement) -> no permitted field.
        session.add(
            Source(
                key=SOURCE_KEY,
                name=SOURCE_KEY,
                kind=SourceKind.COMMERCIAL.value,
                trust_tier="business_document",
                capabilities=["org_discovery"],
                fields=list(PERMITTED),
                access_method=SourceAccessMethod.PORTAL_LOOKUP.value,
                india_coverage={"nationwide": True, "states": []},
                status=SourceStatus.ENABLED.value,
                enabled=True,
                permissions=[],
            )
        )
        subject_id = _seed_subject(session, BIZ_A)
        session.commit()

    with session_factory() as session:
        transport = _transport()
        result = UnstructuredObservationExtractor(session, BIZ_A).extract(
            _facade(transport), _request(subject_id), CONTENT, ACTOR, now=NOW
        )
        assert result.produced is False
        assert result.refused_reason == REASON_SOURCE_NOT_ENTITLED
        assert result.recorded == () and result.evidence_id is None
        assert transport.calls == []  # the model was NEVER called (no egress, no spend)
        session.rollback()

    with session_factory() as session:
        assert session.query(FieldObservation).count() == 0
        assert session.query(EvidenceObject).count() == 0
        assert session.query(UnstructuredObservationExtraction).count() == 0
        # Only the subject-seed audit remains; the refused extraction added no audit entry of its own.
        assert session.query(AuditEvent).count() == 1


# --- 5) rejected gateway output / disabled capability stage nothing --------------------------------


def test_rejected_gateway_output_stages_nothing(session_factory):
    with session_factory() as session:
        _seed_approved_entitled_source(session, BIZ_A)
        subject_id = _seed_subject(session, BIZ_A)
        session.commit()

    with session_factory() as session:
        rejecting = _transport(
            validation={"validationStatus": "failed", "validationReport": {"status": "failed"}}
        )
        result = UnstructuredObservationExtractor(session, BIZ_A).extract(
            _facade(rejecting), _request(subject_id), CONTENT, ACTOR, now=NOW
        )
        assert result.refused_reason == REASON_OUTPUT_REJECTED
        assert result.recorded == () and result.evidence_id is None
        session.rollback()

    with session_factory() as session:
        assert session.query(FieldObservation).count() == 0
        assert session.query(EvidenceObject).count() == 0
        assert session.query(UnstructuredObservationExtraction).count() == 0


def test_disabled_capability_stages_nothing(session_factory):
    with session_factory() as session:
        _seed_approved_entitled_source(session, BIZ_A)
        subject_id = _seed_subject(session, BIZ_A)
        session.commit()

    with session_factory() as session:
        # No facade at all (AI disabled in prod) — fail closed.
        assert (
            UnstructuredObservationExtractor(session, BIZ_A)
            .extract(None, _request(subject_id), CONTENT, ACTOR, now=NOW)
            .refused_reason
            == REASON_CAPABILITY_DISABLED
        )
        # A wired-but-disabled transport — equally fail closed, no call.
        disabled = _transport(enabled=False)
        result = UnstructuredObservationExtractor(session, BIZ_A).extract(
            _facade(disabled), _request(subject_id), CONTENT, ACTOR, now=NOW
        )
        assert result.refused_reason == REASON_CAPABILITY_DISABLED
        assert disabled.calls == []
        session.rollback()

    with session_factory() as session:
        assert session.query(UnstructuredObservationExtraction).count() == 0


# --- 6) idempotent re-extraction -------------------------------------------------------------------


def test_idempotent_reextraction_is_a_no_op(session_factory):
    with session_factory() as session:
        _seed_approved_entitled_source(session, BIZ_A)
        subject_id = _seed_subject(session, BIZ_A)
        session.commit()

    with session_factory() as session:
        UnstructuredObservationExtractor(session, BIZ_A).extract(
            _facade(), _request(subject_id), CONTENT, ACTOR, now=NOW
        )
        session.commit()

    with session_factory() as session:
        transport = _transport()
        again = UnstructuredObservationExtractor(session, BIZ_A).extract(
            _facade(transport), _request(subject_id), CONTENT, ACTOR, now=NOW
        )
        assert again.skipped is True
        assert again.recorded == ()
        assert transport.calls == []  # a replay never re-invokes the model (no spend)
        session.commit()

    with session_factory() as session:
        assert session.query(EvidenceObject).count() == 1
        assert session.query(FieldObservation).count() == 3
        assert session.query(UnstructuredObservationExtraction).count() == 1


# --- 7) request-shape / subject refusals change nothing --------------------------------------------


def test_unknown_subject_is_refused_before_any_state(session_factory):
    with session_factory() as session:
        _seed_approved_entitled_source(session, BIZ_A)
        _seed_subject(session, BIZ_A)
        session.commit()

    with session_factory() as session:
        with pytest.raises(UnstructuredExtractError) as exc:
            UnstructuredObservationExtractor(session, BIZ_A).extract(
                _facade(), _request("does-not-exist"), CONTENT, ACTOR, now=NOW
            )
        assert exc.value.code == REASON_SUBJECT_NOT_FOUND
        session.rollback()

    with session_factory() as session:
        assert session.query(FieldObservation).count() == 0
        assert session.query(UnstructuredObservationExtraction).count() == 0


def test_a_subject_of_another_business_reads_as_not_found(session_factory):
    with session_factory() as session:
        _seed_catalog(session)
        _entitle(session, BIZ_A)
        _entitle(session, BIZ_B)
        b_subject = _seed_subject(session, BIZ_B)
        session.commit()

    with session_factory() as session:
        with pytest.raises(UnstructuredExtractError) as exc:
            UnstructuredObservationExtractor(session, BIZ_A).extract(
                _facade(), _request(b_subject), CONTENT, ACTOR, now=NOW
            )
        assert exc.value.code == REASON_SUBJECT_NOT_FOUND


# --- 8) tenant isolation ---------------------------------------------------------------------------


def test_two_businesses_extract_the_same_content_independently(session_factory):
    with session_factory() as session:
        _seed_catalog(session)
        _entitle(session, BIZ_A)
        _entitle(session, BIZ_B)
        a_subject = _seed_subject(session, BIZ_A)
        b_subject = _seed_subject(session, BIZ_B)
        session.commit()

    with session_factory() as session:
        UnstructuredObservationExtractor(session, BIZ_A).extract(
            _facade(), _request(a_subject), CONTENT, ACTOR, now=NOW
        )
        UnstructuredObservationExtractor(session, BIZ_B).extract(
            _facade(), _request(b_subject), CONTENT, ACTOR, now=NOW
        )
        session.commit()

    with session_factory() as session:
        a = UnstructuredObservationExtractor(session, BIZ_A).extractions_for(SOURCE_KEY, a_subject)
        b = UnstructuredObservationExtractor(session, BIZ_B).extractions_for(SOURCE_KEY, b_subject)
        assert len(a) == 1 and len(b) == 1
        assert a[0].business_id == BIZ_A and b[0].business_id == BIZ_B
        assert a[0].evidence_id != b[0].evidence_id  # independent, business-scoped evidence
        # Neither business sees the other's extraction provenance.
        assert (
            UnstructuredObservationExtractor(session, BIZ_A).extractions_for(SOURCE_KEY, b_subject)
            == []
        )


# --- capability wiring -----------------------------------------------------------------------------


def test_capability_key_is_the_registered_one():
    assert CAPABILITY == "unstructured_observation_extraction"
    # The DCA facade must allow REQUESTING the capability (it is in the DCA-128 allowlist).
    assert _facade().is_known_capability(CAPABILITY)


def test_content_body_never_enters_the_trusted_context(session_factory):
    with session_factory() as session:
        _seed_approved_entitled_source(session, BIZ_A)
        subject_id = _seed_subject(session, BIZ_A)
        session.commit()

    with session_factory() as session:
        transport = _transport()
        UnstructuredObservationExtractor(session, BIZ_A).extract(
            _facade(transport), _request(subject_id), CONTENT, ACTOR, now=NOW
        )
        session.commit()
    call = transport.calls[0]
    assert call["capability"] == CAPABILITY
    # The content body rides ONLY the untrusted channel; the trusted context carries no raw content.
    assert "Textile Manufacturing" not in repr(call["input_context"])
    assert call["input_context"].get("evidenceRefs") == [CONTENT_REF]
    assert call["input_context"].get("permittedFields") == sorted(PERMITTED)
    assert call["untrusted"] == [{"source": "source:acme-profile", "content": CONTENT_BODY}]
