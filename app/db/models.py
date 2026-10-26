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
from enum import Enum

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
from app.db.tenancy import TenantOwned, tenant_unique


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


class DataMode(str, Enum):
    """How the agent is allowed to source prospects for a business (DCA-024).

    ``MY_DATA_ONLY`` (the conservative default) — work only the tenant's own data; never suggest
    platform-sourced prospects. ``SUGGEST_PLATFORM_DATA`` — may *suggest* platform-sourced prospects
    for review. Neither mode grants any sending right: suggestion is not consent and the DCA has no
    send path. Auto-accept is a separate, later capability and is NOT a mode here.
    """

    MY_DATA_ONLY = "my_data_only"
    SUGGEST_PLATFORM_DATA = "suggest_platform_data"


class TenantAgent(Base):
    """One row per business that has a DCA agent registered. Disabled until explicitly enabled.

    Also the business's scoped agent configuration (DCA-024): ``enabled`` + ``data_mode``.
    """

    __tablename__ = "tenant_agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    country: Mapped[str] = mapped_column(String(2), default="IN")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    data_mode: Mapped[str] = mapped_column(
        String(32),
        default=DataMode.MY_DATA_ONLY.value,
        server_default=DataMode.MY_DATA_ONLY.value,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class IcpProfile(Base):
    """The tenant's Ideal Customer Profile — what the agent looks for. One active profile each."""

    __tablename__ = "icp_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    industries: Mapped[list] = mapped_column(JSON, default=list)
    regions: Mapped[list] = mapped_column(JSON, default=list)  # states/cities within India
    roles: Mapped[list] = mapped_column(JSON, default=list)  # roles sought, e.g. "owner"
    exclusions: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | paused
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class DiscoveryRun(TenantOwned, Base):
    """One agent cycle for one tenant — the explainability record of what happened and why."""

    __tablename__ = "discovery_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|completed|failed
    source_keys: Mapped[list] = mapped_column(JSON, default=list)
    found: Mapped[int] = mapped_column(Integer, default=0)
    screened_out: Mapped[int] = mapped_column(Integer, default=0)
    suggested: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class ProspectCandidate(TenantOwned, Base):
    """A suggestion awaiting tenant action. Suggested-only: nothing is pushed into the platform."""

    __tablename__ = "prospect_candidates"
    # Composite tenant constraint: org_identifier is unique PER business, not globally.
    __table_args__ = (tenant_unique("org_identifier", name="uq_candidate_org"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("discovery_runs.id"))
    org_name: Mapped[str] = mapped_column(String(300))
    org_identifier: Mapped[str] = mapped_column(String(64))  # CIN / GSTIN / registry ref
    contact: Mapped[dict] = mapped_column(JSON, default=dict)  # {kind, email?, phone?, role?}
    source_key: Mapped[str] = mapped_column(String(64))
    source_ref: Mapped[str] = mapped_column(String(500))  # registry URL / record locator
    trust_tier: Mapped[str] = mapped_column(String(32))  # official_registry | public_web | …
    match_score: Mapped[float] = mapped_column(Float, default=0.0)
    rationale: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="suggested", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class AcquisitionDecision(TenantOwned, Base):
    """Auditable per-record decision (mirror of the platform's SendDecision philosophy)."""

    __tablename__ = "acquisition_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("discovery_runs.id"), index=True)
    org_identifier: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(32))  # suggested | screened_out
    reason_code: Mapped[str] = mapped_column(String(64))  # ok | source_tier_disallowed | …
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class EntitlementProjection(Base):
    """A permitted, bounded cache of a business's last *active* entitlement snapshot (DCA-023).

    Platform-owned billing truth lives in the platform; this is only a projection the DCA keeps so it
    can authorize a chargeable operation during a brief entitlement-host outage. It is control-plane
    data (like tenant_agents), NOT tenant operational data — deliberately outside tenant RLS. Only an
    *active* snapshot is cached; on a live expiry/revocation the row is cleared, and ``captured_at``
    (the snapshot's as_of) bounds how long it may authorize before it is considered stale.
    """

    __tablename__ = "entitlement_projections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(16))  # mirrors EntitlementStatus
    plan: Mapped[str] = mapped_column(String(64), default="")
    capabilities: Mapped[list] = mapped_column(JSON, default=list)
    limits: Mapped[dict] = mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # the snapshot's as_of
    version: Mapped[str] = mapped_column(String(64))
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class AuditEvent(TenantOwned, Base):
    """DCA-026 — a durable, transactional audit event for a tenant-scoped mutation.

    Mirrors the platform's atomic-audit rule (SEC-001/BR-122): the row is staged into the *caller's*
    session so it commits in the SAME unit of work as the business change it describes — a failed
    transaction leaves neither the change nor its audit entry. The :class:`~app.audit.sink.AuditSink`
    is the only sanctioned writer; it stamps ``business_id`` (the scope) from the verified tenant
    context and refuses any payload carrying raw secrets or contact data, so this table holds only
    actor, scope, action, reason and OPAQUE references (ids/row keys), never personal data.

    Tenant-owned: it inherits the DCA-017 ``business_id`` column and is therefore covered by the
    DCA-018 row-level-security policy like every other tenant data table — one tenant can never read
    another's audit trail.
    """

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # WHO — an opaque actor id (subject/service GUID), never a name/email, plus its kind.
    actor_id: Mapped[str] = mapped_column(String(128))
    actor_type: Mapped[str] = mapped_column(
        String(32)
    )  # service | user | system | platform_support
    # WHAT — a controlled action code (e.g. "candidate.accept"); never free-form user text.
    action: Mapped[str] = mapped_column(String(128), index=True)
    # WHY — a reason code / short server-side explanation (auditable why-allowed/blocked).
    reason: Mapped[str] = mapped_column(Text, default="")
    # Opaque references only: ids/row keys the event points at — guarded free of secrets/contact data.
    references: Mapped[dict] = mapped_column(JSON, default=dict)
    # Trace join to the DCA-012 structured logs (nullable: writes can happen outside a request).
    correlation_id: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AiInvocation(TenantOwned, Base):
    """Durable record of every effortlesspromo-ai gateway call made by this service (B2/B4)."""

    __tablename__ = "ai_invocations"
    __table_args__ = (UniqueConstraint("request_id", name="uq_ai_request_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(String(64))  # "dca:<id>" — DCA-owned namespace
    request_id: Mapped[str] = mapped_column(String(64))  # X-Request-Id (deterministic)
    capability: Mapped[str] = mapped_column(String(64))
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(
        String(16), default="pending"
    )  # pending|completed|failed|unavailable
    response_status: Mapped[str | None] = mapped_column(String(16), default=None)
    cost_amount: Mapped[float] = mapped_column(Float, default=0.0)
    cost_currency: Mapped[str] = mapped_column(String(8), default="USD")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
