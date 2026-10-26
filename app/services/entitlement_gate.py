"""DCA-023 — enforce entitlement freshness before a chargeable operation.

The gate resolves a live entitlement snapshot from the platform (DCA-022 contract), caches a
*permitted* projection of it, and decides whether the operation may proceed:

- Live resolve succeeds → cache the projection if the snapshot is ACTIVE, else CLEAR any cached
  projection (so a learned expiry/revocation fails closed immediately), then evaluate the live
  snapshot.
- Live resolve fails (entitlement host outage) → fall back to the cached projection and evaluate it
  under the SAME freshness rule. Because the cache carries the snapshot's ``as_of``, it authorizes
  only while younger than the configured max age — a prior-active subscription can never keep
  authorizing paid work indefinitely during an outage.
- No usable snapshot (never cached, cleared, or contract disabled) → denied (fail-closed).

The gate only ever caches an active projection — never a raw billing record — and the cache is the
platform's single business-scoped control-plane row for the business.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import EntitlementProjection
from app.entitlements import (
    EntitlementClient,
    EntitlementContractDisabled,
    EntitlementRequest,
    EntitlementSnapshot,
    EntitlementStatus,
    authorize_paid_work,
    build_entitlement_client,
)


class EntitlementGateError(Exception):
    """A chargeable operation was refused by the entitlement gate. ``reason`` is a stable code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def default_entitlement_client(settings: Settings) -> EntitlementClient | None:
    """The entitlement client for this deployment, or ``None`` when the contract is disabled
    (production, until the live adapter ships) — in which case the gate fails closed."""
    try:
        return build_entitlement_client(settings)
    except EntitlementContractDisabled:
        return None


def _cache_projection(session: Session, business_id: str, snapshot: EntitlementSnapshot) -> None:
    """Persist ONLY a permitted (active) projection; clear the row on expiry/revocation."""
    row = session.scalar(
        select(EntitlementProjection).where(EntitlementProjection.business_id == business_id)
    )
    if snapshot.status is not EntitlementStatus.ACTIVE:
        # Learned expiry/revocation: drop any cached authorization so an outage now fails closed.
        if row is not None:
            session.delete(row)
        return
    if row is None:
        row = EntitlementProjection(business_id=business_id)
        session.add(row)
    row.status = snapshot.status.value
    row.plan = snapshot.plan
    row.capabilities = list(snapshot.capabilities)
    row.limits = dict(snapshot.limits)
    row.expires_at = snapshot.expires_at
    row.captured_at = snapshot.as_of
    row.version = snapshot.version


def _load_projection(session: Session, business_id: str) -> EntitlementSnapshot | None:
    """Reconstruct a snapshot from the cached projection, or ``None`` if nothing is cached."""
    row = session.scalar(
        select(EntitlementProjection).where(EntitlementProjection.business_id == business_id)
    )
    if row is None:
        return None
    captured = row.captured_at
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=UTC)  # SQLite returns naive datetimes
    expires = row.expires_at
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return EntitlementSnapshot(
        business_id=row.business_id,
        status=EntitlementStatus(row.status),
        plan=row.plan,
        capabilities=tuple(row.capabilities or ()),
        limits=dict(row.limits or {}),
        expires_at=expires,
        as_of=captured,
        version=row.version,
        synthetic=True,  # the only client today is the synthetic fake
    )


def ensure_entitled(
    session: Session,
    settings: Settings,
    client: EntitlementClient | None,
    business_id: str,
    capability: str,
    *,
    now: datetime | None = None,
) -> EntitlementSnapshot:
    """Validate entitlement freshness for ``capability``; raise :class:`EntitlementGateError` on deny.

    Caches a permitted projection on a successful live resolve; falls back to that projection (under
    the same freshness bound) if the entitlement host is unreachable.
    """
    moment = now or datetime.now(UTC)
    max_age = timedelta(seconds=settings.entitlement_max_age_seconds)

    snapshot: EntitlementSnapshot | None
    if client is None:
        snapshot = None  # contract disabled => fail closed
    else:
        try:
            snapshot = client.resolve(EntitlementRequest(business_id=business_id))
            _cache_projection(session, business_id, snapshot)
            session.commit()  # the projection cache is durable regardless of the decision below
        except EntitlementGateError:
            raise
        except Exception:  # noqa: BLE001 — ANY resolve failure is treated as a host outage
            # Entitlement host outage: fall back to the last permitted cached projection.
            session.rollback()
            snapshot = _load_projection(session, business_id)

    decision = authorize_paid_work(snapshot, capability, now=moment, max_age=max_age)
    if not decision.allowed:
        raise EntitlementGateError(decision.reason)
    return snapshot  # type: ignore[return-value]  # allowed => snapshot is not None
