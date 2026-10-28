"""The 24/7 loop — a plain asyncio ticker, OFF by default (worker_enabled=false).

Each tick runs scheduled platform maintenance (the DCA-049 source-expiry sweep) and then one
discovery cycle per enabled tenant, each in its own session, and a failure for one tenant never stops
the loop or leaks into another tenant's cycle. The tick bodies are plain functions
(``tick_maintenance``, ``tick_all_tenants``) so tests drive them deterministically without the loop.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.models import TenantAgent
from app.services.discovery import DiscoveryError, run_discovery
from app.services.restriction_fanout import run_restriction_fanout
from app.services.source_expiry import ExpirySweepResult, run_expiry_sweep
from app.services.source_revocation import run_revocation_invalidation

log = logging.getLogger("dca.worker")


def tick_maintenance(
    session_factory: sessionmaker[Session], settings: Settings
) -> ExpirySweepResult:
    """Run one scheduled platform-maintenance pass (DCA-049): expire lapsed source approvals/terms.

    Control-plane housekeeping, not tenant work — it runs once per cycle (not per tenant) and only
    ever makes a lapsed right MORE restrictive, so it is not gated behind any feature/source flag. The
    effective-date checks already deny a lapsed right immediately; this pass just stamps the durable
    EXPIRED state. A failure here must not stop the loop or block the tenant ticks that follow.

    It also runs the DCA-050 revocation-invalidation sweep (queued invalidation of observations acquired
    from a now-revoked source). The operator revocation itself is immediate (``is_in_force`` keys on the
    status); this pass is only the durable housekeeping that flips affected observations' scores stale.
    The expiry sweep's result is returned for the existing wiring; the invalidation result is logged.
    """
    result = run_expiry_sweep(session_factory, settings)
    if result.transitioned:
        log.info(
            "source-expiry sweep: expired %d approval(s), %d terms version(s)",
            len(result.expired_approvals),
            len(result.expired_terms),
        )
    invalidation = run_revocation_invalidation(session_factory, settings)
    if invalidation.invalidated:
        log.info(
            "source-revocation invalidation: marked %d observation(s) stale from %d revoked source(s)",
            invalidation.invalidated,
            len(invalidation.revoked_source_keys),
        )

    # DCA-081 — fan out every in-force platform-wide source removal across all tenants: revoke the
    # affected suggestions and record a per-tenant propagation acknowledgment. Also control-plane
    # housekeeping (keyed on the removed-source ledger, not one business); only ever more restrictive,
    # so it is not gated behind any feature/source flag and a failure here must not stop the loop.
    fanouts = run_restriction_fanout(session_factory, settings)
    revoked = sum(f.revoked_count for f in fanouts)
    if revoked:
        log.info(
            "restriction fanout: revoked %d suggestion(s) across %d platform-wide source removal(s)",
            revoked,
            len(fanouts),
        )
    return result


def tick_all_tenants(session_factory: sessionmaker[Session], settings: Settings) -> dict[str, str]:
    """Run one discovery cycle for every enabled tenant. Returns businessId -> outcome code."""
    outcomes: dict[str, str] = {}
    with session_factory() as session:
        business_ids = session.scalars(
            select(TenantAgent.business_id).where(TenantAgent.enabled.is_(True))
        ).all()
    for business_id in business_ids:
        with session_factory() as session:
            try:
                run = run_discovery(session, settings, business_id)
                outcomes[business_id] = f"completed:{run.suggested}"
            except DiscoveryError as exc:
                outcomes[business_id] = f"skipped:{exc.code}"
            except Exception:  # defensive: one tenant's failure must not kill the loop
                log.exception("discovery failed for business %s", business_id)
                outcomes[business_id] = "failed"
    return outcomes


async def worker_loop(session_factory: sessionmaker[Session], settings: Settings) -> None:
    log.info("DCA worker loop started (interval=%ss)", settings.worker_interval_seconds)
    while True:
        try:
            # Scheduled maintenance first (control-plane): expire lapsed source rights durably.
            await asyncio.to_thread(tick_maintenance, session_factory, settings)
        except Exception:
            log.exception("maintenance tick crashed; continuing")
        try:
            outcomes = await asyncio.to_thread(tick_all_tenants, session_factory, settings)
            if outcomes:
                log.info("tick outcomes: %s", outcomes)
        except Exception:
            log.exception("worker tick crashed; continuing")
        await asyncio.sleep(settings.worker_interval_seconds)
