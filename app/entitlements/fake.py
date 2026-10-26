"""DCA-022 — deterministic, in-memory entitlement fake for local tests.

The only entitlement implementation today: the live HTTP adapter to the platform's billing is a
later story. It is dev/test-only — production refuses it (``build_entitlement_client``), mirroring the
identity fake and the seed's dev-only stance. Every snapshot it produces is flagged ``synthetic`` and
carries a plainly-fake plan/version, so a test entitlement can never be mistaken for a real one.

Standing is derived with real logic: an ``expires_at`` in the past is EXPIRED, otherwise ACTIVE; a
business with no fixture resolves to NONE. ``as_of`` is the injectable ``now`` (fresh by default), so
freshness/staleness is testable without a wall clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.entitlements.contract import (
    EntitlementRequest,
    EntitlementSnapshot,
    EntitlementStatus,
)
from app.identity.fake import BUSINESS_A, BUSINESS_B  # reuse the same synthetic businesses

_PAST = datetime(2020, 1, 1, tzinfo=UTC)
_FUTURE = datetime(2999, 1, 1, tzinfo=UTC)

SYNTHETIC_VERSION = "fake-entitlements-v1"


class EntitlementContractDisabled(RuntimeError):
    """Raised when entitlement resolution is requested where the contract is not wired (production)."""


@dataclass(frozen=True)
class _Plan:
    """A raw fixture: the plan a business is on, before status is derived from ``expires_at``."""

    plan: str
    capabilities: tuple[str, ...]
    limits: dict[str, int] = field(default_factory=dict)
    expires_at: datetime | None = None


# Business A — active synthetic plan with two capabilities; B — the same plan but lapsed (expired).
# Any other business has no fixture and resolves to NONE. Plan names are obviously synthetic.
_DEFAULT_PLANS: dict[str, _Plan] = {
    BUSINESS_A: _Plan(
        plan="synthetic-growth",
        capabilities=("discovery", "contact_research"),
        limits={"monthly_discovery_runs": 100, "monthly_candidates": 5000},
        expires_at=_FUTURE,
    ),
    BUSINESS_B: _Plan(
        plan="synthetic-starter",
        capabilities=("discovery",),
        limits={"monthly_discovery_runs": 10, "monthly_candidates": 250},
        expires_at=_PAST,
    ),
}


class FakeEntitlementClient:
    """A deterministic :class:`~app.entitlements.contract.EntitlementClient`. Pure: no I/O, no state.

    ``now`` is injectable so expiry/freshness are testable; it defaults to the real UTC now (the
    fixture dates are far past/future, so the default is still deterministic in practice). Every
    returned snapshot is ``synthetic=True``.
    """

    def __init__(
        self,
        plans: dict[str, _Plan] | None = None,
        *,
        now: datetime | None = None,
        version: str = SYNTHETIC_VERSION,
    ) -> None:
        self._plans = plans if plans is not None else _DEFAULT_PLANS
        self._now = now
        self._version = version

    def resolve(self, request: EntitlementRequest) -> EntitlementSnapshot:
        now = self._now or datetime.now(UTC)
        plan = self._plans.get(request.business_id)
        if plan is None:
            return EntitlementSnapshot(
                business_id=request.business_id,
                status=EntitlementStatus.NONE,
                as_of=now,
                version=self._version,
                synthetic=True,
            )
        expired = plan.expires_at is not None and plan.expires_at <= now
        status = EntitlementStatus.EXPIRED if expired else EntitlementStatus.ACTIVE
        return EntitlementSnapshot(
            business_id=request.business_id,
            status=status,
            plan=plan.plan,
            # Capabilities/limits are reported only while active; an expired plan grants nothing.
            capabilities=() if expired else plan.capabilities,
            limits={} if expired else dict(plan.limits),
            expires_at=plan.expires_at,
            as_of=now,
            version=self._version,
            synthetic=True,
        )


def build_entitlement_client(settings: Any) -> FakeEntitlementClient:
    """The entitlement client for this deployment.

    Only the dev/test fake exists; the live adapter is unbuilt. Production therefore has NO
    entitlement client — the contract stays disabled until that story ships. Calling this under
    production configuration raises rather than silently handing back a fake.
    """
    if getattr(settings, "is_production", False):
        raise EntitlementContractDisabled(
            "entitlement contract is disabled in production: the live adapter is not built yet and "
            "the synthetic fake is dev/test-only"
        )
    return FakeEntitlementClient()
