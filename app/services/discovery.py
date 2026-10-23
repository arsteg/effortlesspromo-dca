"""The discovery cycle — plan → fetch → screen → dedupe → suggest, fully recorded.

Deterministic foundation version: the "plan" is simply the set of enabled adapters and the tenant's
ICP. AI-assisted planning/extraction/scoring arrives only with an explicit AI-capability story
(DCA boundary) — nothing here calls the AI gateway yet.

Every raw candidate produces exactly one AcquisitionDecision (suggested or screened_out with a
stable reason code), and every cycle produces one DiscoveryRun — the explainability spine.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.compliance.rulepacks import (
    REASON_DUPLICATE,
    REASON_OK,
    ScreenResult,
    rulepack_for,
    screen,
)
from app.core.config import Settings
from app.db.models import (
    AcquisitionDecision,
    DiscoveryRun,
    IcpProfile,
    ProspectCandidate,
    TenantAgent,
)
from app.sources.base import SourceAdapter
from app.sources.synthetic_india import SyntheticIndiaRegistry


class DiscoveryError(Exception):
    """Raised with a stable code when a cycle cannot run (tenant disabled, no ICP, no sources)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def enabled_adapters(settings: Settings) -> list[SourceAdapter]:
    adapters: list[SourceAdapter] = []
    if settings.synthetic_sources_enabled:
        adapters.append(SyntheticIndiaRegistry())
    return adapters


def run_discovery(session: Session, settings: Settings, business_id: str) -> DiscoveryRun:
    tenant = session.scalar(select(TenantAgent).where(TenantAgent.business_id == business_id))
    if tenant is None or not tenant.enabled:
        raise DiscoveryError("tenant_not_enabled", f"no enabled DCA agent for {business_id}")
    if tenant.country not in settings.allowed_countries:
        raise DiscoveryError("country_not_allowed", f"country {tenant.country} not in scope")

    icp = session.scalar(select(IcpProfile).where(IcpProfile.business_id == business_id))
    if icp is None or icp.status != "active":
        raise DiscoveryError("icp_missing", "no active ICP profile for this business")

    adapters = enabled_adapters(settings)
    if not adapters:
        raise DiscoveryError("no_sources_enabled", "no data sources are enabled")

    run = DiscoveryRun(business_id=business_id, source_keys=[a.key for a in adapters])
    session.add(run)
    session.flush()

    pack = rulepack_for(tenant.country)
    icp_dict = {"industries": icp.industries, "regions": icp.regions, "roles": icp.roles}
    existing_ids = set(
        session.scalars(
            select(ProspectCandidate.org_identifier).where(
                ProspectCandidate.business_id == business_id
            )
        ).all()
    )

    found = screened_out = suggested = 0
    for adapter in adapters:
        for raw in adapter.fetch(icp_dict):
            found += 1
            result = screen(raw, pack)
            if result.allowed and raw.org_identifier in existing_ids:
                result = ScreenResult(False, REASON_DUPLICATE, "already known for this business")
            session.add(
                AcquisitionDecision(
                    run_id=run.id,
                    business_id=business_id,
                    org_identifier=raw.org_identifier,
                    decision="suggested" if result.allowed else "screened_out",
                    reason_code=result.reason_code,
                )
            )
            if not result.allowed:
                screened_out += 1
                continue
            suggested += 1
            existing_ids.add(raw.org_identifier)
            session.add(
                ProspectCandidate(
                    business_id=business_id,
                    run_id=run.id,
                    org_name=raw.org_name,
                    org_identifier=raw.org_identifier,
                    contact=raw.contact,
                    source_key=raw.source_key,
                    source_ref=raw.source_ref,
                    trust_tier=raw.trust_tier,
                    match_score=1.0,  # deterministic until the AI scoring capability story
                    rationale=f"Matches ICP via {raw.source_key} (reason: {REASON_OK})",
                    expires_at=datetime.now(UTC) + timedelta(days=pack.retention_days),
                )
            )

    run.found, run.screened_out, run.suggested = found, screened_out, suggested
    run.status = "completed"
    run.completed_at = datetime.now(UTC)
    session.commit()
    return run
