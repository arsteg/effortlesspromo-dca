"""DCA-022 — subscription-entitlement snapshots + the synthetic platform fake.

Typed snapshots carry expiry, allowed capabilities and plan limits; the fake is deterministic,
visibly synthetic, and refused in production. An absent, expired, or stale snapshot never authorizes
new paid work.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.entitlements import (
    EntitlementClient,
    EntitlementContractDisabled,
    EntitlementDecision,
    EntitlementRequest,
    EntitlementSnapshot,
    EntitlementStatus,
    FakeEntitlementClient,
    authorize_paid_work,
    build_entitlement_client,
)
from app.entitlements.fake import SYNTHETIC_VERSION
from app.identity.fake import BUSINESS_A, BUSINESS_B
from tests.conftest import make_settings

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _resolve(client: FakeEntitlementClient, business_id: str) -> EntitlementSnapshot:
    return client.resolve(EntitlementRequest(business_id=business_id))


# --- the fake + typed snapshots -------------------------------------------------------------------


def test_fake_is_an_entitlement_client():
    assert isinstance(FakeEntitlementClient(), EntitlementClient)


def test_active_snapshot_reports_plan_capabilities_and_limits():
    snap = _resolve(FakeEntitlementClient(now=NOW), BUSINESS_A)
    assert snap.status is EntitlementStatus.ACTIVE
    assert snap.plan == "synthetic-growth"
    assert snap.allows_capability("discovery")
    assert snap.allows_capability("contact_research")
    assert snap.limit("monthly_discovery_runs") == 100
    assert snap.is_active(now=NOW)


def test_expired_snapshot_grants_no_capabilities():
    snap = _resolve(FakeEntitlementClient(now=NOW), BUSINESS_B)
    assert snap.status is EntitlementStatus.EXPIRED
    assert snap.capabilities == ()
    assert snap.limits == {}
    assert not snap.is_active(now=NOW)


def test_unknown_business_resolves_to_none():
    snap = _resolve(FakeEntitlementClient(now=NOW), "no-such-business")
    assert snap.status is EntitlementStatus.NONE
    assert snap.capabilities == ()


def test_snapshots_are_visibly_synthetic():
    client = FakeEntitlementClient(now=NOW)
    for business_id in (BUSINESS_A, BUSINESS_B, "stranger"):
        snap = _resolve(client, business_id)
        assert snap.synthetic is True
        assert snap.version == SYNTHETIC_VERSION
    assert _resolve(client, BUSINESS_A).plan.startswith("synthetic-")


# --- authorize_paid_work: fail-closed -------------------------------------------------------------


def test_active_entitlement_authorizes_an_included_capability():
    snap = _resolve(FakeEntitlementClient(now=NOW), BUSINESS_A)
    decision = authorize_paid_work(snap, "discovery", now=NOW)
    assert decision.allowed is True
    assert decision.reason == EntitlementDecision.OK


def test_capability_outside_the_plan_is_refused():
    snap = _resolve(FakeEntitlementClient(now=NOW), BUSINESS_A)
    decision = authorize_paid_work(snap, "not_in_plan", now=NOW)
    assert decision.allowed is False
    assert decision.reason == EntitlementDecision.CAPABILITY_NOT_INCLUDED


def test_absent_entitlement_cannot_authorize():
    assert (
        authorize_paid_work(None, "discovery", now=NOW).reason == EntitlementDecision.NO_ENTITLEMENT
    )
    none_snap = _resolve(FakeEntitlementClient(now=NOW), "stranger")
    assert authorize_paid_work(none_snap, "discovery", now=NOW).reason == (
        EntitlementDecision.NO_ENTITLEMENT
    )


def test_expired_entitlement_cannot_authorize():
    snap = _resolve(FakeEntitlementClient(now=NOW), BUSINESS_B)
    decision = authorize_paid_work(snap, "discovery", now=NOW)
    assert decision.allowed is False
    assert decision.reason == EntitlementDecision.EXPIRED


def test_stale_snapshot_cannot_authorize_even_if_not_expired():
    # An active, non-expired snapshot fetched 48h ago is stale (default max age 24h) => refused.
    stale = _resolve(FakeEntitlementClient(now=NOW - timedelta(hours=48)), BUSINESS_A)
    assert stale.status is EntitlementStatus.ACTIVE  # its own expiry is far in the future
    decision = authorize_paid_work(stale, "discovery", now=NOW)
    assert decision.allowed is False
    assert decision.reason == EntitlementDecision.STALE


def test_freshness_boundary_respects_max_age():
    snap = _resolve(FakeEntitlementClient(now=NOW - timedelta(hours=1)), BUSINESS_A)
    # Within a 2h max age the same snapshot authorizes; tighten the window and it is stale.
    assert (
        authorize_paid_work(snap, "discovery", now=NOW, max_age=timedelta(hours=2)).allowed is True
    )
    assert (
        authorize_paid_work(snap, "discovery", now=NOW, max_age=timedelta(minutes=30)).reason
        == EntitlementDecision.STALE
    )


# --- production refuses the fake ------------------------------------------------------------------


def test_production_refuses_the_fake():
    with pytest.raises(EntitlementContractDisabled):
        build_entitlement_client(make_settings(environment="production"))


def test_development_builds_the_fake():
    client = build_entitlement_client(make_settings(environment="development"))
    assert isinstance(client, FakeEntitlementClient)
