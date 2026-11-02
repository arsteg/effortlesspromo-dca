"""DCA-154 — the audience-sync contract + the synthetic EffortlessPromo CRM fake.

Sync an accepted prospect ONLY through EffortlessPromo: a typed request (a stable request key + minimized
class-tagged fields), a one-method port (``sync`` — no create/promote path), a fail-closed decision, an
external-id / acknowledgment result, and a dev/test-only fake refused in production. The load-bearing
behaviour: the contract can NEVER silently promote a restricted CONTACT field under a research-only
acceptance, and a PENDING hand-off differs from an ACKNOWLEDGED write (external ids present only on the
latter).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.audience import (
    AudienceField,
    AudienceFieldClass,
    AudienceSyncAck,
    AudienceSyncContractDisabled,
    AudienceSyncDecision,
    AudienceSyncPort,
    AudienceSyncPurpose,
    AudienceSyncRequest,
    AudienceSyncStatus,
    ExternalRecordRef,
    FakeEffortlessPromoAudienceSyncPort,
    audience_sync_request_from_intent,
    authorize_audience_sync,
    build_audience_sync_port,
)
from app.audience.fake import SYNTHETIC_VERSION
from app.db.models import AcceptanceIntent, AcceptanceIntentStatus, SuggestionVisibility
from app.identity.fake import BUSINESS_A
from tests.conftest import make_settings

ORG_FIELD = AudienceField(
    key="business_type", field_class=AudienceFieldClass.ORGANIZATION, value_digest="retail"
)
CONTACT_FIELD = AudienceField(
    key="email", field_class=AudienceFieldClass.CONTACT, value_digest="sha256-of-email"
)


def _request(
    *,
    visibility: str = SuggestionVisibility.RESEARCH_ONLY.value,
    purpose: AudienceSyncPurpose = AudienceSyncPurpose.RESEARCH,
    fields: tuple[AudienceField, ...] = (ORG_FIELD,),
    key: str = "accept-154",
) -> AudienceSyncRequest:
    return AudienceSyncRequest(
        business_id=BUSINESS_A,
        request_key=key,
        intent_id="intent-154",
        subject_id="subject-154",
        visibility=visibility,
        purpose=purpose,
        fields=fields,
    )


# --- the fake + typed shapes ----------------------------------------------------------------------


def test_fake_is_an_audience_sync_port():
    assert isinstance(FakeEffortlessPromoAudienceSyncPort(), AudienceSyncPort)


def test_port_has_no_direct_create_or_promote_path():
    # The DCA may only REQUEST a sync. No create/upsert/promote/audience/send surface may exist on the
    # port protocol or the fake implementation.
    assert "sync" in dir(FakeEffortlessPromoAudienceSyncPort)
    for forbidden in ("create", "upsert", "promote", "add_to_audience", "send", "write"):
        assert forbidden not in dir(FakeEffortlessPromoAudienceSyncPort)
        assert forbidden not in dir(AudienceSyncPort)


def test_request_rejects_unknown_visibility():
    with pytest.raises(ValueError):
        _request(visibility="public")


def test_request_rejects_an_empty_request_key():
    with pytest.raises(ValueError):
        _request(key="")


def test_request_and_ack_never_confer_consent():
    assert _request().confers_consent is False
    ack = FakeEffortlessPromoAudienceSyncPort().sync(_request())
    assert ack.confers_consent is False


# --- authorize_audience_sync: fail-closed ---------------------------------------------------------


def test_research_only_org_fields_are_allowed():
    decision = authorize_audience_sync(_request())
    assert decision.allowed is True
    assert decision.reason == AudienceSyncDecision.OK
    assert decision.confers_consent is False


def test_restricted_contact_field_is_refused_not_promoted():
    # The acceptance criterion: a CONTACT-class field under a research-only acceptance is refused
    # outright — the contract never silently promotes it.
    req = _request(fields=(ORG_FIELD, CONTACT_FIELD))
    assert req.has_restricted_fields is True
    decision = authorize_audience_sync(req)
    assert decision.allowed is False
    assert decision.reason == AudienceSyncDecision.RESTRICTED_FIELD_NOT_PERMITTED


def test_marketing_purpose_on_research_only_is_refused():
    decision = authorize_audience_sync(_request(purpose=AudienceSyncPurpose.MARKETING))
    assert decision.allowed is False
    assert decision.reason == AudienceSyncDecision.PURPOSE_NOT_PERMITTED


def test_sendable_sync_is_disabled():
    # A sendable subject would be a recipient and needs consent that does not exist — refused.
    decision = authorize_audience_sync(_request(visibility=SuggestionVisibility.SENDABLE.value))
    assert decision.allowed is False
    assert decision.reason == AudienceSyncDecision.SENDABLE_SYNC_DISABLED


# --- sync through the fake: pending vs acknowledged vs disabled -----------------------------------


def test_pending_handoff_has_no_external_ids():
    ack = FakeEffortlessPromoAudienceSyncPort(acknowledges=False).sync(_request())
    assert ack.accepted is True
    assert ack.status is AudienceSyncStatus.PENDING
    assert ack.is_pending is True
    assert ack.is_acknowledged is False
    assert ack.external_ref is None  # queued, not written — no external ids yet


def test_acknowledged_write_carries_external_ids():
    ack = FakeEffortlessPromoAudienceSyncPort(acknowledges=True).sync(_request())
    assert ack.accepted is True
    assert ack.status is AudienceSyncStatus.ACKNOWLEDGED
    assert ack.is_acknowledged is True
    assert ack.external_ref is not None
    assert ack.external_ref.crm_record_id and ack.external_ref.audience_id
    assert ack.external_ref.external_version == SYNTHETIC_VERSION


def test_pending_and_acknowledged_outcomes_differ():
    # The two outcomes are deliberately NOT the same: status and external ids both differ.
    pending = FakeEffortlessPromoAudienceSyncPort(acknowledges=False).sync(_request())
    acknowledged = FakeEffortlessPromoAudienceSyncPort(acknowledges=True).sync(_request())
    assert pending.status is not acknowledged.status
    assert (pending.external_ref is None) and (acknowledged.external_ref is not None)


def test_restricted_field_request_through_fake_is_disabled_not_promoted():
    # The failure case driven through the port: a restricted contact field yields a DISABLED ack with no
    # external ids — nothing was taken into any CRM, nothing promoted.
    ack = FakeEffortlessPromoAudienceSyncPort(acknowledges=True).sync(
        _request(fields=(ORG_FIELD, CONTACT_FIELD))
    )
    assert ack.accepted is False
    assert ack.status is AudienceSyncStatus.DISABLED
    assert ack.reason == AudienceSyncDecision.RESTRICTED_FIELD_NOT_PERMITTED
    assert ack.external_ref is None


def test_sync_is_idempotent_in_its_request_key():
    port = FakeEffortlessPromoAudienceSyncPort(acknowledges=True)
    first = port.sync(_request(key="same-key"))
    second = port.sync(_request(key="same-key"))
    assert first.external_ref == second.external_ref  # deterministic external ids
    assert first.request_key == second.request_key == "same-key"


# --- ack invariant: external ids only when acknowledged -------------------------------------------


def test_ack_refuses_external_ids_unless_acknowledged():
    ref = ExternalRecordRef(crm_record_id="crm-1", audience_id="aud-1")
    # A pending/disabled ack may not carry external ids.
    with pytest.raises(ValueError):
        AudienceSyncAck(
            accepted=True,
            status=AudienceSyncStatus.PENDING,
            reason=AudienceSyncDecision.OK,
            request_key="k",
            external_ref=ref,
        )
    # An acknowledged ack MUST carry them.
    with pytest.raises(ValueError):
        AudienceSyncAck(
            accepted=True,
            status=AudienceSyncStatus.ACKNOWLEDGED,
            reason=AudienceSyncDecision.OK,
            request_key="k",
            external_ref=None,
        )


# --- from an acceptance intent --------------------------------------------------------------------


def test_request_built_from_acceptance_intent_captures_it_unchanged():
    intent = AcceptanceIntent(
        id="intent-xyz",
        business_id=BUSINESS_A,
        suggestion_id="sugg-1",
        run_id="run-1",
        subject_type="organization",
        subject_id="subject-xyz",
        visibility=SuggestionVisibility.RESEARCH_ONLY.value,
        status=AcceptanceIntentStatus.PENDING_SYNC.value,
        outbox_event_id="outbox-evt-1",
        created_at=datetime(2026, 11, 2, tzinfo=UTC),
    )
    req = audience_sync_request_from_intent(intent, fields=(ORG_FIELD,))
    assert req.business_id == BUSINESS_A
    assert req.intent_id == "intent-xyz"
    assert req.subject_id == "subject-xyz"
    assert req.visibility == SuggestionVisibility.RESEARCH_ONLY.value
    assert req.request_key == "outbox-evt-1"  # the stable key the intent was staged under
    assert req.purpose is AudienceSyncPurpose.RESEARCH
    # A request built from a research-only intent is allowed; it cannot carry a sendable visibility.
    assert authorize_audience_sync(req).allowed is True


def test_request_from_intent_falls_back_to_intent_id_without_outbox_key():
    intent = AcceptanceIntent(
        id="intent-no-outbox",
        business_id=BUSINESS_A,
        suggestion_id="sugg-2",
        run_id="run-2",
        subject_type="organization",
        subject_id="subject-2",
        visibility=SuggestionVisibility.RESEARCH_ONLY.value,
        status=AcceptanceIntentStatus.PENDING_SYNC.value,
        outbox_event_id="",
        created_at=datetime(2026, 11, 2, tzinfo=UTC),
    )
    req = audience_sync_request_from_intent(intent)
    assert req.request_key == "intent-no-outbox"


# --- production refuses the fake ------------------------------------------------------------------


def test_production_refuses_the_fake():
    with pytest.raises(AudienceSyncContractDisabled):
        build_audience_sync_port(make_settings(environment="production"))


def test_development_builds_the_fake():
    port = build_audience_sync_port(make_settings(environment="development"))
    assert isinstance(port, FakeEffortlessPromoAudienceSyncPort)


def test_ack_is_immutable():
    ack = FakeEffortlessPromoAudienceSyncPort().sync(_request())
    with pytest.raises((ValueError, TypeError)):
        ack.status = AudienceSyncStatus.ACKNOWLEDGED  # type: ignore[misc]
