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


# Fixed namespace for deriving a STABLE outbox event id from a tenant's logical intent. uuid5 over
# ``(business_id, event_type, dedup_key)`` is deterministic, so re-staging the SAME logical intent
# always yields the SAME event id — the id is stable across retries, and the uniqueness constraint
# (below) turns a duplicate logical intent into a duplicate event id that the database rejects.
_OUTBOX_EVENT_NS = uuid.UUID("f0c1d2e3-4b5a-4c6d-8e7f-0a1b2c3d4e5f")


def outbox_event_id(business_id: str, event_type: str, dedup_key: str) -> str:
    """The stable, deterministic event id for one logical outbox intent within a business."""
    return str(uuid.uuid5(_OUTBOX_EVENT_NS, f"{business_id}\x1f{event_type}\x1f{dedup_key}"))


class DataMode(str, Enum):
    """How the agent is allowed to source prospects for a business (DCA-024).

    ``MY_DATA_ONLY`` (the conservative default) — work only the tenant's own data; never suggest
    platform-sourced prospects. ``SUGGEST_PLATFORM_DATA`` — may *suggest* platform-sourced prospects
    for review. Neither mode grants any sending right: suggestion is not consent and the DCA has no
    send path. Auto-accept is a separate, later capability and is NOT a mode here.
    """

    MY_DATA_ONLY = "my_data_only"
    SUGGEST_PLATFORM_DATA = "suggest_platform_data"


class OutboxStatus(str, Enum):
    """Delivery state of a transactional outbox message (DCA-027).

    ``PENDING`` — staged and committed with the local change, not yet handed to any consumer.
    ``DELIVERED`` — a later relay story (currently disabled) marked it handed off. This story only
    persists the record and its status; it performs NO remote delivery, so rows are only ever created
    ``PENDING`` here (``mark_delivered`` exists to model the lifecycle but is never driven by a send).
    """

    PENDING = "pending"
    DELIVERED = "delivered"


class RevisionStatus(str, Enum):
    """Reconciliation state of a revision-tracked aggregate projection (DCA-031).

    ``CURRENT`` — ``applied_version`` is the highest *contiguous* revision seen; nothing is buffered
    waiting on a missing predecessor. ``GAPPED`` — one or more newer revisions have arrived out of
    order and are buffered (:class:`RevisionPendingEvent`) pending a still-missing revision, so the
    gap is **visible** rather than guessed. A projection flips back to ``CURRENT`` once the missing
    revisions arrive and the buffered events are reconciled in order.
    """

    CURRENT = "current"
    GAPPED = "gapped"


class InboxStatus(str, Enum):
    """Processing state of an inbound-event receipt (DCA-028).

    ``PROCESSED`` — the event was handled and its receipt committed in the SAME unit of work as the
    handler's local change (transactional handler completion). Receipts are created ``PROCESSED`` by
    :class:`~app.inbox.sink.InboxSink` because completion is transactional here.

    ``PENDING`` — reserved for a future, currently-disabled at-least-once *claim/lease* capability
    (claim the event in one transaction, complete it in another). No path creates a ``PENDING``
    receipt in this story; :meth:`~app.inbox.sink.InboxSink.mark_processed` models the transition for
    that future capability but is never driven by a receive path here.
    """

    PENDING = "pending"
    PROCESSED = "processed"


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


