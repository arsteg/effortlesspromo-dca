"""DCA-021 — the Data Concierge feature gate.

A work request is allowed only when BOTH hold:

1. the platform-level feature flag is on (``settings.feature_enabled`` — off by default), and
2. the specific business is enabled (its ``TenantAgent.enabled`` — the per-business narrowing).

So the flag is the platform's master switch; enabling it does not enable any business, and enabling
one business never enables another. With the flag off, no business can start work regardless of its
own enablement.
"""

from __future__ import annotations

from typing import Any

# Canonical flag name, used in logs / diagnostics so the gate is greppable and consistent with the
# platform's feature-flag vocabulary.
FEATURE_NAME = "feature.data-concierge"


def dca_feature_enabled(settings: Any) -> bool:
    """True if the platform-level Data Concierge feature flag is on."""
    return bool(getattr(settings, "feature_enabled", False))


def work_allowed(settings: Any, *, business_enabled: bool) -> bool:
    """A work request is allowed only when the platform flag is on AND the business is enabled."""
    return dca_feature_enabled(settings) and business_enabled
