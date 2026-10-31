"""DCA-139 — draft a first-party acquisition plan (the AI-capability integration).

Service unit tests (in-memory SQLite + the deterministic fixture transport) pin the planning logic: a
bounded plan grounded in the CONFIRMED ICP (+ the business profile for localisation) that proposes ONLY
COMPLIANT first-party capture methods; a non-first-party method, an outbound-to-strangers tactic, a
fabricated contact list and an over-cap suggestion are each clamped/EXCLUDED (recorded) and change
nothing; a B2C zero-data plan fabricates no contacts; the plan is a DRAFT, never executed/published/sent,
never auto-approved; a rejected gateway output, a disabled capability and a missing confirmed ICP each
stage NO plan; it is idempotent per confirmed ICP version; and everything is tenant-scoped.

No AI, no network, no model, no spend — the fixture transport stands in for the gateway (the real gateway
capability schema + its rule_check bound are pinned by effortlesspromo-ai's own contract test).
"""

from __future__ import annotations

import pytest

from app.ai import AiCapabilityFacade, FakeAiGatewayTransport
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import AcquisitionPlan, IcpConfirmation, IcpDraft
from app.services.acquisition_planner import (
    EXCL_FABRICATED_CONTACTS,
    EXCL_METHOD_NOT_ALLOWED,
    EXCL_OUTBOUND_METHOD,
    EXCL_SUGGESTION_CAP_EXCEEDED,
    FIRST_PARTY_METHODS,
    REASON_CAPABILITY_DISABLED,
    REASON_NO_CONFIRMED_ICP,
    REASON_OUTPUT_REJECTED,
    draft_acquisition_plan,
)
from app.services.profile_store import ProfileDraftInput, ProfileStore

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"


def _seed_icp(session, business_id: str, *, geography=("MH",)) -> None:
    """A confirmed ICP version for the business (the targeting the plan is grounded in)."""
    session.add(
        IcpDraft(
            business_id=business_id,
            version=1,
            industries=["textiles"],
            size_bands=["small"],
            roles=["owner"],
            geography=list(geography),
            status="draft",
        )
    )
    session.add(
        IcpConfirmation(business_id=business_id, version=1, actor_id="seed", actor_type="user")
    )


def _seed_profile(session, business_id: str) -> None:
    """A business-profile draft used for localisation (the business's OWN content, not prospect data)."""
    ProfileStore(session, business_id).save_draft(
        ProfileDraftInput(
            products=["handloom sarees"],
            business_type="boutique retail",
            value_proposition="locally-made handloom",
            languages=["Marathi"],
        )
    )


def _suggestion(method: str, *, offer="Opt-in capture", rationale="first-party", **extra) -> dict:
    base = {
        "method": method,
        "offer": offer,
        "placement": "website",
        "rationale": rationale,
        "confidence": 0.7,
    }
    base.update(extra)
    return base


def _facade(structured_output, validation=None, enabled=True) -> AiCapabilityFacade:
    """A facade over the deterministic fixture transport returning a crafted plan (no network/spend)."""
    return AiCapabilityFacade(
        FakeAiGatewayTransport(
            enabled=enabled, structured_output=structured_output, validation=validation
        )
    )


def _plan_output(suggestions, *, rationale="plan", confidence=0.7) -> dict:
    return {"suggestions": suggestions, "rationale": rationale, "confidence": confidence}


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


# --- a bounded, grounded, first-party-only plan --------------------------------------------------