class OutboxMessage(TenantOwned, Base):
    """DCA-027 — a durable, transactional outbox record for a tenant-scoped integration intent.

    Mirrors the platform's transactional-outbox discipline and this service's own atomic-audit rule
    (SEC-001/BR-122): the row is staged into the *caller's* session by
    :class:`~app.outbox.sink.OutboxSink`, so it commits in the SAME unit of work as the local state
    change it describes. A rolled-back transaction leaves neither the local change NOR its outbox
    intent — the headline acceptance criterion.

    Three load-bearing columns:

    - ``event_id`` — a STABLE, deterministic id (:func:`outbox_event_id`) derived from the logical
      intent, so re-staging the same intent re-derives the same id rather than inventing a new one.
    - ``version`` — the payload-contract version, so a later relay/consumer can evolve the envelope.
    - ``status`` — ``pending`` → ``delivered``; rows are created ``pending`` and NO remote delivery
      happens in this story (future relay capability stays disabled).

    ``(business_id, event_id)`` is unique, so a duplicate logical intent (same business, same derived
    event id) is rejected by the database — duplicate intents are constrained, not silently doubled.
    Tenant-owned: it inherits the DCA-017 ``business_id`` column and the DCA-018 row-level-security
    policy like every other tenant data table. Payload/keys hold only OPAQUE references (ids/row
    keys) — screened free of secrets/contact data by the sink before anything is staged.
    """

    __tablename__ = "outbox_messages"
    # The logical intent is unique PER business: the stable event id (derived from the intent) may
    # legitimately repeat across tenants but never within one.
    __table_args__ = (tenant_unique("event_id", name="uq_outbox_event"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # Stable, deterministic id for the logical intent (uuid5 over business + type + dedup key).
    event_id: Mapped[str] = mapped_column(String(36), index=True)
    # WHAT kind of event — a controlled type code (e.g. "candidate.accepted"); never free-form text.
    event_type: Mapped[str] = mapped_column(String(128), index=True)
    # The caller's logical-intent key (e.g. a row id + action); screened opaque, scoped per business.
    dedup_key: Mapped[str] = mapped_column(String(200))
    # Payload-contract version so a later consumer can evolve the envelope without ambiguity.
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    # Opaque references only (ids/row keys) — guarded free of secrets/contact data before staging.
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(
        String(16),
        default=OutboxStatus.PENDING.value,
        server_default=OutboxStatus.PENDING.value,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class InboxReceipt(TenantOwned, Base):
    """DCA-028 — a durable, idempotent-receiver receipt for one inbound event (tenant-owned).

    The inbound counterpart of the DCA-027 outbox: where the outbox makes a tenant's *outgoing*
    intents idempotent, the inbox makes *incoming* events idempotent. One receipt records that a
    given event — identified by its external ``(event_source, event_id)`` natural key — has been
    handled for a business.

    The guarantee is the headline acceptance criterion of this story. The receipt is staged into the
    *caller's* session by :class:`~app.inbox.sink.InboxSink`, so it commits in the SAME transaction as
    the handler's local change (transactional handler completion). Because
    ``(business_id, event_source, event_id)`` is unique:

    - **A replay cannot apply the local change twice.** Re-handling the same event re-stages the same
      receipt; the unique constraint rejects it at commit, so the caller rolls back and the handler's
      change is rolled back with it — the local effect is applied exactly once.
    - **Failed processing can retry safely.** If the handler fails, the caller rolls back; the receipt
      is removed along with the partial change, leaving nothing to block a clean re-run.

    The external ``event_id`` is NOT re-derived (unlike the outbox's deterministic id): the natural
    key comes from the sending system and is unique per business here, so two tenants may legitimately
    receive the same source + id and neither constrains the other.

    Tenant-owned: it inherits the DCA-017 ``business_id`` column and the DCA-018 row-level-security
    policy like every other tenant data table. ``event_source`` / ``event_id`` / ``event_type`` and
    every ``payload`` value are screened opaque (ids / row keys) by the sink before anything is
    staged — the receipt never holds secrets or contact data.
    """

    __tablename__ = "inbox_receipts"
    # The receipt is unique PER business on its external natural key: the same (source, id) may repeat
    # across tenants but never within one — that is what makes a replay a constraint violation.
    __table_args__ = (tenant_unique("event_source", "event_id", name="uq_inbox_event"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # WHERE the event came from — a controlled source code (e.g. "effortlesspromo-api"); the first
    # half of the dedup key. Screened opaque.
    event_source: Mapped[str] = mapped_column(String(128), index=True)
    # The sending system's own event id — the second half of the dedup key. Screened opaque.
    event_id: Mapped[str] = mapped_column(String(200), index=True)
    # WHAT kind of event — an optional controlled type code (e.g. "candidate.enriched").
    event_type: Mapped[str] = mapped_column(String(128), default="", server_default="")
    status: Mapped[str] = mapped_column(
        String(16),
        default=InboxStatus.PROCESSED.value,
        server_default=InboxStatus.PROCESSED.value,
        index=True,
    )
    # Opaque references only (ids / row keys) — guarded free of secrets/contact data before staging.
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # Set when the handler completed; populated at receive time because completion is transactional.
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class RevisionProjection(TenantOwned, Base):
    """DCA-031 — a revision-tracked projection of one aggregate (tenant-owned).

    When this service consumes integration events that *project* an aggregate's state (a consent
    record, an ICP profile, …), events can arrive out of order: a retried or delayed delivery may
    carry an OLDER revision than one already applied. This row records the highest **contiguous**
    revision applied for an aggregate — ``(business_id, aggregate_type, aggregate_id)`` — so the
    single sanctioned writer (:class:`~app.projection.revision.RevisionProjector`) can:

    - **Reject a stale revision.** An event whose ``aggregate_version`` is ``<=`` ``applied_version``
      is refused — an older consent/profile revision can never overwrite the current one.
    - **Make a gap visible, not guessed.** An event whose version is ``> applied_version + 1`` is
      NOT applied (the intervening revisions are missing and their state is unknown); it is buffered
      as a :class:`RevisionPendingEvent`, the projection is marked ``GAPPED``, and the buffered events
      are applied in order once the missing revision arrives (the explicit reconciliation path).

    ``state`` holds only OPAQUE references (ids / row keys) — screened free of secrets/contact data by
    the projector before anything is staged, exactly like the audit / outbox / inbox payloads.
    Tenant-owned: it inherits the DCA-017 ``business_id`` column and the DCA-018 row-level-security
    policy, so one tenant can never read another's projections.
    """

    __tablename__ = "revision_projections"
    # One projection per aggregate PER business; the same aggregate key may repeat across tenants.
    __table_args__ = (
        tenant_unique("aggregate_type", "aggregate_id", name="uq_revision_projection"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # WHICH kind of aggregate this projection tracks — a controlled code (e.g. "consent"). Opaque.
    aggregate_type: Mapped[str] = mapped_column(String(128), index=True)
    # The aggregate's own id within its type. Opaque (an id / row key), never contact data.
    aggregate_id: Mapped[str] = mapped_column(String(200), index=True)
    # Highest CONTIGUOUS revision applied. 0 means nothing has been applied yet (a gap from the start).
    applied_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # The projected state at ``applied_version`` — opaque references only.
    state: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(
        String(16),
        default=RevisionStatus.CURRENT.value,
        server_default=RevisionStatus.CURRENT.value,
        index=True,
    )
    # The event id that last advanced this projection (opaque); "" before anything is applied.
    last_event_id: Mapped[str] = mapped_column(String(200), default="", server_default="")
    last_occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class RevisionPendingEvent(TenantOwned, Base):
    """DCA-031 — a buffered, out-of-order revision awaiting a missing predecessor (tenant-owned).

    When a projected event arrives ahead of its turn (``aggregate_version > applied_version + 1``),
    its state cannot be applied yet — the revisions in between are missing and must not be guessed.
    The event is buffered here so the gap is **visible** (queryable) and so it can be applied, in
    order, once the gap is filled (the reconciliation path). A buffered revision is unique per
    aggregate (``(business_id, aggregate_type, aggregate_id, aggregate_version)``), so a re-delivery
    of the same out-of-order revision is not double-buffered.

    ``state`` holds only OPAQUE references, screened by the projector before staging. Tenant-owned, so
    it inherits the DCA-017 ``business_id`` column and the DCA-018 RLS policy like every other tenant
    data table.
    """

    __tablename__ = "revision_pending_events"
    __table_args__ = (
        tenant_unique(
            "aggregate_type", "aggregate_id", "aggregate_version", name="uq_revision_pending"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    aggregate_type: Mapped[str] = mapped_column(String(128), index=True)
    aggregate_id: Mapped[str] = mapped_column(String(200), index=True)
    # The out-of-order revision this buffered event carries (always > the projection's applied_version
    # when staged).
    aggregate_version: Mapped[int] = mapped_column(Integer, index=True)
    # The event id that produced this buffered revision (opaque).
    event_id: Mapped[str] = mapped_column(String(200), default="", server_default="")
    # The state this revision WOULD project once the gap before it is filled — opaque references only.
    state: Mapped[dict] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


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
