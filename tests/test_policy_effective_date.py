"""DCA-056 — select rules by effective date + immutable evaluation-time snapshots.

Pins the two load-bearing behaviours this story adds on top of the DCA-055 evaluator:

1. **Obligation-specific effective-date selection.** Each rule carries its OWN validity window (where a
   DPDP/TRAI obligation's per-provision commencement date lands — DCA-054 §1). A boundary timestamp
   selects the correct rule: the same request DENIES just before an obligation commences and ALLOWs at/
   after it, and two obligations with different effective dates switch on INDEPENDENTLY — there is no
   universal ``dpdp_enabled`` flag that turns everything on at once.
2. **Immutable evaluation-time rule snapshot.** Every pack-backed decision carries a frozen, content-
   fingerprinted snapshot of the rules that were in force at that instant; :func:`replay` reproduces the
   identical decision (and its relevant denial) from the snapshot ALONE — even after the live pack has
   changed — with no DB access and no state change.

The pure :func:`decide`/:func:`replay`/:func:`select_effective_rules` paths are unit-tested directly;
the DB-backed boundary path (resolving the in-force pack from the DCA-052/053 stores) is exercised on a
disposable PostgreSQL (``DCA_TEST_DATABASE_URL``) for the real transaction/read path — never relying on
SQLite for that. The Postgres test skips when no URL is set.
"""

from __future__ import annotations

import dataclasses
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine

