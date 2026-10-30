"""DCA-097 — the cancellation adapter contract + the synthetic EffortlessPromo cancellation fake.

Request cancellation of queued delivery ONLY through EffortlessPromo: a typed request (a cancellation
idempotency key + the queued-work pointers + the invalidation reason), a one-method port
(``request_cancellation`` — no recall/unsend/provider path), a fail-closed decision, and a dev/test-only
fake refused in production. The load-bearing behaviours:

- already-provider-accepted work is LABELED non-recallable; only still-queued work is CANCELED;
- a host that does NOT acknowledge cancellations DISABLES the request (and leaves the queue untouched)
  instead of being treated as a silent success.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.delivery import (
    CancellationCapabilities,
    CancellationContractDisabled,
    CancellationDecision,
    CancellationHostAvailability,
    CancellationItemStatus,
    CancellationPort,
    CancellationReceipt,
    CancellationRequest,
    FakeEffortlessPromoCancellationPort,
    QueuedWorkRef,
    QueuedWorkState,
    authorize_cancellation,
    build_cancellation_port,
)
from app.delivery.cancellation_fake import SYNTHETIC_VERSION
from app.identity.fake import BUSINESS_A
from tests.conftest import make_settings

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _ref(key: str, channel: str = "email") -> QueuedWorkRef:
    """A pointer to one piece of queued host work — recipient as a one-way digest only."""
    return QueuedWorkRef(
        idempotency_key=key,
        channel=channel,
        recipient_digest=f"sha256-digest-of-{key}",
        consumption_id=f"consumption-{key}",
    )


def _request(*keys: str, cancellation_key: str = "cancel-097") -> CancellationRequest:
    return CancellationRequest(
        business_id=BUSINESS_A,
        cancellation_key=cancellation_key,
        invalidation_reason="consent_withdrawn",
        items=tuple(_ref(k) for k in keys),
    )


# --- the fake + typed shapes ----------------------------------------------------------------------


def test_fake_is_a_cancellation_port():
    assert isinstance(FakeEffortlessPromoCancellationPort(), CancellationPort)


def test_port_has_no_recall_or_provider_path():
    # The DCA may only REQUEST cancellation. There is deliberately no recall/unsend/provider surface,
    # on the port protocol or on the fake implementation.
    assert "request_cancellation" in dir(FakeEffortlessPromoCancellationPort)
    for forbidden in ("recall", "unsend", "send", "deliver", "provider", "transmit"):
        assert forbidden not in dir(FakeEffortlessPromoCancellationPort)
        assert forbidden not in dir(CancellationPort)


def test_capabilities_are_visibly_synthetic_and_advertise_acknowledgment():
    caps = FakeEffortlessPromoCancellationPort(now=NOW).capabilities(BUSINESS_A)
    assert caps.synthetic is True
    assert caps.version == SYNTHETIC_VERSION
    assert caps.host_owns_send is True
    assert caps.is_available is True
    assert caps.offers_cancellation is True
    assert caps.acknowledges_cancellations is True


def test_receipt_never_authorizes_a_send():
    port = FakeEffortlessPromoCancellationPort(queued={"k1": QueuedWorkState.QUEUED}, now=NOW)
    receipt = port.request_cancellation(_request("k1"))
    assert receipt.authorizes_send is False


# --- authorize_cancellation: fail-closed ----------------------------------------------------------


def test_acknowledging_host_allows_the_request():
    caps = FakeEffortlessPromoCancellationPort(now=NOW).capabilities(BUSINESS_A)
    decision = authorize_cancellation(caps, now=NOW)
    assert decision.allowed is True
    assert decision.reason == CancellationDecision.OK
    assert decision.authorizes_send is False


def test_missing_acknowledgment_disables_cancellation():
    # The acceptance failure case: a host that does not acknowledge cancellations disables the request.
    caps = FakeEffortlessPromoCancellationPort(acknowledges=False, now=NOW).capabilities(BUSINESS_A)
    assert caps.acknowledges_cancellations is False
    decision = authorize_cancellation(caps, now=NOW)
    assert decision.allowed is False
    assert decision.reason == CancellationDecision.ACKNOWLEDGMENT_NOT_GUARANTEED


def test_unsupported_cancellation_is_refused():
    caps = FakeEffortlessPromoCancellationPort(supports_cancellation=False, now=NOW).capabilities(
        BUSINESS_A
    )
    assert caps.offers_cancellation is False
    decision = authorize_cancellation(caps, now=NOW)
    assert decision.allowed is False
    assert decision.reason == CancellationDecision.CANCELLATION_NOT_SUPPORTED


def test_absent_or_unavailable_host_cannot_authorize():
    assert authorize_cancellation(None, now=NOW).reason == CancellationDecision.HOST_UNAVAILABLE
    caps = FakeEffortlessPromoCancellationPort(available=False, now=NOW).capabilities(BUSINESS_A)
    assert caps.availability is CancellationHostAvailability.UNAVAILABLE
    assert authorize_cancellation(caps, now=NOW).reason == CancellationDecision.HOST_UNAVAILABLE


def test_stale_capabilities_cannot_authorize_even_if_acknowledging():
    stale = FakeEffortlessPromoCancellationPort(now=NOW - timedelta(hours=2)).capabilities(
        BUSINESS_A
    )
    assert stale.acknowledges_cancellations is True  # claimed ack, but the claim is old
    decision = authorize_cancellation(stale, now=NOW)
    assert decision.allowed is False
    assert decision.reason == CancellationDecision.STALE


def test_freshness_boundary_respects_max_age():
    caps = FakeEffortlessPromoCancellationPort(now=NOW - timedelta(minutes=45)).capabilities(
        BUSINESS_A
    )
    assert authorize_cancellation(caps, now=NOW).allowed is True  # within default 1h
    assert (
        authorize_cancellation(caps, now=NOW, max_age=timedelta(minutes=30)).reason
        == CancellationDecision.STALE
    )


# --- request_cancellation through the fake: cancel vs non-recallable vs disabled ------------------


def test_queued_work_is_canceled_and_provider_accepted_is_labeled_non_recallable():
    # The core acceptance behaviour: a mix of still-queued and already-provider-accepted work.
    port = FakeEffortlessPromoCancellationPort(
        queued={
            "still-queued": QueuedWorkState.QUEUED,
            "already-sent": QueuedWorkState.PROVIDER_ACCEPTED,
        },
        now=NOW,
    )

    # "unknown-key" is deliberately NOT seeded into the queue -> reported NOT_FOUND, never fabricated.
    receipt = port.request_cancellation(_request("still-queued", "already-sent", "unknown-key"))

    assert receipt.acknowledged is True
    assert receipt.status == CancellationReceipt.ACKNOWLEDGED
    by_key = {o.idempotency_key: o for o in receipt.outcomes}

    assert by_key["still-queued"].status is CancellationItemStatus.CANCELED
    assert by_key["still-queued"].recallable is True

    assert by_key["already-sent"].status is CancellationItemStatus.NON_RECALLABLE
    assert by_key["already-sent"].recallable is False

    assert by_key["unknown-key"].status is CancellationItemStatus.NOT_FOUND

    assert receipt.canceled_count == 1
    assert receipt.non_recallable_count == 1

    # The queued item is actually gone; the provider-accepted one is left in place (not pretended away).
    assert port.queue_state("still-queued") is None
    assert port.queue_state("already-sent") is QueuedWorkState.PROVIDER_ACCEPTED


def test_disabled_request_changes_nothing_on_the_host():
    # Acceptance: a non-acknowledging host DISABLES the request and the queue is left UNTOUCHED.
    port = FakeEffortlessPromoCancellationPort(
        queued={"still-queued": QueuedWorkState.QUEUED}, acknowledges=False, now=NOW
    )
    receipt = port.request_cancellation(_request("still-queued"))
    assert receipt.acknowledged is False
    assert receipt.status == CancellationReceipt.DISABLED
    assert receipt.reason == CancellationDecision.ACKNOWLEDGMENT_NOT_GUARANTEED
    assert receipt.outcomes == ()
    assert receipt.acknowledgment_reference == ""
    # Nothing was canceled — the still-queued work is exactly where it was.
    assert port.queue_state("still-queued") is QueuedWorkState.QUEUED


def test_unavailable_host_is_disabled_and_untouched():
    port = FakeEffortlessPromoCancellationPort(
        queued={"still-queued": QueuedWorkState.QUEUED}, available=False, now=NOW
    )
    receipt = port.request_cancellation(_request("still-queued"))
    assert receipt.acknowledged is False
    assert receipt.status == CancellationReceipt.DISABLED
    assert receipt.reason == CancellationDecision.HOST_UNAVAILABLE
    assert port.queue_state("still-queued") is QueuedWorkState.QUEUED


def test_cancellation_is_idempotent_in_its_key():
    # Acknowledgment tracking: a replayed cancellation returns the SAME receipt verbatim, and a
    # re-run never re-reads the now-empty queue into a different (NOT_FOUND) answer.
    port = FakeEffortlessPromoCancellationPort(
        queued={"still-queued": QueuedWorkState.QUEUED}, now=NOW
    )
    first = port.request_cancellation(_request("still-queued", cancellation_key="same-cancel"))
    assert first.outcomes[0].status is CancellationItemStatus.CANCELED
    assert port.queue_state("still-queued") is None  # canceled on the first pass

    second = port.request_cancellation(_request("still-queued", cancellation_key="same-cancel"))
    assert second.acknowledgment_reference == first.acknowledgment_reference
    assert second.outcomes[0].status is CancellationItemStatus.CANCELED  # not NOT_FOUND


def test_disabled_request_is_not_ledgered_so_a_later_retry_can_succeed():
    # A disabled request must NOT be remembered as the answer — once the host recovers, the same
    # cancellation key may retry and succeed.
    port = FakeEffortlessPromoCancellationPort(
        queued={"still-queued": QueuedWorkState.QUEUED}, acknowledges=False, now=NOW
    )
    disabled = port.request_cancellation(_request("still-queued", cancellation_key="retry-key"))
    assert disabled.status == CancellationReceipt.DISABLED

    # Host recovers; the DCA retries with the same key.
    port._acknowledges = True  # noqa: SLF001 - simulate the host regaining the guarantee
    retried = port.request_cancellation(_request("still-queued", cancellation_key="retry-key"))
    assert retried.acknowledged is True
    assert retried.outcomes[0].status is CancellationItemStatus.CANCELED


def test_request_rejects_an_empty_cancellation_key():
    with pytest.raises(ValueError):
        CancellationRequest(
            business_id=BUSINESS_A,
            cancellation_key="",
            invalidation_reason="consent_withdrawn",
            items=(_ref("k1"),),
        )


def test_request_rejects_an_empty_item_list():
    with pytest.raises(ValueError):
        CancellationRequest(
            business_id=BUSINESS_A,
            cancellation_key="cancel-x",
            invalidation_reason="consent_withdrawn",
            items=(),
        )


# --- production refuses the fake ------------------------------------------------------------------


def test_production_refuses_the_fake():
    with pytest.raises(CancellationContractDisabled):
        build_cancellation_port(make_settings(environment="production"))


def test_development_builds_the_fake():
    port = build_cancellation_port(make_settings(environment="development"))
    assert isinstance(port, FakeEffortlessPromoCancellationPort)


def test_capabilities_snapshot_is_immutable():
    caps = CancellationCapabilities(
        business_id=BUSINESS_A,
        availability=CancellationHostAvailability.AVAILABLE,
        version="v1",
        as_of=NOW,
        supports_cancellation=True,
        acknowledges=True,
    )
    with pytest.raises((ValueError, TypeError)):
        caps.acknowledges = False  # type: ignore[misc]
