"""DCA-089 — deterministic, in-memory recipient-preference screening fake for local tests.

The only preference-screening implementation today: the live HTTP adapter to the real preference /
DND registry provider is a later story. It is dev/test-only — production refuses it
(``build_preference_screening_client``), mirroring the DCA-079 suppression fake and the identity /
entitlement / profile fakes. Every snapshot it produces is flagged ``synthetic`` and carries a
plainly-fake version/provider, so a test answer can never be mistaken for a real authoritative one.

Two knobs make the DCA-089 behaviour and its failure case testable without a wall clock:

- ``available`` — when ``False`` the fake simulates the preference registry being unreachable: every
  screen returns an ``UNAVAILABLE`` snapshot (the fail-closed path — clears nothing).
- ``now`` — injected into every snapshot's ``as_of``, so staleness is exercised by asking
  :func:`~app.preferences.contract.screen_preferences` with a later ``now``.

The fake is SMS-scoped by default (``covered_channels=("sms",)``) to make the cross-channel invariant
visible: it cannot clear email or WhatsApp, so a DND result never grants permission there.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.compliance.sms_sender_prerequisites import SMS_CHANNEL, SmsRoute
from app.identity.fake import BUSINESS_A, BUSINESS_B  # reuse the same synthetic businesses
from app.preferences.contract import (
    PreferenceAvailability,
    PreferenceEntry,
    PreferenceScreeningRequest,
    PreferenceScreeningSnapshot,
)

SYNTHETIC_VERSION = "fake-preference-v1"
SYNTHETIC_PROVIDER = "fake-dnd-registry"

# The channels the synthetic preference registry speaks for. SMS-only on purpose: the fake cannot
# clear email / WhatsApp (the gate refuses them — CHANNEL_NOT_SCREENED), so a DND result never leaks
# cross-channel.
DEFAULT_COVERED_CHANNELS: tuple[str, ...] = (SMS_CHANNEL,)

# A fixed synthetic "since" so fixtures are deterministic (no wall clock).
_SINCE = datetime(2026, 1, 1, tzinfo=UTC)

# Business A — one fully-DND recipient and one promotional-only block; everything else is clear.
# Business B — nothing registered (the registry is available and reports a clean recipient set).
# Any other business — no fixture, treated as available-with-no-entries (nothing registered for it).
_DEFAULT_ENTRIES: dict[str, tuple[PreferenceEntry, ...]] = {
    BUSINESS_A: (
        PreferenceEntry(
            contact_point="+919812345678",
            channel=SMS_CHANNEL,
            fully_blocked=True,
            since=_SINCE,
        ),
        PreferenceEntry(
            contact_point="+919823456789",
            channel=SMS_CHANNEL,
            blocked_routes=(SmsRoute.PROMOTIONAL.value,),
            since=_SINCE,
        ),
    ),
    BUSINESS_B: (),
}


class PreferenceContractDisabled(RuntimeError):
    """Raised when preference screening is requested where the contract is not wired (production)."""


class FakePreferenceScreeningClient:
    """A deterministic :class:`~app.preferences.contract.PreferenceScreeningClient`. Pure: no I/O.

    ``entries`` maps a business id to its registered preference list; businesses with no fixture
    resolve to an available snapshot with no entries. ``available=False`` makes EVERY screen return an
    ``UNAVAILABLE`` snapshot (the registry-down path). ``now`` is injected into ``as_of`` so
    freshness/staleness is testable; it defaults to real UTC now. Every snapshot is ``synthetic=True``.
    """

    def __init__(
        self,
        entries: dict[str, tuple[PreferenceEntry, ...]] | None = None,
        *,
        covered_channels: tuple[str, ...] = DEFAULT_COVERED_CHANNELS,
        available: bool = True,
        now: datetime | None = None,
        version: str = SYNTHETIC_VERSION,
        provider: str = SYNTHETIC_PROVIDER,
    ) -> None:
        self._entries = entries if entries is not None else _DEFAULT_ENTRIES
        self._covered_channels = covered_channels
        self._available = available
        self._now = now
        self._version = version
        self._provider = provider

    def screen(self, request: PreferenceScreeningRequest) -> PreferenceScreeningSnapshot:
        now = self._now or datetime.now(UTC)
        if not self._available:
            # Registry unreachable: no authoritative view. Entry/coverage fields are empty and the gate
            # refuses on availability regardless of them.
            return PreferenceScreeningSnapshot(
                business_id=request.business_id,
                availability=PreferenceAvailability.UNAVAILABLE,
                provider=self._provider,
                version=self._version,
                as_of=now,
                synthetic=True,
            )
        return PreferenceScreeningSnapshot(
            business_id=request.business_id,
            availability=PreferenceAvailability.AVAILABLE,
            provider=self._provider,
            version=self._version,
            as_of=now,
            covered_channels=self._covered_channels,
            entries=self._entries.get(request.business_id, ()),
            synthetic=True,
        )


def build_preference_screening_client(settings: Any) -> FakePreferenceScreeningClient:
    """The preference-screening client for this deployment.

    Only the dev/test fake exists; the live adapter to the real preference/DND registry provider is
    unbuilt. Production therefore has NO preference-screening client — the contract stays disabled
    until that story ships. Calling this under production configuration raises rather than silently
    handing back a fake (which would risk treating a synthetic "clear" as an authoritative one — the
    exact fail-open this story forbids).
    """
    if getattr(settings, "is_production", False):
        raise PreferenceContractDisabled(
            "preference-screening contract is disabled in production: the live adapter is not built "
            "yet and the synthetic fake is dev/test-only"
        )
    return FakePreferenceScreeningClient()
