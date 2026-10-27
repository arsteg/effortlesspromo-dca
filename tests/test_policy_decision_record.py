"""DCA-057 — persist policy decision evidence.

Pins the load-bearing behaviours of :mod:`app.services.policy_decision_record` — the single sanctioned
writer of :class:`~app.db.models.PolicyDecisionRecord`:

1. **A stored ALLOW is traceable to the exact reviewed rule/evidence set and can NEVER imply permanent
   authorization.** The row pins the in-force rulepack version + DCA-053 approval + engine version + the
   evidence consulted + the immutable DCA-056 snapshot (fingerprint), AND the writer refuses to persist
   an ALLOW without a non-null, strictly-future expiry — so a stored allow always lapses with the review
   it rests on. :meth:`is_in_force` reports True only for an unexpired ALLOW.
2. **The decision reproduces from the row ALONE, with no state change** — the recorded ALLOW *and* the
   relevant denial both replay to the identical decision even after the live pack has changed, reading
   only the row and writing nothing.
3. **Relevant source/consent/suppression versions, scoping and atomic audit.** The opaque version tags
   are captured on the row, every read/write is confined to one business, and each record stages an
   atomic DCA-026 audit entry in the SAME unit of work.

The store logic is unit-tested on in-memory SQLite; the real transaction/read path (resolve→evaluate→
record on the DCA-052/053 stores) is exercised on a disposable PostgreSQL (``DCA_TEST_DATABASE_URL``),
never relying on SQLite for that. The Postgres test skips when no URL is set. The RLS/migration paths
for ``policy_decision_records`` are covered by ``tests/test_rls.py`` and ``tests/test_migrations.py``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine

from app.audit.sink import ActorType, AuditActor
from app.compliance.policy_engine import (
    REASON_ALLOWED_BY_RULE,
    REASON_DENIED_BY_RULE,
    REASON_NO_IN_FORCE_PACK,
    REASON_NO_MATCHING_RULE,
    InForcePack,
    PolicyDecision,
    PolicyRequest,
    decide,
)
from app.compliance.policy_rulepacks import (
    CompliancePolicyRulepackStore,
    PolicyRuleInput,
    RulepackInput,
)
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import AuditEvent, PolicyDecisionRecord
from app.services.policy_approval import (
    PolicyApprovalReview,
    PolicyApprovalStore,
    PolicyReviewerPrincipal,
)
from app.services.policy_decision_record import (
    AUDIT_ACTION_RECORD_DECISION,
    REASON_ALLOW_REQUIRES_EXPIRY,
    REASON_INVALID_EXPIRY,
    PolicyDecisionContext,
    PolicyDecisionRecordError,
    PolicyDecisionRecordStore,
    evaluate_and_record,
)

COMMENCE = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
AT = COMMENCE
BEFORE = COMMENCE - timedelta(seconds=1)
AFTER = COMMENCE + timedelta(days=30)
REVIEW_EXPIRY = COMMENCE + timedelta(days=365)

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"
ACTOR = AuditActor(id="00000000-0000-4000-9000-0000000a0057", type=ActorType.USER)
OPERATOR = PolicyReviewerPrincipal(subject="op-001", roles=("platform_operator",))


def _evidence(ref: str = "DPDP-2023-s7") -> dict:
    return {"reference": ref, "kind": "statute", "uri": "https://example.test/law"}


def _rule(effect: str, action: str, channel: str, authority: str, **extra) -> dict:
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


def _allow_pack() -> InForcePack:
    """An in-force pack whose only rule allows a consented email contact — a traceable ALLOW."""
    return InForcePack(
        jurisdiction="IN",
        version=1,
        approval_id="appr-1",
        rules=[_rule("allow", "contact", "email", "law", conditions={"consent": "required"})],
        evidence={"reference": "POLICY-REVIEW-2026-11"},
    )


def _consented_request() -> PolicyRequest:
    return PolicyRequest(action="contact", channel="email", facts={"consent": "present"})


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


# --------------------------------------------------------------------------------------------------
# 1) A stored ALLOW is traceable to the exact reviewed rule/evidence set and is not permanent.
# --------------------------------------------------------------------------------------------------


def test_allow_record_is_traceable_to_the_reviewed_rule_and_not_permanent(session_factory):
    evaluation = decide(_consented_request(), _allow_pack(), now=AT)
    assert evaluation.decision is PolicyDecision.ALLOW
    assert evaluation.snapshot is not None

    with session_factory() as session:
        store = PolicyDecisionRecordStore(session, BIZ_A)
        record = store.record(
            evaluation,
            _consented_request(),
            actor=ACTOR,
            expires_at=REVIEW_EXPIRY,
            context=PolicyDecisionContext(
                consent_version="consent-basis-v3",
                suppression_version="suppress-2026-10",
                source_versions={"mca_master_data": 2},
            ),
        )
        session.commit()
        record_id = record.id

    with session_factory() as session:
        row = PolicyDecisionRecordStore(session, BIZ_A).get(record_id)
        assert row is not None
        # Traceable to the EXACT reviewed content that decided it.
        assert row.decision == PolicyDecision.ALLOW.value
        assert row.reason_codes == [REASON_ALLOWED_BY_RULE]
        assert row.rulepack_version == 1
        assert row.approval_id == "appr-1"
        assert row.engine_version == evaluation.engine_version
        assert row.decisive_authority == "law"
        assert row.evidence  # the rule(s)/pack/approval actually consulted
        assert any(e.get("source") == "rule" for e in row.evidence)
        assert row.snapshot is not None
        assert row.snapshot_fingerprint == evaluation.snapshot.fingerprint
        # Relevant source/consent/suppression versions captured as opaque tags.
        assert row.references["consent_version"] == "consent-basis-v3"
        assert row.references["suppression_version"] == "suppress-2026-10"
        assert row.references["source_versions"] == {"mca_master_data": 2}
        # NOT permanent: a non-null, strictly-future expiry that lapses with the review.
        assert row.expires_at is not None
        # In force only strictly before the expiry; never after.
        assert PolicyDecisionRecordStore.is_in_force(row, now=AFTER) is True
        assert PolicyDecisionRecordStore.is_in_force(row, now=REVIEW_EXPIRY) is False
        assert (
            PolicyDecisionRecordStore.is_in_force(row, now=REVIEW_EXPIRY + timedelta(days=1))
            is False
        )


def test_allow_without_expiry_is_refused_and_stages_no_row(session_factory):
    evaluation = decide(_consented_request(), _allow_pack(), now=AT)
    with session_factory() as session:
        store = PolicyDecisionRecordStore(session, BIZ_A)
        with pytest.raises(PolicyDecisionRecordError) as exc:
            store.record(evaluation, _consented_request(), actor=ACTOR, expires_at=None)
        assert exc.value.code == REASON_ALLOW_REQUIRES_EXPIRY
        session.commit()
        # The refusal left no row and no audit entry (raised before the session was mutated).
        assert session.query(PolicyDecisionRecord).count() == 0
        assert session.query(AuditEvent).count() == 0


def test_expiry_at_or_before_evaluation_moment_is_refused(session_factory):
    evaluation = decide(_consented_request(), _allow_pack(), now=AT)
    with session_factory() as session:
        store = PolicyDecisionRecordStore(session, BIZ_A)
        # An expiry equal to the evaluation moment would be a zero-length grant — refused.
        with pytest.raises(PolicyDecisionRecordError) as exc:
            store.record(evaluation, _consented_request(), actor=ACTOR, expires_at=AT)
        assert exc.value.code == REASON_INVALID_EXPIRY
        # An expiry before the evaluation moment is refused too.
        with pytest.raises(PolicyDecisionRecordError) as exc2:
            store.record(evaluation, _consented_request(), actor=ACTOR, expires_at=BEFORE)
        assert exc2.value.code == REASON_INVALID_EXPIRY
        session.commit()
        assert session.query(PolicyDecisionRecord).count() == 0


def test_denial_needs_no_expiry_and_is_never_in_force(session_factory):
    """A non-ALLOW outcome grants nothing; it needs no expiry and is_in_force is always False."""
    evaluation = decide(_consented_request(), None, now=AT)  # no in-force pack => fail-closed DENY
    assert evaluation.decision is PolicyDecision.DENY
    assert evaluation.reasons == (REASON_NO_IN_FORCE_PACK,)
    with session_factory() as session:
        store = PolicyDecisionRecordStore(session, BIZ_A)
        record = store.record(evaluation, _consented_request(), actor=ACTOR, expires_at=None)
        session.commit()
        assert record.expires_at is None
        assert PolicyDecisionRecordStore.is_in_force(record, now=AT) is False


# --------------------------------------------------------------------------------------------------
# 2) The decision reproduces from the row ALONE, with no state change — result AND relevant denial.
# --------------------------------------------------------------------------------------------------


def test_reproduce_allow_from_row_alone_even_after_live_pack_changes(session_factory):
    evaluation = decide(_consented_request(), _allow_pack(), now=AT)
    with session_factory() as session:
        record = PolicyDecisionRecordStore(session, BIZ_A).record(
            evaluation, _consented_request(), actor=ACTOR, expires_at=REVIEW_EXPIRY
        )
        session.commit()
        record_id = record.id

    with session_factory() as session:
        row = PolicyDecisionRecordStore(session, BIZ_A).get(record_id)
        assert row is not None
        rows_before = session.query(PolicyDecisionRecord).count()

        reproduced = PolicyDecisionRecordStore.reproduce(row)

        assert reproduced.decision is PolicyDecision.ALLOW
        assert reproduced.reasons == (REASON_ALLOWED_BY_RULE,)
        assert reproduced.rulepack_version == 1
        assert reproduced.snapshot is not None
        assert reproduced.snapshot.fingerprint == row.snapshot_fingerprint
        # Reproduction is pure: no row added, the session is not left dirty.
        assert session.query(PolicyDecisionRecord).count() == rows_before
        assert not session.new and not session.dirty and not session.deleted


def test_reproduce_the_relevant_denial_from_row_alone(session_factory):
    """A pre-commencement default-deny reproduces from the row (snapshot with an empty selected set)."""
    dated_pack = InForcePack(
        jurisdiction="IN",
        version=1,
        approval_id="appr-1",
        rules=[
            _rule(
                "allow",
                "contact",
                "email",
                "law",
                conditions={"consent": "required"},
                effective_from=COMMENCE.isoformat(),
            )
        ],
    )
    evaluation = decide(_consented_request(), dated_pack, now=BEFORE)
    assert evaluation.decision is PolicyDecision.DENY
    assert evaluation.reasons == (REASON_NO_MATCHING_RULE,)

    with session_factory() as session:
        record = PolicyDecisionRecordStore(session, BIZ_A).record(
            evaluation, _consented_request(), actor=ACTOR, expires_at=None
        )
        session.commit()
        record_id = record.id

    with session_factory() as session:
        row = PolicyDecisionRecordStore(session, BIZ_A).get(record_id)
        assert row is not None
        reproduced = PolicyDecisionRecordStore.reproduce(row)
        assert reproduced.decision is PolicyDecision.DENY
        assert reproduced.reasons == (REASON_NO_MATCHING_RULE,)


def test_reproduce_fail_closed_denial_without_snapshot(session_factory):
    """A fail-closed denial taken before any pack was consulted has no snapshot; reproduce re-decides
    with no pack at the recorded moment and still yields the identical DENY."""
    evaluation = decide(_consented_request(), None, now=AT)
    assert evaluation.snapshot is None
    with session_factory() as session:
        record = PolicyDecisionRecordStore(session, BIZ_A).record(
            evaluation, _consented_request(), actor=ACTOR, expires_at=None
        )
        session.commit()
        record_id = record.id

    with session_factory() as session:
        row = PolicyDecisionRecordStore(session, BIZ_A).get(record_id)
        assert row is not None and row.snapshot is None
        reproduced = PolicyDecisionRecordStore.reproduce(row)
        assert reproduced.decision is PolicyDecision.DENY
        assert reproduced.reasons == (REASON_NO_IN_FORCE_PACK,)


# --------------------------------------------------------------------------------------------------
# 3) Scoping + atomic audit.
# --------------------------------------------------------------------------------------------------


def test_record_stages_an_atomic_audit_entry_in_the_same_unit_of_work(session_factory):
    evaluation = decide(_consented_request(), _allow_pack(), now=AT)
    with session_factory() as session:
        PolicyDecisionRecordStore(session, BIZ_A).record(
            evaluation, _consented_request(), actor=ACTOR, expires_at=REVIEW_EXPIRY
        )
        session.commit()

    with session_factory() as session:
        audits = session.query(AuditEvent).all()
        assert len(audits) == 1
        audit = audits[0]
        assert audit.action == AUDIT_ACTION_RECORD_DECISION
        assert audit.business_id == BIZ_A
        assert audit.actor_id == ACTOR.id
        # Opaque references only — decision + versions, never contact data.
        assert audit.references["decision"] == PolicyDecision.ALLOW.value
        assert audit.references["rulepackVersion"] == 1
        assert audit.references["snapshotFingerprint"] == evaluation.snapshot.fingerprint


def test_records_are_tenant_scoped(session_factory):
    evaluation = decide(_consented_request(), _allow_pack(), now=AT)
    with session_factory() as session:
        record = PolicyDecisionRecordStore(session, BIZ_A).record(
            evaluation, _consented_request(), actor=ACTOR, expires_at=REVIEW_EXPIRY
        )
        session.commit()
        record_id = record.id

    with session_factory() as session:
        # Business B cannot read Business A's decision record — it reads as not-found.
        assert PolicyDecisionRecordStore(session, BIZ_B).get(record_id) is None
        assert PolicyDecisionRecordStore(session, BIZ_B).list() == []
        assert PolicyDecisionRecordStore(session, BIZ_B).latest() is None
        # A's own store sees exactly its one record.
        assert len(PolicyDecisionRecordStore(session, BIZ_A).list()) == 1
        assert PolicyDecisionRecordStore(session, BIZ_A).get(record_id) is not None


# --------------------------------------------------------------------------------------------------
# 4) evaluate_and_record: derive the expiry from the DCA-053 approval (the review expiry).
# --------------------------------------------------------------------------------------------------


def _seed_in_force_pack(session_factory, *, effective_from: datetime | None = None) -> None:
    """Record a DCA-052 India rulepack (one consented-email allow rule) and approve it in force."""
    pack = RulepackInput(
        jurisdiction="IN",
        title="India notice/consent",
        evidence=_evidence("POLICY-REVIEW-2026-11"),
        rules=[
            PolicyRuleInput(
                effect="allow",
                action="contact",
                channel="email",
                authority="law",
                conditions={"consent": "required"},
                evidence=_evidence("DPDP-2023-s7"),
                effective_from=effective_from,
            )
        ],
    )
    with session_factory() as session:
        CompliancePolicyRulepackStore(session).add_version(pack)
        session.commit()
    review = PolicyApprovalReview(
        reviewer_id="op-001",
        references={"reference": "LEGAL-REVIEW-2026-11", "kind": "legal_review"},
        reviewed_at=BEFORE,
        expires_at=REVIEW_EXPIRY,
    )
    with session_factory() as session:
        PolicyApprovalStore(session).approve(1, review, OPERATOR)
        session.commit()


def _run_evaluate_and_record_suite(session_factory) -> None:
    _seed_in_force_pack(session_factory)

    with session_factory() as session:
        evaluation, record = evaluate_and_record(
            session, BIZ_A, _consented_request(), actor=ACTOR, now=AT
        )
        session.commit()
        record_id = record.id

    with session_factory() as session:
        row = PolicyDecisionRecordStore(session, BIZ_A).get(record_id)
        assert row is not None
        assert row.decision == PolicyDecision.ALLOW.value
        # A recorded ALLOW's expiry IS the review expiry — it can never outlive the review.
        assert row.expires_at is not None
        assert row.expires_at.replace(tzinfo=UTC) == REVIEW_EXPIRY
        assert PolicyDecisionRecordStore.is_in_force(row, now=AFTER) is True
        assert PolicyDecisionRecordStore.is_in_force(row, now=REVIEW_EXPIRY) is False
        # Reproduces from the row alone.
        assert PolicyDecisionRecordStore.reproduce(row).decision is PolicyDecision.ALLOW
        # Exactly one record + one audit entry, both scoped to A.
        assert session.query(PolicyDecisionRecord).count() == 1
        assert session.query(AuditEvent).count() == 1


def test_evaluate_and_record_over_sqlite(session_factory):
    _run_evaluate_and_record_suite(session_factory)


PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")


@pytest.mark.skipif(not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres")
def test_evaluate_and_record_over_postgres():
    """Exercise the real resolve→evaluate→record→reproduce path on a disposable PostgreSQL, with real
    transactions — never SQLite for the durable read/write path."""
    engine = create_engine(PG_URL, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        _run_evaluate_and_record_suite(make_session_factory(engine))
        # A denial also persists and reproduces with no state change (relevant denial case).
        factory = make_session_factory(engine)
        with factory() as session:
            evaluation, record = evaluate_and_record(
                session,
                BIZ_A,
                PolicyRequest(action="contact", channel="email"),  # no consent fact
                actor=ACTOR,
                now=AT,
            )
            session.commit()
            assert evaluation.decision in {PolicyDecision.DENY, PolicyDecision.REVIEW}
            reproduced = PolicyDecisionRecordStore.reproduce(record)
            assert reproduced.decision is evaluation.decision
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_denied_by_rule_record_reproduces(session_factory):
    """A rule-driven DENY (a deny rule in force) persists, is never in force, and reproduces."""
    deny_pack = InForcePack(
        jurisdiction="IN",
        version=3,
        approval_id="appr-3",
        rules=[_rule("deny", "contact", "sms", "provider")],
        evidence={"reference": "TRAI-DLT"},
    )
    request = PolicyRequest(action="contact", channel="sms")
    evaluation = decide(request, deny_pack, now=AT)
    assert evaluation.decision is PolicyDecision.DENY
    assert evaluation.reasons == (REASON_DENIED_BY_RULE,)

    with session_factory() as session:
        record = PolicyDecisionRecordStore(session, BIZ_A).record(
            evaluation, request, actor=ACTOR, expires_at=None
        )
        session.commit()
        record_id = record.id

    with session_factory() as session:
        row = PolicyDecisionRecordStore(session, BIZ_A).get(record_id)
        assert row is not None
        assert PolicyDecisionRecordStore.is_in_force(row, now=AT) is False
        reproduced = PolicyDecisionRecordStore.reproduce(row)
        assert reproduced.decision is PolicyDecision.DENY
        assert reproduced.reasons == (REASON_DENIED_BY_RULE,)
