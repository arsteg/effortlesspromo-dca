"""DCA-158 — deliver a recorded BILLABLE acceptance to EffortlessPromo platform billing.

Service unit tests (in-memory SQLite) pin
:meth:`~app.services.usage_delivery.UsageBillingDeliverer.deliver_entry`: a DCA-157 ``recorded``
:class:`~app.db.models.BillableAcceptance` is delivered to the (fake) platform billing capability and
advanced ``recorded → delivered`` ONLY on an ACKNOWLEDGED receipt (a host usage-record handle present),
which also settles its DCA-027 usage record ``pending → delivered`` and stages exactly ONE atomic audit.
Delivery is idempotent: a replay (or a second drain pass) short-circuits on the already-delivered entry —
no second port call / meter / audit — so repeated delivery never double-bills. A PENDING hand-off (a lost /
timed-out response) and a DISABLED refusal (a meter with no configured price) each leave the entry
``recorded`` with no handle, no price and NO charge invented — an unacknowledged delivery is never treated
as success and a missing configured price invents no charge. The usage record's own ``provider_cost_minor``
(the DCA's cost basis) stays DISTINCT from the platform-minted ``customer_price_minor`` on the receipt; the
DCA charges nothing (``charged_by_dca`` is always ``False``). Production refuses the fake. An unauthorized
viewer and an unknown / cross-tenant id are refused with no state change.

SQLite is sufficient here — the behaviour is a tenant-scoped row update + one audit insert + one outbox
update; the RLS / migration paths are covered by ``tests/test_rls.py`` / ``tests/test_migrations.py`` (the
``billable_acceptances`` table + its 0072 delivery-column branch), and a PG-gated test below proves the
delivery is tenant-scoped and atomic over real Postgres when ``DCA_TEST_DATABASE_URL`` is set.
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
    FakeEffortlessPromoAudienceSyncPort,
)
from app.billing import (
    BillingUsageContractDisabled,
    FakeEffortlessPromoBillingPort,
    UsageDeliveryStatus,
    UsageMeter,
    authorize_usage_delivery,
    usage_record_from_entry,
)
from app.billing.fake import DEFAULT_CUSTOMER_PRICE_MINOR
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import (
    AuditEvent,
    BillableAcceptance,
    BillableAcceptanceStatus,
    OutboxMessage,
    OutboxStatus,
    ProspectSuggestion,
    SuggestionVisibility,
)
from app.services.acceptance_intent import AcceptanceIntentActor, AcceptanceIntentRecorder
from app.services.acceptance_sync import AcceptanceRecordSynchronizer, AcceptanceSyncActor
from app.services.billable_acceptance import OUTBOX_EVENT_BILLABLE, BillableAcceptanceEmitter
from app.services.usage_delivery import (
    AUDIT_ACTION_DELIVERY,
    REASON_ENTRY_NOT_FOUND,
    UsageBillingDeliverer,
    UsageDeliveryActor,
    UsageDeliveryAuthorizationError,
    UsageDeliveryError,
)
from tests.conftest import make_settings

BIZ_A = "11111111-1111-1111-1111-111111111158"
BIZ_B = "22222222-2222-2222-2222-222222222258"

ADMIN_ROLES = ("owner",)
VIEWER_ROLES = ("viewer",)

ORG_FIELD = AudienceField(
    key="business_type", field_class=AudienceFieldClass.ORGANIZATION, value_digest="retail"
)
PROVIDER_COST_MINOR = 1200  # the DCA's own cost basis (paise) — NOT the customer charge


def _accept_actor(subject: str = "00000000-0000-4000-9000-0000000a0158a") -> AcceptanceIntentActor:
    return AcceptanceIntentActor(subject=subject, roles=ADMIN_ROLES)


def _sync_admin(subject: str = "00000000-0000-4000-9000-0000000a0158a") -> AcceptanceSyncActor:
    return AcceptanceSyncActor(subject=subject, roles=ADMIN_ROLES)


def _admin(subject: str = "00000000-0000-4000-9000-0000000a0158a") -> UsageDeliveryActor:
    return UsageDeliveryActor(subject=subject, roles=ADMIN_ROLES)


def _viewer(subject: str = "00000000-0000-4000-9000-0000000a0158b") -> UsageDeliveryActor:
    return UsageDeliveryActor(subject=subject, roles=VIEWER_ROLES)


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def _seed_recorded_entry(
    session, business_id: str, subject_id: str, *, now: datetime | None = None
) -> str:
    """Record a real DCA-157 ``recorded`` billable acceptance for an ACKNOWLEDGED audience sync."""
    moment = now or datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    suggestion = ProspectSuggestion(
        business_id=business_id,
        run_id="run-158",
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
    intent_id = (
        AcceptanceIntentRecorder(session, business_id)
        .accept(suggestion.id, _accept_actor(), now=moment)
        .intent.id
    )
    session.commit()
    AcceptanceRecordSynchronizer(
        session,
        business_id,
        make_settings(),
        port=FakeEffortlessPromoAudienceSyncPort(acknowledges=True),
    ).sync(intent_id, _sync_admin(), fields=(ORG_FIELD,), now=moment)
    session.commit()
    entry = BillableAcceptanceEmitter(session, business_id).emit(
        intent_id, _sync_admin(), now=moment
    )
    session.commit()
    return entry.entry.id


def _delivery_audits(session) -> int:
    return session.query(AuditEvent).filter_by(action=AUDIT_ACTION_DELIVERY).count()


def _usage_record(session):
    return session.query(OutboxMessage).filter_by(event_type=OUTBOX_EVENT_BILLABLE).one()


# --------------------------------------------------------------------------------------------------
# Delivered once -> advanced to delivered (happy path)
# --------------------------------------------------------------------------------------------------


def test_delivers_recorded_entry_once_and_advances_to_delivered(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        entry_id = _seed_recorded_entry(session, BIZ_A, "subject-1", now=now)
        port = FakeEffortlessPromoBillingPort(acknowledges=True, now=now)

        result = UsageBillingDeliverer(session, BIZ_A, make_settings(), port=port).deliver_entry(
            entry_id, _admin(), provider_cost_minor=PROVIDER_COST_MINOR, now=now
        )
        session.commit()

        entry = session.get(BillableAcceptance, entry_id)
        assert result.delivered is True
        assert result.already_delivered is False
        assert result.acknowledged is True
        assert entry.status == BillableAcceptanceStatus.DELIVERED.value
        assert entry.delivered_at == now
        assert entry.delivery_receipt_id  # the platform's opaque usage-record handle is persisted
        assert entry.delivery_attempt_count == 1
        # The DCA-027 usage record's lifecycle settled pending -> delivered in the same unit of work.
        assert _usage_record(session).status == OutboxStatus.DELIVERED.value
        # Exactly one atomic audit for the delivery.
        assert _delivery_audits(session) == 1
        # Provider cost (the DCA's basis) and the customer price (platform-minted) are DISTINCT; the DCA
        # never set the customer price and charges nothing.
        assert result.provider_cost_minor == PROVIDER_COST_MINOR
        assert result.customer_price_minor == DEFAULT_CUSTOMER_PRICE_MINOR
        assert result.customer_price_minor != result.provider_cost_minor
        assert result.charged_by_dca is False
        assert result.confers_consent is False
        assert entry.provenance["delivered"] is True
        assert entry.provenance["chargedByDca"] is False
        assert entry.provenance["providerCostMinor"] == PROVIDER_COST_MINOR
        assert entry.provenance["customerPriceMinor"] == DEFAULT_CUSTOMER_PRICE_MINOR
        # Audit carries the two money values separately and no contact value.
        refs = session.query(AuditEvent).filter_by(action=AUDIT_ACTION_DELIVERY).one().references
        assert refs["providerCostMinor"] == PROVIDER_COST_MINOR
        assert refs["customerPriceMinor"] == DEFAULT_CUSTOMER_PRICE_MINOR
        assert refs["chargedByDca"] is False
        assert not any(k.lower() in {"email", "phone", "contact"} for k in refs)


def test_replay_is_idempotent_no_double_deliver(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        entry_id = _seed_recorded_entry(session, BIZ_A, "subject-1", now=now)
        deliverer = UsageBillingDeliverer(
            session, BIZ_A, make_settings(), port=FakeEffortlessPromoBillingPort(acknowledges=True)
        )

        first = deliverer.deliver_entry(entry_id, _admin(), now=now)
        session.commit()
        second = deliverer.deliver_entry(entry_id, _admin(), now=now + timedelta(hours=1))
        session.commit()

        assert first.delivered is True and first.already_delivered is False
        # The replay is an idempotent no-op: no second port call / meter / audit, same handle, one attempt.
        assert second.already_delivered is True
        assert second.delivered is False
        assert second.receipt is None  # no port was called on the replay
        entry = session.get(BillableAcceptance, entry_id)
        assert entry.status == BillableAcceptanceStatus.DELIVERED.value
        assert entry.delivery_attempt_count == 1  # the replay did not re-attempt
        assert _delivery_audits(session) == 1


def test_drain_delivers_all_recorded_then_is_idempotent(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        _seed_recorded_entry(session, BIZ_A, "subject-1", now=now)
        _seed_recorded_entry(session, BIZ_A, "subject-2", now=now)
        deliverer = UsageBillingDeliverer(
            session, BIZ_A, make_settings(), port=FakeEffortlessPromoBillingPort(acknowledges=True)
        )

        first = deliverer.deliver_recorded(_admin(), now=now)
        session.commit()
        assert len(first) == 2
        assert all(r.delivered for r in first)
        assert (
            session.query(BillableAcceptance)
            .filter_by(status=BillableAcceptanceStatus.DELIVERED.value)
            .count()
            == 2
        )

        # A second drain finds no recorded rows left -> delivers nothing (never double-bills).
        second = deliverer.deliver_recorded(_admin(), now=now + timedelta(hours=1))
        session.commit()
        assert second == []
        assert _delivery_audits(session) == 2


# --------------------------------------------------------------------------------------------------
# Unacknowledged delivery is never success; a missing price invents no charge
# --------------------------------------------------------------------------------------------------


def test_pending_delivery_not_marked_success(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        entry_id = _seed_recorded_entry(session, BIZ_A, "subject-1", now=now)
        # acknowledges=False -> a PENDING hand-off (a lost / timed-out response).
        port = FakeEffortlessPromoBillingPort(acknowledges=False, now=now)

        result = UsageBillingDeliverer(session, BIZ_A, make_settings(), port=port).deliver_entry(
            entry_id, _admin(), now=now
        )
        session.commit()

        entry = session.get(BillableAcceptance, entry_id)
        assert result.delivered is False
        assert result.acknowledged is False
        assert result.accepted is True  # taken into the queue, but NOT durably metered
        assert result.receipt.status is UsageDeliveryStatus.PENDING
        # The entry stays recorded: no handle, no customer price, nothing marked delivered.
        assert entry.status == BillableAcceptanceStatus.RECORDED.value
        assert entry.delivered_at is None
        assert entry.delivery_receipt_id == ""
        assert result.customer_price_minor is None
        # The DCA-027 usage record stays pending (not settled on a non-acknowledgment).
        assert _usage_record(session).status == OutboxStatus.PENDING.value


def test_missing_configured_price_invents_no_charge(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        entry_id = _seed_recorded_entry(session, BIZ_A, "subject-1", now=now)
        # priced_meters=() -> the meter has no configured price -> DISABLED (no charge invented).
        port = FakeEffortlessPromoBillingPort(acknowledges=True, priced_meters=(), now=now)

        result = UsageBillingDeliverer(session, BIZ_A, make_settings(), port=port).deliver_entry(
            entry_id, _admin(), now=now
        )
        session.commit()

        entry = session.get(BillableAcceptance, entry_id)
        assert result.delivered is False
        assert result.accepted is False
        assert result.receipt.status is UsageDeliveryStatus.DISABLED
        assert result.reason == "billing_meter_not_priced"
        # No charge invented, nothing delivered: the entry stays recorded with no handle / price.
        assert entry.status == BillableAcceptanceStatus.RECORDED.value
        assert entry.delivery_receipt_id == ""
        assert result.customer_price_minor is None
        assert _usage_record(session).status == OutboxStatus.PENDING.value


def test_unavailable_host_leaves_entry_recorded(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        entry_id = _seed_recorded_entry(session, BIZ_A, "subject-1", now=now)
        port = FakeEffortlessPromoBillingPort(available=False, now=now)

        result = UsageBillingDeliverer(session, BIZ_A, make_settings(), port=port).deliver_entry(
            entry_id, _admin(), now=now
        )
        session.commit()

        assert result.delivered is False
        assert result.reason == "billing_host_unavailable"
        assert (
            session.get(BillableAcceptance, entry_id).status
            == BillableAcceptanceStatus.RECORDED.value
        )


# --------------------------------------------------------------------------------------------------
# Production refuses the fake
# --------------------------------------------------------------------------------------------------


def test_production_refuses_the_fake(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    prod = make_settings(environment="production")
    with session_factory() as session:
        entry_id = _seed_recorded_entry(session, BIZ_A, "subject-1", now=now)

        # Even with a fake injected, production resolution raises rather than hand back a fake.
        with pytest.raises(BillingUsageContractDisabled):
            UsageBillingDeliverer(
                session, BIZ_A, prod, port=FakeEffortlessPromoBillingPort(acknowledges=True)
            ).deliver_entry(entry_id, _admin(), now=now)
        session.rollback()

        assert (
            session.get(BillableAcceptance, entry_id).status
            == BillableAcceptanceStatus.RECORDED.value
        )


# --------------------------------------------------------------------------------------------------
# Authorization + tenant isolation (denials change nothing)
# --------------------------------------------------------------------------------------------------


def test_unauthorized_viewer_refused_with_no_state_change(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        entry_id = _seed_recorded_entry(session, BIZ_A, "subject-1", now=now)

        with pytest.raises(UsageDeliveryAuthorizationError):
            UsageBillingDeliverer(
                session, BIZ_A, make_settings(), port=FakeEffortlessPromoBillingPort()
            ).deliver_entry(entry_id, _viewer(), now=now)
        session.rollback()

        entry = session.get(BillableAcceptance, entry_id)
        assert entry.status == BillableAcceptanceStatus.RECORDED.value
        assert entry.delivery_attempt_count == 0
        assert _delivery_audits(session) == 0


def test_unknown_or_cross_tenant_id_is_not_found(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        b_entry_id = _seed_recorded_entry(session, BIZ_B, "subject-b", now=now)
        deliverer = UsageBillingDeliverer(
            session, BIZ_A, make_settings(), port=FakeEffortlessPromoBillingPort(acknowledges=True)
        )

        with pytest.raises(UsageDeliveryError) as unknown:
            deliverer.deliver_entry("no-such-id", _admin(), now=now)
        assert unknown.value.code == REASON_ENTRY_NOT_FOUND

        # An A-deliverer cannot reach B's entry by id.
        with pytest.raises(UsageDeliveryError) as cross:
            deliverer.deliver_entry(b_entry_id, _admin(), now=now)
        assert cross.value.code == REASON_ENTRY_NOT_FOUND
        session.rollback()

        # B's entry was never delivered via the A-deliverer.
        assert (
            session.get(BillableAcceptance, b_entry_id).status
            == BillableAcceptanceStatus.RECORDED.value
        )


# --------------------------------------------------------------------------------------------------
# Pure contract tests (no DB) — the fail-closed gate + the receipt invariants
# --------------------------------------------------------------------------------------------------


def _record():
    return usage_record_from_entry(
        BillableAcceptance(
            business_id=BIZ_A,
            acceptance_intent_id="intent-1",
            suggestion_id="sug-1",
            subject_type="organization",
            subject_id="subj-1",
            billing_key="billing-1",
            external_crm_record_id="crm-1",
            provenance={},
        ),
        meter=UsageMeter.ACCEPTED_PROSPECT,
    )


def test_authorize_denies_unpriced_meter_and_allows_priced():
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    rec = _record()
    priced = FakeEffortlessPromoBillingPort(now=now).capabilities(BIZ_A)
    unpriced = FakeEffortlessPromoBillingPort(priced_meters=(), now=now).capabilities(BIZ_A)

    assert authorize_usage_delivery(priced, rec, now=now).allowed is True
    denied = authorize_usage_delivery(unpriced, rec, now=now)
    assert denied.allowed is False
    assert denied.reason == "billing_meter_not_priced"
    # Absent capabilities and a stale answer both fail closed.
    assert authorize_usage_delivery(None, rec, now=now).reason == "billing_host_unavailable"
    stale = authorize_usage_delivery(priced, rec, now=now + timedelta(hours=2))
    assert stale.reason == "billing_capabilities_stale"


def test_receipt_rejects_price_or_handle_without_acknowledgment():
    from app.billing import UsageReceipt

    # A PENDING / DISABLED receipt may never carry a host handle or a customer price.
    with pytest.raises(ValueError):
        UsageReceipt(
            accepted=True,
            status=UsageDeliveryStatus.PENDING,
            reason="ok",
            request_key="k",
            host_reference="leaked-handle",
        )
    with pytest.raises(ValueError):
        UsageReceipt(
            accepted=True,
            status=UsageDeliveryStatus.PENDING,
            reason="ok",
            request_key="k",
            customer_price_minor=100,
        )
    # An ACKNOWLEDGED receipt MUST carry both.
    with pytest.raises(ValueError):
        UsageReceipt(
            accepted=True,
            status=UsageDeliveryStatus.ACKNOWLEDGED,
            reason="ok",
            request_key="k",
        )


# --------------------------------------------------------------------------------------------------
# Postgres-backed test — disposable PostgreSQL (real RLS + transactions, not SQLite)
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)
_PG_SCHEMA = "dca_158_test"


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
def test_pg_delivery_is_tenant_scoped_and_atomic(pg_session_factory):
    """On real Postgres: delivering A's recorded entry advances it to delivered + settles its usage record
    + stages one atomic audit, while B's independent recorded entry stays undelivered (recorded)."""
    assert make_url(PG_URL).get_backend_name() == "postgresql"
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with pg_session_factory() as session:
        a_entry_id = _seed_recorded_entry(session, BIZ_A, "shared-subject", now=now)
        _seed_recorded_entry(session, BIZ_B, "shared-subject", now=now)

        UsageBillingDeliverer(
            session, BIZ_A, make_settings(), port=FakeEffortlessPromoBillingPort(acknowledges=True)
        ).deliver_entry(a_entry_id, _admin(), provider_cost_minor=PROVIDER_COST_MINOR, now=now)
        session.commit()

        a_delivered = (
            session.query(BillableAcceptance)
            .filter_by(business_id=BIZ_A, status=BillableAcceptanceStatus.DELIVERED.value)
            .count()
        )
        b_recorded = (
            session.query(BillableAcceptance)
            .filter_by(business_id=BIZ_B, status=BillableAcceptanceStatus.RECORDED.value)
            .count()
        )
        audits = _delivery_audits(session)

    assert a_delivered == 1
    assert b_recorded == 1
    assert audits == 1
