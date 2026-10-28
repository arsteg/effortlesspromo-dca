"""DCA-079 — authoritative suppression lookup + the synthetic platform-suppression fake.

The snapshot discloses version, freshness and affected scope; the fake is deterministic, visibly
synthetic, and refused in production. The load-bearing guarantee: an **unavailable** or **stale**
authoritative answer produces NO activation permission, and an allow is never recipient consent (the
host owns both suppression and consent).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.identity.fake import BUSINESS_A, BUSINESS_B
from app.suppression import (
    ActivationDecision,
    FakePlatformSuppressionClient,
    PlatformSuppressionClient,
    SuppressionAvailability,
    SuppressionContractDisabled,
    SuppressionLookupRequest,
    SuppressionSnapshot,
    authorize_activation,
    build_platform_suppression_client,
)
from app.suppression.fake import DEFAULT_COVERED_CHANNELS, SYNTHETIC_VERSION
from tests.conftest import make_settings

NOW = datetime(2026, 6, 1, tzinfo=UTC)

# Fixtures from app/suppression/fake.py: A has these suppressed; B has nothing suppressed.
SUPPRESSED_EMAIL = "opted-out@synthetic-a.example"
CLEAR_EMAIL = "prospect@synthetic-a.example"


def _lookup(client: FakePlatformSuppressionClient, business_id: str) -> SuppressionSnapshot:
    return client.lookup(SuppressionLookupRequest(business_id=business_id))


# --- the fake + typed snapshot (version / freshness / affected scope) -----------------------------


def test_fake_is_a_platform_suppression_client():
    assert isinstance(FakePlatformSuppressionClient(), PlatformSuppressionClient)


def test_snapshot_discloses_version_freshness_and_affected_scope():
    snap = _lookup(FakePlatformSuppressionClient(now=NOW), BUSINESS_A)
    assert snap.availability is SuppressionAvailability.AVAILABLE
    assert snap.version == SYNTHETIC_VERSION  # version
    assert snap.as_of == NOW  # freshness
    assert snap.covered_channels == DEFAULT_COVERED_CHANNELS  # affected scope (channels)
    assert snap.is_suppressed(SUPPRESSED_EMAIL, "email")  # affected scope (entries)
    assert not snap.is_suppressed(CLEAR_EMAIL, "email")


def test_host_consent_ownership_is_explicit_on_the_snapshot():
    snap = _lookup(FakePlatformSuppressionClient(now=NOW), BUSINESS_B)
    assert snap.host_owns_consent is True


def test_snapshots_are_visibly_synthetic():
    client = FakePlatformSuppressionClient(now=NOW)
    for business_id in (BUSINESS_A, BUSINESS_B, "stranger"):
        snap = _lookup(client, business_id)
        assert snap.synthetic is True
        assert snap.version == SYNTHETIC_VERSION


def test_suppression_match_is_case_insensitive():
    snap = _lookup(FakePlatformSuppressionClient(now=NOW), BUSINESS_A)
    assert snap.is_suppressed("OPTED-OUT@Synthetic-A.Example", "Email")


# --- authorize_activation: the allow path ---------------------------------------------------------


def test_available_fresh_and_not_suppressed_permits_activation():
    snap = _lookup(FakePlatformSuppressionClient(now=NOW), BUSINESS_A)
    decision = authorize_activation(snap, CLEAR_EMAIL, "email", now=NOW)
    assert decision.allowed is True
    assert decision.reason == ActivationDecision.OK


def test_clean_business_permits_activation():
    snap = _lookup(FakePlatformSuppressionClient(now=NOW), BUSINESS_B)
    decision = authorize_activation(snap, CLEAR_EMAIL, "email", now=NOW)
    assert decision.allowed is True


def test_an_allow_is_not_recipient_consent():
    snap = _lookup(FakePlatformSuppressionClient(now=NOW), BUSINESS_B)
    decision = authorize_activation(snap, CLEAR_EMAIL, "email", now=NOW)
    assert decision.allowed is True
    # Clearing the suppression blocker never asserts consent — the host owns that.
    assert decision.grants_consent is False


# --- authorize_activation: fail-closed ------------------------------------------------------------


def test_suppressed_contact_point_denies_activation():
    snap = _lookup(FakePlatformSuppressionClient(now=NOW), BUSINESS_A)
    decision = authorize_activation(snap, SUPPRESSED_EMAIL, "email", now=NOW)
    assert decision.allowed is False
    assert decision.reason == ActivationDecision.SUPPRESSED


def test_unavailable_lookup_produces_no_activation_permission():
    # The acceptance criterion: an UNAVAILABLE authoritative answer clears nothing, even for a contact
    # point that has no suppression entry (absence of a record is not evidence of its absence).
    snap = _lookup(FakePlatformSuppressionClient(available=False, now=NOW), BUSINESS_A)
    assert snap.availability is SuppressionAvailability.UNAVAILABLE
    decision = authorize_activation(snap, CLEAR_EMAIL, "email", now=NOW)
    assert decision.allowed is False
    assert decision.reason == ActivationDecision.LOOKUP_UNAVAILABLE


def test_absent_snapshot_produces_no_activation_permission():
    decision = authorize_activation(None, CLEAR_EMAIL, "email", now=NOW)
    assert decision.allowed is False
    assert decision.reason == ActivationDecision.LOOKUP_UNAVAILABLE


def test_stale_snapshot_denies_even_when_not_suppressed():
    # A clean, available snapshot fetched 2h ago is stale (default max age 1h) => refused, because an
    # unsubscribe could have been recorded in the interval.
    stale = _lookup(FakePlatformSuppressionClient(now=NOW - timedelta(hours=2)), BUSINESS_B)
    assert stale.availability is SuppressionAvailability.AVAILABLE
    decision = authorize_activation(stale, CLEAR_EMAIL, "email", now=NOW)
    assert decision.allowed is False
    assert decision.reason == ActivationDecision.STALE


def test_freshness_boundary_respects_max_age():
    snap = _lookup(FakePlatformSuppressionClient(now=NOW - timedelta(minutes=30)), BUSINESS_B)
    # Within a 1h window the same snapshot clears; tighten to 15m and it is stale.
    assert authorize_activation(snap, CLEAR_EMAIL, "email", now=NOW).allowed is True
    assert (
        authorize_activation(
            snap, CLEAR_EMAIL, "email", now=NOW, max_age=timedelta(minutes=15)
        ).reason
        == ActivationDecision.STALE
    )


def test_channel_outside_authoritative_scope_is_refused():
    # The snapshot only speaks for email/sms; a channel it does not cover cannot be cleared.
    snap = _lookup(FakePlatformSuppressionClient(now=NOW), BUSINESS_B)
    decision = authorize_activation(snap, CLEAR_EMAIL, "whatsapp", now=NOW)
    assert decision.allowed is False
    assert decision.reason == ActivationDecision.CHANNEL_NOT_COVERED


def test_fail_closed_order_unavailability_beats_everything():
    # Even for a channel it would not cover and a contact point that is suppressed, an unavailable
    # answer reports unavailability first — the strongest fail-closed reason.
    snap = _lookup(FakePlatformSuppressionClient(available=False, now=NOW), BUSINESS_A)
    assert (
        authorize_activation(snap, SUPPRESSED_EMAIL, "whatsapp", now=NOW).reason
        == ActivationDecision.LOOKUP_UNAVAILABLE
    )


# --- production refuses the fake ------------------------------------------------------------------


def test_production_refuses_the_fake():
    with pytest.raises(SuppressionContractDisabled):
        build_platform_suppression_client(make_settings(environment="production"))


def test_development_builds_the_fake():
    client = build_platform_suppression_client(make_settings(environment="development"))
    assert isinstance(client, FakePlatformSuppressionClient)
