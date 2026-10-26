"""DCA-009 — development-only seed for two isolated test businesses (A and B).

Explicitly dev-only: the seed **refuses to run under production configuration** so a real
deployment can never acquire fabricated tenants. It is **idempotent** — every record is upserted by
its fixed synthetic ``business_id``, so re-running leaves row counts and primary-key ids stable.
Everything is synthetic: deterministic, obviously-fake business GUIDs, synthetic operator labels and
ICP data, and synthetic org identifiers. **No real contacts or credentials** are ever written.

DCA has no user table — real users/credentials belong to the platform (DCA-001). A "test business"
here is therefore its tenant principal: a ``TenantAgent`` (the business_id scope) plus its synthetic
``IcpProfile``. These two isolated tenants are the baseline fixture for later tenant-isolation
stories (every DCA query is business_id-scoped; A must never see B's rows).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import IcpProfile, TenantAgent


class SeedRefused(RuntimeError):
    """Raised when seeding is attempted under a configuration that forbids it (production)."""


@dataclass(frozen=True)
class SeedBusiness:
    """One synthetic tenant. ``operator`` is a synthetic user label only (no credential, no row)."""

    business_id: str
    operator: str
    org_identifier: str  # synthetic registry ref — NOT a real CIN/GSTIN
    industries: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)


# Fixed version-4-shaped GUIDs (String(36)) so repeated seeding is idempotent and these ids can
# never collide with a real platform business GUID. The trailing a/b makes A vs B obvious.
TEST_BUSINESSES: tuple[SeedBusiness, ...] = (
    SeedBusiness(
        business_id="00000000-0000-4000-8000-00000000000a",
        operator="synthetic-operator-a@example.test",
        org_identifier="SYNTH-A-0001",
        industries=["software"],
        regions=["Karnataka"],
        roles=["owner"],
    ),
    SeedBusiness(
        business_id="00000000-0000-4000-8000-00000000000b",
        operator="synthetic-operator-b@example.test",
        org_identifier="SYNTH-B-0001",
        industries=["manufacturing"],
        regions=["Maharashtra"],
        roles=["founder"],
    ),
)


def seed_test_businesses(session: Session, settings: Settings) -> dict:
    """Idempotently upsert the two synthetic test businesses. Refuses under production config.

    Returns a summary: the business ids, and which were newly created vs already present (updated).
    The caller owns the transaction boundary only in that this commits once at the end; a production
    refusal raises BEFORE any write so no partial state is left behind.
    """
    if settings.is_production:
        raise SeedRefused(
            "seed is development-only and refuses to run in production "
            "(unset EFFORTLESSPROMO_DCA_ENVIRONMENT=production to seed test data)"
        )

    created: list[str] = []
    updated: list[str] = []
    for biz in TEST_BUSINESSES:
        tenant = session.scalar(
            select(TenantAgent).where(TenantAgent.business_id == biz.business_id)
        )
        if tenant is None:
            session.add(TenantAgent(business_id=biz.business_id, country="IN", enabled=False))
            created.append(biz.business_id)
        else:
            # Re-apply the baseline without disturbing the stable primary key.
            tenant.country = "IN"
            updated.append(biz.business_id)

        icp = session.scalar(select(IcpProfile).where(IcpProfile.business_id == biz.business_id))
        if icp is None:
            icp = IcpProfile(business_id=biz.business_id)
            session.add(icp)
        # Re-applying the same baseline is deliberate: idempotent, and version is left untouched so a
        # repeated seed is not mistaken for a real ICP revision.
        icp.industries = list(biz.industries)
        icp.regions = list(biz.regions)
        icp.roles = list(biz.roles)
        icp.exclusions = []
        icp.status = "active"

    session.commit()
    return {
        "businesses": [b.business_id for b in TEST_BUSINESSES],
        "created": created,
        "updated": updated,
    }
