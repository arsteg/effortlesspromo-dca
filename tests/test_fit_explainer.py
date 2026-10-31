"""DCA-138 — explain business fit (the AI-capability integration).

Pins the load-bearing behaviours of :mod:`app.services.fit_explainer` — the service that produces a
grounded, evidence-cited EXPLANATION of why a prospect fits the business's confirmed ICP, through the one
DCA AI chokepoint:

1. **A grounded, cited fit explanation is produced.** Each stored assessment cites a supported fact the
   DCA holds (its ref + a span present in the fact), and the advisory fit band is recorded.
2. **Fit is STRICTLY separate from eligibility — a high fit does NOT unblock a blocked prospect.** The
   caller's permission outcome is carried through verbatim; a top ``high`` fit leaves a ``blocked``
   eligibility exactly ``blocked``.
3. **Ungrounded claims are dropped.** An assessment whose cited span is not present in any supported fact
   (or cites an unknown ref) is dropped, never stored — the gateway's "valid" does not authorize a write.
4. **Sensitive inferred traits are never a scoring input.** A prohibited personal-class fact is excluded
   BEFORE egress (never in the outgoing context, never citeable) and recorded honestly.
5. **Tenant isolation.** Explanations are business-private.
6. **Idempotent re-run.** Re-explaining the same subject returns the existing row — no duplicate, and the
   eligibility stays exactly as first recorded.
7. **Fail-closed denials.** A disabled capability, a rejected gateway output and no permitted supported
   facts each stage NO explanation (and never call the model where applicable).

No AI, no network, no model, no spend — the deterministic fixture transport stands in for the gateway
(the real gateway capability schema + its rule_check bound are pinned by effortlesspromo-ai's own
contract test). Unit-tested on in-memory SQLite; the RLS/migration paths for the table are covered by
``tests/test_rls.py`` and ``tests/test_migrations.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.ai import AiCapabilityFacade, FakeAiGatewayTransport
from app.compliance.policy_engine import PolicyDecision
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import FitExplanation
from app.quality.quality_summary import Eligibility
from app.services.fit_explainer import (
    CAPABILITY,
    EXCL_SENSITIVE_TRAIT,
    REASON_CAPABILITY_DISABLED,
    REASON_NO_SUPPORTED_FACTS,
    REASON_OUTPUT_REJECTED,
    EligibilityStatus,
    FitExplanationStore,
    SupportedFact,
    explain_business_fit,
)

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)

ICP = {"industries": ["textiles"], "sizeBands": ["small"], "roles": ["owner"], "geography": ["MH"]}

# The prospect's already-gathered supported facts (company facts only).
FACTS = [
    SupportedFact(ref="obs-1", field="industry", value="textiles"),
    SupportedFact(ref="obs-2", field="state", value="MH"),
]

ALLOWED = EligibilityStatus(decision=PolicyDecision.ALLOW, reason="allowed_by_rule")
BLOCKED = EligibilityStatus(decision=PolicyDecision.DENY, reason="denied_by_rule")


def make_session():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _output(assessments, *, fit_band="high", confidence=0.85) -> dict:
    return {
        "assessments": assessments,
        "fitBand": fit_band,
        "rationale": "advisory fit only — it changes no channel eligibility",
        "confidence": confidence,
    }


def _assessment(field, ref, cited, *, criterion=None, verdict="met", confidence=0.8) -> dict:
    return {
        "criterion": criterion or field,
        "field": field,
        "factRef": ref,
        "citedText": cited,
        "verdict": verdict,
        "inferenceLabel": "inferred",
        "confidence": confidence,
    }


def _facade(output, *, enabled=True, fail=False):
    """A fixture transport returning a crafted model output. Returns (facade, transport) for inspection."""
    transport = FakeAiGatewayTransport(enabled=enabled, fail=fail, structured_output=output)
    return AiCapabilityFacade(transport), transport


# 1) A grounded, cited fit explanation is produced --------------------------------------------


def test_grounded_fit_explanation_is_produced():
    sf = make_session()
    output = _output(
        [_assessment("industry", "obs-1", "textiles"), _assessment("state", "obs-2", "MH")]
    )
    facade, _ = _facade(output)
    with sf() as session:
        result = explain_business_fit(
            session,
            facade,
            BIZ_A,
            subject_ref="prospect-1",
            supported_facts=FACTS,
            eligibility=ALLOWED,
            icp=ICP,
            icp_version=3,
            invocation_key="fit:1",
            now=NOW,
        )
        session.commit()

    assert result.produced
    assert result.fit_band == "high"
    assert len(result.grounded) == 2
    assert result.synthetic is True
    exp = result.explanation
    assert exp.subject_ref == "prospect-1"
    assert exp.icp_version == 3
    # Every stored assessment cites a real supported fact + its span.
    refs = {a["factRef"] for a in exp.assessments}
    assert refs == {"obs-1", "obs-2"}
    assert all(a["citedText"] for a in exp.assessments)
    with sf() as session:
        assert session.query(FitExplanation).count() == 1


# 2) Fit is strictly separate from eligibility — a high fit does NOT unblock a blocked prospect


def test_high_fit_does_not_unblock_a_blocked_prospect():
    sf = make_session()
    # A perfect-looking fit, but the prospect's channel eligibility is BLOCKED.
    output = _output(
        [_assessment("industry", "obs-1", "textiles"), _assessment("state", "obs-2", "MH")]
    )
    facade, _ = _facade(output)
    with sf() as session:
        result = explain_business_fit(
            session,
            facade,
            BIZ_A,
            subject_ref="prospect-blocked",
            supported_facts=FACTS,
            eligibility=BLOCKED,
            icp=ICP,
            invocation_key="fit:2",
            now=NOW,
        )
        session.commit()

    assert result.fit_band == "high"  # high desirability …
    # … but eligibility is carried through UNCHANGED — the high fit flipped nothing.
    assert result.eligibility == Eligibility.BLOCKED.value
    assert result.eligibility_reason == "denied_by_rule"
    with sf() as session:
        exp = session.query(FitExplanation).one()
        assert exp.eligibility == "blocked"
        assert exp.eligibility_reason == "denied_by_rule"
        assert exp.fit_band == "high"


# 3) Ungrounded claims are dropped ------------------------------------------------------------


def test_ungrounded_claim_is_dropped():
    sf = make_session()
    # One grounded assessment + one whose cited span is NOT present in the supported fact (hallucinated),
    # plus one citing a ref the DCA never supplied.
    output = _output(
        [
            _assessment("industry", "obs-1", "textiles"),  # grounded
            _assessment("state", "obs-2", "mining"),  # span not in the fact → dropped
            _assessment("industry", "obs-999", "textiles"),  # unknown ref → dropped
        ]
    )
    facade, _ = _facade(output)
    with sf() as session:
        result = explain_business_fit(
            session,
            facade,
            BIZ_A,
            subject_ref="prospect-3",
            supported_facts=FACTS,
            eligibility=ALLOWED,
            icp=ICP,
            invocation_key="fit:3",
            now=NOW,
        )
        session.commit()

    assert len(result.grounded) == 1
    assert result.grounded[0]["factRef"] == "obs-1"
    assert len(result.dropped) == 2  # the hallucinated span + the unknown ref
    with sf() as session:
        exp = session.query(FitExplanation).one()
        assert [a["factRef"] for a in exp.assessments] == ["obs-1"]
        assert len(exp.dropped) == 2


# 4) Sensitive inferred traits are never a scoring input --------------------------------------


def test_sensitive_trait_is_excluded_before_egress():
    sf = make_session()
    facts = [
        SupportedFact(ref="obs-1", field="industry", value="textiles"),
        SupportedFact(
            ref="obs-s", field="director_religion", value="hindu"
        ),  # sensitive → excluded
    ]
    output = _output([_assessment("industry", "obs-1", "textiles")], fit_band="medium")
    facade, transport = _facade(output)
    with sf() as session:
        result = explain_business_fit(
            session,
            facade,
            BIZ_A,
            subject_ref="prospect-4",
            supported_facts=facts,
            eligibility=ALLOWED,
            icp=ICP,
            invocation_key="fit:4",
            now=NOW,
        )
        session.commit()

    assert "director_religion" in result.excluded_sensitive
    # The sensitive fact NEVER left the DCA (the recording fake never saw it) and is not citeable.
    sent = transport.calls[0]["input_context"]
    sent_refs = {f["ref"] for f in sent["supportedFacts"]}
    assert sent_refs == {"obs-1"}
    assert "director_religion" not in sent["permittedFields"]
    assert all("religion" not in f["field"] for f in sent["supportedFacts"])
    # Recorded honestly on the explanation's dropped record (name + reason only, never the value).
    with sf() as session:
        exp = session.query(FitExplanation).one()
        assert any(
            d.get("field") == "director_religion" and d.get("reason") == EXCL_SENSITIVE_TRAIT
            for d in exp.dropped
        )


# 5) Tenant isolation -------------------------------------------------------------------------


def test_tenant_scoped():
    sf = make_session()
    output = _output([_assessment("industry", "obs-1", "textiles")])
    with sf() as session:
        explain_business_fit(
            session,
            _facade(output)[0],
            BIZ_A,
            subject_ref="prospect-1",
            supported_facts=FACTS,
            eligibility=ALLOWED,
            icp=ICP,
            invocation_key="fit:5",
            now=NOW,
        )
        session.commit()

    with sf() as session:
        assert len(FitExplanationStore(session, BIZ_A).explanations()) == 1
        assert FitExplanationStore(session, BIZ_B).explanations() == []


# 6) Idempotent re-run ------------------------------------------------------------------------


def test_idempotent_rerun_returns_existing_and_keeps_eligibility():
    sf = make_session()
    output = _output([_assessment("industry", "obs-1", "textiles")])
    with sf() as session:
        first = explain_business_fit(
            session,
            _facade(output)[0],
            BIZ_A,
            subject_ref="prospect-1",
            supported_facts=FACTS,
            eligibility=BLOCKED,
            icp=ICP,
            invocation_key="fit:6",
            now=NOW,
        )
        session.commit()
        first_id = first.explanation.id

    with sf() as session:
        # Re-run — even with a different (ALLOW) eligibility, the existing row is returned unchanged.
        again = explain_business_fit(
            session,
            _facade(output)[0],
            BIZ_A,
            subject_ref="prospect-1",
            supported_facts=FACTS,
            eligibility=ALLOWED,
            icp=ICP,
            invocation_key="fit:6",
            now=NOW,
        )
        session.commit()
        assert again.produced
        assert again.explanation.id == first_id
        assert again.eligibility == "blocked"  # the first-recorded outcome, not re-derived

    with sf() as session:
        assert session.query(FitExplanation).count() == 1


# 7) Fail-closed denials ----------------------------------------------------------------------


def test_disabled_capability_stages_nothing_and_never_calls_the_model():
    sf = make_session()
    output = _output([_assessment("industry", "obs-1", "textiles")])
    facade, transport = _facade(output, enabled=False)
    with sf() as session:
        result = explain_business_fit(
            session,
            facade,
            BIZ_A,
            subject_ref="prospect-1",
            supported_facts=FACTS,
            eligibility=BLOCKED,
            icp=ICP,
            invocation_key="x",
            now=NOW,
        )
        session.commit()

    assert result.refused_reason == REASON_CAPABILITY_DISABLED
    assert result.eligibility == "blocked"  # carried even on a refusal
    assert transport.calls == []
    with sf() as session:
        assert session.query(FitExplanation).count() == 0


def test_rejected_gateway_output_stages_nothing():
    sf = make_session()
    output = _output([_assessment("industry", "obs-1", "textiles")])
    facade, _ = _facade(output, fail=True)
    with sf() as session:
        result = explain_business_fit(
            session,
            facade,
            BIZ_A,
            subject_ref="prospect-1",
            supported_facts=FACTS,
            eligibility=ALLOWED,
            icp=ICP,
            invocation_key="x",
            now=NOW,
        )
        session.commit()

    assert result.refused_reason == REASON_OUTPUT_REJECTED
    with sf() as session:
        assert session.query(FitExplanation).count() == 0


def test_no_permitted_supported_facts_refused_without_a_model_call():
    sf = make_session()
    # Only a sensitive fact is supplied — it is excluded, leaving nothing to ground an explanation on.
    facts = [SupportedFact(ref="obs-s", field="contact_aadhaar", value="1234")]
    output = _output([])
    facade, transport = _facade(output)
    with sf() as session:
        result = explain_business_fit(
            session,
            facade,
            BIZ_A,
            subject_ref="prospect-1",
            supported_facts=facts,
            eligibility=ALLOWED,
            icp=ICP,
            invocation_key="x",
            now=NOW,
        )
        session.commit()

    assert result.refused_reason == REASON_NO_SUPPORTED_FACTS
    assert "contact_aadhaar" in result.excluded_sensitive
    assert transport.calls == []
    with sf() as session:
        assert session.query(FitExplanation).count() == 0


# 8) The capability key is registered in the DCA allowlist ------------------------------------


def test_capability_key_is_registered_in_the_dca_allowlist():
    from app.ai import KNOWN_CAPABILITIES

    assert CAPABILITY in KNOWN_CAPABILITIES
