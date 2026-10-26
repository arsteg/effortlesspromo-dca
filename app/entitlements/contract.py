"""DCA-022 — typed subscription-entitlement snapshots + the client port and evaluation.

The DCA never owns billing or subscriptions — they live in the platform. This defines only the
*contract* the DCA uses to ask "what is business B entitled to right now?": a typed request, an
immutable snapshot (expiry, allowed capabilities, plan limits, a freshness stamp), a client
``Protocol``, and the deterministic rule that turns a snapshot into an allow/deny for paid work.

Fail-closed is the whole point: an absent, expired, or *stale* snapshot never authorizes new paid
work. Staleness is distinct from expiry — a snapshot the DCA fetched too long ago may no longer
reflect the platform's truth, so it is refused even if its own ``expires_at`` is in the future.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

# A snapshot older than this is treated as stale and cannot authorize paid work — the platform is
# the source of truth and a long-cached entitlement may be out of date.
DEFAULT_MAX_SNAPSHOT_AGE = timedelta(hours=24)


class EntitlementStatus(str, Enum):
    """The subscription standing the platform reports for a business."""

    ACTIVE = "active"
    EXPIRED = "expired"
    NONE = "none"  # no subscription record for this business


class EntitlementRequest(BaseModel):
    """Resolve a business's current entitlements (DCA -> platform)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    business_id: str = Field(min_length=1)
    correlation_id: str | None = None


class EntitlementSnapshot(BaseModel):
    """An immutable point-in-time view of a business's entitlements.

    ``capabilities`` and ``limits`` are only meaningful while the snapshot is *active*; the
    evaluation below never consults them for an expired/none snapshot. ``as_of`` is when the platform
    produced the snapshot (its freshness), ``synthetic`` marks a non-production fixture so a test
    snapshot can never be mistaken for a real entitlement.
    """

    model_config = ConfigDict(frozen=True)

    business_id: str = Field(min_length=1)
    status: EntitlementStatus
    plan: str = ""
    capabilities: tuple[str, ...] = ()
    limits: dict[str, int] = Field(default_factory=dict)
    expires_at: datetime | None = None
    as_of: datetime
    version: str = Field(min_length=1)
    synthetic: bool = False

    def is_active(self, *, now: datetime) -> bool:
        return self.status is EntitlementStatus.ACTIVE and (
            self.expires_at is None or self.expires_at > now
        )

    def allows_capability(self, capability: str) -> bool:
        return capability in self.capabilities

    def limit(self, name: str) -> int | None:
        return self.limits.get(name)


@runtime_checkable
class EntitlementClient(Protocol):
    """Port the DCA uses to resolve platform entitlements. Synchronous, matching the other clients.

    The only implementation today is the dev/test fake; a live HTTP adapter is a later story and
    until it ships the contract stays disabled in production.
    """

    def resolve(self, request: EntitlementRequest) -> EntitlementSnapshot: ...


@dataclass(frozen=True)
class EntitlementDecision:
    """Why paid work was allowed or refused. ``reason`` is a stable code for callers/audit."""

    allowed: bool
    reason: str

    # stable reason codes
    OK = "ok"
    NO_ENTITLEMENT = "no_entitlement"
    EXPIRED = "expired"
    STALE = "stale"
    CAPABILITY_NOT_INCLUDED = "capability_not_included"


def authorize_paid_work(
    snapshot: EntitlementSnapshot | None,
    capability: str,
    *,
    now: datetime | None = None,
    max_age: timedelta = DEFAULT_MAX_SNAPSHOT_AGE,
) -> EntitlementDecision:
    """Decide whether ``snapshot`` authorizes ``capability`` as new paid work. Fail-closed at each
    step: absent → expired → stale → capability-not-included → allowed."""
    moment = now or datetime.now(UTC)
    if snapshot is None or snapshot.status is EntitlementStatus.NONE:
        return EntitlementDecision(False, EntitlementDecision.NO_ENTITLEMENT)
    if not snapshot.is_active(now=moment):
        return EntitlementDecision(False, EntitlementDecision.EXPIRED)
    if moment - snapshot.as_of > max_age:
        return EntitlementDecision(False, EntitlementDecision.STALE)
    if not snapshot.allows_capability(capability):
        return EntitlementDecision(False, EntitlementDecision.CAPABILITY_NOT_INCLUDED)
    return EntitlementDecision(True, EntitlementDecision.OK)
