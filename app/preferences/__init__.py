"""DCA-089 — the provider-backed SMS recipient-preference / consent screening contract (DCA-side port).

The DCA never owns the recipient-preference / DND registry; this package defines only the contract it
uses to ask a provider "has this recipient registered a preference that blocks this SMS route right
now?": a typed request/snapshot (disclosing version, freshness and affected scope), the client
``Protocol``, the fail-closed rule that turns a snapshot into a clear/deny for the requested SMS route,
and a deterministic synthetic fake for local tests. An unavailable or stale answer never clears the
route, a DND result grants nothing on another channel, and a clear is never recipient consent (the
product's consent-first default is a separate gate). The live adapter is a later story and stays
unbuilt; production refuses the fake.
"""

from __future__ import annotations

from app.preferences.contract import (
    DEFAULT_MAX_PREFERENCE_AGE,
    PreferenceAvailability,
    PreferenceEntry,
    PreferenceScreeningClient,
    PreferenceScreeningDecision,
    PreferenceScreeningRequest,
    PreferenceScreeningSnapshot,
    dnd_applies_to_route,
    screen_preferences,
)
from app.preferences.fake import (
    DEFAULT_COVERED_CHANNELS,
    SYNTHETIC_PROVIDER,
    SYNTHETIC_VERSION,
    FakePreferenceScreeningClient,
    PreferenceContractDisabled,
    build_preference_screening_client,
)

__all__ = [
    "DEFAULT_COVERED_CHANNELS",
    "DEFAULT_MAX_PREFERENCE_AGE",
    "SYNTHETIC_PROVIDER",
    "SYNTHETIC_VERSION",
    "FakePreferenceScreeningClient",
    "PreferenceAvailability",
    "PreferenceContractDisabled",
    "PreferenceEntry",
    "PreferenceScreeningClient",
    "PreferenceScreeningDecision",
    "PreferenceScreeningRequest",
    "PreferenceScreeningSnapshot",
    "build_preference_screening_client",
    "dnd_applies_to_route",
    "screen_preferences",
]
