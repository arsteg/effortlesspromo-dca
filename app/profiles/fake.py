"""DCA-037 — deterministic, in-memory platform-profile fake for local tests.

The only platform-profile implementation today: the live HTTP adapter into the platform (B1) is a
later story. It is dev/test-only — production refuses it (``build_platform_profile_client``), mirroring
the identity/entitlement fakes and the seed's dev-only stance. Every snapshot it produces is flagged
``synthetic`` with a plainly-fake ``source_version``, so an imported draft can never be mistaken for one
built from a real platform profile.

Crucially this reads **no host table**: the fixtures are hard-coded synthetic content for the two
synthetic businesses (A/B), so the import path can be exercised end-to-end with nothing real behind it.
A business with no fixture resolves to a ``found=False`` empty snapshot (the importer refuses to stage
an empty draft), so the "nothing to import" denial is reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.identity.fake import BUSINESS_A, BUSINESS_B  # reuse the same synthetic businesses
from app.profiles.contract import PlatformProfileRequest, PlatformProfileSnapshot

# The provenance a draft imported from the fake retains — obviously synthetic, versioned.
SYNTHETIC_SOURCE = "platform_profile_snapshot"
SYNTHETIC_SOURCE_VERSION = "fake-platform-profile-v1"


class PlatformProfileContractDisabled(RuntimeError):
    """Raised when a platform profile fetch is requested where the contract is not wired (production).

    Mirrors :class:`~app.identity.fake.IdentityContractDisabled` /
    :class:`~app.entitlements.fake.EntitlementContractDisabled`.
    """


@dataclass(frozen=True)
class _Fixture:
    """A raw synthetic profile fixture for one business (the platform's own-profile + Brand KB facts)."""

    products: tuple[str, ...]
    business_type: str
    value_proposition: str
    languages: tuple[str, ...]
    facts: tuple[str, ...] = field(default_factory=tuple)


# Two DISTINCT synthetic profiles so A/B imports are visibly different (the manual-test step).
_DEFAULT_FIXTURES: dict[str, _Fixture] = {
    BUSINESS_A: _Fixture(
        products=("artisan masala chai", "single-estate darjeeling"),
        business_type="retail",
        value_proposition="small-batch Indian teas sourced direct from growers",
        languages=("en", "hi"),
        facts=("FSSAI licensed", "ships pan-India", "GST registered in Karnataka"),
    ),
    BUSINESS_B: _Fixture(
        products=("fleet telematics platform",),
        business_type="b2b_saas",
        value_proposition="real-time GPS + fuel analytics for Indian logistics fleets",
        languages=("en",),
        facts=("ISO 27001 certified", "serves Mumbai & Pune"),
    ),
}


class FakePlatformProfileClient:
    """A deterministic :class:`~app.profiles.contract.PlatformProfileClient`. Pure: no I/O, no host
    table, no state.

    ``now`` is injectable so ``as_of`` is testable without a wall clock; it defaults to the real UTC
    now. Every returned snapshot is ``synthetic=True``.
    """

    def __init__(
        self,
        fixtures: dict[str, _Fixture] | None = None,
        *,
        now: datetime | None = None,
        source: str = SYNTHETIC_SOURCE,
        source_version: str = SYNTHETIC_SOURCE_VERSION,
    ) -> None:
        self._fixtures = fixtures if fixtures is not None else _DEFAULT_FIXTURES
        self._now = now
        self._source = source
        self._source_version = source_version

    def fetch(self, request: PlatformProfileRequest) -> PlatformProfileSnapshot:
        now = self._now or datetime.now(UTC)
        fixture = self._fixtures.get(request.business_id)
        if fixture is None:
            # No profile on record for this business — an empty, not-found snapshot. The importer
            # refuses to stage it, so "nothing to import" is a reproducible denial with no state change.
            return PlatformProfileSnapshot(
                business_id=request.business_id,
                source=self._source,
                source_version=self._source_version,
                found=False,
                as_of=now,
                synthetic=True,
            )
        return PlatformProfileSnapshot(
            business_id=request.business_id,
            source=self._source,
            source_version=self._source_version,
            found=True,
            products=fixture.products,
            business_type=fixture.business_type,
            value_proposition=fixture.value_proposition,
            languages=fixture.languages,
            facts=fixture.facts,
            as_of=now,
            synthetic=True,
        )


def build_platform_profile_client(settings: Any) -> FakePlatformProfileClient:
    """The platform-profile client for this deployment.

    Only the dev/test fake exists; the live adapter (B1) is unbuilt. Production therefore has NO
    platform-profile client — the contract stays disabled until that story ships. Calling this under
    production configuration raises rather than silently handing back a fake.
    """
    if getattr(settings, "is_production", False):
        raise PlatformProfileContractDisabled(
            "platform profile contract is disabled in production: the live adapter is not built yet "
            "and the synthetic fake is dev/test-only (B1)"
        )
    return FakePlatformProfileClient()
