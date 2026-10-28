"""DCA-079 — deterministic, in-memory platform-suppression fake for local tests.

The only authoritative-suppression implementation today: the live HTTP adapter to the host's
suppression registry is a later story. It is dev/test-only — production refuses it
(``build_platform_suppression_client``), mirroring the identity/entitlement/profile fakes and the
seed's dev-only stance. Every snapshot it produces is flagged ``synthetic`` and carries a plainly-fake
version, so a test answer can never be mistaken for a real authoritative one.

Two knobs make the DCA-079 behaviour and its failure case testable without a wall clock:

- ``available`` — when ``False`` the fake simulates the authoritative registry being unreachable: every
  lookup returns an ``UNAVAILABLE`` snapshot (the fail-closed path — clears nothing).
- ``now`` — injected into every snapshot's ``as_of``, so staleness is exercised by asking
  :func:`~app.suppression.contract.authorize_activation` with a later ``now``.

Host consent ownership stays explicit: every snapshot has ``host_owns_consent=True``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.identity.fake import BUSINESS_A, BUSINESS_B  # reuse the same synthetic businesses
from app.suppression.contract import (
    SuppressionAvailability,
    SuppressionEntry,
    SuppressionLookupRequest,
    SuppressionSnapshot,
)

SYNTHETIC_VERSION = "fake-suppression-v1"

# The channels the synthetic authoritative registry speaks for. A channel outside this cannot be
# cleared by the fake — the gate refuses it (CHANNEL_NOT_COVERED).
DEFAULT_COVERED_CHANNELS: tuple[str, ...] = ("email", "sms")

# A fixed synthetic "since" so fixtures are deterministic (no wall clock).
_SINCE = datetime(2026, 1, 1, tzinfo=UTC)

# Business A — one suppressed email (an unsubscribe) and one suppressed sms; everything else is clear.
# Business B — nothing suppressed (the registry is available and reports a clean business).
# Any other business — no fixture, treated as available-with-no-entries (nothing suppressed for it).
_DEFAULT_ENTRIES: dict[str, tuple[SuppressionEntry, ...]] = {
    BUSINESS_A: (
        SuppressionEntry(
            contact_point="opted-out@synthetic-a.example",
            channel="email",
            reason="unsubscribe",
            since=_SINCE,
        ),
        SuppressionEntry(
            contact_point="+919900000000",
            channel="sms",
            reason="stop_keyword",
            since=_SINCE,
        ),
    ),
    BUSINESS_B: (),
}


class SuppressionContractDisabled(RuntimeError):
    """Raised when suppression lookup is requested where the contract is not wired (production)."""


class FakePlatformSuppressionClient:
    """A deterministic :class:`~app.suppression.contract.PlatformSuppressionClient`. Pure: no I/O.

    ``entries`` maps a business id to its suppressed ``(contact_point, channel)`` list; businesses with
    no fixture resolve to an available snapshot with no entries. ``available=False`` makes EVERY lookup
    return an ``UNAVAILABLE`` snapshot (the registry-down path). ``now`` is injected into ``as_of`` so
    freshness/staleness is testable; it defaults to real UTC now. Every snapshot is ``synthetic=True``
    and ``host_owns_consent=True``.
    """

    def __init__(
        self,
        entries: dict[str, tuple[SuppressionEntry, ...]] | None = None,
        *,
        covered_channels: tuple[str, ...] = DEFAULT_COVERED_CHANNELS,
        available: bool = True,
        now: datetime | None = None,
        version: str = SYNTHETIC_VERSION,
    ) -> None:
        self._entries = entries if entries is not None else _DEFAULT_ENTRIES
        self._covered_channels = covered_channels
        self._available = available
        self._now = now
        self._version = version

    def lookup(self, request: SuppressionLookupRequest) -> SuppressionSnapshot:
        now = self._now or datetime.now(UTC)
        if not self._available:
            # Registry unreachable: no authoritative view. Entry/coverage fields are empty and the gate
            # refuses on availability regardless of them.
            return SuppressionSnapshot(
                business_id=request.business_id,
                availability=SuppressionAvailability.UNAVAILABLE,
                version=self._version,
                as_of=now,
                host_owns_consent=True,
                synthetic=True,
            )
        return SuppressionSnapshot(
            business_id=request.business_id,
            availability=SuppressionAvailability.AVAILABLE,
            version=self._version,
            as_of=now,
            covered_channels=self._covered_channels,
            entries=self._entries.get(request.business_id, ()),
            host_owns_consent=True,
            synthetic=True,
        )


def build_platform_suppression_client(settings: Any) -> FakePlatformSuppressionClient:
    """The platform-suppression client for this deployment.

    Only the dev/test fake exists; the live adapter to the host suppression registry is unbuilt.
    Production therefore has NO suppression client — the contract stays disabled until that story
    ships. Calling this under production configuration raises rather than silently handing back a fake
    (which would risk treating a synthetic "clear" as an authoritative one — the exact fail-open this
    story forbids).
    """
    if getattr(settings, "is_production", False):
        raise SuppressionContractDisabled(
            "platform-suppression contract is disabled in production: the live adapter is not built "
            "yet and the synthetic fake is dev/test-only"
        )
    return FakePlatformSuppressionClient()
