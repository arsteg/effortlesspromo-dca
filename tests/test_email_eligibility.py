"""DCA-087 — evaluate email marketing eligibility.

Pins the composed email decision's acceptance behaviour:

- a fully-consented, fresh, source-entitled, suppression-clear, provider-allowed address is ELIGIBLE
  (the reproducible allow);
- a VERIFIED / HIGH-FIT address with NO in-force consent stays BLOCKED (``consent_not_in_force``) —
  verification and fit are carried for audit only and never qualify an address;
- provider restrictions deny INDEPENDENTLY: an otherwise-passing address is BLOCKED with only
  ``provider_restricted``;
- each remaining gate (source rights, freshness, suppression, non-consent basis) blocks on its own,
  fail-closed;
- the decision reads nothing and changes nothing, so a given ``(request, now)`` is reproducible.

The pure :func:`decide_email_eligibility` path is unit-tested directly. The DCA-075 projection ->
eligibility read path is exercised over a disposable PostgreSQL (``DCA_TEST_DATABASE_URL``) against
real persisted consent-receipt rows — never relying on SQLite for that transactional read. The
Postgres test skips when no URL is set.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine

from app.compliance.email_eligibility import (
    BASIS_CONSENT,
    DEFAULT_MAX_EVIDENCE_AGE,
    REASON_BASIS_NOT_CONSENT,
    REASON_CONSENT_NOT_IN_FORCE,
    REASON_EVIDENCE_STALE,
    REASON_OK,
    REASON_PROVIDER_RESTRICTED,
    REASON_SOURCE_RIGHTS_INSUFFICIENT,
    REASON_SUPPRESSED,
    EmailEligibility,
    EmailEligibilityRequest,
    Freshness,
    PermissionBasis,
    ProviderRule,
    SourceRights,
    decide_email_eligibility,
    evaluate_email_eligibility,
)
from app.consent.permission_state import (
    PermissionEvent,
    PermissionState,
    project_permission_states,
)
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import ConsentReceipt
from app.suppression.contract import (
    ActivationDecision,
    SuppressionAvailability,
    SuppressionEntry,
    SuppressionSnapshot,
)

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
CONTACT = "ceo@acme-synthetic.example"
PURPOSE = "marketing_email"

# Every gate passing — the baseline the failure cases each break exactly one of.
_GRANTED = PermissionBasis(basis=BASIS_CONSENT, permission_state=PermissionState.GRANTED.value)
_RIGHTS_OK = SourceRights(permits_email_contact=True)
_FRESH = Freshness(age=timedelta(days=10))
_SUPPRESSION_OK = ActivationDecision(True, ActivationDecision.OK)
_PROVIDER_OK = ProviderRule(allowed=True)


def _request(**overrides) -> EmailEligibilityRequest:
    base = dict(
        contact_point=CONTACT,
        purpose=PURPOSE,
        basis=_GRANTED,
        source_rights=_RIGHTS_OK,
        freshness=_FRESH,
        suppression=_SUPPRESSION_OK,
        provider=_PROVIDER_OK,
        email_verified=True,
        fit_band="high",
    )
    base.update(overrides)
    return EmailEligibilityRequest(**base)


def test_all_gates_pass_is_eligible():
    result = decide_email_eligibility(_request(), now=NOW)
    assert result.decision is EmailEligibility.ELIGIBLE
    assert result.eligible is True
    assert result.reasons == (REASON_OK,)
    assert result.blockers == ()
    assert result.decisive is None
    # Eligibility is never recipient consent / a send authorization.
    assert result.grants_consent is False


def test_verified_high_fit_without_consent_is_blocked():
    """The headline acceptance: verification + high fit never substitute for the required evidence."""
    no_consent = PermissionBasis(basis=BASIS_CONSENT, permission_state=PermissionState.NONE.value)
    result = decide_email_eligibility(_request(basis=no_consent), now=NOW)
    assert result.decision is EmailEligibility.BLOCKED
    # verified/high-fit were recorded but did NOT influence the outcome
    assert result.email_verified is True and result.fit_band == "high"
    assert REASON_CONSENT_NOT_IN_FORCE in result.reasons
    assert result.decisive is not None and result.decisive.gate == "permission"


def test_provider_restriction_denies_independently():
    """Everything else passes; a provider rule alone blocks (the independent-deny acceptance)."""
    denied = ProviderRule(allowed=False, reason="recipient_domain_blocklisted")
    result = decide_email_eligibility(_request(provider=denied), now=NOW)
    assert result.decision is EmailEligibility.BLOCKED
    assert result.reasons == (REASON_PROVIDER_RESTRICTED,)
    assert result.decisive.gate == "provider"
    assert result.decisive.detail == "recipient_domain_blocklisted"


def test_withdrawn_consent_is_blocked():
    withdrawn = PermissionBasis(
        basis=BASIS_CONSENT, permission_state=PermissionState.WITHDRAWN.value
    )
    result = decide_email_eligibility(_request(basis=withdrawn), now=NOW)
    assert result.decision is EmailEligibility.BLOCKED
    assert REASON_CONSENT_NOT_IN_FORCE in result.reasons


def test_non_consent_basis_rejected_no_b2b_exception():
    """A generic B2B / legitimate-interest basis is rejected even with a granted-looking state."""
    b2b = PermissionBasis(
        basis="legitimate_interest", permission_state=PermissionState.GRANTED.value
    )
    result = decide_email_eligibility(_request(basis=b2b), now=NOW)
    assert result.decision is EmailEligibility.BLOCKED
    assert REASON_BASIS_NOT_CONSENT in result.reasons


def test_source_rights_gate_blocks():
    rights = SourceRights(permits_email_contact=False, reason="operation_not_entitled")
    result = decide_email_eligibility(_request(source_rights=rights), now=NOW)
    assert result.decision is EmailEligibility.BLOCKED
    assert result.reasons == (REASON_SOURCE_RIGHTS_INSUFFICIENT,)
    assert result.decisive.detail == "operation_not_entitled"


def test_stale_evidence_blocks():
    stale = Freshness(age=DEFAULT_MAX_EVIDENCE_AGE + timedelta(days=1))
    result = decide_email_eligibility(_request(freshness=stale), now=NOW)
    assert result.decision is EmailEligibility.BLOCKED
    assert result.reasons == (REASON_EVIDENCE_STALE,)


def test_unknown_evidence_age_blocks():
    result = decide_email_eligibility(_request(freshness=Freshness(age=None)), now=NOW)
    assert result.decision is EmailEligibility.BLOCKED
    assert result.reasons == (REASON_EVIDENCE_STALE,)


def test_suppression_gate_propagates_its_reason():
    suppressed = ActivationDecision(False, ActivationDecision.SUPPRESSED)
    result = decide_email_eligibility(_request(suppression=suppressed), now=NOW)
    assert result.decision is EmailEligibility.BLOCKED
    assert result.reasons == (REASON_SUPPRESSED,)
    assert result.decisive.detail == ActivationDecision.SUPPRESSED


def test_unavailable_suppression_fails_closed():
    unavailable = ActivationDecision(False, ActivationDecision.LOOKUP_UNAVAILABLE)
    result = decide_email_eligibility(_request(suppression=unavailable), now=NOW)
    assert result.decision is EmailEligibility.BLOCKED
    assert result.reasons == (REASON_SUPPRESSED,)


def test_every_gate_reported_in_order():
    """All failing gates are collected (not short-circuited), in a fixed order, so independence shows."""
    result = decide_email_eligibility(
        _request(
            basis=PermissionBasis(basis="b2b", permission_state=PermissionState.NONE.value),
            source_rights=SourceRights(permits_email_contact=False),
            freshness=Freshness(age=None),
            suppression=ActivationDecision(False, ActivationDecision.STALE),
            provider=ProviderRule(allowed=False),
        ),
        now=NOW,
    )
    gates = [b.gate for b in result.blockers]
    assert gates == ["basis", "permission", "source_rights", "freshness", "suppression", "provider"]


def test_decision_is_reproducible():
    a = decide_email_eligibility(_request(), now=NOW)
    b = decide_email_eligibility(_request(), now=NOW)
    assert a == b


def test_evaluate_convenience_builds_from_snapshot_and_projection():
    """The convenience derives permission+freshness from a projection and suppression from a snapshot."""
    events = [
        PermissionEvent(
            receipt_id="r1",
            brand_key="acme",
            contact_point_id="cp1",
            channel="email",
            purpose=PURPOSE,
            action="grant",
            collected_at=NOW - timedelta(days=5),
        )
    ]
    projection = next(iter(project_permission_states(events, now=NOW).values()))
    snapshot = SuppressionSnapshot(
        business_id="biz-a",
        availability=SuppressionAvailability.AVAILABLE,
        version="v1",
        as_of=NOW,
        covered_channels=("email",),
        entries=(),
        synthetic=True,
    )
    result = evaluate_email_eligibility(
        contact_point=CONTACT,
        purpose=PURPOSE,
        permission=projection,
        source_rights=_RIGHTS_OK,
        suppression_snapshot=snapshot,
        provider=_PROVIDER_OK,
        now=NOW,
        email_verified=True,
        fit_band="high",
    )
    assert result.decision is EmailEligibility.ELIGIBLE

    # A flag-only import produced NO receipt -> None projection -> no consent in force -> BLOCKED.
    flag_only = evaluate_email_eligibility(
        contact_point=CONTACT,
        purpose=PURPOSE,
        permission=None,
        source_rights=_RIGHTS_OK,
        suppression_snapshot=snapshot,
        provider=_PROVIDER_OK,
        now=NOW,
        email_verified=True,
        fit_band="high",
    )
    assert flag_only.decision is EmailEligibility.BLOCKED
    assert REASON_CONSENT_NOT_IN_FORCE in flag_only.reasons


# -- disposable-PostgreSQL read path: real persisted consent receipts -> projection -> eligibility --


def _seed_receipt(session, *, action: str, collected_at: datetime, rid: str) -> None:
    session.add(
        ConsentReceipt(
            id=rid,
            business_id="biz-a",
            brand_key="acme",
            subject_type="person",
            subject_id="p1",
            contact_point_id="cp1",
            channel="email",
            purpose=PURPOSE,
            action=action,
            notice_version="n1",
            form_version="f1",
            language="en",
            evidence_id="ev1",
            collected_at=collected_at,
            created_at=collected_at,
        )
    )


def _run_projection_suite(session_factory) -> None:
    # A granted-then-still-granted timeline over real rows -> ELIGIBLE.
    with session_factory() as session:
        _seed_receipt(session, action="grant", collected_at=NOW - timedelta(days=3), rid="r1")
        session.commit()
    with session_factory() as session:
        rows = session.query(ConsentReceipt).filter_by(business_id="biz-a").all()
        events = [
            PermissionEvent(
                receipt_id=r.id,
                brand_key=r.brand_key,
                contact_point_id=r.contact_point_id,
                channel=r.channel,
                purpose=r.purpose,
                action=r.action,
                collected_at=r.collected_at,
                recorded_at=r.created_at,
                evidence_id=r.evidence_id,
            )
            for r in rows
        ]
    projection = next(iter(project_permission_states(events, now=NOW).values()))
    assert projection.state is PermissionState.GRANTED

    snapshot = SuppressionSnapshot(
        business_id="biz-a",
        availability=SuppressionAvailability.AVAILABLE,
        version="v1",
        as_of=NOW,
        covered_channels=("email",),
        entries=(),
    )
    eligible = evaluate_email_eligibility(
        contact_point=CONTACT,
        purpose=PURPOSE,
        permission=projection,
        source_rights=_RIGHTS_OK,
        suppression_snapshot=snapshot,
        provider=_PROVIDER_OK,
        now=NOW,
    )
    assert eligible.decision is EmailEligibility.ELIGIBLE

    # A suppression entry for the contact point over the same inputs -> BLOCKED (suppression gate).
    suppressed_snapshot = SuppressionSnapshot(
        business_id="biz-a",
        availability=SuppressionAvailability.AVAILABLE,
        version="v1",
        as_of=NOW,
        covered_channels=("email",),
        entries=(SuppressionEntry(contact_point=CONTACT, channel="email", reason="unsubscribe"),),
    )
    blocked = evaluate_email_eligibility(
        contact_point=CONTACT,
        purpose=PURPOSE,
        permission=projection,
        source_rights=_RIGHTS_OK,
        suppression_snapshot=suppressed_snapshot,
        provider=_PROVIDER_OK,
        now=NOW,
    )
    assert blocked.decision is EmailEligibility.BLOCKED
    assert REASON_SUPPRESSED in blocked.reasons

    # The read changed nothing — the single receipt row is still the only one.
    with session_factory() as session:
        assert session.query(ConsentReceipt).filter_by(business_id="biz-a").count() == 1


@pytest.fixture
def sqlite_session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def test_projection_chain_over_sqlite(sqlite_session_factory):
    _run_projection_suite(sqlite_session_factory)


PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")


@pytest.mark.skipif(not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres")
def test_projection_chain_over_postgres():
    """Exercise the real persisted-receipt -> projection -> eligibility read path on disposable Postgres."""
    engine = create_engine(PG_URL, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        _run_projection_suite(make_session_factory(engine))
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
