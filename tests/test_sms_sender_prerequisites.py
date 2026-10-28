"""DCA-088 — validate Indian SMS sender prerequisites.

Pins the composed DLT sender-readiness decision's acceptance behaviour:

- a fully-registered, ACTIVE, bound, category-matched sender on a given route is READY (the
  reproducible allow);
- missing / not-ACTIVE / unbound / category-mismatched setup DENIES the route, fail-closed, with the
  offending prerequisite's stable reason code — one case per gate;
- READY is SENDER readiness only: it never authorizes a recipient (``authorizes_recipient`` /
  ``grants_consent`` stay ``False``), and ``recipient_consent_present`` is recorded but never consulted
  (setup alone cannot authorize a recipient, and recipient consent cannot make an unregistered sender
  routable);
- the decision reads nothing and changes nothing, so a given ``(request, now)`` is reproducible.

The evaluator is a pure function of structured DLT-registration inputs — it opens no DB, calls no
adapter and consults no model — so there is no persisted read path to exercise (unlike the DCA-087
recipient line, which read real ``ConsentReceipt`` rows). The "changing no state" acceptance criterion
is satisfied intrinsically: nothing is read or written.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.compliance.sms_sender_prerequisites import (
    REASON_CONSENT_TEMPLATE_NOT_REGISTERED,
    REASON_CONTENT_TEMPLATE_NOT_REGISTERED,
    REASON_DELIVERY_CHAIN_INCOMPLETE,
    REASON_HEADER_NOT_REGISTERED,
    REASON_OK,
    REASON_PRINCIPAL_ENTITY_UNREGISTERED,
    REASON_ROUTE_CATEGORY_MISMATCH,
    SMS_CHANNEL,
    ConsentTemplate,
    ContentTemplate,
    DeliveryChain,
    DltRegistrationStatus,
    Header,
    PrincipalEntity,
    SmsRoute,
    SmsSenderReadiness,
    SmsSenderReadinessRequest,
    decide_sms_sender_readiness,
    requires_consent_template,
)

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
ACTIVE = DltRegistrationStatus.ACTIVE.value
ENTITY_ID = "PE-ACME-SYNTH"
HEADER_ID = "ACMEIN"
PROMO = SmsRoute.PROMOTIONAL.value
TXN = SmsRoute.TRANSACTIONAL.value

# Every prerequisite satisfied on the PROMOTIONAL route — the baseline each failure case breaks once.
_ENTITY = PrincipalEntity(entity_id=ENTITY_ID, status=ACTIVE)
_HEADER = Header(header_id=HEADER_ID, category=PROMO, status=ACTIVE, bound_entity_id=ENTITY_ID)
_CONTENT = ContentTemplate(
    template_id="CT-1", category=PROMO, status=ACTIVE, bound_header_id=HEADER_ID
)
_CONSENT = ConsentTemplate(template_id="CN-1", category=PROMO, status=ACTIVE)
_CHAIN = DeliveryChain(telemarketer_id="TM-1", status=ACTIVE, bound_entity_id=ENTITY_ID)


def _request(**overrides) -> SmsSenderReadinessRequest:
    base = dict(
        route=PROMO,
        principal_entity=_ENTITY,
        header=_HEADER,
        content_template=_CONTENT,
        delivery_chain=_CHAIN,
        consent_template=_CONSENT,
        recipient_consent_present=False,
    )
    base.update(overrides)
    return SmsSenderReadinessRequest(**base)


def test_all_prerequisites_present_is_ready():
    result = decide_sms_sender_readiness(_request(), now=NOW)
    assert result.decision is SmsSenderReadiness.READY
    assert result.ready is True
    assert result.reasons == (REASON_OK,)
    assert result.blockers == ()
    assert result.decisive is None
    assert result.channel == SMS_CHANNEL
    # Sender readiness is never recipient permission / a send authorization.
    assert result.authorizes_recipient is False
    assert result.grants_consent is False


def test_ready_does_not_authorize_recipient_even_with_consent_recorded():
    """The headline acceptance: setup alone cannot authorize a recipient (and vice-versa)."""
    # Recipient consent recorded, but it is NOT consulted — only the sender prerequisites decide READY.
    with_consent = decide_sms_sender_readiness(_request(recipient_consent_present=True), now=NOW)
    assert with_consent.decision is SmsSenderReadiness.READY
    assert with_consent.recipient_consent_present is True
    assert (
        with_consent.authorizes_recipient is False
    )  # READY still does not authorize the recipient

    # Conversely, a recipient's consent cannot make an unregistered sender routable.
    unregistered = decide_sms_sender_readiness(
        _request(
            principal_entity=PrincipalEntity(entity_id=ENTITY_ID, status=ACTIVE),
            header=Header(header_id="", category=PROMO),
            recipient_consent_present=True,
        ),
        now=NOW,
    )
    assert unregistered.decision is SmsSenderReadiness.BLOCKED
    assert REASON_HEADER_NOT_REGISTERED in unregistered.reasons


def test_principal_entity_unregistered_blocks():
    result = decide_sms_sender_readiness(
        _request(
            principal_entity=PrincipalEntity(
                entity_id=ENTITY_ID, status=DltRegistrationStatus.PENDING.value
            )
        ),
        now=NOW,
    )
    assert result.decision is SmsSenderReadiness.BLOCKED
    # An unregistered PE also unbinds the header + chain (they bind to the PE id, present here) — but the
    # decisive, headline denial is the principal entity itself, reported first.
    assert result.decisive is not None and result.decisive.gate == "principal_entity"
    assert result.decisive.reason == REASON_PRINCIPAL_ENTITY_UNREGISTERED


def test_missing_entity_id_unbinds_header_and_chain():
    result = decide_sms_sender_readiness(
        _request(principal_entity=PrincipalEntity(entity_id="", status=ACTIVE)),
        now=NOW,
    )
    assert result.decision is SmsSenderReadiness.BLOCKED
    gates = [b.gate for b in result.blockers]
    assert gates == ["principal_entity", "header", "delivery_chain"]


def test_header_not_registered_blocks():
    result = decide_sms_sender_readiness(
        _request(
            header=Header(
                header_id=HEADER_ID,
                category=PROMO,
                status=DltRegistrationStatus.SUSPENDED.value,
                bound_entity_id=ENTITY_ID,
            )
        ),
        now=NOW,
    )
    assert result.decision is SmsSenderReadiness.BLOCKED
    assert result.reasons == (REASON_HEADER_NOT_REGISTERED,)
    assert result.decisive.detail == DltRegistrationStatus.SUSPENDED.value


def test_header_bound_to_other_entity_blocks():
    """An ACTIVE header that belongs to a different principal entity is not this sender's header."""
    foreign = Header(
        header_id=HEADER_ID, category=PROMO, status=ACTIVE, bound_entity_id="PE-SOMEONE-ELSE"
    )
    result = decide_sms_sender_readiness(_request(header=foreign), now=NOW)
    assert result.decision is SmsSenderReadiness.BLOCKED
    assert result.reasons == (REASON_HEADER_NOT_REGISTERED,)
    assert result.decisive.detail == "unbound_entity"


