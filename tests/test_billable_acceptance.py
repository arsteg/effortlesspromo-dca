"""DCA-157 — emit a BILLABLE acceptance ONLY AFTER acknowledgment.

Service unit tests (in-memory SQLite) pin
:meth:`~app.services.billable_acceptance.BillableAcceptanceEmitter.emit`: a billable ledger entry (+ a
durable DCA-027 usage record + exactly ONE atomic audit) is emitted ONLY when the DCA-153
:class:`~app.db.models.AcceptanceIntent` is ``synced`` with ``sync_status='acknowledged'`` and the
platform's external mapping present. A ``pending_sync`` (never-synced) intent, a ``pending`` (lost-
acknowledgment) hand-off, and an in-doubt ``needs_review`` reconcile outcome are each REFUSED with NO
entry, NO usage record and NO audit — an excluded / failed / pending record is never charged. Emission is
idempotent: a duplicate acknowledgment (a replay, or a DCA-156 reconcile that re-resolved the SAME intent
under the SAME request key) short-circuits on the existing entry (``already_emitted``) — exactly one
billable event per accepted prospect, never double-charged. The ``billing_key`` is the STABLE logical
billing key (deterministic in business + intent). Emitting makes NO live billing call and charges nothing
(``charged`` is always ``False``); it confers NO recipient consent and performs NO send. An unauthorized
viewer and an unknown / cross-tenant id are refused with no state change.

SQLite is sufficient here — the behaviour is a tenant-scoped row insert + one audit insert + one outbox
insert; the RLS / migration paths are covered by ``tests/test_rls.py`` / ``tests/test_migrations.py``
(which pin the ``billable_acceptances`` table + its 0071 create branch), and a PG-gated test below proves
the emission is tenant-scoped and atomic over real Postgres when ``DCA_TEST_DATABASE_URL`` is set.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event
from sqlalchemy.engine import make_url

from app.audience import (
    AudienceField,
    AudienceFieldClass,
    AudienceSyncStatus,
    FakeEffortlessPromoAudienceSyncPort,
)
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import (
    AcceptanceIntent,
    AcceptanceIntentStatus,
    AuditEvent,
    BillableAcceptance,
    BillableAcceptanceStatus,
    OutboxMessage,
    ProspectSuggestion,
    SuggestionVisibility,
    billable_acceptance_key,
)
from app.services.acceptance_intent import AcceptanceIntentActor, AcceptanceIntentRecorder
from app.services.acceptance_sync import AcceptanceRecordSynchronizer, AcceptanceSyncActor
from app.services.acceptance_sync_reconcile import (
    RECONCILE_NEEDS_REVIEW,
    UncertainAudienceSyncReconciler,
)
from app.services.billable_acceptance import (
    AUDIT_ACTION_BILLABLE,
    OUTBOX_EVENT_BILLABLE,
    REASON_INTENT_NOT_FOUND,
    REASON_NOT_ACKNOWLEDGED,
    BillableAcceptanceAuthorizationError,
    BillableAcceptanceEmitter,
    BillableAcceptanceError,
)
from tests.conftest import make_settings

BIZ_A = "11111111-1111-1111-1111-111111111157"
BIZ_B = "22222222-2222-2222-2222-222222222257"

ADMIN_ROLES = ("owner",)
VIEWER_ROLES = ("viewer",)

ORG_FIELD = AudienceField(
    key="business_type", field_class=AudienceFieldClass.ORGANIZATION, value_digest="retail"
)


def _accept_actor(subject: str = "00000000-0000-4000-9000-0000000a0157a") -> AcceptanceIntentActor:
    return AcceptanceIntentActor(subject=subject, roles=ADMIN_ROLES)


def _admin(subject: str = "00000000-0000-4000-9000-0000000a0157a") -> AcceptanceSyncActor:
    return AcceptanceSyncActor(subject=subject, roles=ADMIN_ROLES)


def _viewer(subject: str = "00000000-0000-4000-9000-0000000a0157b") -> AcceptanceSyncActor:
    return AcceptanceSyncActor(subject=subject, roles=VIEWER_ROLES)


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def _seed_pending_sync_intent(
    session, business_id: str, subject_id: str, *, now: datetime | None = None
) -> str:
    """Record a real ``pending_sync`` acceptance intent (the DCA-153 path), NEVER synced."""
    moment = now or datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    suggestion = ProspectSuggestion(
        business_id=business_id,
        run_id="run-157",
        subject_type="organization",
        subject_id=subject_id,
        visibility=SuggestionVisibility.RESEARCH_ONLY.value,
        match_score=0.7,
        rationale="ICP fit 0.70",
        scored_icp_version=7,
        schema_version=1,
        status="suggested",
        provenance={"source": "prospect_suggestion", "matchedDimensions": ["industry"]},
        created_at=moment,
        expires_at=moment + timedelta(days=30),
    )
    session.add(suggestion)
    session.commit()
    result = AcceptanceIntentRecorder(session, business_id).accept(
        suggestion.id, _accept_actor(), now=moment
    )
    session.commit()
    return result.intent.id


def _seed_acknowledged_intent(
    session, business_id: str, subject_id: str, *, now: datetime | None = None
) -> str:
    """Drive a real DCA-155 sync that ACKNOWLEDGES, leaving the intent ``synced`` (billable)."""
    moment = now or datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    intent_id = _seed_pending_sync_intent(session, business_id, subject_id, now=moment)
    AcceptanceRecordSynchronizer(
        session,
        business_id,
        make_settings(),
        port=FakeEffortlessPromoAudienceSyncPort(acknowledges=True),
    ).sync(intent_id, _admin(), fields=(ORG_FIELD,), now=moment)
    session.commit()
    intent = session.get(AcceptanceIntent, intent_id)
    assert intent.status == AcceptanceIntentStatus.SYNCED.value
    assert intent.sync_status == AudienceSyncStatus.ACKNOWLEDGED.value
    return intent_id


def _seed_uncertain_intent(
    session, business_id: str, subject_id: str, *, now: datetime | None = None
) -> str:
    """Drive a real DCA-155 sync that does NOT acknowledge, leaving the intent PENDING (uncertain)."""
    moment = now or datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    intent_id = _seed_pending_sync_intent(session, business_id, subject_id, now=moment)
    AcceptanceRecordSynchronizer(
        session,
        business_id,
        make_settings(),
        port=FakeEffortlessPromoAudienceSyncPort(acknowledges=False),
    ).sync(intent_id, _admin(), fields=(ORG_FIELD,), now=moment)
    session.commit()
    intent = session.get(AcceptanceIntent, intent_id)
    assert intent.sync_status == AudienceSyncStatus.PENDING.value
    return intent_id


def _seed_needs_review_intent(
    session, business_id: str, subject_id: str, *, now: datetime | None = None
) -> str:
    """Reconcile an uncertain intent with an UNCONFIRMED re-drive, leaving it ``needs_review``."""
    moment = now or datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    intent_id = _seed_uncertain_intent(session, business_id, subject_id, now=moment)
    UncertainAudienceSyncReconciler(
        session,
        business_id,
        make_settings(),
        port=FakeEffortlessPromoAudienceSyncPort(acknowledges=False),
    ).reconcile(intent_id, _admin(), fields=(ORG_FIELD,), now=moment)
    session.commit()
    intent = session.get(AcceptanceIntent, intent_id)
    assert intent.sync_reconcile_state == RECONCILE_NEEDS_REVIEW
    return intent_id


def _billable_audits(session) -> int:
    return session.query(AuditEvent).filter_by(action=AUDIT_ACTION_BILLABLE).count()


def _usage_records(session) -> int:
    return session.query(OutboxMessage).filter_by(event_type=OUTBOX_EVENT_BILLABLE).count()


# --------------------------------------------------------------------------------------------------
# Billable emitted EXACTLY once, ONLY on acknowledgment
# --------------------------------------------------------------------------------------------------


def test_emits_billable_on_acknowledgment(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        intent_id = _seed_acknowledged_intent(session, BIZ_A, "subject-1", now=now)

        result = BillableAcceptanceEmitter(session, BIZ_A).emit(intent_id, _admin(), now=now)
        session.commit()

        entry = session.get(BillableAcceptance, result.entry.id)
        assert result.emitted is True
        assert result.already_emitted is False
        # Exactly one ledger entry, one usage record, one atomic audit.
        assert session.query(BillableAcceptance).count() == 1
        assert _usage_records(session) == 1
        assert _billable_audits(session) == 1
        # The stable logical billing key is deterministic in (business, intent).
        assert result.billing_key == billable_acceptance_key(BIZ_A, intent_id)
        assert entry.billing_key == result.billing_key
        assert entry.status == BillableAcceptanceStatus.RECORDED.value
        assert entry.acceptance_intent_id == intent_id
        # The external mapping that proves acknowledgment is captured.
        assert entry.external_crm_record_id
        # A billable fact is not a charge / send / consent.
        assert entry.provenance["billable"] is True
        assert entry.provenance["delivered"] is False
        assert entry.provenance["sent"] is False
        assert entry.provenance["spent"] is False
        assert entry.provenance["consentConferred"] is False
        assert result.charged is False
        assert result.confers_consent is False
        # Audit carries only opaque references (no contact value).
        refs = session.query(AuditEvent).filter_by(action=AUDIT_ACTION_BILLABLE).one().references
        assert refs["acceptanceIntentId"] == intent_id
        assert refs["billingKey"] == result.billing_key
        assert refs["charged"] is False
        assert not any(k.lower() in {"email", "phone", "contact"} for k in refs)


def test_replay_is_idempotent_no_double_charge(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        intent_id = _seed_acknowledged_intent(session, BIZ_A, "subject-1", now=now)
        emitter = BillableAcceptanceEmitter(session, BIZ_A)

        first = emitter.emit(intent_id, _admin(), now=now)
        session.commit()
        second = emitter.emit(intent_id, _admin(), now=now + timedelta(hours=1))
        session.commit()

        assert first.emitted is True and first.already_emitted is False
        # The second emission is an idempotent no-op: no second entry / usage record / audit.
        assert second.already_emitted is True
        assert second.emitted is False
        assert second.entry.id == first.entry.id
        assert second.billing_key == first.billing_key
        assert session.query(BillableAcceptance).count() == 1
        assert _usage_records(session) == 1
        assert _billable_audits(session) == 1


def test_reconcile_re_resolution_never_double_charges(session_factory):
    """A DCA-156 reconcile can re-resolve the SAME intent under the SAME request key; emitting the
    billable acceptance after it still yields exactly ONE entry — never a second charge."""
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        intent_id = _seed_acknowledged_intent(session, BIZ_A, "subject-1", now=now)
        emitter = BillableAcceptanceEmitter(session, BIZ_A)
        first = emitter.emit(intent_id, _admin(), now=now)
        session.commit()

        # A later reconcile re-drives the SAME synced intent (idempotent no-op on the sync side) and a
        # duplicate billable emission still short-circuits — one entry, one usage record, one audit.
        UncertainAudienceSyncReconciler(
            session,
            BIZ_A,
            make_settings(),
            port=FakeEffortlessPromoAudienceSyncPort(acknowledges=True),
        ).reconcile(intent_id, _admin(), fields=(ORG_FIELD,), now=now + timedelta(hours=1))
        session.commit()
        second = emitter.emit(intent_id, _admin(), now=now + timedelta(hours=2))
        session.commit()

        assert second.already_emitted is True
        assert second.entry.id == first.entry.id
        assert session.query(BillableAcceptance).count() == 1
        assert _usage_records(session) == 1
        assert _billable_audits(session) == 1


# --------------------------------------------------------------------------------------------------
# NOT emitted for pending / uncertain / failed records
# --------------------------------------------------------------------------------------------------


def test_not_emitted_for_never_synced_intent(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        intent_id = _seed_pending_sync_intent(session, BIZ_A, "subject-1", now=now)

        with pytest.raises(BillableAcceptanceError) as exc:
            BillableAcceptanceEmitter(session, BIZ_A).emit(intent_id, _admin(), now=now)
        session.rollback()

        assert exc.value.code == REASON_NOT_ACKNOWLEDGED
        assert session.query(BillableAcceptance).count() == 0
        assert _usage_records(session) == 0
        assert _billable_audits(session) == 0


def test_not_emitted_for_pending_uncertain_sync(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        intent_id = _seed_uncertain_intent(session, BIZ_A, "subject-1", now=now)

        with pytest.raises(BillableAcceptanceError) as exc:
            BillableAcceptanceEmitter(session, BIZ_A).emit(intent_id, _admin(), now=now)
        session.rollback()

        assert exc.value.code == REASON_NOT_ACKNOWLEDGED
        assert session.query(BillableAcceptance).count() == 0
        assert _usage_records(session) == 0
        assert _billable_audits(session) == 0


def test_not_emitted_for_needs_review_intent(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        intent_id = _seed_needs_review_intent(session, BIZ_A, "subject-1", now=now)

        with pytest.raises(BillableAcceptanceError) as exc:
            BillableAcceptanceEmitter(session, BIZ_A).emit(intent_id, _admin(), now=now)
        session.rollback()

        assert exc.value.code == REASON_NOT_ACKNOWLEDGED
        assert session.query(BillableAcceptance).count() == 0
        assert _usage_records(session) == 0
        assert _billable_audits(session) == 0


# --------------------------------------------------------------------------------------------------
# Authorization + tenant isolation (denials change nothing)
# --------------------------------------------------------------------------------------------------


def test_unauthorized_viewer_refused_with_no_state_change(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        intent_id = _seed_acknowledged_intent(session, BIZ_A, "subject-1", now=now)

        with pytest.raises(BillableAcceptanceAuthorizationError):
            BillableAcceptanceEmitter(session, BIZ_A).emit(intent_id, _viewer(), now=now)
        session.rollback()

        assert session.query(BillableAcceptance).count() == 0
        assert _usage_records(session) == 0
        assert _billable_audits(session) == 0


def test_unknown_or_cross_tenant_id_is_not_found(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        b_intent_id = _seed_acknowledged_intent(session, BIZ_B, "subject-b", now=now)
        emitter = BillableAcceptanceEmitter(session, BIZ_A)

        with pytest.raises(BillableAcceptanceError) as unknown:
            emitter.emit("no-such-id", _admin(), now=now)
        assert unknown.value.code == REASON_INTENT_NOT_FOUND

        # An A-emitter cannot reach B's acknowledged intent by id.
        with pytest.raises(BillableAcceptanceError) as cross:
            emitter.emit(b_intent_id, _admin(), now=now)
        assert cross.value.code == REASON_INTENT_NOT_FOUND

        # B's intent produced no billable entry via the A-emitter.
        assert session.query(BillableAcceptance).count() == 0


# --------------------------------------------------------------------------------------------------
# Postgres-backed test — disposable PostgreSQL (real RLS + transactions, not SQLite)
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)
_PG_SCHEMA = "dca_157_test"


@pytest.fixture
def pg_session_factory():
    engine = make_engine(PG_URL)

    @event.listens_for(engine, "connect")
    def _set_search_path(dbapi_conn, _record):  # pragma: no cover - exercised only with a PG URL
        cur = dbapi_conn.cursor()
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{_PG_SCHEMA}"')
        cur.execute(f'SET search_path TO "{_PG_SCHEMA}"')
        cur.close()

    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        with engine.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{_PG_SCHEMA}" CASCADE')
        engine.dispose()


@requires_pg
def test_pg_emit_is_tenant_scoped_and_atomic(pg_session_factory):
    """On real Postgres: emitting A's billable acceptance records one ledger entry + one usage record +
    one atomic audit, while B's independent acknowledged intent for the same subject stays unmetered."""
    assert make_url(PG_URL).get_backend_name() == "postgresql"
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with pg_session_factory() as session:
        a_intent_id = _seed_acknowledged_intent(session, BIZ_A, "shared-subject", now=now)
        _seed_acknowledged_intent(session, BIZ_B, "shared-subject", now=now)

        BillableAcceptanceEmitter(session, BIZ_A).emit(a_intent_id, _admin(), now=now)
        session.commit()

        a_entries = session.query(BillableAcceptance).filter_by(business_id=BIZ_A).count()
        b_entries = session.query(BillableAcceptance).filter_by(business_id=BIZ_B).count()
        usage = _usage_records(session)
        audits = _billable_audits(session)

    assert a_entries == 1
    assert b_entries == 0
    assert usage == 1
    assert audits == 1
