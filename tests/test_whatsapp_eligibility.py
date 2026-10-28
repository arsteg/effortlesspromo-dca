"""DCA-090 — evaluate WhatsApp eligibility.

Pins the composed WhatsApp-marketing decision's acceptance behaviour and the deliberate separation of
its two concern-families:

- **A public phone or inbound service inquiry alone cannot authorize future promotional messages.** Both
  origins fail the contact-origin gate, and the opt-in gate independently requires an in-force marketing
  consent. An inbound service inquiry OPENS the service window (satisfying the *delivery* constraint),
  yet the message is still BLOCKED on the marketing-permission family — the approved-template /
  service-window constraints never substitute for marketing permission.
- **Approved-template / service-window constraints are kept distinct from marketing permission.** A
  properly opted-in recipient is ELIGIBLE inside the window (session path) and outside it with an
  approved marketing template, but BLOCKED outside the window with no approved template — the delivery
  constraint is real and independent of permission.
- **Reproducible, changing no state.** The decision is a pure function of its structured inputs at a
  given ``now`` — nothing is read or written.

The evaluator is a pure function of structured inputs — it opens no DB, calls no adapter and consults no
model — so the "changing no state" criterion is satisfied intrinsically.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.compliance.whatsapp_eligibility import (
    DELIVERY_APPROVED_TEMPLATE,
    DELIVERY_SERVICE_WINDOW,
    FAMILY_MARKETING_PERMISSION,
    FAMILY_MESSAGE_CONSTRAINT,
    REASON_APPROVED_TEMPLATE_REQUIRED,
    REASON_BASIS_NOT_CONSENT,
    REASON_CONTACT_ORIGIN_NOT_OPT_IN,
    REASON_OK,
    REASON_OPT_IN_NOT_IN_FORCE,
    WHATSAPP_CHANNEL,
    ApprovedTemplate,
    MarketingOptIn,
    ServiceWindow,
    TemplateStatus,
    WhatsAppCategory,
    WhatsAppContactOrigin,
    WhatsAppEligibility,
    WhatsAppEligibilityRequest,
    decide_whatsapp_eligibility,
    evaluate_whatsapp_eligibility,
    opt_in_from_projection,
    origin_is_opt_in,
)
from app.consent.permission_state import (
    PermissionEvent,
    PermissionState,
    project_permission_states,
)

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
PURPOSE = "marketing_whatsapp"
CONTACT = "+919900000001"
MARKETING = WhatsAppCategory.MARKETING.value


# -- helpers -----------------------------------------------------------------------------------------


def _granted_projection(channel: str = WHATSAPP_CHANNEL):
    events = [
        PermissionEvent(
            receipt_id="receipt-1",
            brand_key="brand",
            contact_point_id="cp-1",
            channel=channel,
            purpose=PURPOSE,
            action="grant",
            collected_at=NOW - timedelta(days=3),
        )
    ]
    return next(iter(project_permission_states(events, now=NOW).values()))


def _opt_in(
    *,
    basis: str = "consent",
    state: str = PermissionState.GRANTED.value,
    origin: str = WhatsAppContactOrigin.EXPLICIT_OPT_IN.value,
) -> MarketingOptIn:
    return MarketingOptIn(basis=basis, permission_state=state, contact_origin=origin)


def _approved_template() -> ApprovedTemplate:
    return ApprovedTemplate(
        template_id="tmpl-1",
        category=MARKETING,
        status=TemplateStatus.APPROVED.value,
        language="en",
    )


def _open_window() -> ServiceWindow:
    return ServiceWindow(last_inbound_at=NOW - timedelta(hours=2))


def _closed_window() -> ServiceWindow:
    return ServiceWindow(last_inbound_at=NOW - timedelta(hours=30))


def _request(
    *,
    opt_in: MarketingOptIn,
    template: ApprovedTemplate | None = None,
    window: ServiceWindow | None = None,
) -> WhatsAppEligibilityRequest:
    return WhatsAppEligibilityRequest(
        contact_point=CONTACT,
        purpose=PURPOSE,
        opt_in=opt_in,
        template=template or ApprovedTemplate(),
        service_window=window or ServiceWindow(),
    )


# -- the reproducible allow --------------------------------------------------------------------------


def test_opted_in_with_approved_template_is_eligible():
    result = decide_whatsapp_eligibility(
        _request(opt_in=_opt_in(), template=_approved_template(), window=_closed_window()),
        now=NOW,
    )
    assert result.decision is WhatsAppEligibility.ELIGIBLE
    assert result.eligible is True
    assert result.reasons == (REASON_OK,)
    assert result.blockers == ()
    assert result.channel == WHATSAPP_CHANNEL
    assert result.delivery_path == DELIVERY_APPROVED_TEMPLATE
    assert result.grants_consent is False
    assert result.marketing_permission_granted is True
    assert result.message_delivery_available is True


def test_opted_in_inside_service_window_is_eligible_via_session():
    """Inside the open 24h window a session path carries it — no approved template required."""
    result = decide_whatsapp_eligibility(
        _request(opt_in=_opt_in(), template=ApprovedTemplate(), window=_open_window()),
        now=NOW,
    )
    assert result.decision is WhatsAppEligibility.ELIGIBLE
    assert result.delivery_path == DELIVERY_SERVICE_WINDOW


# -- acceptance: public phone / inbound inquiry cannot authorize promotion ---------------------------


def test_public_phone_alone_cannot_authorize_promotion():
    """A scraped/listed public number is BLOCKED on the marketing-permission family."""
    result = decide_whatsapp_eligibility(
        _request(
            opt_in=_opt_in(
                state=PermissionState.NONE.value,
                origin=WhatsAppContactOrigin.PUBLIC_PHONE.value,
            ),
            template=_approved_template(),  # delivery is possible...
        ),
        now=NOW,
    )
    assert result.decision is WhatsAppEligibility.BLOCKED
    assert result.marketing_permission_granted is False
    # ...but delivery being possible never grants permission.
    assert result.message_delivery_available is True
    reasons = result.reasons
    assert REASON_OPT_IN_NOT_IN_FORCE in reasons
    assert REASON_CONTACT_ORIGIN_NOT_OPT_IN in reasons
    assert all(b.family == FAMILY_MARKETING_PERMISSION for b in result.blockers)


def test_inbound_service_inquiry_open_window_still_blocks_promotion():
    """The headline DCA-090 case: an inbound support DM OPENS the service window (delivery OK) yet a
    promotional message is still BLOCKED on marketing permission — the window never substitutes for it."""
    result = decide_whatsapp_eligibility(
        _request(
            opt_in=_opt_in(
                state=PermissionState.NONE.value,
                origin=WhatsAppContactOrigin.INBOUND_SERVICE_INQUIRY.value,
            ),
            template=ApprovedTemplate(),  # no approved template...
            window=_open_window(),  # ...but the inbound inquiry opened the window
        ),
        now=NOW,
    )
    assert result.decision is WhatsAppEligibility.BLOCKED
    # Delivery constraint SATISFIED (open window) — proving the two families are independent.
    assert result.message_delivery_available is True
    assert result.delivery_path == DELIVERY_SERVICE_WINDOW
    assert result.message_constraint_blockers == ()
    # Permission WITHHELD — the decisive denial is a marketing-permission gate, not the window.
    assert result.marketing_permission_granted is False
    assert result.decisive is not None
    assert result.decisive.family == FAMILY_MARKETING_PERMISSION
    assert REASON_CONTACT_ORIGIN_NOT_OPT_IN in result.reasons
    assert REASON_OPT_IN_NOT_IN_FORCE in result.reasons


def test_inbound_inquiry_blocks_even_if_consent_flag_claimed_without_evidence():
    """Even if a GRANTED state were claimed, an inbound-inquiry origin still fails the origin gate —
    origin is a distinct requirement from the permission state."""
    result = decide_whatsapp_eligibility(
        _request(
            opt_in=_opt_in(
                state=PermissionState.GRANTED.value,
                origin=WhatsAppContactOrigin.INBOUND_SERVICE_INQUIRY.value,
            ),
            template=_approved_template(),
        ),
        now=NOW,
    )
    assert result.decision is WhatsAppEligibility.BLOCKED
    assert result.reasons == (REASON_CONTACT_ORIGIN_NOT_OPT_IN,)
    assert result.decisive.family == FAMILY_MARKETING_PERMISSION


# -- the message-delivery constraint is real and separate --------------------------------------------


def test_opted_in_outside_window_without_template_is_blocked_on_delivery():
    """A fully opted-in recipient is still BLOCKED when no approved template exists and the window is
    closed — the message-constraint family denies independently of permission."""
    result = decide_whatsapp_eligibility(
        _request(opt_in=_opt_in(), template=ApprovedTemplate(), window=_closed_window()),
        now=NOW,
    )
    assert result.decision is WhatsAppEligibility.BLOCKED
    assert result.marketing_permission_granted is True
    assert result.message_delivery_available is False
    assert result.reasons == (REASON_APPROVED_TEMPLATE_REQUIRED,)
    assert result.decisive.family == FAMILY_MESSAGE_CONSTRAINT
    assert result.delivery_path == ""


def test_pending_or_wrong_category_template_is_not_usable():
    pending = ApprovedTemplate(
        template_id="t", category=MARKETING, status=TemplateStatus.PENDING.value
    )
    wrong_category = ApprovedTemplate(
        template_id="t",
        category=WhatsAppCategory.UTILITY.value,
        status=TemplateStatus.APPROVED.value,
    )
    for tmpl in (pending, wrong_category):
        result = decide_whatsapp_eligibility(
            _request(opt_in=_opt_in(), template=tmpl, window=_closed_window()), now=NOW
        )
        assert result.decision is WhatsAppEligibility.BLOCKED
        assert result.reasons == (REASON_APPROVED_TEMPLATE_REQUIRED,)


def test_non_consent_basis_rejected():
    """Consent-first: a generic B2B / legitimate-interest basis is rejected outright."""
    result = decide_whatsapp_eligibility(
        _request(
            opt_in=_opt_in(basis="legitimate_interest", state=PermissionState.NONE.value),
            template=_approved_template(),
        ),
        now=NOW,
    )
    assert result.decision is WhatsAppEligibility.BLOCKED
    assert result.reasons[0] == REASON_BASIS_NOT_CONSENT
    gates = [b.gate for b in result.marketing_permission_blockers]
    assert gates[0] == "basis"


# -- scope / channel isolation -----------------------------------------------------------------------


def test_permits_only_whatsapp_channel():
    result = decide_whatsapp_eligibility(
        _request(opt_in=_opt_in(), template=_approved_template()), now=NOW
    )
    assert result.permits_channel(WHATSAPP_CHANNEL) is True
    assert result.permits_channel("email") is False
    assert result.permits_channel("sms") is False


def test_non_whatsapp_projection_does_not_grant_opt_in():
    """A GRANTED opt-in on the SMS scope cannot satisfy the WhatsApp opt-in gate."""
    sms_projection = _granted_projection(channel="sms")
    opt_in = opt_in_from_projection(sms_projection, WhatsAppContactOrigin.EXPLICIT_OPT_IN.value)
    result = decide_whatsapp_eligibility(
        _request(opt_in=opt_in, template=_approved_template()), now=NOW
    )
    assert result.decision is WhatsAppEligibility.BLOCKED
    assert REASON_OPT_IN_NOT_IN_FORCE in result.reasons


# -- convenience path, origin helper, purity ---------------------------------------------------------


def test_evaluate_convenience_from_projection_eligible():
    result = evaluate_whatsapp_eligibility(
        contact_point=CONTACT,
        purpose=PURPOSE,
        permission=_granted_projection(),
        contact_origin=WhatsAppContactOrigin.EXPLICIT_OPT_IN.value,
        template=_approved_template(),
        now=NOW,
    )
    assert result.decision is WhatsAppEligibility.ELIGIBLE


def test_evaluate_convenience_none_projection_blocks():
    result = evaluate_whatsapp_eligibility(
        contact_point=CONTACT,
        purpose=PURPOSE,
        permission=None,  # flag-only import, no evidenced opt-in
        contact_origin=WhatsAppContactOrigin.PUBLIC_PHONE.value,
        template=_approved_template(),
        now=NOW,
    )
    assert result.decision is WhatsAppEligibility.BLOCKED
    assert REASON_OPT_IN_NOT_IN_FORCE in result.reasons
    assert REASON_CONTACT_ORIGIN_NOT_OPT_IN in result.reasons


def test_origin_helper():
    assert origin_is_opt_in(WhatsAppContactOrigin.EXPLICIT_OPT_IN.value) is True
    assert origin_is_opt_in(WhatsAppContactOrigin.PUBLIC_PHONE.value) is False
    assert origin_is_opt_in(WhatsAppContactOrigin.INBOUND_SERVICE_INQUIRY.value) is False
    assert origin_is_opt_in(WhatsAppContactOrigin.UNKNOWN.value) is False


def test_service_window_boundaries():
    w = ServiceWindow(last_inbound_at=NOW - timedelta(hours=24))
    assert w.is_open(NOW) is True  # exactly at the edge
    assert ServiceWindow(last_inbound_at=NOW - timedelta(hours=24, minutes=1)).is_open(NOW) is False
    assert ServiceWindow(last_inbound_at=None).is_open(NOW) is False
    # a future inbound timestamp is not a valid open window (fail-closed)
    assert ServiceWindow(last_inbound_at=NOW + timedelta(hours=1)).is_open(NOW) is False


def test_decision_is_reproducible_and_changes_no_state():
    req = _request(opt_in=_opt_in(), template=_approved_template())
    a = decide_whatsapp_eligibility(req, now=NOW)
    b = decide_whatsapp_eligibility(req, now=NOW)
    assert a == b


def test_blockers_collected_in_fixed_family_order():
    """All gates evaluated (not short-circuited): basis+opt-in+origin then the message constraint."""
    result = decide_whatsapp_eligibility(
        _request(
            opt_in=_opt_in(
                basis="none",
                state=PermissionState.WITHDRAWN.value,
                origin=WhatsAppContactOrigin.PUBLIC_PHONE.value,
            ),
            template=ApprovedTemplate(),
            window=_closed_window(),
        ),
        now=NOW,
    )
    families = [b.family for b in result.blockers]
    # permission-family blockers precede the message-constraint blocker
    assert families == [
        FAMILY_MARKETING_PERMISSION,
        FAMILY_MARKETING_PERMISSION,
        FAMILY_MARKETING_PERMISSION,
        FAMILY_MESSAGE_CONSTRAINT,
    ]
    assert result.reasons == (
        REASON_BASIS_NOT_CONSENT,
        REASON_OPT_IN_NOT_IN_FORCE,
        REASON_CONTACT_ORIGIN_NOT_OPT_IN,
        REASON_APPROVED_TEMPLATE_REQUIRED,
    )
