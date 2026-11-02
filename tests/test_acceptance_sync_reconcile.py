"""DCA-156 — reconcile an UNCERTAIN audience synchronization.

Service unit tests (in-memory SQLite) pin
:meth:`~app.services.acceptance_sync_reconcile.UncertainAudienceSyncReconciler.reconcile`: an in-doubt
intent (a DCA-155 sync left ``pending_sync`` / ``sync_status='pending'`` by a lost / timed-out / PENDING
acknowledgment) is reconciled by RE-DRIVING the DCA-154 port under the SAME stable request key. An
ACKNOWLEDGED re-drive resolves the intent ``pending_sync -> synced`` and persists the platform's external
mapping with exactly ONE atomic reconcile audit; an UNCONFIRMED re-drive (still pending / an unavailable
receipt lookup) leaves the intent ``pending_sync`` with NO external ids and marks ``needs_review`` for
operator review — it NEVER implies success. A re-drive is DUPLICATE-safe (the stable request key only ever
maps to the SAME external record); an already-synced intent is an idempotent no-op (no re-drive, no second
audit) returning the SAME refs; ``needs_review`` is not terminal (a later ack still resolves it). A
restricted CONTACT field and a disallowed purpose are REFUSED immediately before the re-drive (no port
call, no state change) — reconciliation never silently promotes a restricted field; a never-synced intent
carries no ambiguity to reconcile; production refuses the contract outright; an unauthorized viewer and an
unknown / cross-tenant id are refused with no state change; and reconciling confers NO recipient consent
and performs NO send.

SQLite is sufficient here — the behaviour is a tenant-scoped row update + one audit insert; the RLS /
migration paths are covered by ``tests/test_rls.py`` / ``tests/test_migrations.py`` (which pin the
``acceptance_intents`` table + its 0070 add-column branch), and a PG-gated test below proves the
reconciliation is tenant-scoped and atomic over real Postgres when ``DCA_TEST_DATABASE_URL`` is set.
Reconciling an uncertain sync drives ONE record and sends nothing, spends nothing and confers no consent.
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
    AudienceSyncContractDisabled,
    AudienceSyncDecision,
    AudienceSyncPurpose,
    AudienceSyncStatus,
    FakeEffortlessPromoAudienceSyncPort,
)
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import (
    AcceptanceIntent,
    AcceptanceIntentStatus,
    AuditEvent,
    ProspectSuggestion,
    SuggestionVisibility,
)
from app.services.acceptance_intent import AcceptanceIntentActor, AcceptanceIntentRecorder
from app.services.acceptance_sync import AcceptanceRecordSynchronizer, AcceptanceSyncActor
from app.services.acceptance_sync_reconcile import (
    AUDIT_ACTION_RECONCILE,
    RECONCILE_NEEDS_REVIEW,
    RECONCILE_RESOLVED,
    REASON_INTENT_NOT_FOUND,
    REASON_NOT_UNCERTAIN,
    AcceptanceSyncReconcileAuthorizationError,
    AcceptanceSyncReconcileError,
    UncertainAudienceSyncReconciler,
)
from tests.conftest import make_settings

BIZ_A = "11111111-1111-1111-1111-111111111156"
BIZ_B = "22222222-2222-2222-2222-222222222256"

ADMIN_ROLES = ("owner",)
VIEWER_ROLES = ("viewer",)

ORG_FIELD = AudienceField(
    key="business_type", field_class=AudienceFieldClass.ORGANIZATION, value_digest="retail"
)
CONTACT_FIELD = AudienceField(
    key="email", field_class=AudienceFieldClass.CONTACT, value_digest="sha256-of-synthetic-email"
)


class _CountingPort:
    """Wraps a port and counts ``sync`` calls — proves a no-op / refusal makes no port call."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls = 0

    def sync(self, request):
        self.calls += 1
        return self._inner.sync(request)


def _accept_actor(subject: str = "00000000-0000-4000-9000-0000000a0156a") -> AcceptanceIntentActor:
    return AcceptanceIntentActor(subject=subject, roles=ADMIN_ROLES)


def _admin(subject: str = "00000000-0000-4000-9000-0000000a0156a") -> AcceptanceSyncActor:
    return AcceptanceSyncActor(subject=subject, roles=ADMIN_ROLES)


