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
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false as sa_false,
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


class ScoreStatus(str, Enum):
    """Recomputation state of a recommendation's match score (DCA-043).

    ``CURRENT`` — the ``match_score`` reflects the targeting that produced it (the ``scored_targeting_version``
    it was computed against). ``STALE`` — a *confirmed targeting revision change* (a new ICP version
    confirmed, DCA-040) has invalidated the score: it is flagged for recomputation by a future (disabled)
    rescore capability. Flagging a score stale is a score-only change — it NEVER alters the recommendation's
    ``status`` (its acceptance/approval) and grants/revokes no sending authority (the DCA has no send path).
    """

    CURRENT = "current"
    STALE = "stale"


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


class ProfileStatus(str, Enum):
    """Lifecycle state of a business-profile row (DCA-036).

    ``DRAFT`` — a versioned, reviewable draft of the business profile. Every save creates a NEW draft
    version; prior versions are retained, never overwritten. ``CONFIRMED`` — the single accepted
    profile a draft may one day be promoted to. **Promotion/confirmation is a DISABLED future
    capability** (its own later story): no path in this service writes a ``CONFIRMED`` row, so saving a
    draft can never silently replace the confirmed profile.
    """

    DRAFT = "draft"
    CONFIRMED = "confirmed"


class SourceKind(str, Enum):
    """The provenance class of a catalog source (DCA-044).

    ``OFFICIAL`` — an official/government registry or dataset (e.g. a company registry). ``COMMERCIAL``
    — a licensed commercial data provider. The kind is descriptive metadata only: neither kind, on
    creation, grants any usage right — a newly catalogued source of either kind is a DISABLED draft.
    """

    OFFICIAL = "official"
    COMMERCIAL = "commercial"


class SourceAccessMethod(str, Enum):
    """How a catalogued source's data is obtained (DCA-044).

    Descriptive metadata; declaring an access method enables nothing on its own. ``API`` — a queryable
    API/endpoint; ``BULK_DOWNLOAD`` — a downloadable dataset/dump; ``PORTAL_LOOKUP`` — a human/portal
    lookup; ``MANUAL`` — manually supplied records.
    """

    API = "api"
    BULK_DOWNLOAD = "bulk_download"
    PORTAL_LOOKUP = "portal_lookup"
    MANUAL = "manual"


class SourceStatus(str, Enum):
    """Lifecycle state of a catalog source (DCA-044).

    ``DRAFT`` — a reviewable, DISABLED draft. Every source is created here as a draft and is NOT usable:
    it grants no acquisition right and no discovery adapter may consult it. ``ENABLED`` — a future,
    approved/active source a draft may one day be promoted to. **Promotion/approval (and the source's
    rights details) is a DISABLED future capability** (its own later story, gated behind a rights
    review): no path in this service writes an ``ENABLED`` source, so cataloguing a draft can never
    silently make a source usable.
    """

    DRAFT = "draft"
    ENABLED = "enabled"


class SourceOperation(str, Enum):
    """The distinct *uses* a source's terms grant (or withhold) a right for, per field (DCA-045).

    Source rights are modelled per operation — never as one legal boolean — so a terms version can,
    for example, permit ``DISPLAY`` of a field but withhold ``EXPORT`` of it. The lifecycle a datum
    travels, each gated independently:

    - ``COLLECT`` — acquire the datum from the source at all.
    - ``STORE`` — retain it durably in this service.
    - ``DERIVE`` — compute derived facts/signals from it (e.g. scoring features).
    - ``DISPLAY`` — show it back to the owning business in-product.
    - ``EXPORT`` — hand it out of the service (download / push to the platform / to a tenant).
    - ``SHARE`` — disclose it to a third party / across businesses.
    - ``MODEL_PROCESSING`` — send it to the AI gateway for model processing.
    """

    COLLECT = "collect"
    STORE = "store"
    DERIVE = "derive"
    DISPLAY = "display"
    EXPORT = "export"
    SHARE = "share"
    MODEL_PROCESSING = "model_processing"


class SourceTermsStatus(str, Enum):
    """Lifecycle state of a source terms version (DCA-045).

    ``DRAFT`` — a reviewable, NOT-yet-active terms version. Every terms version is created here as a
    draft. Recording a version documents what a source's contract *would* permit; it does not, on its
    own, put any right in force — acting on these rights additionally requires the source to be an
    ENABLED catalog entry (DCA-044), which stays a DISABLED future capability. ``ACTIVE`` is the state
    a future, disabled approval story would promote one reviewed version to; no path in this service
    writes an ``ACTIVE`` terms version, so cataloguing rights can never silently authorize acquisition.
    """

    DRAFT = "draft"
    ACTIVE = "active"