def test_bounded_first_party_plan_generated(session_factory):
    with session_factory() as session:
        _seed_icp(session, BIZ_A)
        _seed_profile(session, BIZ_A)
        session.flush()

        suggestions = [
            _suggestion("website_form"),
            _suggestion("qr_code", offer="QR opt-in on packaging"),
        ]
        result = draft_acquisition_plan(
            session,
            _facade(_plan_output(suggestions)),
            BIZ_A,
            invocation_key="acq-plan:test:1",
            suggestion_cap=5,
        )
        session.flush()

        assert result.plan is not None
        assert result.plan.status == "draft"
        assert result.icp_version == 1
        assert result.profile_version == 1  # localisation profile linked

        kept_methods = {s["method"] for s in result.suggestions}
        assert kept_methods == {"website_form", "qr_code"}
        # Every kept method is a COMPLIANT first-party capture method.
        assert kept_methods <= set(FIRST_PARTY_METHODS)
        # No suggestion carries contacts/recipients (a zero-data plan fabricates none).
        for s in result.suggestions:
            assert not ({"contacts", "recipients", "leads"} & set(s))

        # A plan is a PROPOSAL — nothing was executed, published, or sent.
        assert result.plan.provenance["executed"] is False
        assert result.plan.provenance["published"] is False
        assert result.plan.provenance["sent"] is False
        assert result.plan.provenance["autoApproved"] is False
        assert result.plan.provenance["fabricatedContacts"] is False


def test_non_first_party_method_is_excluded(session_factory):
    with session_factory() as session:
        _seed_icp(session, BIZ_A)
        session.flush()
        suggestions = [
            _suggestion("website_form"),
            _suggestion("cold_email_blast"),  # NOT a first-party method
            _suggestion("buy_leads"),  # NOT a first-party method
        ]
        result = draft_acquisition_plan(
            session, _facade(_plan_output(suggestions)), BIZ_A, invocation_key="acq-plan:test:nonfp"
        )
    assert {s["method"] for s in result.suggestions} == {"website_form"}
    dropped = {(e.get("method"), e["reason"]) for e in result.excluded}
    assert ("cold_email_blast", EXCL_METHOD_NOT_ALLOWED) in dropped
    assert ("buy_leads", EXCL_METHOD_NOT_ALLOWED) in dropped


def test_outbound_tactic_text_is_excluded(session_factory):
    with session_factory() as session:
        _seed_icp(session, BIZ_A)
        session.flush()
        suggestions = [
            # A permitted method, but the TEXT proposes an outbound-to-strangers tactic -> dropped.
            _suggestion("website_form", offer="Upload a purchased list and cold email everyone"),
            _suggestion("qr_code"),
        ]
        result = draft_acquisition_plan(
            session,
            _facade(_plan_output(suggestions)),
            BIZ_A,
            invocation_key="acq-plan:test:outbound",
        )
    assert {s["method"] for s in result.suggestions} == {"qr_code"}
    outbound = [e for e in result.excluded if e["reason"] == EXCL_OUTBOUND_METHOD]
    assert len(outbound) == 1


def test_fabricated_contacts_suggestion_is_excluded(session_factory):
    with session_factory() as session:
        _seed_icp(session, BIZ_A)
        session.flush()
        suggestions = [
            # A permitted method but carrying a fabricated contact list -> dropped (no ready list).
            _suggestion("website_form", contacts=[{"email": "x@example.com"}]),
            _suggestion("newsletter_signup"),
        ]
        result = draft_acquisition_plan(
            session, _facade(_plan_output(suggestions)), BIZ_A, invocation_key="acq-plan:test:fab"
        )
    assert {s["method"] for s in result.suggestions} == {"newsletter_signup"}
    assert any(e["reason"] == EXCL_FABRICATED_CONTACTS for e in result.excluded)


def test_over_cap_plan_is_clamped(session_factory):
    with session_factory() as session:
        _seed_icp(session, BIZ_A)
        session.flush()
        suggestions = [_suggestion(m) for m in FIRST_PARTY_METHODS[:5]]
        result = draft_acquisition_plan(
            session,
            _facade(_plan_output(suggestions)),
            BIZ_A,
            invocation_key="acq-plan:test:cap",
            suggestion_cap=2,
        )
    assert len(result.suggestions) == 2  # clamped to the cap
    overflow = [e for e in result.excluded if e["reason"] == EXCL_SUGGESTION_CAP_EXCEEDED]
    assert len(overflow) == 3


