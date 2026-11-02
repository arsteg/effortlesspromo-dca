"""DCA-155 — synchronize ONE accepted record THROUGH EffortlessPromo.

Service unit tests (in-memory SQLite) pin
:meth:`~app.services.acceptance_sync.AcceptanceRecordSynchronizer.sync`: an ACKNOWLEDGED ack advances the
intent ``pending_sync -> synced`` and PERSISTS the platform's external record mapping with exactly ONE
atomic audit entry; a PENDING hand-off (a lost / delayed acknowledgment) leaves the intent ``pending_sync``
(pending reconciliation) with NO external ids and NO accepted status; a replay is idempotent on the
request key (no second port call, no duplicate sync, no second audit) and returns the SAME external refs;
a restricted CONTACT field, a sendable sync and a marketing purpose are each REFUSED immediately before
sync (no port call, no state change) — the sync never silently promotes a restricted field; production
refuses the contract outright (the fake is dev/test-only); an unauthorized viewer and an unknown /
cross-tenant id are refused with no state change; and a sync confers NO recipient consent and performs
NO send.

SQLite is sufficient here — the behaviour is a tenant-scoped row update + one audit insert; the RLS /
migration paths are covered by ``tests/test_rls.py`` / ``tests/test_migrations.py`` (which pin the
``acceptance_intents`` table + its 0069 add-column branch), and a PG-gated test below proves the sync is
tenant-scoped and atomic over real Postgres when ``DCA_TEST_DATABASE_URL`` is set. Synchronizing an
accepted record syncs ONE record and sends nothing, spends nothing and confers no consent.
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
from app.services.acceptance_sync import (
    AUDIT_ACTION_SYNC,
    REASON_INTENT_NOT_FOUND,
    AcceptanceRecordSynchronizer,
    AcceptanceSyncActor,
    AcceptanceSyncAuthorizationError,
    AcceptanceSyncError,
)
from tests.conftest import make_settings

BIZ_A = "11111111-1111-1111-1111-111111111155"
BIZ_B = "22222222-2222-2222-2222-222222222255"

ADMIN_ROLES = ("owner",)
VIEWER_ROLES = ("viewer",)

ORG_FIELD = AudienceField(
    key="business_type", field_class=AudienceFieldClass.ORGANIZATION, value_digest="retail"
)
CONTACT_FIELD = AudienceField(
    key="email", field_class=AudienceFieldClass.CONTACT, value_digest="sha256-of-synthetic-email"
)


class _CountingPort:
    """Wraps a port and counts ``sync`` calls — proves an idempotent replay makes no second call."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls = 0

    def sync(self, request):
        self.calls += 1
        return self._inner.sync(request)


def _accept_actor(subject: str = "00000000-0000-4000-9000-0000000a0155a") -> AcceptanceIntentActor:
    return AcceptanceIntentActor(subject=subject, roles=ADMIN_ROLES)


def _admin(subject: str = "00000000-0000-4000-9000-0000000a0155a") -> AcceptanceSyncActor:
    return AcceptanceSyncActor(subject=subject, roles=ADMIN_ROLES)


