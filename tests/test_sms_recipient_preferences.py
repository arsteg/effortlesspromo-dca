"""DCA-089 — evaluate SMS recipient preferences.

Pins the composed recipient-eligibility decision's acceptance behaviour and its provider-backed
preference/consent screening contract:

- **Unavailable required screening fails closed.** An unavailable / stale / off-scope preference
  answer never clears the route — the decision is BLOCKED with the screening gate's stable reason code.
- **DND results do not grant email or WhatsApp permission.** The decision is SMS-scoped
  (``permits_channel`` is False for any other channel) and the SMS-only screening snapshot cannot clear
  email / WhatsApp (``channel_not_in_screening_scope``).
- **Consent-first product default is kept distinct from law.** The product consent gate and the legal
  DND gate are independent and BOTH must pass: a not-on-DND recipient without consent is still BLOCKED,
  and a consented recipient who IS on DND is still BLOCKED.
- **Reproducible, changing no state.** The decision and the screening are pure functions of their
  structured inputs at a given ``now`` — nothing is read or written.

The evaluators are pure functions of structured inputs — they open no DB, call no adapter and consult
no model — so the "changing no state" criterion is satisfied intrinsically. The synthetic fake
(``app/preferences/fake.py``) is used only to exercise the provider-backed contract deterministically.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.compliance.sms_recipient_preferences import (
    REASON_BASIS_NOT_CONSENT,
    REASON_OK,
    REASON_PRODUCT_CONSENT_NOT_IN_FORCE,
    ProductConsent,
    SmsRecipientEligibility,
    SmsRecipientEligibilityRequest,
    consent_from_projection,
    decide_sms_recipient_eligibility,
    evaluate_sms_recipient_eligibility,
)
from app.compliance.sms_sender_prerequisites import SMS_CHANNEL, SmsRoute
from app.consent.permission_state import (
    PermissionEvent,
    PermissionState,
    project_permission_states,
)
from app.identity.fake import BUSINESS_A, BUSINESS_B
from app.preferences.contract import (
    PreferenceAvailability,
    PreferenceEntry,
    PreferenceScreeningDecision,
    PreferenceScreeningRequest,
    PreferenceScreeningSnapshot,
    dnd_applies_to_route,
    screen_preferences,
)
from app.preferences.fake import (
    FakePreferenceScreeningClient,
    PreferenceContractDisabled,
    build_preference_screening_client,
)

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
PURPOSE = "marketing_sms"
PROMO = SmsRoute.PROMOTIONAL.value
TXN = SmsRoute.TRANSACTIONAL.value
CLEAR_PHONE = "+919900000001"
DND_PHONE = "+919812345678"  # fully-DND in the fake's Business A fixture


# -- helpers -----------------------------------------------------------------------------------------


def _granted_sms_consent(contact_point_id: str = "cp-1") -> ProductConsent:
    events = [
        PermissionEvent(
            receipt_id=f"receipt-{contact_point_id}",
            brand_key="brand",
            contact_point_id=contact_point_id,
            channel=SMS_CHANNEL,
            purpose=PURPOSE,
            action="grant",
            collected_at=NOW - timedelta(days=3),
        )
    ]
    projection = next(iter(project_permission_states(events, now=NOW).values()))
    return consent_from_projection(projection)


def _clear_snapshot(
    business_id: str = BUSINESS_A,
    *,
    channels: tuple[str, ...] = (SMS_CHANNEL,),
    as_of: datetime = NOW,
    entries: tuple[PreferenceEntry, ...] = (),
) -> PreferenceScreeningSnapshot:
    return PreferenceScreeningSnapshot(
        business_id=business_id,
        availability=PreferenceAvailability.AVAILABLE,
        provider="test-provider",
        version="v1",
        as_of=as_of,
        covered_channels=channels,
        entries=entries,
        synthetic=True,
    )


def _clear_screening(route: str = PROMO) -> PreferenceScreeningDecision:
    return screen_preferences(_clear_snapshot(), CLEAR_PHONE, route, now=NOW)


# -- the composed decision ---------------------------------------------------------------------------


def test_consented_and_dnd_clear_is_eligible():
    """The reproducible allow: consent in force AND the preference registry clears the route."""
    result = decide_sms_recipient_eligibility(
        SmsRecipientEligibilityRequest(
            contact_point=CLEAR_PHONE,
            purpose=PURPOSE,
            route=PROMO,
            consent=_granted_sms_consent(),
            screening=_clear_screening(),
        ),
        now=NOW,
    )
    assert result.decision is SmsRecipientEligibility.ELIGIBLE
    assert result.eligible is True
    assert result.reasons == (REASON_OK,)
    assert result.blockers == ()
    assert result.channel == SMS_CHANNEL
    assert result.grants_consent is False


def test_unavailable_screening_fails_closed():
    """Acceptance: an unavailable required screening blocks (never a silent allow), even with consent."""
    fake = FakePreferenceScreeningClient(available=False, now=NOW)
    snapshot = fake.screen(
        PreferenceScreeningRequest(business_id=BUSINESS_A, contact_point=CLEAR_PHONE)
    )
    assert snapshot.availability is PreferenceAvailability.UNAVAILABLE

    result = evaluate_sms_recipient_eligibility(
        contact_point=CLEAR_PHONE,
        purpose=PURPOSE,
        route=PROMO,
        permission=_granted_projection(),
        screening_snapshot=snapshot,
        now=NOW,
    )
    assert result.decision is SmsRecipientEligibility.BLOCKED
    assert PreferenceScreeningDecision.UNAVAILABLE in result.reasons
    assert result.decisive is not None and result.decisive.gate == "preference_screening"
    # Required screening unavailable means NOTHING is authorised on any channel.
    assert result.permits_channel(SMS_CHANNEL) is False


def test_stale_screening_fails_closed():
    """A clean-but-stale answer is refused: a DND registered since could be in force."""
    stale = _clear_snapshot(as_of=NOW - timedelta(days=30))
    screening = screen_preferences(stale, CLEAR_PHONE, PROMO, now=NOW)
    assert screening.reason == PreferenceScreeningDecision.STALE

    result = decide_sms_recipient_eligibility(
        SmsRecipientEligibilityRequest(
            contact_point=CLEAR_PHONE,
            purpose=PURPOSE,
            route=PROMO,
            consent=_granted_sms_consent(),
            screening=screening,
        ),
        now=NOW,
    )
    assert result.decision is SmsRecipientEligibility.BLOCKED
    assert PreferenceScreeningDecision.STALE in result.reasons


def test_dnd_registered_blocks_even_with_consent():
    """A consented recipient who IS on DND is still blocked — law and product are independent gates."""
    dnd_entry = PreferenceEntry(contact_point=DND_PHONE, channel=SMS_CHANNEL, fully_blocked=True)
    screening = screen_preferences(_clear_snapshot(entries=(dnd_entry,)), DND_PHONE, PROMO, now=NOW)
    assert screening.reason == PreferenceScreeningDecision.DND_REGISTERED

    result = decide_sms_recipient_eligibility(
        SmsRecipientEligibilityRequest(
            contact_point=DND_PHONE,
            purpose=PURPOSE,
            route=PROMO,
            consent=_granted_sms_consent(),  # consent IS in force
            screening=screening,
        ),
        now=NOW,
    )
    assert result.decision is SmsRecipientEligibility.BLOCKED
    assert result.reasons == (PreferenceScreeningDecision.DND_REGISTERED,)
    assert result.decisive.detail == "fully_blocked"


def test_not_on_dnd_without_consent_still_blocks():
    """Acceptance (law distinct from product): a not-on-DND recipient without consent stays BLOCKED."""
    result = evaluate_sms_recipient_eligibility(
        contact_point=CLEAR_PHONE,
        purpose=PURPOSE,
        route=PROMO,
        permission=None,  # a flag-only import that produced no evidenced consent
        screening_snapshot=_clear_snapshot(),  # law says: not on DND
        now=NOW,
    )
    assert result.decision is SmsRecipientEligibility.BLOCKED
    assert REASON_PRODUCT_CONSENT_NOT_IN_FORCE in result.reasons
    assert result.decisive.gate == "product_consent"


def test_non_consent_basis_rejected():
    """Consent-first: a generic B2B / legitimate-interest basis is rejected outright (no exception)."""
    result = decide_sms_recipient_eligibility(
        SmsRecipientEligibilityRequest(
            contact_point=CLEAR_PHONE,
            purpose=PURPOSE,
            route=PROMO,
            consent=ProductConsent(
                basis="legitimate_interest", permission_state=PermissionState.NONE.value
            ),
            screening=_clear_screening(),
        ),
        now=NOW,
    )
    assert result.decision is SmsRecipientEligibility.BLOCKED
    gates = [b.gate for b in result.blockers]
    assert gates == ["basis", "product_consent"]
    assert result.reasons[0] == REASON_BASIS_NOT_CONSENT


def test_dnd_clear_does_not_grant_other_channels():
    """Acceptance: a DND/SMS result grants NOTHING on email or WhatsApp — permits_channel is SMS-only."""
    result = decide_sms_recipient_eligibility(
        SmsRecipientEligibilityRequest(
            contact_point=CLEAR_PHONE,
            purpose=PURPOSE,
            route=PROMO,
            consent=_granted_sms_consent(),
            screening=_clear_screening(),
        ),
        now=NOW,
    )
    assert result.decision is SmsRecipientEligibility.ELIGIBLE
    assert result.permits_channel(SMS_CHANNEL) is True
    assert result.permits_channel("email") is False
    assert result.permits_channel("whatsapp") is False


def test_sms_screening_cannot_clear_other_channels():
    """An SMS-only screening snapshot refuses email/WhatsApp — it never leaks cross-channel."""
    sms_only = _clear_snapshot(channels=(SMS_CHANNEL,))
    for channel in ("email", "whatsapp"):
        decision = screen_preferences(sms_only, CLEAR_PHONE, PROMO, channel=channel, now=NOW)
        assert decision.allowed is False
        assert decision.reason == PreferenceScreeningDecision.CHANNEL_NOT_SCREENED
        assert decision.permits_channel(channel) is False


def test_screening_decision_never_grants_consent():
    """Clearing DND is the legal gate only — never recipient consent."""
    cleared = _clear_screening()
    assert cleared.allowed is True
    assert cleared.grants_consent is False


def test_fully_dnd_blocks_promotional_but_not_transactional_route():
    """DND is route-aware: a fully-DND registration blocks promotional, not the exempt transactional route."""
    assert dnd_applies_to_route(PROMO) is True
    assert dnd_applies_to_route(TXN) is False
    entry = PreferenceEntry(contact_point=DND_PHONE, channel=SMS_CHANNEL, fully_blocked=True)
    snap = _clear_snapshot(entries=(entry,))

    promo = screen_preferences(snap, DND_PHONE, PROMO, now=NOW)
    assert promo.reason == PreferenceScreeningDecision.DND_REGISTERED

    txn = screen_preferences(snap, DND_PHONE, TXN, now=NOW)
    assert txn.allowed is True  # transactional is exempt from DND


def test_category_blocked_route_denies():
    """A route-specific (non-fully) DND block denies that route by category match."""
    entry = PreferenceEntry(contact_point=DND_PHONE, channel=SMS_CHANNEL, blocked_routes=(PROMO,))
    decision = screen_preferences(_clear_snapshot(entries=(entry,)), DND_PHONE, PROMO, now=NOW)
    assert decision.reason == PreferenceScreeningDecision.DND_REGISTERED
    assert decision.detail == PROMO


def test_decision_is_reproducible():
    req = SmsRecipientEligibilityRequest(
        contact_point=CLEAR_PHONE,
        purpose=PURPOSE,
        route=PROMO,
        consent=_granted_sms_consent(),
        screening=_clear_screening(),
    )
    a = decide_sms_recipient_eligibility(req, now=NOW)
    b = decide_sms_recipient_eligibility(req, now=NOW)
    assert a == b


def test_tenant_isolation_inputs_are_independent():
    """Business A (DND) and Business B (clear) resolve independently from their own fixtures."""
    fake = FakePreferenceScreeningClient(now=NOW)
    snap_a = fake.screen(
        PreferenceScreeningRequest(business_id=BUSINESS_A, contact_point=DND_PHONE)
    )
    snap_b = fake.screen(
        PreferenceScreeningRequest(business_id=BUSINESS_B, contact_point=CLEAR_PHONE)
    )
    a = evaluate_sms_recipient_eligibility(
        contact_point=DND_PHONE,
        purpose=PURPOSE,
        route=PROMO,
        permission=_granted_projection(),
        screening_snapshot=snap_a,
        now=NOW,
    )
    b = evaluate_sms_recipient_eligibility(
        contact_point=CLEAR_PHONE,
        purpose=PURPOSE,
        route=PROMO,
        permission=_granted_projection(),
        screening_snapshot=snap_b,
        now=NOW,
    )
    assert a.decision is SmsRecipientEligibility.BLOCKED  # A's recipient is fully-DND
    assert a.reasons == (PreferenceScreeningDecision.DND_REGISTERED,)
    assert b.decision is SmsRecipientEligibility.ELIGIBLE  # B's recipient is clear + consented


def test_production_refuses_the_fake():
    """The provider-backed contract stays disabled in production (the live adapter is unbuilt)."""

    class _Prod:
        is_production = True

    try:
        build_preference_screening_client(_Prod())
    except PreferenceContractDisabled:
        pass
    else:  # pragma: no cover - the assertion is the failure path
        raise AssertionError("expected PreferenceContractDisabled in production")


# -- a granted SMS permission projection for the convenience path -----------------------------------


def _granted_projection():
    events = [
        PermissionEvent(
            receipt_id="receipt-x",
            brand_key="brand",
            contact_point_id="cp-x",
            channel=SMS_CHANNEL,
            purpose=PURPOSE,
            action="grant",
            collected_at=NOW - timedelta(days=3),
        )
    ]
    return next(iter(project_permission_states(events, now=NOW).values()))