def test_zero_data_plan_fabricates_no_contacts(session_factory):
    """A B2C business with NO profile/data still gets a plan — grounded in its ICP, proposing capture
    methods, promising NO contacts and NO ready-to-message list."""
    with session_factory() as session:
        _seed_icp(session, BIZ_A)  # confirmed ICP only; no profile, no prospect data
        session.flush()
        result = draft_acquisition_plan(
            session,
            _facade(_plan_output([_suggestion("in_store_signup")])),
            BIZ_A,
            invocation_key="acq-plan:test:zero",
        )
        session.flush()
    assert result.plan is not None
    assert result.profile_version is None
    assert result.suggestions  # a plan was still produced
    # The plan itself carries only suggestions — never a contacts/recipients list.
    assert result.plan.provenance["fabricatedContacts"] is False
    for s in result.suggestions:
        assert "contacts" not in s and "recipients" not in s


# --- idempotency ---------------------------------------------------------------------------------


def test_idempotent_per_confirmed_icp(session_factory):
    with session_factory() as session:
        _seed_icp(session, BIZ_A)
        session.flush()
        first = draft_acquisition_plan(
            session,
            _facade(_plan_output([_suggestion("website_form")])),
            BIZ_A,
            invocation_key="acq-plan:test:idem1",
        )
        session.flush()
        # A re-run for the SAME confirmed ICP returns the EXISTING plan — no duplicate row.
        second = draft_acquisition_plan(
            session,
            _facade(_plan_output([_suggestion("qr_code")])),
            BIZ_A,
            invocation_key="acq-plan:test:idem2",
        )
        session.flush()
        assert first.plan is not None and second.plan is not None
        assert second.plan.id == first.plan.id
        assert session.query(AcquisitionPlan).count() == 1


# --- denials stage nothing -----------------------------------------------------------------------


def test_no_confirmed_icp_refused(session_factory):
    with session_factory() as session:
        session.flush()  # no ICP confirmed
        result = draft_acquisition_plan(
            session,
            _facade(_plan_output([_suggestion("website_form")])),
            BIZ_A,
            invocation_key="acq-plan:test:noicp",
        )
        assert result.plan is None
        assert result.refused_reason == REASON_NO_CONFIRMED_ICP
        assert session.query(AcquisitionPlan).count() == 0


def test_disabled_capability_refused(session_factory):
    with session_factory() as session:
        _seed_icp(session, BIZ_A)
        session.flush()
        for facade in (None, _facade(_plan_output([]), enabled=False)):
            result = draft_acquisition_plan(
                session, facade, BIZ_A, invocation_key="acq-plan:test:off"
            )
            assert result.plan is None
            assert result.refused_reason == REASON_CAPABILITY_DISABLED
        assert session.query(AcquisitionPlan).count() == 0


def test_rejected_output_stages_nothing(session_factory):
    with session_factory() as session:
        _seed_icp(session, BIZ_A)
        session.flush()
        facade = _facade(
            _plan_output([_suggestion("website_form")]),
            validation={"validationStatus": "failed", "validationReport": {"status": "failed"}},
        )
        result = draft_acquisition_plan(session, facade, BIZ_A, invocation_key="acq-plan:test:rej")
        assert result.plan is None
        assert result.refused_reason == REASON_OUTPUT_REJECTED
        assert session.query(AcquisitionPlan).count() == 0


def test_tenant_scoped(session_factory):
    with session_factory() as session:
        _seed_icp(session, BIZ_A)
        session.flush()
        draft_acquisition_plan(
            session,
            _facade(_plan_output([_suggestion("website_form")])),
            BIZ_A,
            invocation_key="acq-plan:test:tenant",
        )
        session.flush()
        a_rows = session.query(AcquisitionPlan).filter(AcquisitionPlan.business_id == BIZ_A).count()
        b_rows = session.query(AcquisitionPlan).filter(AcquisitionPlan.business_id == BIZ_B).count()
    assert a_rows == 1
    assert b_rows == 0