def _viewer(subject: str = "00000000-0000-4000-9000-0000000a0156b") -> AcceptanceSyncActor:
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
    session,
    business_id: str,
    subject_id: str,
    *,
    visibility: str = SuggestionVisibility.RESEARCH_ONLY.value,
    now: datetime | None = None,
) -> str:
    """Record a real ``pending_sync`` acceptance intent (the DCA-153 path), returning its id."""
    moment = now or datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    suggestion = ProspectSuggestion(
        business_id=business_id,
        run_id="run-156",
        subject_type="organization",
        subject_id=subject_id,
        visibility=visibility,
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


def _seed_uncertain_intent(
    session,
    business_id: str,
    subject_id: str,
    *,
    now: datetime | None = None,
) -> str:
    """Drive a real DCA-155 sync that does NOT acknowledge, leaving the intent in the UNCERTAIN
    (pending_sync / sync_status='pending') state a lost acknowledgment produces — the exact in-doubt
    state DCA-156 reconciles."""
    moment = now or datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    intent_id = _seed_pending_sync_intent(session, business_id, subject_id, now=moment)
    AcceptanceRecordSynchronizer(
        session,
        business_id,
        make_settings(),
        port=FakeEffortlessPromoAudienceSyncPort(acknowledges=False),
    ).sync(intent_id, _admin(), fields=(ORG_FIELD,), now=moment)
    session.commit()
    # sanity: the intent really is in the in-doubt state before we reconcile it
    intent = session.get(AcceptanceIntent, intent_id)
    assert intent.status == AcceptanceIntentStatus.PENDING_SYNC.value
    assert intent.sync_status == AudienceSyncStatus.PENDING.value
    return intent_id


def _reconcile_audits(session) -> int:
    return session.query(AuditEvent).filter_by(action=AUDIT_ACTION_RECONCILE).count()


# --------------------------------------------------------------------------------------------------
# Acknowledged re-drive resolves the uncertain outcome to synced (with external refs)
# --------------------------------------------------------------------------------------------------


def test_acknowledged_redrive_resolves_to_synced(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        intent_id = _seed_uncertain_intent(session, BIZ_A, "subject-1", now=now)
        port = _CountingPort(FakeEffortlessPromoAudienceSyncPort(acknowledges=True))

        result = UncertainAudienceSyncReconciler(
            session, BIZ_A, make_settings(), port=port
        ).reconcile(intent_id, _admin(), fields=(ORG_FIELD,), now=now)
        session.commit()

        intent = session.get(AcceptanceIntent, intent_id)
        assert result.resolved is True
        assert result.needs_review is False
        assert result.already_resolved is False
        assert result.attempted is True
        assert port.calls == 1
        # The in-doubt outcome is resolved pending_sync -> synced ONLY on an acknowledged re-drive.
        assert intent.status == AcceptanceIntentStatus.SYNCED.value
        assert intent.sync_status == AudienceSyncStatus.ACKNOWLEDGED.value
        assert intent.sync_reconcile_state == RECONCILE_RESOLVED
        assert intent.external_crm_record_id
        assert intent.external_audience_id
        assert intent.synced_at is not None
        assert intent.last_reconciled_at is not None
        assert intent.reconcile_attempt_count == 1
        assert intent.provenance["synced"] is True
        assert intent.provenance["sent"] is False
        # Exactly one atomic reconcile audit, carrying only opaque references (no contact value).
        assert _reconcile_audits(session) == 1
        refs = session.query(AuditEvent).filter_by(action=AUDIT_ACTION_RECONCILE).one().references
        assert refs["acceptanceIntentId"] == intent_id
        assert refs["resolved"] is True
        assert refs["consentConferred"] is False
        assert not any(k.lower() in {"email", "phone", "contact"} for k in refs)


def test_reconcile_confers_no_consent_or_send_side_effect(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        intent_id = _seed_uncertain_intent(session, BIZ_A, "subject-1", now=now)
        result = UncertainAudienceSyncReconciler(
            session,
            BIZ_A,
            make_settings(),
            port=FakeEffortlessPromoAudienceSyncPort(acknowledges=True),
        ).reconcile(intent_id, _admin(), fields=(ORG_FIELD,), now=now)
        session.commit()
        prov = session.get(AcceptanceIntent, intent_id).provenance

    assert result.confers_consent is False
    assert result.external_ref is not None
    assert prov["sent"] is False
    assert prov["spent"] is False
    assert prov["consentConferred"] is False
    assert prov["membershipCreated"] is False


# --------------------------------------------------------------------------------------------------
# Duplicate-safety: a re-drive maps to the SAME external record a direct sync would have produced
# --------------------------------------------------------------------------------------------------


def test_redrive_is_duplicate_safe_under_the_same_request_key(session_factory):
    """The reconcile re-drive uses the intent's stable request key, so the resolved external record is
    DETERMINISTIC in that key — exactly the record a (first, successful) direct DCA-155 sync would have
    minted. A retry can never create a second CRM record / duplicate audience member."""
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        intent_id = _seed_uncertain_intent(session, BIZ_A, "subject-1", now=now)
        intent = session.get(AcceptanceIntent, intent_id)
        request_key = intent.outbox_event_id

        result = UncertainAudienceSyncReconciler(
            session,
            BIZ_A,
            make_settings(),
            port=FakeEffortlessPromoAudienceSyncPort(acknowledges=True),
        ).reconcile(intent_id, _admin(), fields=(ORG_FIELD,), now=now)
        session.commit()

        # The external ids are the SAME deterministic handles keyed on the request key — never a new one.
        assert result.external_ref.crm_record_id == f"synthetic-crm-{request_key}"
        assert session.get(AcceptanceIntent, intent_id).external_crm_record_id == (
            f"synthetic-crm-{request_key}"
        )


def test_idempotent_already_synced_is_a_noop(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        intent_id = _seed_uncertain_intent(session, BIZ_A, "subject-1", now=now)
        port = _CountingPort(FakeEffortlessPromoAudienceSyncPort(acknowledges=True))
        reconciler = UncertainAudienceSyncReconciler(session, BIZ_A, make_settings(), port=port)

        first = reconciler.reconcile(intent_id, _admin(), fields=(ORG_FIELD,), now=now)
        session.commit()
        second = reconciler.reconcile(
            intent_id, _admin(), fields=(ORG_FIELD,), now=now + timedelta(hours=1)
        )
        session.commit()

        assert first.resolved is True and first.already_resolved is False
        # The second reconcile is a terminal no-op: no re-drive, no duplicate sync, no second audit.
        assert second.already_resolved is True
        assert second.resolved is False
        assert second.attempted is False
        assert second.external_ref == first.external_ref
        assert port.calls == 1
        assert _reconcile_audits(session) == 1
        assert session.get(AcceptanceIntent, intent_id).reconcile_attempt_count == 1


# --------------------------------------------------------------------------------------------------
# Unconfirmed re-drive -> operator review (an unavailable status lookup NEVER implies success)
# --------------------------------------------------------------------------------------------------


def test_unconfirmed_redrive_marks_operator_review(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        intent_id = _seed_uncertain_intent(session, BIZ_A, "subject-1", now=now)
        port = _CountingPort(FakeEffortlessPromoAudienceSyncPort(acknowledges=False))

        result = UncertainAudienceSyncReconciler(
            session, BIZ_A, make_settings(), port=port
        ).reconcile(intent_id, _admin(), fields=(ORG_FIELD,), now=now)
        session.commit()

        intent = session.get(AcceptanceIntent, intent_id)
        assert result.resolved is False
        assert result.needs_review is True
        assert result.attempted is True
        assert port.calls == 1
        # The outcome is NOT assumed successful: no accepted status, no external ids.
        assert intent.status == AcceptanceIntentStatus.PENDING_SYNC.value
        assert intent.sync_status == AudienceSyncStatus.PENDING.value
        assert intent.sync_reconcile_state == RECONCILE_NEEDS_REVIEW
        assert intent.external_crm_record_id == ""
        assert intent.synced_at is None
        assert intent.reconcile_attempt_count == 1
        assert intent.provenance["synced"] is False
        assert _reconcile_audits(session) == 1


def test_needs_review_is_not_terminal_and_resolves_on_a_later_ack(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        intent_id = _seed_uncertain_intent(session, BIZ_A, "subject-1", now=now)

        review = UncertainAudienceSyncReconciler(
            session,
            BIZ_A,
            make_settings(),
            port=FakeEffortlessPromoAudienceSyncPort(acknowledges=False),
        ).reconcile(intent_id, _admin(), fields=(ORG_FIELD,), now=now)
        session.commit()
        assert review.needs_review is True

        # A later reconcile, once the host acknowledges, still resolves the (non-terminal) review.
        resolved = UncertainAudienceSyncReconciler(
            session,
            BIZ_A,
            make_settings(),
            port=FakeEffortlessPromoAudienceSyncPort(acknowledges=True),
        ).reconcile(intent_id, _admin(), fields=(ORG_FIELD,), now=now + timedelta(hours=2))
        session.commit()

        intent = session.get(AcceptanceIntent, intent_id)
        assert resolved.resolved is True
        assert intent.status == AcceptanceIntentStatus.SYNCED.value
        assert intent.sync_reconcile_state == RECONCILE_RESOLVED
        assert intent.external_crm_record_id
        assert intent.reconcile_attempt_count == 2


# --------------------------------------------------------------------------------------------------
# Only an in-doubt outcome is reconcilable
# --------------------------------------------------------------------------------------------------


def test_never_synced_intent_is_not_uncertain(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        # A freshly-recorded intent that was NEVER synced carries no ambiguity to reconcile.
        intent_id = _seed_pending_sync_intent(session, BIZ_A, "subject-1", now=now)
        port = _CountingPort(FakeEffortlessPromoAudienceSyncPort(acknowledges=True))

        with pytest.raises(AcceptanceSyncReconcileError) as exc:
            UncertainAudienceSyncReconciler(session, BIZ_A, make_settings(), port=port).reconcile(
                intent_id, _admin(), fields=(ORG_FIELD,), now=now
            )
        session.rollback()

        assert exc.value.code == REASON_NOT_UNCERTAIN
        assert port.calls == 0
        intent = session.get(AcceptanceIntent, intent_id)
        assert intent.sync_status == ""
        assert intent.sync_reconcile_state == ""
        assert _reconcile_audits(session) == 0


# --------------------------------------------------------------------------------------------------
# Revalidation immediately before the re-drive: a restricted field / purpose is never promoted
# --------------------------------------------------------------------------------------------------


def test_restricted_contact_field_refused_not_promoted(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        intent_id = _seed_uncertain_intent(session, BIZ_A, "subject-1", now=now)
        port = _CountingPort(FakeEffortlessPromoAudienceSyncPort(acknowledges=True))

        result = UncertainAudienceSyncReconciler(
            session, BIZ_A, make_settings(), port=port
        ).reconcile(intent_id, _admin(), fields=(ORG_FIELD, CONTACT_FIELD), now=now)
        session.commit()

        intent = session.get(AcceptanceIntent, intent_id)
        assert result.refused is True
        assert result.reason == AudienceSyncDecision.RESTRICTED_FIELD_NOT_PERMITTED
        assert result.external_ref is None
        # No re-drive and no state change — the restricted field was never promoted.
        assert port.calls == 0
        assert intent.status == AcceptanceIntentStatus.PENDING_SYNC.value
        assert intent.sync_status == AudienceSyncStatus.PENDING.value
        assert intent.sync_reconcile_state == ""
        assert intent.external_crm_record_id == ""
        assert _reconcile_audits(session) == 0


def test_marketing_purpose_refused(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        intent_id = _seed_uncertain_intent(session, BIZ_A, "subject-1", now=now)
        port = _CountingPort(FakeEffortlessPromoAudienceSyncPort(acknowledges=True))

        result = UncertainAudienceSyncReconciler(
            session, BIZ_A, make_settings(), port=port
        ).reconcile(
            intent_id, _admin(), fields=(ORG_FIELD,), purpose=AudienceSyncPurpose.MARKETING, now=now
        )
        session.commit()

        assert result.refused is True
        assert result.reason == AudienceSyncDecision.PURPOSE_NOT_PERMITTED
        assert port.calls == 0
        assert _reconcile_audits(session) == 0


# --------------------------------------------------------------------------------------------------
# Disabled in production (the live adapter is unbuilt; the fake is dev/test-only)
# --------------------------------------------------------------------------------------------------


def test_disabled_in_production_refusal(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        intent_id = _seed_uncertain_intent(session, BIZ_A, "subject-1", now=now)
        prod = make_settings(environment="production")
        reconciler = UncertainAudienceSyncReconciler(session, BIZ_A, prod)

        with pytest.raises(AudienceSyncContractDisabled):
            reconciler.reconcile(intent_id, _admin(), fields=(ORG_FIELD,), now=now)
        session.rollback()

        intent = session.get(AcceptanceIntent, intent_id)
        assert intent.status == AcceptanceIntentStatus.PENDING_SYNC.value
        assert intent.sync_reconcile_state == ""
        assert _reconcile_audits(session) == 0


# --------------------------------------------------------------------------------------------------
# Authorization + tenant isolation (denials change nothing)
# --------------------------------------------------------------------------------------------------


def test_unauthorized_viewer_refused_with_no_state_change(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        intent_id = _seed_uncertain_intent(session, BIZ_A, "subject-1", now=now)
        port = _CountingPort(FakeEffortlessPromoAudienceSyncPort(acknowledges=True))

        with pytest.raises(AcceptanceSyncReconcileAuthorizationError):
            UncertainAudienceSyncReconciler(session, BIZ_A, make_settings(), port=port).reconcile(
                intent_id, _viewer(), fields=(ORG_FIELD,), now=now
            )
        session.rollback()

        assert port.calls == 0
        intent = session.get(AcceptanceIntent, intent_id)
        assert intent.status == AcceptanceIntentStatus.PENDING_SYNC.value
        assert intent.sync_reconcile_state == ""
        assert _reconcile_audits(session) == 0


def test_unknown_or_cross_tenant_id_is_not_found(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        b_intent_id = _seed_uncertain_intent(session, BIZ_B, "subject-b", now=now)
        port = _CountingPort(FakeEffortlessPromoAudienceSyncPort(acknowledges=True))
        reconciler = UncertainAudienceSyncReconciler(session, BIZ_A, make_settings(), port=port)

        with pytest.raises(AcceptanceSyncReconcileError) as unknown:
            reconciler.reconcile("no-such-id", _admin(), fields=(ORG_FIELD,), now=now)
        assert unknown.value.code == REASON_INTENT_NOT_FOUND

        # An A-reconciler cannot reach B's intent by id.
        with pytest.raises(AcceptanceSyncReconcileError) as cross:
            reconciler.reconcile(b_intent_id, _admin(), fields=(ORG_FIELD,), now=now)
        assert cross.value.code == REASON_INTENT_NOT_FOUND

        assert port.calls == 0
        # B's intent is wholly untouched (still in its uncertain state, never reconciled).
        b_intent = session.get(AcceptanceIntent, b_intent_id)
        assert b_intent.status == AcceptanceIntentStatus.PENDING_SYNC.value
        assert b_intent.sync_reconcile_state == ""


# --------------------------------------------------------------------------------------------------
# Postgres-backed test — disposable PostgreSQL (real RLS + transactions, not SQLite)
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)
_PG_SCHEMA = "dca_156_test"


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
def test_pg_reconcile_is_tenant_scoped_and_atomic(pg_session_factory):
    """On real Postgres: reconciling A's uncertain intent with an acknowledging re-drive resolves it to
    synced + persists the external mapping + one reconcile audit, atomically, while B's independent
    uncertain intent for the same subject stays pending_sync (unreconciled)."""
    assert make_url(PG_URL).get_backend_name() == "postgresql"
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with pg_session_factory() as session:
        a_intent_id = _seed_uncertain_intent(session, BIZ_A, "shared-subject", now=now)
        b_intent_id = _seed_uncertain_intent(session, BIZ_B, "shared-subject", now=now)

        UncertainAudienceSyncReconciler(
            session,
            BIZ_A,
            make_settings(),
            port=FakeEffortlessPromoAudienceSyncPort(acknowledges=True),
        ).reconcile(a_intent_id, _admin(), fields=(ORG_FIELD,), now=now)
        session.commit()

        a_intent = session.get(AcceptanceIntent, a_intent_id)
        b_intent = session.get(AcceptanceIntent, b_intent_id)
        audits = _reconcile_audits(session)

    assert a_intent.status == AcceptanceIntentStatus.SYNCED.value
    assert a_intent.sync_reconcile_state == RECONCILE_RESOLVED
    assert a_intent.external_crm_record_id
    assert b_intent.status == AcceptanceIntentStatus.PENDING_SYNC.value
    assert b_intent.sync_reconcile_state == ""
    assert b_intent.external_crm_record_id == ""
    assert audits == 1
