"""DCA-055 — evaluate one deterministic policy request.

Pins the evaluator's acceptance behaviour:

- the four decisions are produced deterministically: ALLOW (an in-force rule permits), DENY (an
  in-force rule forbids), REVIEW (a governing rule needs a missing fact), NOT_APPLICABLE (the request
  involves no personal data);
- a MISSING pack and an UNRESOLVED/UNSUPPORTED jurisdiction can NEVER yield ALLOW — both fail closed to
  DENY with a stable reason (the core acceptance invariant);
- unknown action/channel scope defaults to no personal-data permission (DENY);
- evaluation consults no LLM and changes no state, so a given ``(request, pack, now)`` is reproducible.

The pure :func:`decide` path is unit-tested directly; the DB-backed :class:`PolicyEvaluator`
(resolving the in-force pack from the DCA-052/053 stores) is exercised on in-memory SQLite for the
store logic, and on a disposable PostgreSQL (``DCA_TEST_DATABASE_URL``) for the real
transaction/read path — never relying on SQLite for that. The Postgres test skips when no URL is set.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine

from app.compliance.policy_engine import (
    POLICY_ENGINE_VERSION,
    REASON_ALLOWED_BY_RULE,
    REASON_DENIED_BY_RULE,
    REASON_INSUFFICIENT_FACTS,
    REASON_NO_IN_FORCE_PACK,
    REASON_NO_MATCHING_RULE,
    REASON_NOT_PERSONAL_DATA,
    REASON_UNRESOLVED_JURISDICTION,
    REASON_UNSUPPORTED_ACTION,
    REASON_UNSUPPORTED_CHANNEL,
    REASON_UNSUPPORTED_JURISDICTION,
    InForcePack,
    PolicyDecision,
    PolicyEvaluator,
    PolicyRequest,
    decide,
)
from app.compliance.policy_rulepacks import (
    CompliancePolicyRulepackStore,
    PolicyRuleInput,
    RulepackInput,
)
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import CompliancePolicyApproval, CompliancePolicyRulepack
from app.services.policy_approval import (
    PolicyApprovalReview,
    PolicyApprovalStore,
    PolicyReviewerPrincipal,
)

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
OPERATOR = PolicyReviewerPrincipal(subject="op-001", roles=("platform_operator",))


def _evidence(ref: str = "DPDP-2023-s7") -> dict:
    return {"reference": ref, "kind": "statute", "uri": "https://example.test/law"}


# --------------------------------------------------------------------------------------------------
# Synthetic in-force pack fixtures for the PURE decide() path (no DB needed).
# --------------------------------------------------------------------------------------------------


def _rule(effect: str, action: str, channel: str, authority: str, **extra) -> dict:
    """A rule dict shaped exactly like DCA-052 persists it (ISO-string windows)."""
    rule = {
        "effect": effect,
        "action": action,
        "channel": channel,
        "authority": authority,
        "conditions": extra.get("conditions", {}),
        "evidence": extra.get("evidence", _evidence()),
        "effective_from": extra.get("effective_from"),
        "expires_at": extra.get("expires_at"),
    }
    return rule


def _pack(*rules: dict, version: int = 1) -> InForcePack:
    return InForcePack(
        jurisdiction="IN",
        version=version,
        approval_id="appr-1",
        rules=list(rules),
        evidence={"reference": "POLICY-REVIEW-2026-11"},
    )


# --- the four decisions, pure --------------------------------------------------------------------


def test_allow_when_rule_permits_and_conditions_met():
    pack = _pack(
        _rule("allow", "contact", "email", "law", conditions={"consent": "required"}),
    )
    result = decide(
        PolicyRequest(action="contact", channel="email", facts={"consent": "present"}),
        pack,
        now=NOW,
    )
    assert result.decision is PolicyDecision.ALLOW
    assert result.allowed is True
    assert result.reasons == (REASON_ALLOWED_BY_RULE,)
    assert result.rulepack_version == 1
    assert result.approval_id == "appr-1"
    assert result.decisive_authority == "law"
    assert result.llm_consulted is False
    assert result.engine_version == POLICY_ENGINE_VERSION
    # Evidence names the decisive rule + the pack + the approval it relied on (nothing fabricated).
    sources = {e["source"] for e in result.evidence}
    assert sources == {"rule", "rulepack", "approval"}


def test_deny_when_rule_forbids():
    pack = _pack(_rule("deny", "contact", "sms", "law"))
    result = decide(PolicyRequest(action="contact", channel="sms"), pack, now=NOW)
    assert result.decision is PolicyDecision.DENY
    assert result.reasons == (REASON_DENIED_BY_RULE,)
    assert result.allowed is False


def test_deny_dominates_when_allow_and_deny_both_apply():
    # A product default denies what a law rule would allow — the stricter deny wins (law is a ceiling;
    # product may only narrow it). The deny-dominant resolution is the safe direction.
    pack = _pack(
        _rule("allow", "contact", "email", "law", conditions={"consent": "required"}),
        _rule("deny", "contact", "email", "product"),
    )
    result = decide(
        PolicyRequest(action="contact", channel="email", facts={"consent": "present"}),
        pack,
        now=NOW,
    )
    assert result.decision is PolicyDecision.DENY
    assert result.reasons == (REASON_DENIED_BY_RULE,)


def test_review_when_governing_rule_needs_a_missing_fact():
    pack = _pack(_rule("allow", "contact", "email", "law", conditions={"consent": "required"}))
    # consent fact absent => cannot confirm the allow, must not guess => REVIEW (never ALLOW).
    result = decide(PolicyRequest(action="contact", channel="email", facts={}), pack, now=NOW)
    assert result.decision is PolicyDecision.REVIEW
    assert result.reasons == (REASON_INSUFFICIENT_FACTS,)
    assert result.allowed is False


def test_not_applicable_when_no_personal_data():
    pack = _pack(_rule("deny", "contact", "email", "law"))
    result = decide(
        PolicyRequest(action="contact", channel="email", involves_personal_data=False),
        pack,
        now=NOW,
    )
    assert result.decision is PolicyDecision.NOT_APPLICABLE
    assert result.reasons == (REASON_NOT_PERSONAL_DATA,)
    assert result.allowed is False


# --- the core invariant: a missing pack / unresolved jurisdiction can NEVER allow ----------------


def test_missing_pack_denies_never_allows():
    result = decide(PolicyRequest(action="contact", channel="email"), None, now=NOW)
    assert result.decision is PolicyDecision.DENY
    assert result.reasons == (REASON_NO_IN_FORCE_PACK,)
    assert result.rulepack_version is None
    assert result.allowed is False


def test_unresolved_jurisdiction_denies():
    # An ambiguous country resolves to "" — fail closed, even with a permissive pack present.
    pack = _pack(_rule("allow", "contact", "email", "law"))
    result = decide(
        PolicyRequest(action="contact", channel="email", jurisdiction=""), pack, now=NOW
    )
    assert result.decision is PolicyDecision.DENY
    assert result.reasons == (REASON_UNRESOLVED_JURISDICTION,)
    assert result.allowed is False


def test_unsupported_foreign_jurisdiction_denies():
    pack = _pack(_rule("allow", "contact", "email", "law"))
    result = decide(
        PolicyRequest(action="contact", channel="email", jurisdiction="US"), pack, now=NOW
    )
    assert result.decision is PolicyDecision.DENY
    assert result.reasons == (REASON_UNSUPPORTED_JURISDICTION,)


def test_unknown_scope_defaults_to_no_permission():
    pack = _pack(_rule("allow", "contact", "email", "law"))
    unknown_action = decide(PolicyRequest(action="harvest", channel="email"), pack, now=NOW)
    assert unknown_action.decision is PolicyDecision.DENY
    assert unknown_action.reasons == (REASON_UNSUPPORTED_ACTION,)
    unknown_channel = decide(PolicyRequest(action="contact", channel="fax"), pack, now=NOW)
    assert unknown_channel.decision is PolicyDecision.DENY
    assert unknown_channel.reasons == (REASON_UNSUPPORTED_CHANNEL,)


def test_no_matching_rule_defaults_to_deny():
    # The pack governs email contact only; an sms contact has no granting rule => default-deny.
    pack = _pack(_rule("allow", "contact", "email", "law"))
    result = decide(PolicyRequest(action="contact", channel="sms"), pack, now=NOW)
    assert result.decision is PolicyDecision.DENY
    assert result.reasons == (REASON_NO_MATCHING_RULE,)


def test_expired_rule_window_is_not_applicable():
    pack = _pack(
        _rule(
            "allow",
            "contact",
            "email",
            "law",
            effective_from=(NOW - timedelta(days=10)).isoformat(),
            expires_at=(NOW - timedelta(days=1)).isoformat(),
        )
    )
    result = decide(PolicyRequest(action="contact", channel="email"), pack, now=NOW)
    # The only rule has lapsed => no granting rule => default-deny.
    assert result.decision is PolicyDecision.DENY
    assert result.reasons == (REASON_NO_MATCHING_RULE,)


def test_channel_any_rule_matches_specific_channel():
    pack = _pack(_rule("deny", "acquire", "any", "law"))
    result = decide(PolicyRequest(action="acquire", channel="email"), pack, now=NOW)
    assert result.decision is PolicyDecision.DENY
    assert result.reasons == (REASON_DENIED_BY_RULE,)


def test_decision_is_reproducible():
    pack = _pack(_rule("allow", "contact", "email", "law", conditions={"consent": "required"}))
    req = PolicyRequest(action="contact", channel="email", facts={"consent": "present"})
    first = decide(req, pack, now=NOW)
    second = decide(req, pack, now=NOW)
    assert first == second  # frozen dataclass equality — identical result, no hidden state


# --------------------------------------------------------------------------------------------------
# DB-backed PolicyEvaluator: resolve the in-force pack from the DCA-052/053 stores, then decide.
# --------------------------------------------------------------------------------------------------


def _record_and_approve(session_factory, *, approve: bool) -> None:
    """Record one India rulepack draft; optionally promote it to in-force via a DCA-053 approval."""
    pack = RulepackInput(
        jurisdiction="IN",
        title="India baseline policy",
        evidence=_evidence("POLICY-REVIEW-2026-11"),
        rules=[
            PolicyRuleInput(
                effect="allow",
                action="contact",
                channel="email",
                authority="law",
                conditions={"consent": "required"},
                evidence=_evidence(),
            )
        ],
    )
    with session_factory() as session:
        CompliancePolicyRulepackStore(session).add_version(pack)
        session.commit()
    if approve:
        review = PolicyApprovalReview(
            reviewer_id="op-001",
            references={"reference": "LEGAL-REVIEW-2026-11", "kind": "legal_review"},
            reviewed_at=NOW,
            expires_at=NOW + timedelta(days=365),
        )
        with session_factory() as session:
            PolicyApprovalStore(session).approve(1, review, OPERATOR)
            session.commit()


def _run_evaluator_suite(session_factory) -> None:
    """The DB-backed acceptance scenario, reusable across SQLite and Postgres."""
    # 1) Only a DRAFT recorded (not approved) => no in-force pack => missing-pack DENY, never allow.
    _record_and_approve(session_factory, approve=False)
    with session_factory() as session:
        result = PolicyEvaluator(session).evaluate(
            PolicyRequest(action="contact", channel="email", facts={"consent": "present"}),
            now=NOW,
        )
    assert result.decision is PolicyDecision.DENY
    assert result.reasons == (REASON_NO_IN_FORCE_PACK,)

    # 2) Approve the version => it is in force => the same request now ALLOWs.
    review = PolicyApprovalReview(
        reviewer_id="op-001",
        references={"reference": "LEGAL-REVIEW-2026-11", "kind": "legal_review"},
        reviewed_at=NOW,
        expires_at=NOW + timedelta(days=365),
    )
    with session_factory() as session:
        PolicyApprovalStore(session).approve(1, review, OPERATOR)
        session.commit()
    with session_factory() as session:
        allowed = PolicyEvaluator(session).evaluate(
            PolicyRequest(action="contact", channel="email", facts={"consent": "present"}),
            now=NOW,
        )
        missing_fact = PolicyEvaluator(session).evaluate(
            PolicyRequest(action="contact", channel="email", facts={}), now=NOW
        )
        ambiguous = PolicyEvaluator(session).evaluate(
            PolicyRequest(action="contact", channel="email", jurisdiction=""), now=NOW
        )
        # Evaluation changes no state — no approval/rulepack rows are written by a read.
        approvals = session.query(CompliancePolicyApproval).count()
        packs = session.query(CompliancePolicyRulepack).count()
    assert allowed.decision is PolicyDecision.ALLOW
    assert allowed.rulepack_version == 1 and allowed.approval_id is not None
    assert missing_fact.decision is PolicyDecision.REVIEW
    assert ambiguous.decision is PolicyDecision.DENY
    assert ambiguous.reasons == (REASON_UNRESOLVED_JURISDICTION,)
    assert approvals == 1 and packs == 1  # unchanged by the three evaluations


@pytest.fixture
def sqlite_session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def test_evaluator_over_sqlite(sqlite_session_factory):
    _run_evaluator_suite(sqlite_session_factory)


PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")


@pytest.mark.skipif(not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres")
def test_evaluator_over_postgres():
    """Exercise the real resolve→decide read path on a disposable PostgreSQL, not SQLite."""
    engine = create_engine(PG_URL, future=True)
    # Isolate this run's tables so the suite is self-cleaning on a shared throwaway Postgres.
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        _run_evaluator_suite(make_session_factory(engine))
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