class SourceApprovalStatus(str, Enum):
    """Lifecycle state of a source-configuration approval (DCA-046).

    ``ACTIVE`` — the approval a platform operator recorded is in force: it endorses one reviewed
    :class:`SourceTerms` version and carries the evidence, reviewer, review date and expiry of the
    review. ``REVOKED`` is the state a future, disabled revoke capability would move it to; no path
    in this service writes a ``REVOKED`` approval, so an approval can only be created, never silently
    withdrawn here. An approval is platform control-plane data and, on its own, enables no acquisition
    (the source catalog entry staying ENABLED is a separate, disabled capability — DCA-044).
    """

    ACTIVE = "active"
    REVOKED = "revoked"


class BusinessProfileDraft(TenantOwned, Base):
    """DCA-036 — a scoped, versioned draft of a business's profile (tenant-owned, RLS-protected).

    One row is one *version* of the tenant's own business profile — what the agent learns/records about
    the business itself (its products, business type, value proposition, languages, and the specific
    facts it has confirmed). Each save inserts a new version (``(business_id, version)`` is unique), so
    a new draft **preserves** every prior version rather than mutating it; there is no update path.

    ``unknowns`` makes the profile honest about what is *not* yet known: canonical aspects left empty
    are retained there explicitly (``app.services.profile_store``) rather than silently dropped, so a
    reader can tell "unknown" apart from "known to be empty". The aspects named in ``unknowns`` and the
    populated aspects are kept disjoint by the writer.

    ``status`` separates a reviewable DRAFT from the accepted CONFIRMED profile. Confirmation is a
    DISABLED future capability (its own later story): :class:`~app.services.profile_store.ProfileStore`
    only ever stages ``status='draft'`` rows and never mutates an existing one, so a draft can never
    silently replace the confirmed profile.

    ``source`` / ``source_version`` / ``provenance`` carry the DCA-037 *provenance* of an imported
    draft: a draft built by importing an authorized platform profile snapshot records WHERE the facts
    came from (``source``, e.g. ``platform_profile_snapshot``) and the exact source contract/snapshot
    version (``source_version``) they were read at, so an imported draft always retains its source
    version and stays a reviewable DRAFT. ``provenance`` is the self-describing record (source, source
    version, the snapshot's ``as_of``, whether it was synthetic). A manually-authored draft (DCA-036)
    leaves all three empty; only an import populates them. The draft's business scope is its
    ``business_id`` — an imported snapshot is always stored under the tenant it was fetched for.

    This is the tenant's OWN business content (not prospect contact data or secrets), so — unlike the
    audit/outbox/inbox/revision payloads — its fields are NOT opaque-reference screened. Tenant-owned:
    it inherits the DCA-017 ``business_id`` column and the DCA-018 RLS policy like every other tenant
    data table, so one tenant can never read another's profile versions.
    """

    __tablename__ = "business_profile_drafts"
    # A version number is unique PER business: the same ordinal recurs across tenants but never within
    # one, so a new version can never collide with or overwrite an existing version of the same tenant.
    __table_args__ = (tenant_unique("version", name="uq_profile_draft_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # 1-based version ordinal within the business; a new draft is always max(version)+1.
    version: Mapped[int] = mapped_column(Integer, default=1)
    # The tenant's own business content — free text/lists (NOT opaque references, NOT contact data).
    products: Mapped[list] = mapped_column(JSON, default=list)
    business_type: Mapped[str] = mapped_column(String(200), default="", server_default="")
    value_proposition: Mapped[str] = mapped_column(Text, default="", server_default="")
    languages: Mapped[list] = mapped_column(JSON, default=list)
    # Facts the tenant has explicitly confirmed true about itself (opaque strings, retained as given).
    confirmed_facts: Mapped[list] = mapped_column(JSON, default=list)
    # Canonical aspects explicitly retained as NOT-yet-known (never silently dropped). Disjoint from the
    # populated aspects — the writer keeps "unknown" and "known" from overlapping.
    unknowns: Mapped[list] = mapped_column(JSON, default=list)
    # draft (reviewable, versioned) vs confirmed (the accepted profile). Confirmation is disabled, so
    # every row written here is 'draft'; nothing promotes a draft to 'confirmed' yet.
    status: Mapped[str] = mapped_column(
        String(16),
        default=ProfileStatus.DRAFT.value,
        server_default=ProfileStatus.DRAFT.value,
        index=True,
    )
    # DCA-037 provenance — empty for a manually-authored draft; populated when a draft is imported from
    # an authorized platform profile snapshot. ``source`` names the origin contract; ``source_version``
    # is the exact snapshot/contract version the facts were read at (retained so an imported draft can
    # always be traced back to its source version). ``provenance`` is the full self-describing record.
    source: Mapped[str] = mapped_column(String(64), default="", server_default="")
    source_version: Mapped[str] = mapped_column(String(64), default="", server_default="")
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


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
    """The tenant's Ideal Customer Profile — what the agent looks for. One active profile each.

    This is the EARLY single-active ICP record (platform-owned control-plane catalog, like
    :class:`TenantAgent`, deliberately outside tenant RLS). The *reviewable, versioned* ideal-customer
    profile drafts live in :class:`IcpDraft` (DCA-039); this row is retained for the existing
    ``PUT /v1/tenants/{id}/icp`` upsert and is not touched by the draft store.
    """

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


class IcpDraft(TenantOwned, Base):
    """DCA-039 — a scoped, versioned draft Ideal Customer Profile (tenant-owned, RLS-protected).

    One row is one *version* of the tenant's ideal-customer profile: the structured target the agent
    should look for — ``industries``, ``size_bands`` (a controlled vocabulary), ``roles``, ``geography``
    (recognized Indian states/UTs — the service is India-only), and ``exclusions``. Each save inserts a
    new version (``(business_id, version)`` is unique), so a new draft **preserves** every prior version
    rather than mutating it; there is no update path, mirroring :class:`BusinessProfileDraft` (DCA-036).

    ``profile_version`` links the ICP draft to the business-profile draft version it was authored
    against (the DCA-036 :class:`BusinessProfileDraft` ``version`` for the SAME business), so an ICP can
    be reviewed alongside the business profile it describes. It is optional (``None`` when no profile
    version is linked); when supplied it must name an existing draft version of this business —
    :class:`~app.services.icp_store.IcpStore` rejects an unknown version before staging anything.

    ``status`` separates a reviewable DRAFT from a future accepted/active ICP. Promotion is a DISABLED
    future capability (its own later story): the store only ever stages ``status='draft'`` rows and
    never mutates an existing one, so a draft can never silently become the active targeting profile.

    This is the tenant's OWN targeting content (not prospect contact data or secrets), so — unlike the
    audit/outbox/inbox/revision payloads — its fields are NOT opaque-reference screened. Tenant-owned:
    it inherits the DCA-017 ``business_id`` column and the DCA-018 RLS policy like every other tenant
    data table, so one tenant can never read another's ICP versions.
    """

    __tablename__ = "icp_drafts"
    # A version number is unique PER business: the same ordinal recurs across tenants but never within
    # one, so a new version can never collide with or overwrite an existing version of the same tenant.
    __table_args__ = (tenant_unique("version", name="uq_icp_draft_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # 1-based version ordinal within the business; a new draft is always max(version)+1.
    version: Mapped[int] = mapped_column(Integer, default=1)
    # The structured target — free-text/controlled lists (NOT opaque references, NOT contact data).
    industries: Mapped[list] = mapped_column(JSON, default=list)
    # Controlled vocabulary (app.services.icp_store.SIZE_BANDS); an unsupported band is a 422.
    size_bands: Mapped[list] = mapped_column(JSON, default=list)
    roles: Mapped[list] = mapped_column(JSON, default=list)
    # Recognized Indian states/UTs (ISO 3166-2:IN codes); an unrecognized value is a 422 (India-only).
    geography: Mapped[list] = mapped_column(JSON, default=list)
    exclusions: Mapped[list] = mapped_column(JSON, default=list)
    # Optional link to the DCA-036 business-profile draft version this ICP was authored against. NULL
    # when unlinked; when set it must be an existing draft version of this business.
    profile_version: Mapped[int | None] = mapped_column(Integer, default=None)
    # draft (reviewable, versioned) vs a future active/accepted ICP. Promotion is disabled, so every
    # row written here is 'draft'; nothing promotes a draft to 'active' yet.
    status: Mapped[str] = mapped_column(
        String(16), default="draft", server_default="draft", index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class IcpConfirmation(TenantOwned, Base):
    """DCA-040 — the ONE confirmed/active ICP version for a business (tenant-owned, RLS-protected).

    An explicit confirmation operation selects which :class:`IcpDraft` version is *active* for the
    business, recording WHO confirmed it (``actor_id`` / ``actor_type`` — an opaque subject id, never a
    name/email) and WHEN (``confirmed_at``). There is exactly ONE such row per business
    (``business_id`` is unique), so a business has at most one active ICP version at a time; confirming
    a different version re-points this single row rather than inserting a second.

    This is the *pointer*, kept separate from the immutable DCA-039 :class:`IcpDraft` version history:
    drafts stay insert-only (editing an ICP always creates a NEW draft version and never touches this
    row), so a freshly-edited draft does NOT become active — the previously-confirmed version stays
    active until a new version is explicitly confirmed. Only a draft version that exists for this
    business AND carries material targeting can be confirmed (see
    :mod:`app.services.icp_confirmation`); an unknown version or an empty (no-material-targeting) draft
    is refused before this row is touched.

    Confirmation selects the active targeting version ONLY. It grants no campaign/sending authority —
    the DCA has no send path, and acquisition/consent/suppression remain platform-gated — so marking a
    version active can never, on its own, cause a message to be sent.

    Tenant-owned: it inherits the DCA-017 ``business_id`` column and the DCA-018 RLS policy like every
    other tenant data table, so one tenant can never read or change another's active ICP.
    """

    __tablename__ = "icp_confirmations"
    # Exactly one confirmation (active version) per business — a business never has two active ICPs.
    __table_args__ = (UniqueConstraint("business_id", name="uq_icp_confirmation_business"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # The active IcpDraft version for this business (the version that was confirmed).
    version: Mapped[int] = mapped_column(Integer)
    # WHO confirmed — an opaque subject/service id (never a name/email) plus its kind. Supplied by the
    # caller (the service-auth gate authenticates the calling service, not the acting user) and screened
    # for secret/contact data by the audit sink in the SAME unit of work.
    actor_id: Mapped[str] = mapped_column(String(128))
    actor_type: Mapped[str] = mapped_column(String(32), default="user", server_default="user")
    # WHEN the active version was last confirmed (set/updated on every confirm).
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class BudgetLimit(TenantOwned, Base):
    """DCA-042 — a scoped, versioned set of per-business spending limits (tenant-owned, RLS-protected).

    One row is one *version* of a business's spend caps: how much the agent is allowed to spend in a
    civil day, in a civil month, and in a single agent run. Each save inserts a NEW version
    (``(business_id, version)`` is unique), so a new set of limits **preserves** every prior version
    rather than mutating it; there is no update path, mirroring :class:`BusinessProfileDraft` (DCA-036)
    and :class:`IcpDraft` (DCA-039). The highest version is the one in force; older versions are kept
    as an immutable, auditable history of what the caps were and who changed them.

    **Precise monetary values.** Amounts are stored as **integer minor units** (INR *paise*), never a
    floating-point rupee value, so a limit is exact and never accrues binary-float rounding error. The
    service is India-only, so ``currency`` is always ``INR`` (the store refuses anything else). A major
    (rupee) view is derived for display only; the minor-unit integer is the source of truth.

    **Timezone semantics.** ``timezone`` is the IANA zone whose civil calendar defines when the *daily*
    and *monthly* windows begin and end (e.g. a "day" is midnight-to-midnight in that zone, not UTC).
    India observes a single zone, so it is ``Asia/Kolkata``; the store validates it and normalizes the
    legacy ``Asia/Calcutta`` alias to it. Storing the zone with the caps keeps the limit unambiguous:
    the same paise figure means a different wall-clock window in a different zone, so the window is
    pinned here rather than guessed by a reader.

    ``actor_id`` / ``actor_type`` record WHO set this version (an opaque subject id, never a name/email)
    — the same actor the atomic DCA-026 audit entry carries. Setting caps is an administrator action
    (:data:`~app.identity.permissions.Scope.BUDGET_MANAGE`); an ordinary user cannot create a version.

    Storing a limit grants no spend: the DCA has no send/charge path here, and these caps are the
    ceiling a *future* (disabled) spending capability would enforce. Tenant-owned: it inherits the
    DCA-017 ``business_id`` column and the DCA-018 RLS policy like every other tenant data table, so one
    tenant can never read or change another's spending limits.
    """

    __tablename__ = "budget_limits"
    # A version number is unique PER business: the same ordinal recurs across tenants but never within
    # one, so a new version can never collide with or overwrite an existing version of the same tenant.
    __table_args__ = (tenant_unique("version", name="uq_budget_limit_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # 1-based version ordinal within the business; a new set of limits is always max(version)+1.
    version: Mapped[int] = mapped_column(Integer, default=1)
    # India-only: always "INR". The store refuses any other currency (a clear 422).
    currency: Mapped[str] = mapped_column(String(3), default="INR", server_default="INR")
    # Caps in INR MINOR units (paise) — integer, exact, never a float. Non-negative (validated).
    daily_cap_minor: Mapped[int] = mapped_column(BigInteger)
    monthly_cap_minor: Mapped[int] = mapped_column(BigInteger)
    per_run_cap_minor: Mapped[int] = mapped_column(BigInteger)
    # The IANA zone whose civil day/month bounds the daily/monthly windows (India: "Asia/Kolkata").
    timezone: Mapped[str] = mapped_column(
        String(64), default="Asia/Kolkata", server_default="Asia/Kolkata"
    )
    # WHO set this version — an opaque subject id (never a name/email) plus its kind, screened by the
    # audit sink in the SAME unit of work as the row.
    actor_id: Mapped[str] = mapped_column(String(128))
    actor_type: Mapped[str] = mapped_column(String(32), default="user", server_default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


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
    # DCA-043 — recomputation state of ``match_score``. ``current`` until a confirmed targeting revision
    # change (a new ICP version confirmed) invalidates it, flipping it to ``stale`` (flagged for a future,
    # disabled rescore). This is a SCORE-only marker: it is kept strictly separate from ``status`` (the
    # recommendation's acceptance/approval) so invalidating a score never alters approval or grants any
    # sending right.
    score_status: Mapped[str] = mapped_column(
        String(16),
        default=ScoreStatus.CURRENT.value,
        server_default=ScoreStatus.CURRENT.value,
        index=True,
    )
    # The confirmed ICP version ``match_score`` was computed against, or NULL for a placeholder score
    # produced before any targeting was confirmed (discovery's deterministic default). A score is
    # "affected" by a confirmed targeting change when this differs from the newly-confirmed version.
    scored_targeting_version: Mapped[int | None] = mapped_column(Integer, default=None)
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


class Source(Base):
    """DCA-044 — a platform-owned catalog entry describing a prospect data source.

    One row is one *source* the Data Concierge could, once approved, acquire prospects from — an
    official registry or a licensed commercial dataset. It records the source's declared shape: its
    ``kind`` (official/commercial), the ``capabilities`` it offers, the ``fields`` it can return, the
    ``access_method`` by which its data is obtained, its declared ``trust_tier`` (what the acquisition
    rulepack screens on), and its ``india_coverage`` — the service is India-only, so coverage is
    expressed as recognized Indian state/UT codes and/or a nationwide flag.

    **Disabled draft by default, no implicit permissions.** A newly catalogued source is ALWAYS a
    ``DRAFT`` with ``enabled=False`` and an EMPTY ``permissions`` set: its mere existence grants nothing
    and no discovery path may consult it (:class:`~app.services.source_catalog.SourceCatalogStore`
    treats a draft as not usable). Approving/enabling a source — and defining its rights — is a separate,
    later capability that stays DISABLED here (the store only ever writes drafts and never flips
    ``enabled`` or promotes to ``ENABLED``). So cataloguing a source can never, on its own, authorize
    any acquisition.

    This is platform-sourced CONTROL-PLANE data — a shared catalog across all tenants, like
    :class:`TenantAgent` / :class:`EntitlementProjection` — deliberately OUTSIDE tenant RLS and never
    tenant-scoped. It holds only the source's own descriptive metadata: no prospect contact data and no
    secrets.
    """

    __tablename__ = "sources"
    # The catalog key is globally unique — two catalog entries can never share one key.
    __table_args__ = (UniqueConstraint("key", name="uq_source_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # Stable catalog key / slug (e.g. "mca_master_data"); unique across the catalog. Opaque, not free text.
    key: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(200), default="", server_default="")
    # official | commercial — descriptive provenance class; neither grants a usage right on creation.
    kind: Mapped[str] = mapped_column(String(16), index=True)
    # The source's declared trust tier (e.g. "official_registry"); the acquisition rulepack screens on it.
    trust_tier: Mapped[str] = mapped_column(String(32), default="", server_default="")
    # What the source offers / which fields it can return — declarative string lists (NOT contact data).
    capabilities: Mapped[list] = mapped_column(JSON, default=list)
    fields: Mapped[list] = mapped_column(JSON, default=list)
    # api | bulk_download | portal_lookup | manual — how the data is obtained.
    access_method: Mapped[str] = mapped_column(String(32))
    # India coverage (India-only service): {"nationwide": bool, "states": [ISO 3166-2:IN codes]}.
    india_coverage: Mapped[dict] = mapped_column(JSON, default=dict)
    # draft (reviewable, DISABLED) vs enabled (approved/active). Approval is disabled, so every row
    # written here is 'draft'; nothing promotes a draft to 'enabled' yet.
    status: Mapped[str] = mapped_column(
        String(16),
        default=SourceStatus.DRAFT.value,
        server_default=SourceStatus.DRAFT.value,
        index=True,
    )
    # Disabled by default — a draft source is never usable. Flipped on only by a future approval
    # capability (which stays disabled here), so its mere creation enables nothing.
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_false())
    # No implicit permissions: a draft carries an EMPTY permission set. A later rights/approval story
    # populates this; cataloguing a source grants none of them.
    permissions: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class SourceTerms(Base):
    """DCA-045 — one *version* of a catalog source's field-and-operation rights.

    A terms version records what the source's contract/licence permits, expressed NOT as a single
    legal boolean but as rights that can differ per field and per use (:class:`SourceOperation`):

    - ``operation_rights`` — the source-wide default grant for each of the seven operations
      (collect/store/derive/display/export/share/model_processing). Deny-by-default: an operation
      absent here is not granted.
    - ``field_rights`` — per-field OVERRIDES: ``{field: {operation: granted}}``. A field named here
      overrides the source-wide default for exactly the operations it lists (so one field can be
      display-only while another is also exportable); a field not named inherits the defaults.
    - ``purposes`` — the purposes the rights are bound to (rights are purpose-limited, not blanket).
    - ``attribution`` — any attribution the source requires when its data is shown/used.
    - ``retention`` — how long the data may be retained (``{"max_days": int|None, "policy": str}``).
    - ``contract_evidence`` — the evidence the right exists (``{"reference", "kind", "uri"}``); a
      terms version cannot be staged without it.
    - ``effective_from`` / ``expires_at`` — the right's validity window is EXPLICIT: an expired
      version grants nothing (:class:`~app.services.source_terms.SourceTermsStore` denies with a
      stable code), rather than a boolean that never lapses.

    Versioned per source: ``(source_key, version)`` is unique and the store is insert-only, so every
    prior version is preserved for review and a version never overwrites another. ``status`` is always
    ``DRAFT`` — a terms version is reviewable but never in force on its own; promotion to ``ACTIVE``,
    and the source-enable capability it would pair with, stay DISABLED future stories.

    Like :class:`Source`, this is platform CONTROL-PLANE data — a shared registry of source rights,
    deliberately OUTSIDE tenant RLS and never tenant-scoped. It holds only declarative rights metadata:
    no prospect contact data and no secrets.
    """

    __tablename__ = "source_terms"
    # One version per source; a (source_key, version) pair is never reused — insert-only history.
    __table_args__ = (UniqueConstraint("source_key", "version", name="uq_source_terms_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # The catalog key of the Source these terms describe (DCA-044); validated to exist by the store.
    source_key: Mapped[str] = mapped_column(String(64), index=True)
    # Monotonic per-source version (1-based); explicit, so rights history is reviewable version-by-version.
    version: Mapped[int] = mapped_column(Integer)
    # Source-wide default grant per operation: {operation: bool}. Deny-by-default (missing => not granted).
    operation_rights: Mapped[dict] = mapped_column(JSON, default=dict)
    # Per-field overrides: {field: {operation: bool}} — lets rights differ per field (display-only vs export).
    field_rights: Mapped[dict] = mapped_column(JSON, default=dict)
    # The purposes the rights are limited to (rights are purpose-bound, not blanket).
    purposes: Mapped[list] = mapped_column(JSON, default=list)
    # Attribution the source requires: {"required": bool, "notice": str}.
    attribution: Mapped[dict] = mapped_column(JSON, default=dict)
    # Retention terms: {"max_days": int|None, "policy": str}.
    retention: Mapped[dict] = mapped_column(JSON, default=dict)
    # Evidence the right exists (contract/licence): {"reference": str, "kind": str, "uri": str}. Required.
    contract_evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    # Explicit validity window — rights lapse rather than living in one never-expiring boolean.
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # draft (reviewable, NOT in force) vs active (approved). Promotion is disabled, so every row here
    # is 'draft'; recording rights never puts them in force on its own.
    status: Mapped[str] = mapped_column(
        String(16),
        default=SourceTermsStatus.DRAFT.value,
        server_default=SourceTermsStatus.DRAFT.value,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class SourceTermsApproval(Base):
    """DCA-046 — a platform operator's review/approval of one source-terms configuration version.

    Approving a source *configuration* is the capability that lets a reviewed :class:`SourceTerms`
    version (DCA-045) be accepted independently. One row is one approval: it endorses exactly one
    ``(source_key, terms_version)`` and records the four review essentials the story requires —

    - ``review_evidence`` — the evidence the operator relied on to approve (``{reference, kind, uri}``);
      an approval cannot be staged without a non-empty ``reference`` (a bare approval is refused).
    - ``reviewer_id`` / ``reviewer_type`` — WHO approved: an opaque subject id of a *platform
      operator* (never a name/email). Approval is a platform-plane action
      (:attr:`~app.identity.permissions.Scope.SOURCE_TERMS_APPROVE`); a tenant administrator can never
      perform it — the catalog is shared control-plane data, not a tenant resource.
    - ``reviewed_at`` — the review DATE (when the operator reviewed), required.
    - ``expires_at`` — the approval's EXPLICIT expiry, required: an approval lapses rather than living
      forever, so a stale endorsement must be re-reviewed. ``effective_from`` optionally delays it.

    ``approved_operation_rights`` / ``approved_field_rights`` are a SNAPSHOT of the rights the version
    documented at approval time — the approval endorses exactly the documented field/use rights and
    can never exceed them (:class:`~app.services.source_approval.SourceApprovalStore` refuses a request
    to approve a right the version does not document). The snapshot makes the approval self-describing:
    a reader sees precisely which field/use rights were accepted, independent of later versions.

    Like :class:`Source` / :class:`SourceTerms`, this is platform CONTROL-PLANE data — a shared
    registry, deliberately OUTSIDE tenant RLS and never tenant-scoped. It holds only the review record
    (opaque reviewer id + declarative rights snapshot): no prospect contact data and no secrets.
    Approving a version promotes that :class:`SourceTerms` row to ``active``, but grants NO acquisition
    right on its own — the source catalog entry must additionally be ENABLED (DCA-044), which stays a
    disabled future capability, so an approval never, by itself, authorizes acquisition.
    """

    __tablename__ = "source_term_approvals"
    # One approval per terms version: a (source_key, terms_version) pair is approved at most once, so
    # a double approval is a constraint violation, never a silent second endorsement.
    __table_args__ = (
        UniqueConstraint("source_key", "terms_version", name="uq_source_term_approval_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # The catalog key + version of the SourceTerms this approval endorses (validated to exist).
    source_key: Mapped[str] = mapped_column(String(64), index=True)
    terms_version: Mapped[int] = mapped_column(Integer)
    # WHO approved — an opaque platform-operator subject id (never a name/email) plus its kind.
    reviewer_id: Mapped[str] = mapped_column(String(128))
    reviewer_type: Mapped[str] = mapped_column(
        String(32), default="platform_operator", server_default="platform_operator"
    )
    # The evidence the operator relied on: {"reference": str, "kind": str, "uri": str}. Required.
    review_evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    # WHEN the operator reviewed (the review date) — required.
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The approval's EXPLICIT validity window — it lapses rather than living in a never-expiring flag.
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Snapshot of the field/use rights the version documented and this approval endorsed — never more
    # than the version documents (the store refuses an over-reaching approval before staging).
    approved_operation_rights: Mapped[dict] = mapped_column(JSON, default=dict)
    approved_field_rights: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(
        String(16),
        default=SourceApprovalStatus.ACTIVE.value,
        server_default=SourceApprovalStatus.ACTIVE.value,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class BusinessSourceEntitlement(TenantOwned, Base):
    """DCA-047 — a business's own, versioned entitlement to a platform-approved source (tenant-owned).

    One row is one *version* of a single business's decision to use one catalog :class:`Source` and
    the field/operation/purpose rights it grants ITSELF for that source. It exists to NARROW the
    platform-approved rights down to what the business wants to exercise — never to widen them. The
    platform ceiling is the in-force :class:`SourceTermsApproval` (DCA-046): a business can only
    entitle a source that is approved at the platform level, and every operation / field right /
    purpose it grants must be a subset of what that approval documents. The single sanctioned writer
    (:class:`~app.services.source_entitlement.BusinessSourceEntitlementStore`) refuses, before staging
    anything, an attempt to enable an unapproved source or to grant a right the platform denies — so a
    business can never, through its own configuration, reach a capability the platform withheld.

    - ``enabled`` — the business's own enable/disable intent for the source. An entitlement can only be
      saved (enabled OR disabled) against a source with an in-force platform approval; enabling grants
      NO acquisition right on its own (no discovery path consults this table — that stays a disabled
      future capability), it merely records the narrowed rights the business has opted into.
    - ``operation_rights`` — the business's narrowed source-wide grant per operation ({operation: bool},
      deny-by-default), always a subset of the approval's ``approved_operation_rights``.
    - ``field_rights`` — the business's narrowed per-field overrides ({field: {operation: bool}}),
      each a subset of the approval's effective (field-override-or-default) grant for that field.
    - ``purposes`` — the purposes the business limits its use to; a subset of the approved terms'
      purposes (rights stay purpose-bound, never widened).
    - ``approved_terms_version`` — the platform terms version (DCA-045/046) this entitlement was
      narrowed against, so a reader can tell exactly which platform ceiling it was checked under.

    Versioned per source: ``(business_id, source_key, version)`` is unique and the store is
    insert-only, so every prior version is preserved as an immutable, auditable history and a new
    version never overwrites another; the highest version for a ``(business, source)`` is the one in
    force. Tenant-owned: it inherits the DCA-017 ``business_id`` column and the DCA-018 RLS policy like
    every other tenant data table, so one business can never read or change another's entitlements. It
    holds only declarative rights metadata (opaque source key + boolean grants): no prospect contact
    data and no secrets.
    """

    __tablename__ = "business_source_entitlements"
    # A (source, version) pair is unique PER business: the same source/version recurs across tenants
    # but never within one, so a new version can never collide with or overwrite an existing version.
    __table_args__ = (
        tenant_unique("source_key", "version", name="uq_business_source_entitlement_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # The catalog key of the Source this entitlement narrows (DCA-044); validated to be platform-approved.
    source_key: Mapped[str] = mapped_column(String(64), index=True)
    # 1-based version ordinal within (business, source); a new entitlement is always max(version)+1.
    version: Mapped[int] = mapped_column(Integer, default=1)
    # The business's own enable intent for the source. Enabling grants no acquisition right on its own
    # (no discovery path consults this) — acquisition stays a disabled future capability.
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_false())
    # Narrowed source-wide grant per operation: {operation: bool}. Always a subset of the platform
    # approval's documented operation rights (deny-by-default; a right the platform denies is refused).
    operation_rights: Mapped[dict] = mapped_column(JSON, default=dict)
    # Narrowed per-field overrides: {field: {operation: bool}} — each a subset of the approved rights.
    field_rights: Mapped[dict] = mapped_column(JSON, default=dict)
    # The purposes the business limits use to — a subset of the approved terms' purposes.
    purposes: Mapped[list] = mapped_column(JSON, default=list)
    # The platform terms version (DCA-045/046) this entitlement was narrowed against (the ceiling it
    # was checked under), so a reader knows exactly which approved rights snapshot it is a subset of.
    approved_terms_version: Mapped[int] = mapped_column(Integer)
    # WHO set this version — an opaque subject id (never a name/email) plus its kind, screened by the
    # audit sink in the SAME unit of work as the row.
    actor_id: Mapped[str] = mapped_column(String(128))
    actor_type: Mapped[str] = mapped_column(String(32), default="user", server_default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


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


class IdempotencyRecord(TenantOwned, Base):
    """DCA-035 — a durable, tenant/route-scoped request-idempotency record (tenant-owned).

    Protects a *mutation request* against accidental re-execution (a client retry, a double
    submit, a network hiccup). One record is the stored outcome of one logical write, identified by
    the caller's ``Idempotency-Key`` within a single ``(business_id, method, route)`` — so the key is
    scoped to the tenant AND the route, never global.

    Three load-bearing columns, mirroring the platform's idempotency discipline (BR-124):

    - ``idempotency_key`` — the caller-supplied key (an ``Idempotency-Key`` header), opaque to us.
    - ``request_digest`` — a SHA-256 of the request body bytes. A replay of the SAME key MUST carry
      the SAME body: a matching digest returns the stored result; a *different* digest is a key reuse
      and is rejected (``409``), never silently served the first call's result.
    - ``response_status`` / ``response_body`` — the stored outcome. A replay returns these verbatim,
      so a retried write produces exactly the first call's response and NO second state change.

    The record is staged into the *caller's* session (like the DCA-026/027/028 sinks) and committed
    in the SAME unit of work as the business change it records, so the write and its idempotency
    receipt are atomic: a replay that loses a race on the unique constraint rolls back its own
    (duplicate) change along with the rejected record, and then returns the winner's stored result —
    the effect is applied exactly once.

    ``(business_id, method, route, idempotency_key)`` is unique, so the same key reused for the same
    route within one tenant is constrained (the row that makes a retry a replay), while two tenants —
    or two routes — may legitimately use the same key and neither constrains the other.

    Tenant-owned: it inherits the DCA-017 ``business_id`` column and the DCA-018 row-level-security
    policy like every other tenant data table. Only opaque control fields of the route's own response
    are stored (ids / statuses / flags), never contact data.
    """

    __tablename__ = "idempotency_records"
    # The key is unique PER tenant AND route+method: a client's key scopes to one logical operation on
    # one business; the same key may recur on another route or another tenant without colliding.
    __table_args__ = (
        tenant_unique("method", "route", "idempotency_key", name="uq_idempotency_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # The caller-supplied Idempotency-Key header value (opaque). Scoped per (business, method, route).
    idempotency_key: Mapped[str] = mapped_column(String(200), index=True)
    # The mutating HTTP method (POST/PUT/PATCH/DELETE) — part of the per-operation scope.
    method: Mapped[str] = mapped_column(String(8))
    # The route template (or path) the key was used on — the "route-scoped" half of the dedup key.
    route: Mapped[str] = mapped_column(String(300))
    # SHA-256 (hex) of the request body bytes: same key + same body replays; same key + different body
    # is rejected as a key reuse.
    request_digest: Mapped[str] = mapped_column(String(64))
    # The stored outcome replayed verbatim on a matching retry (no second state change).
    response_status: Mapped[int] = mapped_column(Integer, default=200)
    response_body: Mapped[dict] = mapped_column(JSON, default=dict)
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
