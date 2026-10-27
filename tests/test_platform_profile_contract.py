"""DCA-037 — the typed platform-profile contract + its synthetic fake.

Pins the fake's behaviour (the contract shapes are exercised through it): two DISTINCT synthetic
profiles for A/B, a not-found snapshot for an unknown business, every snapshot flagged synthetic with a
versioned source, and production refusing the fake (the live adapter is unbuilt, B1). No host table,
no I/O, no state.
"""

from __future__ import annotations

import pytest

from app.core.config import DeploymentMode, Settings
from app.identity.fake import BUSINESS_A, BUSINESS_B
from app.profiles import (
    SYNTHETIC_SOURCE,
    SYNTHETIC_SOURCE_VERSION,
    FakePlatformProfileClient,
    PlatformProfileClient,
    PlatformProfileContractDisabled,
    PlatformProfileRequest,
    PlatformProfileSnapshot,
    build_platform_profile_client,
)


def test_fake_satisfies_the_client_protocol():
    assert isinstance(FakePlatformProfileClient(), PlatformProfileClient)


def test_fetch_returns_a_distinct_versioned_snapshot_per_business():
    fake = FakePlatformProfileClient()
    a = fake.fetch(PlatformProfileRequest(business_id=BUSINESS_A))
    b = fake.fetch(PlatformProfileRequest(business_id=BUSINESS_B))

    assert a.found and b.found
    # Scope + provenance are retained on the snapshot.
    assert a.business_id == BUSINESS_A
    assert a.source == SYNTHETIC_SOURCE
    assert a.source_version == SYNTHETIC_SOURCE_VERSION
    assert a.synthetic is True
    # A and B are visibly different profiles (the manual-test contrast).
    assert a.products != b.products
    assert a.business_type != b.business_type
    assert a.facts and b.facts and a.facts != b.facts


def test_unknown_business_is_a_not_found_empty_snapshot():
    snap = FakePlatformProfileClient().fetch(PlatformProfileRequest(business_id="no-such-business"))
    assert isinstance(snap, PlatformProfileSnapshot)
    assert snap.found is False
    assert snap.products == () and snap.facts == ()
    # Even a not-found snapshot still carries its source version + synthetic flag (honest provenance).
    assert snap.source_version == SYNTHETIC_SOURCE_VERSION
    assert snap.synthetic is True


def test_request_rejects_empty_business_id():
    with pytest.raises(ValueError):
        PlatformProfileRequest(business_id="")


def _settings(environment: str, **overrides) -> Settings:
    base = {
        "environment": environment,
        "database_url": "sqlite+pysqlite:///:memory:",
        "service_auth_token": "t",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_build_returns_fake_in_development():
    client = build_platform_profile_client(_settings("development"))
    assert isinstance(client, FakePlatformProfileClient)


def test_build_refuses_the_fake_in_production():
    prod = _settings(
        "production",
        deployment_mode=DeploymentMode.LIVE,
        database_url="postgresql+psycopg://user@db/dca",
        synthetic_sources_enabled=False,
        auto_create_schema=False,
    )
    with pytest.raises(PlatformProfileContractDisabled):
        build_platform_profile_client(prod)