from app.compliance.policy_engine import (
    REASON_ALLOWED_BY_RULE,
    REASON_DENIED_BY_RULE,
    REASON_NO_MATCHING_RULE,
    EvaluationSnapshot,
    InForcePack,
    PolicyDecision,
    PolicyEvaluator,
    PolicyRequest,
    RuleSnapshot,
    decide,
    obligation_key,
    replay,
    select_effective_rules,
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

# The DPDP notice/consent obligation commences at this boundary in the scenarios below.
COMMENCE = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
BEFORE = COMMENCE - timedelta(seconds=1)
AT = COMMENCE
AFTER = COMMENCE + timedelta(days=30)
OPERATOR = PolicyReviewerPrincipal(subject="op-001", roles=("platform_operator",))


def _evidence(ref: str = "DPDP-2023-s7") -> dict:
    return {"reference": ref, "kind": "statute", "uri": "https://example.test/law"}


def _rule(effect: str, action: str, channel: str, authority: str, **extra) -> dict:
    """A rule dict shaped exactly like DCA-052 persists it (ISO-string windows)."""
    return {
        "effect": effect,
        "action": action,
        "channel": channel,
        "authority": authority,
        "conditions": extra.get("conditions", {}),
        "evidence": extra.get("evidence", _evidence(extra.get("reference", "DPDP-2023-s7"))),
        "effective_from": extra.get("effective_from"),
        "expires_at": extra.get("expires_at"),
    }


def _pack(*rules: dict, version: int = 1) -> InForcePack:
    return InForcePack(
        jurisdiction="IN",
        version=version,
        approval_id="appr-1",
        rules=list(rules),
        evidence={"reference": "POLICY-REVIEW-2026-11"},
    )


# --------------------------------------------------------------------------------------------------
# 1) Obligation-specific effective-date selection — boundary timestamps select the correct rule.
# --------------------------------------------------------------------------------------------------


def test_boundary_timestamp_flips_deny_to_allow_at_commencement():
    """The DPDP notice/consent obligation commences at COMMENCE. The SAME consented request denies
    just before, and allows at/after — a boundary timestamp selects the correct rule."""
    pack = _pack(
        _rule(
            "allow",
            "contact",
            "email",
            "law",
            conditions={"consent": "required"},
            effective_from=COMMENCE.isoformat(),
        )
    )
    req = PolicyRequest(action="contact", channel="email", facts={"consent": "present"})

    before = decide(req, pack, now=BEFORE)
    at = decide(req, pack, now=AT)
    after = decide(req, pack, now=AFTER)

    # Before commencement the obligation is not in force => no granting rule => default-deny (never a
    # silent allow). There is NO global flag; selection is purely the rule's own effective_from.
    assert before.decision is PolicyDecision.DENY
    assert before.reasons == (REASON_NO_MATCHING_RULE,)
    # At the exact effective instant (half-open [from, to)) and after, the rule is selected => ALLOW.
    assert at.decision is PolicyDecision.ALLOW
    assert at.reasons == (REASON_ALLOWED_BY_RULE,)
    assert after.decision is PolicyDecision.ALLOW


def test_boundary_timestamp_flips_allow_to_deny_at_expiry():
    """A rule in force until SUNSET applies up to (not including) it, then lapses — the expiry boundary
    selects the correct rule."""
    sunset = AFTER
    pack = _pack(
        _rule(
            "allow",
            "contact",
            "email",
            "law",
            conditions={"consent": "required"},
            expires_at=sunset.isoformat(),
        )
    )
    req = PolicyRequest(action="contact", channel="email", facts={"consent": "present"})

    assert decide(req, pack, now=sunset - timedelta(seconds=1)).decision is PolicyDecision.ALLOW
    # Half-open window: AT the expiry instant the rule is no longer in force => default-deny.
    assert decide(req, pack, now=sunset).decision is PolicyDecision.DENY
    assert decide(req, pack, now=sunset).reasons == (REASON_NO_MATCHING_RULE,)


def test_two_obligations_commence_independently_no_universal_flag():
    """Two obligations with DIFFERENT effective dates switch on one at a time — proving there is no
    single ``dpdp_enabled`` shortcut that enables them together."""
    email_from = COMMENCE
    sms_from = COMMENCE + timedelta(days=365)  # a later tranche
    pack = _pack(
        _rule(
            "allow",
            "contact",
            "email",
            "law",
            conditions={"consent": "required"},
            effective_from=email_from.isoformat(),
            reference="DPDP-email-notice",
        ),
        _rule(
            "deny",
            "contact",
            "sms",
            "provider",
            effective_from=sms_from.isoformat(),
            reference="TRAI-sms-dlt",
        ),
    )
    email_req = PolicyRequest(action="contact", channel="email", facts={"consent": "present"})
    sms_req = PolicyRequest(action="contact", channel="sms")

    # Between the two commencement dates: the email obligation is in force, the sms one is NOT yet.
    mid = COMMENCE + timedelta(days=30)
    assert decide(email_req, pack, now=mid).decision is PolicyDecision.ALLOW
    sms_mid = decide(sms_req, pack, now=mid)
    assert sms_mid.decision is PolicyDecision.DENY
    # Not a rule-driven deny — the sms obligation simply has not commenced (default-deny), distinct
    # from the denied-by-rule it becomes once its tranche lands.
    assert sms_mid.reasons == (REASON_NO_MATCHING_RULE,)

    # After the sms tranche commences, the sms obligation is now a rule-driven deny.
    later = sms_from + timedelta(days=1)
    sms_later = decide(sms_req, pack, now=later)
    assert sms_later.decision is PolicyDecision.DENY
    assert sms_later.reasons == (REASON_DENIED_BY_RULE,)


def test_select_effective_rules_picks_by_each_rules_own_window():
    not_yet = _rule("allow", "contact", "email", "law", effective_from=COMMENCE.isoformat())
    lapsed = _rule("allow", "acquire", "any", "law", expires_at=BEFORE.isoformat())
    always = _rule("deny", "contact", "sms", "law")

    at_before = select_effective_rules([not_yet, lapsed, always], now=BEFORE)
    at_after = select_effective_rules([not_yet, lapsed, always], now=AFTER)

    # Before COMMENCE: ``not_yet`` is excluded; ``lapsed`` still in window (expires AT=BEFORE is
    # half-open, so BEFORE-1s is in); ``always`` always in.
    assert not_yet not in at_before and always in at_before
    # After COMMENCE: ``not_yet`` is in, ``lapsed`` is out.
    assert not_yet in at_after and lapsed not in at_after and always in at_after


def test_obligation_key_prefers_cited_reference_then_scope():
    assert (
        obligation_key(_rule("allow", "contact", "email", "law", reference="DPDP-s7")) == "DPDP-s7"
    )
    # No evidence reference => fall back to the rule's scope signature.
    bare = {"effect": "allow", "action": "contact", "channel": "email", "authority": "law"}
    assert obligation_key(bare) == "law:contact:email"


# --------------------------------------------------------------------------------------------------
# 2) Immutable evaluation-time rule snapshot.
# --------------------------------------------------------------------------------------------------


def test_snapshot_captures_in_force_rules_and_is_immutable():
    pack = _pack(
        _rule(
            "allow",
            "contact",
            "email",
            "law",
            conditions={"consent": "required"},
            effective_from=COMMENCE.isoformat(),
            reference="DPDP-s7",
        )
    )
    req = PolicyRequest(action="contact", channel="email", facts={"consent": "present"})
    result = decide(req, pack, now=AT)

    snap = result.snapshot
    assert isinstance(snap, EvaluationSnapshot)
    assert snap.evaluated_at == AT.isoformat()
    assert snap.rulepack_version == 1 and snap.approval_id == "appr-1"
    assert snap.fingerprint and len(snap.fingerprint) == 64  # sha256 hex
    assert len(snap.selected_rules) == 1
    only = snap.selected_rules[0]
    assert only.obligation == "DPDP-s7"
    assert only.effective_from == COMMENCE.isoformat()
    assert only.time_eligible is True

    # Immutable: neither the snapshot nor its rule snapshots can be reassigned, and the rule list is a
    # tuple (not a mutable list), so a recorded decision can never drift after capture.
    assert isinstance(snap.selected_rules, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.fingerprint = "tampered"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        only.effect = "deny"  # type: ignore[misc]
    assert isinstance(only, RuleSnapshot)


def test_fingerprint_is_stable_for_identical_evaluations_and_moves_across_a_boundary():
    pack = _pack(
        _rule(
            "allow",
            "contact",
            "email",
            "law",
            conditions={"consent": "required"},
            effective_from=COMMENCE.isoformat(),
        )
    )
    req = PolicyRequest(action="contact", channel="email", facts={"consent": "present"})

    at_a = decide(req, pack, now=AT).snapshot
    at_b = decide(req, pack, now=AT).snapshot
    before = decide(req, pack, now=BEFORE).snapshot
    assert at_a is not None and at_b is not None and before is not None
    # Identical evaluation => identical fingerprint (reproducible, no hidden state).
    assert at_a.fingerprint == at_b.fingerprint
    # Different side of the boundary selects a different rule set => a different fingerprint, and the
    # pre-commencement snapshot has NO selected rules (the obligation was not yet in force).
    assert before.fingerprint != at_a.fingerprint
    assert before.selected_rules == ()


# --------------------------------------------------------------------------------------------------
# 3) Reproducibility via replay — reproduce the result AND the relevant denial, no state change.
# --------------------------------------------------------------------------------------------------


def test_replay_reproduces_decision_from_snapshot_alone():
    pack = _pack(
        _rule(
            "allow",
            "contact",
            "email",
            "law",
            conditions={"consent": "required"},
            effective_from=COMMENCE.isoformat(),
        )
    )
    req = PolicyRequest(action="contact", channel="email", facts={"consent": "present"})
    original = decide(req, pack, now=AT)
    assert original.snapshot is not None

    reproduced = replay(req, original.snapshot)
    assert reproduced.decision is original.decision
    assert reproduced.reasons == original.reasons
    assert reproduced.rulepack_version == original.rulepack_version
    # The replay re-derives the identical immutable snapshot (same fingerprint).
    assert reproduced.snapshot is not None
    assert reproduced.snapshot.fingerprint == original.snapshot.fingerprint


def test_replay_reproduces_historical_decision_even_after_live_pack_changes():
    """An ALLOW recorded under the in-force pack stays reproducible from its snapshot even though the
    live rulepack has since been superseded by a deny — the snapshot is the durable record."""
    allow_pack = _pack(
        _rule(
            "allow",
            "contact",
            "email",
            "law",
            conditions={"consent": "required"},
            effective_from=COMMENCE.isoformat(),
        )
    )
    req = PolicyRequest(action="contact", channel="email", facts={"consent": "present"})
    historical = decide(req, allow_pack, now=AT)
    assert historical.decision is PolicyDecision.ALLOW

    # The live pack later changes to deny the very same action — the CURRENT decision flips...
    superseded_pack = _pack(_rule("deny", "contact", "email", "law"), version=2)
    assert decide(req, superseded_pack, now=AT).decision is PolicyDecision.DENY
    # ...but replaying the OLD snapshot still reproduces the historical ALLOW (immutable record).
    assert historical.snapshot is not None
    assert replay(req, historical.snapshot).decision is PolicyDecision.ALLOW


def test_replay_reproduces_the_relevant_denial():
    """The pre-commencement denial reproduces from its snapshot too (empty selected set)."""
    pack = _pack(
        _rule(
            "allow",
            "contact",
            "email",
            "law",
            conditions={"consent": "required"},
            effective_from=COMMENCE.isoformat(),
        )
    )
    req = PolicyRequest(action="contact", channel="email", facts={"consent": "present"})
    denied = decide(req, pack, now=BEFORE)
    assert denied.decision is PolicyDecision.DENY
    assert denied.reasons == (REASON_NO_MATCHING_RULE,)
    assert denied.snapshot is not None and denied.snapshot.selected_rules == ()

    reproduced = replay(req, denied.snapshot)
    assert reproduced.decision is PolicyDecision.DENY
    assert reproduced.reasons == (REASON_NO_MATCHING_RULE,)


# --------------------------------------------------------------------------------------------------
# 4) DB-backed boundary path on a disposable PostgreSQL (never SQLite for the real read path).
# --------------------------------------------------------------------------------------------------


def _record_dated_pack_and_approve(session_factory) -> None:
    """Record an India rulepack whose only rule commences at COMMENCE, then approve it in force."""
    pack = RulepackInput(
        jurisdiction="IN",
        title="India notice/consent — staged commencement",
        evidence=_evidence("POLICY-REVIEW-2026-11"),
        rules=[
            PolicyRuleInput(
                effect="allow",
                action="contact",
                channel="email",
                authority="law",
                conditions={"consent": "required"},
                evidence=_evidence("DPDP-2023-s7"),
                effective_from=COMMENCE,
            )
        ],
    )
    with session_factory() as session:
        CompliancePolicyRulepackStore(session).add_version(pack)
        session.commit()
    review = PolicyApprovalReview(
        reviewer_id="op-001",
        references={"reference": "LEGAL-REVIEW-2026-11", "kind": "legal_review"},
        reviewed_at=BEFORE,  # approved before the obligation commences; still gated by the rule window
        expires_at=AFTER + timedelta(days=365),
    )
    with session_factory() as session:
        PolicyApprovalStore(session).approve(1, review, OPERATOR)
        session.commit()


def _run_boundary_suite(session_factory) -> None:
    """The DB-backed acceptance scenario: the same request flips at the obligation's effective date,
    and evaluating writes nothing."""
    _record_dated_pack_and_approve(session_factory)
    req = PolicyRequest(action="contact", channel="email", facts={"consent": "present"})

    with session_factory() as session:
        evaluator = PolicyEvaluator(session)
        before = evaluator.evaluate(req, now=BEFORE)
        at = evaluator.evaluate(req, now=AT)
        after = evaluator.evaluate(req, now=AFTER)
        packs = session.query(CompliancePolicyRulepack).count()
        approvals = session.query(CompliancePolicyApproval).count()

    # The approval is in force the whole time; only the RULE's effective date gates the decision —
    # there is no universal flag that pre-enables it.
    assert before.decision is PolicyDecision.DENY
    assert before.reasons == (REASON_NO_MATCHING_RULE,)
    assert at.decision is PolicyDecision.ALLOW
    assert after.decision is PolicyDecision.ALLOW
    # Snapshots are produced for the real read path too, and reproduce their decisions.
    assert at.snapshot is not None and replay(req, at.snapshot).decision is PolicyDecision.ALLOW
    assert (
        before.snapshot is not None and replay(req, before.snapshot).decision is PolicyDecision.DENY
    )
    # Evaluating changed no state.
    assert packs == 1 and approvals == 1


@pytest.fixture
def sqlite_session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def test_boundary_evaluator_over_sqlite(sqlite_session_factory):
    # SQLite exercises the store logic; the REAL transaction/read path is pinned on Postgres below.
    _run_boundary_suite(sqlite_session_factory)


PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")


@pytest.mark.skipif(not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres")
def test_boundary_evaluator_over_postgres():
    """Exercise the real resolve→decide boundary path on a disposable PostgreSQL, not SQLite."""
    engine = create_engine(PG_URL, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        _run_boundary_suite(make_session_factory(engine))
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
