"""The 24/7 loop — a plain asyncio ticker, OFF by default (worker_enabled=false).

Each tick runs one discovery cycle per enabled tenant, each in its own session, and a failure for
one tenant never stops the loop or leaks into another tenant's cycle. The tick body is a plain
function (``tick_all_tenants``) so tests drive it deterministically without the loop.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.models import TenantAgent
from app.services.discovery import DiscoveryError, run_discovery

log = logging.getLogger("dca.worker")


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
            outcomes = await asyncio.to_thread(tick_all_tenants, session_factory, settings)
            if outcomes:
                log.info("tick outcomes: %s", outcomes)
        except Exception:
            log.exception("worker tick crashed; continuing")
        await asyncio.sleep(settings.worker_interval_seconds)