def test_content_template_not_registered_blocks():
    result = decide_sms_sender_readiness(
        _request(
            content_template=ContentTemplate(
                template_id="CT-1",
                category=PROMO,
                status=DltRegistrationStatus.NONE.value,
                bound_header_id=HEADER_ID,
            )
        ),
        now=NOW,
    )
    assert result.decision is SmsSenderReadiness.BLOCKED
    assert result.reasons == (REASON_CONTENT_TEMPLATE_NOT_REGISTERED,)


def test_content_template_bound_to_other_header_blocks():
    result = decide_sms_sender_readiness(
        _request(
            content_template=ContentTemplate(
                template_id="CT-1", category=PROMO, status=ACTIVE, bound_header_id="OTHERHDR"
            )
        ),
        now=NOW,
    )
    assert result.decision is SmsSenderReadiness.BLOCKED
    assert result.reasons == (REASON_CONTENT_TEMPLATE_NOT_REGISTERED,)
    assert result.decisive.detail == "unbound_header"


def test_consent_template_required_on_promotional_route():
    """Promotional is a consent-based route: a missing consent template denies it."""
    result = decide_sms_sender_readiness(_request(consent_template=None), now=NOW)
    assert result.decision is SmsSenderReadiness.BLOCKED
    assert result.reasons == (REASON_CONSENT_TEMPLATE_NOT_REGISTERED,)
    assert result.decisive.detail == "missing"


