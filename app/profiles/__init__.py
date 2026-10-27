"""DCA-037 — the typed platform-profile contract (platform-owned profile, DCA-side port).

The DCA never owns the business's canonical profile or Brand KB; this package defines only the contract
it uses to ask the platform for them — a typed request/snapshot, the client ``Protocol``, and a
deterministic synthetic fake (no host-table access) for local tests. The live adapter is a later story
and stays unbuilt; production refuses the fake. Importing a snapshot into a provenance-bearing draft is
``app/services/profile_importer.py``.
"""

from __future__ import annotations

from app.profiles.contract import (
    PlatformProfileClient,
    PlatformProfileRequest,
    PlatformProfileSnapshot,
)
from app.profiles.fake import (
    SYNTHETIC_SOURCE,
    SYNTHETIC_SOURCE_VERSION,
    FakePlatformProfileClient,
    PlatformProfileContractDisabled,
    build_platform_profile_client,
)

__all__ = [
    "SYNTHETIC_SOURCE",
    "SYNTHETIC_SOURCE_VERSION",
    "FakePlatformProfileClient",
    "PlatformProfileClient",
    "PlatformProfileContractDisabled",
    "PlatformProfileRequest",
    "PlatformProfileSnapshot",
    "build_platform_profile_client",
]