def _viewer(subject: str = "00000000-0000-4000-9000-0000000a0155b") -> AcceptanceSyncActor:
    return AcceptanceSyncActor(subject=subject, roles=VIEWER_ROLES)


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def _seed_intent(
    session,
    business_id: str,
    subject_id: str,
    *,
    visibility: str = SuggestionVisibility.RESEARCH_ONLY.value,
    now: datetime | None = None,
) -> str:
    """Create a reviewable suggestion and record a real ``pending_sync`` acceptance intent over it (the
    DCA-153 path), returning the intent id. The intent carries a stable outbox request key."""
    moment = now or datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    suggestion = ProspectSuggestion(
        business_id=business_id,
        run_id="run-155",
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


def _sync_audits(session) -> int:
    return session.query(AuditEvent).filter_by(action=AUDIT_ACTION_SYNC).count()


# --------------------------------------------------------------------------------------------------
# Success: acknowledged advances status + records external refs
# --------------------------------------------------------------------------------------------------


def test_acknowledged_sync_advances_status_and_records_external_refs(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        intent_id = _seed_intent(session, BIZ_A, "subject-1", now=now)
        port = FakeEffortlessPromoAudienceSyncPort(acknowledges=True)

        result = AcceptanceRecordSynchronizer(session, BIZ_A, make_settings(), port=port).sync(
            intent_id, _admin(), fields=(ORG_FIELD,), now=now
        )
        session.commit()

        intent = session.get(AcceptanceIntent, intent_id)
        assert result.acknowledged is True
        assert result.already_synced is False
        # Status advanced pending_sync -> synced ONLY on acknowledgment.
        assert intent.status == AcceptanceIntentStatus.SYNCED.value
        assert intent.sync_status == AudienceSyncStatus.ACKNOWLEDGED.value
        # The platform's external record mapping is persisted.
        assert intent.external_crm_record_id
        assert intent.external_audience_id
        assert intent.synced_at is not None
        assert intent.last_sync_attempt_at is not None
        # Only the 'synced' invariant flipped; it is still never a send / spend / consent.
        prov = intent.provenance
        assert prov["synced"] is True
        assert prov["sent"] is False
        assert prov["consentConferred"] is False
        assert prov["membershipCreated"] is False
        # Exactly one atomic audit entry, carrying only opaque references (no contact value).
        assert _sync_audits(session) == 1
        refs = session.query(AuditEvent).filter_by(action=AUDIT_ACTION_SYNC).one().references
        assert refs["acceptanceIntentId"] == intent_id
        assert refs["acknowledged"] is True
        assert refs["consentConferred"] is False
        assert not any(k.lower() in {"email", "phone", "contact"} for k in refs)


def test_sync_confers_no_consent_or_send_side_effect(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        intent_id = _seed_intent(session, BIZ_A, "subject-1", now=now)
        result = AcceptanceRecordSynchronizer(
            session,
            BIZ_A,
            make_settings(),
            port=FakeEffortlessPromoAudienceSyncPort(acknowledges=True),
        ).sync(intent_id, _admin(), fields=(ORG_FIELD,), now=now)
        session.commit()
        prov = session.get(AcceptanceIntent, intent_id).provenance

    # A sync is never recipient consent and never a send — the invariants hold on the result and the row.
    assert result.confers_consent is False
    assert result.external_ref is not None
    assert prov["sent"] is False
    assert prov["spent"] is False
    assert prov["consentConferred"] is False
    assert prov["membershipCreated"] is False


# --------------------------------------------------------------------------------------------------
# Timeout / lost acknowledgment: remains pending reconciliation (no accepted status)
# --------------------------------------------------------------------------------------------------


def test_pending_handoff_remains_pending_reconciliation(session_factory):
    """A PENDING hand-off (a lost / delayed acknowledgment) leaves the intent pending_sync: no accepted
    status, no external ids, only a pending-reconciliation marker."""
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        intent_id = _seed_intent(session, BIZ_A, "subject-1", now=now)
        port = FakeEffortlessPromoAudienceSyncPort(acknowledges=False)

        result = AcceptanceRecordSynchronizer(session, BIZ_A, make_settings(), port=port).sync(
            intent_id, _admin(), fields=(ORG_FIELD,), now=now
        )
        session.commit()

        intent = session.get(AcceptanceIntent, intent_id)
        assert result.accepted is True
        assert result.acknowledged is False
        # The status never advances on anything short of an acknowledgment.
        assert intent.status == AcceptanceIntentStatus.PENDING_SYNC.value
        assert intent.sync_status == AudienceSyncStatus.PENDING.value
        assert intent.external_crm_record_id == ""
        assert intent.external_audience_id == ""
        assert intent.synced_at is None
        assert intent.last_sync_attempt_at is not None  # a pending attempt IS recorded
        assert intent.provenance["synced"] is False


# --------------------------------------------------------------------------------------------------
# Idempotent replay on the request key (no duplicate sync)
# --------------------------------------------------------------------------------------------------


def test_idempotent_replay_makes_no_second_sync(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        intent_id = _seed_intent(session, BIZ_A, "subject-1", now=now)
        port = _CountingPort(FakeEffortlessPromoAudienceSyncPort(acknowledges=True))
        synchronizer = AcceptanceRecordSynchronizer(session, BIZ_A, make_settings(), port=port)

        first = synchronizer.sync(intent_id, _admin(), fields=(ORG_FIELD,), now=now)
        session.commit()
        second = synchronizer.sync(
            intent_id, _admin(), fields=(ORG_FIELD,), now=now + timedelta(hours=1)
        )
        session.commit()

        assert first.already_synced is False
        assert second.already_synced is True
        # Same external mapping, no second port call, no duplicate audit.
        assert second.external_ref == first.external_ref
        assert port.calls == 1
        assert _sync_audits(session) == 1
        assert (
            session.get(AcceptanceIntent, intent_id).status == AcceptanceIntentStatus.SYNCED.value
        )


# --------------------------------------------------------------------------------------------------
# Revalidation immediately before sync: a restricted field is never promoted
# --------------------------------------------------------------------------------------------------


def test_restricted_contact_field_refused_not_promoted(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        intent_id = _seed_intent(session, BIZ_A, "subject-1", now=now)
        port = _CountingPort(FakeEffortlessPromoAudienceSyncPort(acknowledges=True))

        result = AcceptanceRecordSynchronizer(session, BIZ_A, make_settings(), port=port).sync(
            intent_id, _admin(), fields=(ORG_FIELD, CONTACT_FIELD), now=now
        )
        session.commit()

        intent = session.get(AcceptanceIntent, intent_id)
        assert result.accepted is False
        assert result.reason == AudienceSyncDecision.RESTRICTED_FIELD_NOT_PERMITTED
        assert result.external_ref is None
        # No port call and no state change — the restricted field was never promoted.
        assert port.calls == 0
        assert intent.status == AcceptanceIntentStatus.PENDING_SYNC.value
        assert intent.sync_status == ""
        assert intent.external_crm_record_id == ""
        assert _sync_audits(session) == 0


def test_sendable_sync_disabled(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        intent_id = _seed_intent(
            session, BIZ_A, "subject-1", visibility=SuggestionVisibility.SENDABLE.value, now=now
        )
        port = _CountingPort(FakeEffortlessPromoAudienceSyncPort(acknowledges=True))

        result = AcceptanceRecordSynchronizer(session, BIZ_A, make_settings(), port=port).sync(
            intent_id, _admin(), fields=(ORG_FIELD,), now=now
        )
        session.commit()

        assert result.accepted is False
        assert result.reason == AudienceSyncDecision.SENDABLE_SYNC_DISABLED
        assert port.calls == 0
        assert (
            session.get(AcceptanceIntent, intent_id).status
            == AcceptanceIntentStatus.PENDING_SYNC.value
        )
        assert _sync_audits(session) == 0


def test_marketing_purpose_disabled(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        intent_id = _seed_intent(session, BIZ_A, "subject-1", now=now)
        port = _CountingPort(FakeEffortlessPromoAudienceSyncPort(acknowledges=True))

        result = AcceptanceRecordSynchronizer(session, BIZ_A, make_settings(), port=port).sync(
            intent_id, _admin(), fields=(ORG_FIELD,), purpose=AudienceSyncPurpose.MARKETING, now=now
        )
        session.commit()

        assert result.accepted is False
        assert result.reason == AudienceSyncDecision.PURPOSE_NOT_PERMITTED
        assert port.calls == 0
        assert _sync_audits(session) == 0


# --------------------------------------------------------------------------------------------------
# Disabled in production (the live adapter is unbuilt; the fake is dev/test-only)
# --------------------------------------------------------------------------------------------------


def test_disabled_in_production_refusal(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        intent_id = _seed_intent(session, BIZ_A, "subject-1", now=now)
        prod = make_settings(environment="production")
        # No injected port: production resolves through the build factory, which refuses the fake.
        synchronizer = AcceptanceRecordSynchronizer(session, BIZ_A, prod)

        with pytest.raises(AudienceSyncContractDisabled):
            synchronizer.sync(intent_id, _admin(), fields=(ORG_FIELD,), now=now)
        session.rollback()

        intent = session.get(AcceptanceIntent, intent_id)
        assert intent.status == AcceptanceIntentStatus.PENDING_SYNC.value
        assert intent.sync_status == ""
        assert _sync_audits(session) == 0


# --------------------------------------------------------------------------------------------------
# Authorization + tenant isolation (denials change nothing)
# --------------------------------------------------------------------------------------------------


def test_unauthorized_viewer_refused_with_no_state_change(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        intent_id = _seed_intent(session, BIZ_A, "subject-1", now=now)
        port = _CountingPort(FakeEffortlessPromoAudienceSyncPort(acknowledges=True))

        with pytest.raises(AcceptanceSyncAuthorizationError):
            AcceptanceRecordSynchronizer(session, BIZ_A, make_settings(), port=port).sync(
                intent_id, _viewer(), fields=(ORG_FIELD,), now=now
            )
        session.rollback()

        assert port.calls == 0
        assert (
            session.get(AcceptanceIntent, intent_id).status
            == AcceptanceIntentStatus.PENDING_SYNC.value
        )
        assert _sync_audits(session) == 0


def test_unknown_or_cross_tenant_id_is_not_found(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        b_intent_id = _seed_intent(session, BIZ_B, "subject-b", now=now)
        port = _CountingPort(FakeEffortlessPromoAudienceSyncPort(acknowledges=True))
        synchronizer = AcceptanceRecordSynchronizer(session, BIZ_A, make_settings(), port=port)

        with pytest.raises(AcceptanceSyncError) as unknown:
            synchronizer.sync("no-such-id", _admin(), fields=(ORG_FIELD,), now=now)
        assert unknown.value.code == REASON_INTENT_NOT_FOUND

        # An A-synchronizer cannot reach B's intent by id.
        with pytest.raises(AcceptanceSyncError) as cross:
            synchronizer.sync(b_intent_id, _admin(), fields=(ORG_FIELD,), now=now)
        assert cross.value.code == REASON_INTENT_NOT_FOUND

        assert port.calls == 0
        # B's intent is wholly untouched.
        assert (
            session.get(AcceptanceIntent, b_intent_id).status
            == AcceptanceIntentStatus.PENDING_SYNC.value
        )


# --------------------------------------------------------------------------------------------------
# Postgres-backed test — disposable PostgreSQL (real RLS + transactions, not SQLite)
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)
_PG_SCHEMA = "dca_155_test"


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
def test_pg_acknowledged_sync_is_tenant_scoped_and_atomic(pg_session_factory):
    """On real Postgres: acknowledging A's intent advances it to synced + persists the external mapping +
    one audit, atomically, while B's independent intent for the same subject stays pending_sync."""
    assert make_url(PG_URL).get_backend_name() == "postgresql"
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with pg_session_factory() as session:
        a_intent_id = _seed_intent(session, BIZ_A, "shared-subject", now=now)
        b_intent_id = _seed_intent(session, BIZ_B, "shared-subject", now=now)

        AcceptanceRecordSynchronizer(
            session,
            BIZ_A,
            make_settings(),
            port=FakeEffortlessPromoAudienceSyncPort(acknowledges=True),
        ).sync(a_intent_id, _admin(), fields=(ORG_FIELD,), now=now)
        session.commit()

        a_intent = session.get(AcceptanceIntent, a_intent_id)
        b_intent = session.get(AcceptanceIntent, b_intent_id)
        audits = _sync_audits(session)

    assert a_intent.status == AcceptanceIntentStatus.SYNCED.value
    assert a_intent.external_crm_record_id
    assert b_intent.status == AcceptanceIntentStatus.PENDING_SYNC.value
    assert b_intent.external_crm_record_id == ""
    assert audits == 1
