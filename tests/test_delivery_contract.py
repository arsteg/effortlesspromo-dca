"""DCA-096 — the delivery adapter contract + the synthetic EffortlessPromo delivery fake.

Request delivery ONLY through EffortlessPromo: a typed request (idempotency key + consumed decision
context), a one-method port (``request_delivery`` — no send/provider path), a fail-closed decision, and
a dev/test-only fake refused in production. The load-bearing behaviour: a host that does NOT enforce
delivery callbacks DISABLES live delivery instead of being bypassed with a fire-and-forget request.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.delivery import (
    DeliveryCapabilities,
    DeliveryContext,
    DeliveryContractDisabled,
    DeliveryDecision,
    DeliveryHostAvailability,
    DeliveryPort,
    DeliveryReceipt,
    DeliveryRequest,
    FakeEffortlessPromoDeliveryPort,
    authorize_delivery,
    build_delivery_port,
)
from app.delivery.fake import SYNTHETIC_VERSION
from app.identity.fake import BUSINESS_A
from tests.conftest import make_settings

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _context(channel: str = "email") -> DeliveryContext:
    """A decision context standing in for a consumed DCA-095 grant — recipient as a digest only."""
    return DeliveryContext(
        business_id=BUSINESS_A,
        channel=channel,
        recipient_digest="sha256-digest-of-recipient",
        contact_point_id="cp-096",
        consumption_id="consumption-096",
        reference_fingerprint="fp-096",
        permission_version="perm-v3",
        source_version="src-v2",
        suppression_version="supp-v1",
        provider="ses",
        campaign_revision="rev-7",
    )


def _request(key: str = "dispatch-096") -> DeliveryRequest:
    return DeliveryRequest(idempotency_key=key, context=_context())


# --- the fake + typed shapes ----------------------------------------------------------------------


def test_fake_is_a_delivery_port():
    assert isinstance(FakeEffortlessPromoDeliveryPort(), DeliveryPort)


def test_port_has_no_direct_send_or_provider_path():
    # The DCA may only REQUEST delivery. There is deliberately no send/provider/channel-I/O surface,
    # on the port protocol or on the fake implementation.
    assert "request_delivery" in dir(FakeEffortlessPromoDeliveryPort)
    for forbidden in ("send", "send_message", "deliver", "provider", "transmit"):
        assert forbidden not in dir(FakeEffortlessPromoDeliveryPort)
        assert forbidden not in dir(DeliveryPort)


def test_capabilities_are_visibly_synthetic_and_advertise_callback_enforcement():
    caps = FakeEffortlessPromoDeliveryPort(now=NOW).capabilities(BUSINESS_A)
    assert caps.synthetic is True
    assert caps.version == SYNTHETIC_VERSION
    assert caps.host_owns_send is True
    assert caps.is_available is True
    assert caps.enforces_callbacks is True
    assert caps.supports_channel("email") and caps.supports_channel("EMAIL")
    assert not caps.supports_channel("whatsapp")


def test_context_and_receipt_never_authorize_a_send():
    assert _context().authorizes_send is False
    receipt = FakeEffortlessPromoDeliveryPort(now=NOW).request_delivery(_request())
    assert receipt.authorizes_send is False


# --- authorize_delivery: fail-closed --------------------------------------------------------------


def test_callback_enforcing_host_allows_the_request():
    caps = FakeEffortlessPromoDeliveryPort(now=NOW).capabilities(BUSINESS_A)
    decision = authorize_delivery(caps, _context(), now=NOW)
    assert decision.allowed is True
    assert decision.reason == DeliveryDecision.OK
    assert decision.authorizes_send is False


def test_missing_callback_enforcement_disables_delivery():
    # The acceptance criterion: a host that does not enforce callbacks disables live delivery.
    caps = FakeEffortlessPromoDeliveryPort(callback_enforcement=False, now=NOW).capabilities(
        BUSINESS_A
    )
    assert caps.enforces_callbacks is False
    decision = authorize_delivery(caps, _context(), now=NOW)
    assert decision.allowed is False
    assert decision.reason == DeliveryDecision.CALLBACK_ENFORCEMENT_UNAVAILABLE


def test_absent_or_unavailable_host_cannot_authorize():
    assert authorize_delivery(None, _context(), now=NOW).reason == DeliveryDecision.HOST_UNAVAILABLE
    caps = FakeEffortlessPromoDeliveryPort(available=False, now=NOW).capabilities(BUSINESS_A)
    assert caps.availability is DeliveryHostAvailability.UNAVAILABLE
    assert authorize_delivery(caps, _context(), now=NOW).reason == (
        DeliveryDecision.HOST_UNAVAILABLE
    )


def test_stale_capabilities_cannot_authorize_even_if_callback_enforcing():
    stale = FakeEffortlessPromoDeliveryPort(now=NOW - timedelta(hours=2)).capabilities(BUSINESS_A)
    assert stale.enforces_callbacks is True  # it claimed callbacks, but the claim is old
    decision = authorize_delivery(stale, _context(), now=NOW)
    assert decision.allowed is False
    assert decision.reason == DeliveryDecision.STALE


def test_freshness_boundary_respects_max_age():
    caps = FakeEffortlessPromoDeliveryPort(now=NOW - timedelta(minutes=45)).capabilities(BUSINESS_A)
    assert authorize_delivery(caps, _context(), now=NOW).allowed is True  # within default 1h
    assert (
        authorize_delivery(caps, _context(), now=NOW, max_age=timedelta(minutes=30)).reason
        == DeliveryDecision.STALE
    )


def test_unsupported_channel_is_refused():
    caps = FakeEffortlessPromoDeliveryPort(now=NOW).capabilities(BUSINESS_A)
    decision = authorize_delivery(caps, _context(channel="whatsapp"), now=NOW)
    assert decision.allowed is False
    assert decision.reason == DeliveryDecision.CHANNEL_NOT_SUPPORTED


# --- request_delivery through the fake: accept vs disabled ----------------------------------------


def test_request_through_callback_enforcing_host_is_accepted_not_sent():
    port = FakeEffortlessPromoDeliveryPort(now=NOW)
    receipt = port.request_delivery(_request())
    assert receipt.accepted is True
    assert receipt.status == DeliveryReceipt.ACCEPTED
    assert receipt.reason == DeliveryDecision.OK
    assert receipt.callback_enforced is True
    assert receipt.host_reference  # the platform took it into its pipeline
    assert receipt.authorizes_send is False  # accepting is not sending


def test_request_through_non_callback_host_is_disabled_not_bypassed():
    # Send through a fake host WITHOUT callback support: live delivery is disabled, nothing is accepted.
    port = FakeEffortlessPromoDeliveryPort(callback_enforcement=False, now=NOW)
    receipt = port.request_delivery(_request())
    assert receipt.accepted is False
    assert receipt.status == DeliveryReceipt.DISABLED
    assert receipt.reason == DeliveryDecision.CALLBACK_ENFORCEMENT_UNAVAILABLE
    assert receipt.callback_enforced is False
    assert receipt.host_reference == ""  # nothing was taken into any pipeline


def test_request_through_unavailable_host_is_disabled():
    port = FakeEffortlessPromoDeliveryPort(available=False, now=NOW)
    receipt = port.request_delivery(_request())
    assert receipt.accepted is False
    assert receipt.status == DeliveryReceipt.DISABLED
    assert receipt.reason == DeliveryDecision.HOST_UNAVAILABLE


def test_request_is_idempotent_in_its_key():
    port = FakeEffortlessPromoDeliveryPort(now=NOW)
    first = port.request_delivery(_request("same-key"))
    second = port.request_delivery(_request("same-key"))
    assert first.host_reference == second.host_reference
    assert first.idempotency_key == second.idempotency_key == "same-key"


def test_request_rejects_an_empty_idempotency_key():
    with pytest.raises(ValueError):
        DeliveryRequest(idempotency_key="", context=_context())


# --- production refuses the fake ------------------------------------------------------------------


def test_production_refuses_the_fake():
    with pytest.raises(DeliveryContractDisabled):
        build_delivery_port(make_settings(environment="production"))


def test_development_builds_the_fake():
    port = build_delivery_port(make_settings(environment="development"))
    assert isinstance(port, FakeEffortlessPromoDeliveryPort)


def test_capabilities_snapshot_is_immutable():
    caps = DeliveryCapabilities(
        business_id=BUSINESS_A,
        availability=DeliveryHostAvailability.AVAILABLE,
        version="v1",
        as_of=NOW,
        callback_enforcement=True,
    )
    with pytest.raises((ValueError, TypeError)):
        caps.callback_enforcement = False  # type: ignore[misc]
