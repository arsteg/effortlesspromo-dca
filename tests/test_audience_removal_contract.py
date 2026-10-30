"""DCA-098 — the advertising-audience removal contract + the synthetic EffortlessPromo removal fake.

Request removal of a revoked, previously-uploaded audience member ONLY through EffortlessPromo: a typed
request (a removal idempotency key + hashed-member pointers + the withdrawal reason), a one-method port
(``request_removal`` — no upload/add/sync/provider path), a fail-closed decision, and a dev/test-only
fake refused in production. The load-bearing behaviours (the acceptance criteria):

- the withdrawal is authoritative — the prior upload never overrides it (a member whose removal is still
  pending reads as ``withdrawn``, never reinstated);
- removal failures stay VISIBLE and PENDING — a host that is unavailable/non-acknowledging, or a
  per-member transient failure, leaves the removal owed (reported PENDING, audience untouched), never
  faked as done; only a fully-confirmed removal is COMPLETED and ledgered (idempotent).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.advertising import (
    AudienceMemberRef,
    AudienceMemberState,
    AudienceRemovalContractDisabled,
    FakeEffortlessPromoAudienceRemovalPort,
    RemovalCapabilities,
    RemovalDecision,
    RemovalHostAvailability,
    RemovalItemStatus,
    RemovalPort,
    RemovalReceipt,
    RemovalRequest,
    authorize_removal,
    build_removal_port,
)
from app.advertising.audience_removal_fake import SYNTHETIC_VERSION
from app.compliance.customer_match_eligibility import CUSTOMER_MATCH_CHANNEL
from app.identity.fake import BUSINESS_A
from tests.conftest import make_settings

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _ref(digest: str, key_type: str = "email") -> AudienceMemberRef:
    """A pointer to one previously-uploaded member — hashed digest only, never the raw identifier."""
    return AudienceMemberRef(
        member_digest=digest,
        key_type=key_type,
        source_membership_id=f"membership-{digest}",
    )


def _request(*digests: str, removal_key: str = "remove-098") -> RemovalRequest:
    return RemovalRequest(
        business_id=BUSINESS_A,
        removal_key=removal_key,
        withdrawal_reason="consent_withdrawn",
        audience_channel=CUSTOMER_MATCH_CHANNEL,
        audience_name="synthetic-prospects",
        members=tuple(_ref(d) for d in digests),
    )


# --- the fake + typed shapes ----------------------------------------------------------------------


def test_fake_is_a_removal_port():
    assert isinstance(FakeEffortlessPromoAudienceRemovalPort(), RemovalPort)


def test_port_has_no_upload_or_provider_path():
    # The DCA may only REQUEST removal. There is deliberately no upload/add/sync/provider surface, on the
    # port protocol or on the fake implementation.
    assert "request_removal" in dir(FakeEffortlessPromoAudienceRemovalPort)
    for forbidden in ("upload", "add", "add_member", "sync", "send", "provider", "transmit"):
        assert forbidden not in dir(FakeEffortlessPromoAudienceRemovalPort)
        assert forbidden not in dir(RemovalPort)


def test_capabilities_are_visibly_synthetic_and_advertise_acknowledgment():
    caps = FakeEffortlessPromoAudienceRemovalPort(now=NOW).capabilities(BUSINESS_A)
    assert caps.synthetic is True
    assert caps.version == SYNTHETIC_VERSION
    assert caps.host_owns_audience is True
    assert caps.is_available is True
    assert caps.offers_removal is True
    assert caps.acknowledges_removals is True
    assert caps.supports_audience(CUSTOMER_MATCH_CHANNEL) is True


def test_receipt_never_authorizes_a_send_and_always_upholds_withdrawal():
    port = FakeEffortlessPromoAudienceRemovalPort(
        audience={"m1": AudienceMemberState.PRESENT}, now=NOW
    )
    receipt = port.request_removal(_request("m1"))
    assert receipt.authorizes_send is False
    assert receipt.withdrawal_upheld is True


# --- authorize_removal: fail-closed ---------------------------------------------------------------


def test_acknowledging_host_allows_the_request():
    caps = FakeEffortlessPromoAudienceRemovalPort(now=NOW).capabilities(BUSINESS_A)
    decision = authorize_removal(caps, CUSTOMER_MATCH_CHANNEL, now=NOW)
    assert decision.allowed is True
    assert decision.reason == RemovalDecision.OK
    assert decision.authorizes_send is False


def test_missing_acknowledgment_disables_the_request():
    caps = FakeEffortlessPromoAudienceRemovalPort(acknowledges=False, now=NOW).capabilities(
        BUSINESS_A
    )
    assert caps.acknowledges_removals is False
    decision = authorize_removal(caps, CUSTOMER_MATCH_CHANNEL, now=NOW)
    assert decision.allowed is False
    assert decision.reason == RemovalDecision.ACKNOWLEDGMENT_NOT_GUARANTEED


def test_unsupported_removal_is_refused():
    caps = FakeEffortlessPromoAudienceRemovalPort(supports_removal=False, now=NOW).capabilities(
        BUSINESS_A
    )
    assert caps.offers_removal is False
    decision = authorize_removal(caps, CUSTOMER_MATCH_CHANNEL, now=NOW)
    assert decision.allowed is False
    assert decision.reason == RemovalDecision.REMOVAL_NOT_SUPPORTED


def test_unsupported_audience_is_refused():
    caps = FakeEffortlessPromoAudienceRemovalPort(now=NOW).capabilities(BUSINESS_A)
    decision = authorize_removal(caps, "some_other_audience", now=NOW)
    assert decision.allowed is False
    assert decision.reason == RemovalDecision.AUDIENCE_NOT_SUPPORTED


def test_absent_or_unavailable_host_cannot_authorize():
    assert (
        authorize_removal(None, CUSTOMER_MATCH_CHANNEL, now=NOW).reason
        == RemovalDecision.HOST_UNAVAILABLE
    )
    caps = FakeEffortlessPromoAudienceRemovalPort(available=False, now=NOW).capabilities(BUSINESS_A)
    assert caps.availability is RemovalHostAvailability.UNAVAILABLE
    assert (
        authorize_removal(caps, CUSTOMER_MATCH_CHANNEL, now=NOW).reason
        == RemovalDecision.HOST_UNAVAILABLE
    )


def test_stale_capabilities_cannot_authorize_even_if_acknowledging():
    stale = FakeEffortlessPromoAudienceRemovalPort(now=NOW - timedelta(hours=2)).capabilities(
        BUSINESS_A
    )
    assert stale.acknowledges_removals is True  # claimed ack, but the claim is old
    decision = authorize_removal(stale, CUSTOMER_MATCH_CHANNEL, now=NOW)
    assert decision.allowed is False
    assert decision.reason == RemovalDecision.STALE


def test_freshness_boundary_respects_max_age():
    caps = FakeEffortlessPromoAudienceRemovalPort(now=NOW - timedelta(minutes=45)).capabilities(
        BUSINESS_A
    )
    assert authorize_removal(caps, CUSTOMER_MATCH_CHANNEL, now=NOW).allowed is True  # within 1h
    assert (
        authorize_removal(
            caps, CUSTOMER_MATCH_CHANNEL, now=NOW, max_age=timedelta(minutes=30)
        ).reason
        == RemovalDecision.STALE
    )


# --- request_removal through the fake: removed vs not-present vs pending ---------------------------


def test_present_member_is_removed_and_absent_member_is_not_present():
    # The success behaviour: a present member is removed; an already-removed / never-uploaded member is
    # NOT_PRESENT (never fabricated as a removal).
    port = FakeEffortlessPromoAudienceRemovalPort(
        audience={
            "present-member": AudienceMemberState.PRESENT,
            "already-removed": AudienceMemberState.REMOVED,
        },
        now=NOW,
    )
    receipt = port.request_removal(_request("present-member", "already-removed", "never-uploaded"))

    assert receipt.acknowledged is True
    assert receipt.status == RemovalReceipt.COMPLETED
    assert receipt.withdrawal_upheld is True
    by_digest = {o.member_digest: o for o in receipt.outcomes}

    assert by_digest["present-member"].status is RemovalItemStatus.REMOVED
    assert by_digest["present-member"].removed is True
    assert by_digest["already-removed"].status is RemovalItemStatus.NOT_PRESENT
    assert by_digest["never-uploaded"].status is RemovalItemStatus.NOT_PRESENT

    assert receipt.removed_count == 1
    assert receipt.not_present_count == 2
    assert receipt.pending_count == 0
    assert receipt.has_pending is False

    # The present member is actually gone from the audience; nothing is owed.
    assert port.audience_state("present-member") is AudienceMemberState.REMOVED
    assert port.owed_removals == frozenset()
    # Every named member reads as withdrawn (authoritative).
    assert port.membership("present-member") == "withdrawn"


def test_removal_failure_stays_visible_and_pending_prior_upload_does_not_override():
    # THE acceptance criterion: a temporary platform failure on a present member leaves the removal
    # PENDING (visible, owed), the member still in the audience, yet the DCA's view is already WITHDRAWN —
    # the prior upload never overrides the current withdrawal.
    port = FakeEffortlessPromoAudienceRemovalPort(
        audience={"revoked-member": AudienceMemberState.PRESENT},
        transient_failures={"revoked-member"},
        now=NOW,
    )
    receipt = port.request_removal(_request("revoked-member"))

    assert receipt.status == RemovalReceipt.PENDING
    assert receipt.has_pending is True
    assert receipt.pending_count == 1
    assert receipt.outcomes[0].status is RemovalItemStatus.PENDING
    assert receipt.outcomes[0].removed is False

    # The removal is still owed and visible; the member is left in the audience (not faked as removed).
    assert "revoked-member" in port.owed_removals
    assert port.audience_state("revoked-member") is AudienceMemberState.PRESENT
    # ...but the withdrawal is authoritative: the prior upload does NOT reinstate membership.
    assert receipt.withdrawal_upheld is True
    assert port.membership("revoked-member") == "withdrawn"

    # Host recovers; retrying the SAME key re-attempts and now completes (pending request was not ledgered).
    port.recover("revoked-member")
    retried = port.request_removal(_request("revoked-member"))
    assert retried.status == RemovalReceipt.COMPLETED
    assert retried.outcomes[0].status is RemovalItemStatus.REMOVED
    assert port.audience_state("revoked-member") is AudienceMemberState.REMOVED
    assert port.owed_removals == frozenset()


def test_unavailable_host_leaves_whole_request_pending_and_audience_untouched():
    # Host-level failure case: an unavailable host makes the WHOLE request PENDING, every member owed and
    # reported PENDING, and the audience is left exactly as it was — never a silent success.
    port = FakeEffortlessPromoAudienceRemovalPort(
        audience={"revoked-member": AudienceMemberState.PRESENT}, available=False, now=NOW
    )
    receipt = port.request_removal(_request("revoked-member"))

    assert receipt.acknowledged is False
    assert receipt.status == RemovalReceipt.PENDING
    assert receipt.reason == RemovalDecision.HOST_UNAVAILABLE
    assert receipt.outcomes[0].status is RemovalItemStatus.PENDING  # visible, not dropped
    assert "revoked-member" in port.owed_removals
    assert port.audience_state("revoked-member") is AudienceMemberState.PRESENT  # untouched
    # Still authoritative withdrawal even though the host never confirmed.
    assert receipt.withdrawal_upheld is True
    assert port.membership("revoked-member") == "withdrawn"


def test_non_acknowledging_host_leaves_removal_pending():
    port = FakeEffortlessPromoAudienceRemovalPort(
        audience={"revoked-member": AudienceMemberState.PRESENT}, acknowledges=False, now=NOW
    )
    receipt = port.request_removal(_request("revoked-member"))
    assert receipt.status == RemovalReceipt.PENDING
    assert receipt.reason == RemovalDecision.ACKNOWLEDGMENT_NOT_GUARANTEED
    assert "revoked-member" in port.owed_removals
    assert port.audience_state("revoked-member") is AudienceMemberState.PRESENT


def test_completed_removal_is_idempotent_in_its_key():
    # A replayed COMPLETED removal returns the SAME receipt verbatim, and a re-run never re-reads the
    # now-removed member into a different (NOT_PRESENT) answer.
    port = FakeEffortlessPromoAudienceRemovalPort(
        audience={"m": AudienceMemberState.PRESENT}, now=NOW
    )
    first = port.request_removal(_request("m", removal_key="same-removal"))
    assert first.outcomes[0].status is RemovalItemStatus.REMOVED
    assert port.audience_state("m") is AudienceMemberState.REMOVED

    second = port.request_removal(_request("m", removal_key="same-removal"))
    assert second.acknowledgment_reference == first.acknowledgment_reference
    assert second.outcomes[0].status is RemovalItemStatus.REMOVED  # not re-read as NOT_PRESENT


def test_pending_request_is_not_ledgered_so_a_later_retry_can_succeed():
    # A pending request must NOT be remembered as the answer — once the host recovers, the same removal
    # key may retry and succeed.
    port = FakeEffortlessPromoAudienceRemovalPort(
        audience={"m": AudienceMemberState.PRESENT}, acknowledges=False, now=NOW
    )
    pending = port.request_removal(_request("m", removal_key="retry-key"))
    assert pending.status == RemovalReceipt.PENDING

    port._acknowledges = True  # noqa: SLF001 - simulate the host regaining the guarantee
    retried = port.request_removal(_request("m", removal_key="retry-key"))
    assert retried.status == RemovalReceipt.COMPLETED
    assert retried.outcomes[0].status is RemovalItemStatus.REMOVED


def test_request_rejects_an_empty_removal_key():
    with pytest.raises(ValueError):
        RemovalRequest(
            business_id=BUSINESS_A,
            removal_key="",
            withdrawal_reason="consent_withdrawn",
            audience_channel=CUSTOMER_MATCH_CHANNEL,
            members=(_ref("m"),),
        )


def test_request_rejects_an_empty_member_list():
    with pytest.raises(ValueError):
        RemovalRequest(
            business_id=BUSINESS_A,
            removal_key="remove-x",
            withdrawal_reason="consent_withdrawn",
            audience_channel=CUSTOMER_MATCH_CHANNEL,
            members=(),
        )


def test_ref_rejects_unexpected_fields_and_carries_no_raw_identifier():
    # extra="forbid" keeps a raw address from sneaking into a member pointer.
    with pytest.raises(ValueError):
        AudienceMemberRef(
            member_digest="m",
            key_type="email",
            raw_email="person@example.com",  # type: ignore[call-arg]
        )


# --- production refuses the fake ------------------------------------------------------------------


def test_production_refuses_the_fake():
    with pytest.raises(AudienceRemovalContractDisabled):
        build_removal_port(make_settings(environment="production"))


def test_development_builds_the_fake():
    port = build_removal_port(make_settings(environment="development"))
    assert isinstance(port, FakeEffortlessPromoAudienceRemovalPort)


def test_capabilities_snapshot_is_immutable():
    caps = RemovalCapabilities(
        business_id=BUSINESS_A,
        availability=RemovalHostAvailability.AVAILABLE,
        version="v1",
        as_of=NOW,
        supports_removal=True,
        acknowledges=True,
    )
    with pytest.raises((ValueError, TypeError)):
        caps.acknowledges = False  # type: ignore[misc]
