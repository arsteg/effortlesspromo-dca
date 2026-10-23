"""Source adapter seam. Live sources (MCA/GSTIN crawlers, licensed APIs) are LATER stories — this
foundation ships only the synthetic fixture adapter, and nothing is consulted unless explicitly
enabled by configuration. An adapter declares its trust tier; the rulepack decides acceptability.
"""

from __future__ import annotations

from typing import Protocol

from app.compliance.rulepacks import RawCandidate


class SourceAdapter(Protocol):
    key: str
    trust_tier: str

    def fetch(self, icp: dict) -> list[RawCandidate]:
        """Return raw candidates matching the ICP. Must be side-effect-free and rate-limited."""
        ...
