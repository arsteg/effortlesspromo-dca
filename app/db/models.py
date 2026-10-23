"""DCA persistence model — per-tenant agent state, discovery output, and durable AI bookkeeping.

Tenancy: every tenant-facing table carries ``business_id`` (the platform's business GUID as a
string) and every query in the service filters on it explicitly. There is no cross-tenant read
path. Candidate acceptance is a DCA-local status change only — it is NOT recipient consent and
grants no sending rights (consent/suppression are enforced by the platform's send pipeline).

AI bookkeeping (DCA-001 B2/B4): ``AiInvocation`` is this service's durable record of every gateway
call. DCA mints its own job ids in a ``dca:`` namespace (the AI envelope treats jobId as an opaque
string) and derives the idempotency key from the row id, so a timed-out call is retried with the
SAME ``X-Request-Id`` and reconciled without double spend.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


class TenantAgent(Base):
    """One row per business that has a DCA agent registered. Disabled until explicitly enabled."""

    __tablename__ = "tenant_agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    country: Mapped[str] = mapped_column(String(2), default="IN")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class IcpProfile(Base):
    """The tenant's Ideal Customer Profile — what the agent looks for. One active profile each."""

    __tablename__ = "icp_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    industries: Mapped[list] = mapped_column(JSON, default=list)
    regions: Mapped[list] = mapped_column(JSON, default=list)         # states/cities within India
    roles: Mapped[list] = mapped_column(JSON, default=list)           # roles sought, e.g. "owner"
    exclusions: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | paused
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class DiscoveryRun(Base):
    """One agent cycle for one tenant — the explainability record of what happened and why."""

    __tablename__ = "discovery_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|completed|failed
    source_keys: Mapped[list] = mapped_column(JSON, default=list)
    found: Mapped[int] = mapped_column(Integer, default=0)
    screened_out: Mapped[int] = mapped_column(Integer, default=0)
    suggested: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class ProspectCandidate(Base):
    """A suggestion awaiting tenant action. Suggested-only: nothing is pushed into the platform."""

    __tablename__ = "prospect_candidates"
    __table_args__ = (UniqueConstraint("business_id", "org_identifier", name="uq_candidate_org"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str] = mapped_column(String(36), index=True)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("discovery_runs.id"))
    org_name: Mapped[str] = mapped_column(String(300))
    org_identifier: Mapped[str] = mapped_column(String(64))   # CIN / GSTIN / registry ref
    contact: Mapped[dict] = mapped_column(JSON, default=dict)  # {kind, email?, phone?, role?}
    source_key: Mapped[str] = mapped_column(String(64))
    source_ref: Mapped[str] = mapped_column(String(500))       # registry URL / record locator
    trust_tier: Mapped[str] = mapped_column(String(32))        # official_registry | public_web | …
    match_score: Mapped[float] = mapped_column(Float, default=0.0)
    rationale: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="suggested", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class AcquisitionDecision(Base):
    """Auditable per-record decision (mirror of the platform's SendDecision philosophy)."""

    __tablename__ = "acquisition_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("discovery_runs.id"), index=True)
    business_id: Mapped[str] = mapped_column(String(36), index=True)
    org_identifier: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(32))          # suggested | screened_out
    reason_code: Mapped[str] = mapped_column(String(64))       # ok | source_tier_disallowed | …
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AiInvocation(Base):
    """Durable record of every effortlesspromo-ai gateway call made by this service (B2/B4)."""

    __tablename__ = "ai_invocations"
    __table_args__ = (UniqueConstraint("request_id", name="uq_ai_request_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str] = mapped_column(String(36), index=True)
    job_id: Mapped[str] = mapped_column(String(64))            # "dca:<id>" — DCA-owned namespace
    request_id: Mapped[str] = mapped_column(String(64))        # X-Request-Id (deterministic)
    capability: Mapped[str] = mapped_column(String(64))
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|completed|failed|unavailable
    response_status: Mapped[str | None] = mapped_column(String(16), default=None)
    cost_amount: Mapped[float] = mapped_column(Float, default=0.0)
    cost_currency: Mapped[str] = mapped_column(String(8), default="USD")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