def test_consent_template_not_required_on_transactional_route():
    """Transactional is not consent-based: no consent template is needed and its absence is fine."""
    assert requires_consent_template(TXN) is False
    result = decide_sms_sender_readiness(
        _request(
            route=TXN,
            header=Header(
                header_id=HEADER_ID, category=TXN, status=ACTIVE, bound_entity_id=ENTITY_ID
            ),
            content_template=ContentTemplate(
                template_id="CT-1", category=TXN, status=ACTIVE, bound_header_id=HEADER_ID
            ),
            consent_template=None,
        ),
        now=NOW,
    )
    assert result.decision is SmsSenderReadiness.READY
    assert result.reasons == (REASON_OK,)


def test_route_category_mismatch_blocks():
    """A promotional header/template cannot carry a transactional route — mismatched setup denies."""
    result = decide_sms_sender_readiness(
        _request(
            route=TXN,
            consent_template=None,  # not required on transactional
        ),
        now=NOW,
    )
    # header + content are registered PROMOTIONAL but the route is TRANSACTIONAL -> mismatch.
    assert result.decision is SmsSenderReadiness.BLOCKED
    assert REASON_ROUTE_CATEGORY_MISMATCH in result.reasons
    mismatch = next(b for b in result.blockers if b.reason == REASON_ROUTE_CATEGORY_MISMATCH)
    assert "header=promotional" in mismatch.detail and "content=promotional" in mismatch.detail


def test_delivery_chain_incomplete_blocks():
    result = decide_sms_sender_readiness(
        _request(
            delivery_chain=DeliveryChain(
                telemarketer_id="TM-1",
                status=DltRegistrationStatus.EXPIRED.value,
                bound_entity_id=ENTITY_ID,
            )
        ),
        now=NOW,
    )
    assert result.decision is SmsSenderReadiness.BLOCKED
    assert result.reasons == (REASON_DELIVERY_CHAIN_INCOMPLETE,)


def test_delivery_chain_unbound_blocks():
    result = decide_sms_sender_readiness(
        _request(
            delivery_chain=DeliveryChain(
                telemarketer_id="TM-1", status=ACTIVE, bound_entity_id="PE-OTHER"
            )
        ),
        now=NOW,
    )
    assert result.decision is SmsSenderReadiness.BLOCKED
    assert result.reasons == (REASON_DELIVERY_CHAIN_INCOMPLETE,)
    assert result.decisive.detail == "unbound_entity"


def test_expired_registration_is_not_usable():
    """An ACTIVE-but-past-expiry header is treated as lapsed (fail-closed on the expiry window)."""
    expired = Header(
        header_id=HEADER_ID,
        category=PROMO,
        status=ACTIVE,
        bound_entity_id=ENTITY_ID,
        expires_at=NOW - timedelta(days=1),
    )
    result = decide_sms_sender_readiness(_request(header=expired), now=NOW)
    assert result.decision is SmsSenderReadiness.BLOCKED
    assert result.reasons == (REASON_HEADER_NOT_REGISTERED,)


def test_every_prerequisite_reported_in_order():
    """All failing prerequisites are collected (not short-circuited), in a fixed gate order."""
    result = decide_sms_sender_readiness(
        SmsSenderReadinessRequest(
            route=PROMO,
            principal_entity=PrincipalEntity(
                entity_id=ENTITY_ID, status=DltRegistrationStatus.BLACKLISTED.value
            ),
            header=Header(
                header_id=HEADER_ID,
                category=TXN,
                status=DltRegistrationStatus.NONE.value,
                bound_entity_id="",
            ),
            content_template=ContentTemplate(
                template_id="", category=TXN, status=DltRegistrationStatus.NONE.value
            ),
            delivery_chain=DeliveryChain(
                telemarketer_id="TM-1", status=DltRegistrationStatus.NONE.value, bound_entity_id=""
            ),
            consent_template=None,
        ),
        now=NOW,
    )
    gates = [b.gate for b in result.blockers]
    assert gates == [
        "principal_entity",
        "header",
        "content_template",
        "consent_template",
        "route_category",
        "delivery_chain",
    ]


def test_decision_is_reproducible():
    a = decide_sms_sender_readiness(_request(), now=NOW)
    b = decide_sms_sender_readiness(_request(), now=NOW)
    assert a == b
