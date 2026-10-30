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
    true as sa_true,
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


class JobRequestStatus(str, Enum):
    """Lifecycle state of a durable job request (DCA-099).

    ``QUEUED`` — the job has been persisted and is waiting to be picked up. DCA-099 persists durable job
    *requests* and deliberately does not execute them; the claim path (DCA-101) flips a row to ``RUNNING``
    and the fenced-completion path (DCA-103) flips a running row to ``SUCCEEDED``, but no path here ever
    EXECUTES a job — the states model the lifecycle, the handler body stays a disabled future capability:

    - ``RUNNING`` — claimed by a worker and in progress (the DCA-101 lease capability).
    - ``SUCCEEDED`` — completed: a worker COMMITTED a fenced result (DCA-103), guarded by the current lease
      and a re-check of the authorization revisions it relied on. Terminal; the lease is cleared.
    - ``FAILED`` — a retriable attempt failed; it will be retried while ``attempt_count < max_attempts``.
    - ``DEAD_LETTER`` — retries were exhausted (or the job was non-retriable); parked for inspection.
    - ``CANCELLED`` — the request was withdrawn before it ran.
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"
    CANCELLED = "cancelled"


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
    the DCA-046 approval story promotes one reviewed version to. ``EXPIRED`` is the terminal state the
    DCA-049 scheduled expiry sweep moves a lapsed ACTIVE version to once it is past its own validity
    window (or once the approval that promoted it has itself lapsed) — a durable record that the rights
    are no longer in force. The expiry decision is governed by the explicit validity window
    (:attr:`SourceTerms.effective_from`/:attr:`SourceTerms.expires_at`), never by the status flag alone,
    so a right is treated as lapsed the moment its window closes even before the sweep stamps it EXPIRED.
    ``REVOKED`` is the terminal state the DCA-050 operator-revocation moves an ACTIVE version to when a
    platform operator withdraws the approval that promoted it — a durable record that the rights were
    deliberately pulled (distinct from a lapse). Like EXPIRED it only ever makes a right MORE
    restrictive; it is never consulted to authorize a fetch.
    """

    DRAFT = "draft"
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


class SourceApprovalStatus(str, Enum):
    """Lifecycle state of a source-configuration approval (DCA-046).

    ``ACTIVE`` — the approval a platform operator recorded is in force: it endorses one reviewed
    :class:`SourceTerms` version and carries the evidence, reviewer, review date and expiry of the
    review. ``EXPIRED`` is the terminal state the DCA-049 scheduled expiry sweep moves an ACTIVE
    approval to once it is past its explicit ``expires_at`` review deadline — a durable record that the
    endorsement has lapsed and must be re-reviewed. The in-force decision is governed by the explicit
    validity window (:meth:`~app.services.source_approval.SourceApprovalStore.is_in_force` honours
    ``effective_from``/``expires_at`` at ``now``), never by the status flag alone, so a lapsed approval
    endorses nothing the moment its window closes — even before the sweep stamps it EXPIRED. ``REVOKED``
    is the terminal state the DCA-050 operator-revocation moves an ACTIVE approval to when a platform
    operator deliberately withdraws it (recording who/when/why on the approval row). The moment the
    approval is ``REVOKED`` it is no longer ACTIVE, so
    :meth:`~app.services.source_approval.SourceApprovalStore.is_in_force` returns ``False`` immediately —
    an already-running task holding the old (now-revoked) source version is denied at its next read/use
    check, with no sweep required (DCA-050). A revocation only ever makes a right MORE restrictive; it
    never re-enables or widens one. An approval is platform control-plane data and, on its own, enables
    no acquisition (the source catalog entry staying ENABLED is a separate, disabled capability — DCA-044).
    """

    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


class RuleEffect(str, Enum):
    """The decision one rulepack rule expresses (DCA-052).

    ``ALLOW`` — the rule permits the action/channel it scopes (under its conditions). ``DENY`` — the
    rule forbids it. A rule is never ambiguous: it is exactly one of these. Two rules in the SAME pack
    that scope the identical (action, channel, authority, conditions) with OPPOSITE effects over an
    overlapping validity window are *contradictory* and the pack is refused before anything is staged.
    """

    ALLOW = "allow"
    DENY = "deny"


class RuleAuthority(str, Enum):
    """Where a rule's force originates (DCA-052) — the distinction the story requires.

    A rulepack keeps three kinds of default apart so a reader can tell WHY a rule exists and which
    outranks which:

    - ``LAW`` — a statutory/regulatory default (e.g. an Indian data-protection or telecom rule). The
      strongest authority; a product/provider default can never widen past it.
    - ``PROVIDER`` — a default imposed by a data source / channel provider's terms.
    - ``PRODUCT`` — a conservative platform (product) default we choose for ourselves.

    Opposite effects across DIFFERENT authorities are NOT a contradiction (that is precedence, resolved
    LAW > PROVIDER > PRODUCT); only opposite effects WITHIN the same authority and scope are.
    """

    LAW = "law"
    PROVIDER = "provider"
    PRODUCT = "product"


class RuleAction(str, Enum):
    """The acquisition/contact action a rulepack rule governs (DCA-052; India-only policy).

    Controlled vocabulary — a rule naming an action outside this set is malformed and refused. These
    mirror the data lifecycle the DCA touches; expressing a rule grants nothing (the pack is a draft).
    """

    ACQUIRE = "acquire"  # obtain a prospect record from a source at all
    STORE = "store"  # retain the record durably
    ENRICH = "enrich"  # derive/augment facts about the record
    PROFILE = "profile"  # build a targeting/scoring profile from it
    CONTACT = (
        "contact"  # reach the prospect over a channel (the platform still gates the actual send)
    )


class RuleChannel(str, Enum):
    """The channel a rulepack rule scopes (DCA-052). ``ANY`` means the rule is channel-independent.

    Controlled vocabulary — an unrecognized channel is malformed and refused. A ``CONTACT`` rule is
    naturally channel-specific; a data-lifecycle rule (acquire/store/enrich/profile) uses ``ANY``.
    """

    ANY = "any"
    EMAIL = "email"
    SMS = "sms"
    WHATSAPP = "whatsapp"
    VOICE = "voice"
    POSTAL = "postal"


class PolicyRulepackStatus(str, Enum):
    """Lifecycle state of a compliance policy rulepack version (DCA-052/DCA-053).

    ``DRAFT`` — a reviewable, NOT-in-force rulepack version. Every pack is created here as a draft
    (DCA-052): defining rules documents what the policy *would* decide, but puts nothing in force on
    its own — ``no rule becomes live just by existing``. ``ACTIVE`` is the state the DCA-053 policy
    approval promotes ONE reviewed version to when a platform operator records a complete review
    (approver + references + review expiry). ``EXPIRED`` is the terminal state a lapsed ACTIVE version
    moves to once the approval that promoted it is past its review-expiry window (the scheduled sweep
    is a later story; until then a lapse is detected by the explicit window, never by the flag alone —
    :meth:`~app.services.policy_approval.PolicyApprovalStore.is_in_force`). A DRAFT or EXPIRED pack
    authorizes nothing: only an ACTIVE version inside its approval window is in force, and changing
    approved content requires a NEW version (the store is insert-only), never an edit in place.
    """

    DRAFT = "draft"
    ACTIVE = "active"
    EXPIRED = "expired"


class PolicyApprovalStatus(str, Enum):
    """Lifecycle state of a compliance policy approval (DCA-053).

    ``ACTIVE`` — the approval a platform operator recorded is in force: it endorses exactly one
    :class:`CompliancePolicyRulepack` version and carries the review essentials (the approver, the
    references/evidence relied on, the review date and an explicit review expiry). ``EXPIRED`` is the
    terminal state a lapsed ACTIVE approval moves to once past its explicit ``expires_at`` review
    deadline — a durable record that the endorsement must be re-reviewed (the scheduled sweep is a
    later story). The in-force decision is governed by the explicit validity window
    (:meth:`~app.services.policy_approval.PolicyApprovalStore.is_in_force` honours
    ``effective_from``/``expires_at`` at ``now``), never by the flag alone, so a lapsed approval
    endorses nothing the moment its window closes — even before any sweep stamps it EXPIRED. An
    approval is platform control-plane data and, on its own, enables no acquisition/contact: the
    policy engine that would CONSULT an in-force pack stays a disabled future capability.
    """

    ACTIVE = "active"
    EXPIRED = "expired"


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
    # DCA-050 revocation record — all NULL until a platform operator revokes this approval. ``revoked_by``
    # is the opaque operator subject id (never a name/email); ``revocation_reason`` is a short opaque
    # reason code/explanation; ``revoked_at`` is when it was pulled. They make the approval row itself the
    # durable who/when/why of the revocation (mirroring how the review essentials capture the approval).
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    revocation_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class CompliancePolicyRulepack(Base):
    """DCA-052 — one *version* of the India compliance policy rulepack (platform control-plane).

    A rulepack is the deterministic, auditable data structure that a FUTURE (disabled) policy engine
    would consult to decide whether an acquisition/contact ``action`` over a ``channel`` is allowed in
    a jurisdiction, under explicit ``conditions``, backed by ``evidence``, within an effective-date
    window, and labelled by its ``authority`` (law vs provider vs product default). This story defines
    and VALIDATES the schema; it enforces nothing.

    ``rules`` is the validated, normalized list of rules (see
    :mod:`app.services`/:mod:`app.compliance.policy_rulepacks`). Each rule carries: ``effect``
    (allow/deny), ``action`` (:class:`RuleAction`), ``channel`` (:class:`RuleChannel`), ``authority``
    (:class:`RuleAuthority`), a structured ``conditions`` object, an ``evidence`` reference (required —
    a rule cannot exist without citing why), and an EXPLICIT per-rule validity window
    (``effective_from``/``expires_at``). The pack itself also carries a pack-level window.

    **Malformed or contradictory rules cannot be saved; no rule becomes live just by existing.** The
    single sanctioned writer (:class:`~app.compliance.policy_rulepacks.CompliancePolicyRulepackStore`)
    validates the whole pack FIRST — unknown enum values, missing evidence, an incoherent window, a
    malformed condition, or two rules that contradict each other (same action/channel/authority/
    conditions, opposite effect, overlapping window) each raise before any row is staged, so a refusal
    leaves no row. Every stored pack is ``status='draft'`` — recording rules never puts them in force.

    Versioned per jurisdiction: ``(jurisdiction, version)`` is unique and the store is insert-only, so
    every prior version is preserved for review and a version never overwrites another. The service is
    India-only, so ``jurisdiction`` is always ``IN`` (the store refuses anything else).

    Like :class:`Source` / :class:`SourceTerms` / :class:`SourceTermsApproval`, this is platform
    CONTROL-PLANE data — a shared policy registry, deliberately OUTSIDE tenant RLS and never
    tenant-scoped. It holds only declarative policy metadata: no prospect contact data and no secrets.
    """

    __tablename__ = "compliance_policy_rulepacks"
    # One version per jurisdiction; a (jurisdiction, version) pair is never reused — insert-only history.
    __table_args__ = (
        UniqueConstraint("jurisdiction", "version", name="uq_policy_rulepack_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # India-only: always "IN". The store refuses any other jurisdiction (a clear, stable code).
    jurisdiction: Mapped[str] = mapped_column(
        String(2), default="IN", server_default="IN", index=True
    )
    # Monotonic per-jurisdiction version (1-based); explicit, so policy history is reviewable version-by-version.
    version: Mapped[int] = mapped_column(Integer)
    # Optional human-readable title for the pack version (not a decision input).
    title: Mapped[str] = mapped_column(String(200), default="", server_default="")
    # The validated, normalized rules — a list of rule objects (see policy_rulepacks for the shape).
    rules: Mapped[list] = mapped_column(JSON, default=list)
    # The pack-level evidence the policy itself rests on: {"reference": str, "kind": str, "uri": str}.
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    # Pack-level EXPLICIT validity window — a policy lapses rather than living in a never-expiring flag.
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # draft (reviewable, NOT in force) vs active (approved). Promotion is disabled, so every row here
    # is 'draft'; recording rules never puts them in force on its own.
    status: Mapped[str] = mapped_column(
        String(16),
        default=PolicyRulepackStatus.DRAFT.value,
        server_default=PolicyRulepackStatus.DRAFT.value,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class CompliancePolicyApproval(Base):
    """DCA-053 — a platform operator's review/approval of one compliance policy rulepack version.

    Approving a POLICY *version* is the capability that lets a reviewed
    :class:`CompliancePolicyRulepack` version (DCA-052) be accepted independently. One row is one
    approval: it endorses exactly one ``(jurisdiction, policy_version)`` and records the review
    essentials the story requires —

    - ``review_references`` — the references/evidence the operator relied on to approve
      (``{reference, kind, uri}``); an approval cannot be staged without a non-empty ``reference`` (a
      bare approval is refused). This is why the service never AUTHORS a legal conclusion: the operator
      cites the external basis and the row records it, rather than the system synthesizing one.
    - ``reviewer_id`` / ``reviewer_type`` — WHO approved: an opaque subject id of a *platform
      operator* (never a name/email). Approval is a platform-plane action
      (:attr:`~app.identity.permissions.Scope.POLICY_RULEPACK_APPROVE`); a tenant administrator can
      never perform it — the policy registry is shared control-plane data, not a tenant resource.
    - ``reviewed_at`` — the review DATE (when the operator reviewed), required.
    - ``expires_at`` — the approval's EXPLICIT review expiry, required: an approval lapses rather than
      living forever, so a stale endorsement must be re-reviewed. ``effective_from`` optionally delays
      when it takes effect.

    ``content_fingerprint`` is a stable digest of the rulepack's normalized rules at approval time, so
    the approval is bound to the EXACT content it reviewed — ``immutable approved versions``: changing
    approved content means recording a NEW version (the DCA-052 store is insert-only), which starts as
    a fresh DRAFT and is unapproved until separately reviewed. An approval can never silently follow an
    edited pack.

    Like :class:`CompliancePolicyRulepack` / the source registries, this is platform CONTROL-PLANE
    data — a shared registry, deliberately OUTSIDE tenant RLS and never tenant-scoped. It holds only
    the review record (opaque reviewer id + references + a content digest): no prospect contact data
    and no secrets. Approving a version promotes that rulepack row ``draft`` → ``active``, but on its
    own enables NO acquisition/contact: the policy engine that would CONSULT an in-force pack stays a
    disabled future capability, so an approval never, by itself, authorizes production behavior.
    """

    __tablename__ = "compliance_policy_approvals"
    # One approval per rulepack version: a (jurisdiction, policy_version) pair is approved at most
    # once, so a double approval is a constraint violation, never a silent second endorsement.
    __table_args__ = (
        UniqueConstraint("jurisdiction", "policy_version", name="uq_policy_approval_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # India-only, mirroring the rulepack it endorses: always "IN".
    jurisdiction: Mapped[str] = mapped_column(
        String(2), default="IN", server_default="IN", index=True
    )
    # The rulepack version this approval endorses (validated to exist as a DCA-052 draft).
    policy_version: Mapped[int] = mapped_column(Integer)
    # WHO approved — an opaque platform-operator subject id (never a name/email) plus its kind.
    reviewer_id: Mapped[str] = mapped_column(String(128))
    reviewer_type: Mapped[str] = mapped_column(
        String(32), default="platform_operator", server_default="platform_operator"
    )
    # The references/evidence the operator relied on: {"reference": str, "kind": str, "uri": str}.
    # Required (a non-empty reference) — the approval cites an external basis; it authors no conclusion.
    review_references: Mapped[dict] = mapped_column(JSON, default=dict)
    # WHEN the operator reviewed (the review date) — required.
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The approval's EXPLICIT review-expiry window — it lapses rather than living in a never-expiring flag.
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Stable digest of the rulepack's normalized rules at approval time — binds the approval to the
    # EXACT content reviewed, so changing approved content requires a new (unapproved) version.
    content_fingerprint: Mapped[str] = mapped_column(String(64), default="", server_default="")
    status: Mapped[str] = mapped_column(
        String(16),
        default=PolicyApprovalStatus.ACTIVE.value,
        server_default=PolicyApprovalStatus.ACTIVE.value,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PolicyDecisionOutcome(str, Enum):
    """The four outcomes a persisted policy decision can record (mirrors
    :class:`~app.compliance.policy_engine.PolicyDecision`; kept as a column value for query/filter).

    ``ALLOW`` is the ONLY outcome that grants the personal-data permission — and even then only within
    the recorded decision's explicit expiry (:attr:`PolicyDecisionRecord.expires_at`). ``DENY`` /
    ``REVIEW`` / ``NOT_APPLICABLE`` grant nothing. The outcome is a safe, stable code — never free text.
    """

    ALLOW = "allow"
    DENY = "deny"
    REVIEW = "review"
    NOT_APPLICABLE = "not_applicable"


class PolicyDecisionRecord(TenantOwned, Base):
    """DCA-057 — a durable, auditable record of ONE policy decision (tenant-owned, RLS-protected).

    One row is one evaluation of a single :class:`~app.compliance.policy_engine.PolicyRequest` against
    the in-force India compliance policy (DCA-052/053) by the deterministic DCA-055/056 evaluator,
    persisted so the result can be *reviewed and reproduced independently*. Expressing a decision
    enforces nothing — the DCA has no send/acquisition path — but recording one preserves WHY an action
    was allowed or denied, traceable to the exact reviewed rule/evidence set, bounded by an explicit
    expiry so a stored ALLOW can never imply *permanent* authorization.

    The load-bearing guarantees (the DCA-057 acceptance criteria):

    - **A stored ALLOW is traceable to the exact reviewed rule/evidence set and is NOT permanent.** The
      row pins the in-force ``rulepack_version`` + ``approval_id`` (the DCA-053 review that put the pack
      in force), the ``engine_version``, the ``evidence`` actually consulted, and the immutable,
      content-fingerprinted evaluation-time ``snapshot`` (DCA-056) — so a reader can resolve the precise
      obligations/rules that decided it. Every ALLOW additionally carries a non-null ``expires_at`` (the
      single writer refuses to persist an ALLOW without one): the authorization lapses with the review
      it rests on, so a stored ALLOW is never read as a standing grant.

    - **The decision reproduces from the row ALONE, with no state change.** ``request_facts`` +
      ``snapshot`` are a complete, self-contained capture;
      :meth:`~app.services.policy_decision_record.PolicyDecisionRecordStore.reproduce` rebuilds the
      request and replays the snapshot (or re-decides a fail-closed denial whose snapshot is ``None``)
      to yield the identical decision — even after the live pack has changed — reading only this row and
      writing nothing.

    - **Relevant source / consent / suppression versions are captured.** ``references`` records the
      opaque version identifiers the decision was taken under (e.g. consent-basis version, suppression-
      list version, per-source terms versions), so the decision can be tied to the exact inputs that
      governed it. These are declarative version tags, never recipient contact data.

    ``decision`` is a safe :class:`PolicyDecisionOutcome` code and ``reason_codes`` the stable reason
    codes the engine returned (never free-form text). ``actor_id`` / ``actor_type`` record WHO persisted
    the record (an opaque subject id, never a name/email) — the same actor the atomic DCA-026 audit entry
    carries, staged in the SAME unit of work as the row.

    This is the tenant's OWN decision evidence (declarative policy metadata: rule obligations, version
    tags, categorical facts such as consent-present) — NOT prospect contact data or secrets — so, like
    :class:`BusinessProfileDraft` / :class:`IcpDraft`, its JSON fields are not opaque-reference screened
    (the atomic audit entry it stages IS screened). Tenant-owned: it inherits the DCA-017 ``business_id``
    column and the DCA-018 RLS policy like every other tenant data table, so one business can never read
    another's decision records.
    """

    __tablename__ = "policy_decision_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # The resolved recipient jurisdiction the decision was taken for (India-only service: "IN", or ""
    # when it could not be resolved — a fail-closed DENY).
    jurisdiction: Mapped[str] = mapped_column(String(2), default="", server_default="")
    # The acquisition/contact scope the request named (controlled vocabularies at evaluation time).
    action: Mapped[str] = mapped_column(String(32))
    channel: Mapped[str] = mapped_column(String(32), default="any", server_default="any")
    # The safe outcome code (allow/deny/review/not_applicable) and the stable reason codes behind it.
    decision: Mapped[str] = mapped_column(String(16), index=True)
    reason_codes: Mapped[list] = mapped_column(JSON, default=list)
    # The EXACT reviewed content the decision rests on: the in-force rulepack version + the DCA-053
    # approval that put it in force + the evaluator version. NULL version/approval => a fail-closed
    # denial taken with no in-force pack (a missing pack can never allow).
    rulepack_version: Mapped[int | None] = mapped_column(Integer, default=None)
    approval_id: Mapped[str | None] = mapped_column(String(36), default=None)
    engine_version: Mapped[int] = mapped_column(Integer, default=1)
    # Which authority's rule decided an allow/deny (law/provider/product), for a quick read.
    decisive_authority: Mapped[str | None] = mapped_column(String(16), default=None)
    # The evidence actually consulted (rule(s)/pack/approval) — traceable to the reviewed rule set.
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    # Opaque version tags of the relevant inputs the decision was taken under: consent basis version,
    # suppression-list version, per-source terms versions, etc. Declarative tags, never contact data.
    references: Mapped[dict] = mapped_column(JSON, default=dict)
    # The request's asserted facts (categorical context: consent-present, trust tier, hour, …) — needed
    # to reproduce the decision from the row alone. NOT contact data; retained exactly as supplied.
    request_facts: Mapped[dict] = mapped_column(JSON, default=dict)
    involves_personal_data: Mapped[bool] = mapped_column(Boolean, default=True)
    # The immutable, content-fingerprinted evaluation-time snapshot (DCA-056), serialized. NULL for a
    # fail-closed denial taken before any pack was consulted (reproduced by re-deciding with no pack).
    snapshot: Mapped[dict | None] = mapped_column(JSON, default=None)
    snapshot_fingerprint: Mapped[str] = mapped_column(String(64), default="", server_default="")
    # The evaluation moment the decision was taken at (replay re-decides at exactly this instant).
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The decision's EXPLICIT expiry — required for an ALLOW so a stored allow can never imply permanent
    # authorization; it lapses with the review it rests on. NULL is permitted only for a non-ALLOW.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # WHO persisted the record — an opaque subject id (never a name/email) plus its kind, screened by the
    # audit sink in the SAME unit of work as the row.
    actor_id: Mapped[str] = mapped_column(String(128))
    actor_type: Mapped[str] = mapped_column(String(32), default="user", server_default="user")
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


class OrgObservationType(str, Enum):
    """DCA-059 — whether an organization observation is a LEGAL ENTITY or one of its ESTABLISHMENTS.

    ``LEGAL_ENTITY`` — the registered legal person (e.g. a company identified by its CIN/PAN): the top
    of a scoped observation tree. ``ESTABLISHMENT`` — a place of business / registration of that legal
    entity (e.g. a state GST registration or a branch): it always hangs off exactly one parent
    LEGAL_ENTITY within the SAME business. The distinction is load-bearing — one legal entity can have
    many establishments (in India, typically one GSTIN per state), and the two are never conflated. An
    establishment is never the parent of another observation.
    """

    LEGAL_ENTITY = "legal_entity"
    ESTABLISHMENT = "establishment"


class OrgExternalScheme(str, Enum):
    """DCA-059 — the identifier scheme of an external platform-ID mapping (controlled vocabulary).

    A scheme names WHICH external register/platform an identifier belongs to, so a mapping is never an
    ambiguous bare string. India-oriented company identifiers plus the platform's own org id:

    - ``CIN`` — Corporate Identification Number (a legal entity, MCA).
    - ``PAN`` — Permanent Account Number (a legal entity).
    - ``LEI`` — Legal Entity Identifier (global, a legal entity).
    - ``GSTIN`` — Goods & Services Tax Identification Number (an establishment — per state).
    - ``TAN`` — Tax Deduction and Collection Account Number.
    - ``DUNS`` — D-U-N-S number (commercial).
    - ``PLATFORM_ORG_ID`` — the EffortlessPromo platform's own organisation id (an opaque mapping to
      the platform master held per-business; NOT a claim of global visibility).

    An unrecognized scheme is refused by the store — a mapping can never carry an unknown register.
    """

    CIN = "cin"
    PAN = "pan"
    LEI = "lei"
    GSTIN = "gstin"
    TAN = "tan"
    DUNS = "duns"
    PLATFORM_ORG_ID = "platform_org_id"


class OrgObservation(TenantOwned, Base):
    """DCA-059 — a scoped organization observation identity (tenant-owned, RLS-protected).

    One row is ONE observation this business has recorded about an organization it is looking at — a
    ``LEGAL_ENTITY`` or one of its ``ESTABLISHMENT``s (:class:`OrgObservationType`). It is an *identity
    node* only: it carries the observation's own type, display name, and (for an establishment) the
    Indian state/UT it operates in. It is NOT a prospect / host record — no contact data, no
    acquisition and no send path — and creating one grants nothing; those stay disabled future
    capabilities.

    **Scoped, never globally visible.** The observation is owned by exactly one ``business_id`` and is
    only ever read within that business (DCA-017/018). The SAME real-world company observed by two
    businesses yields TWO independent observation rows — one per tenant — never a shared global node:
    this is deliberately NOT a cross-tenant master graph, so an identity here can never imply that one
    business can see another's observations (contrast the platform's global Organisation master).

    **Legal entity vs establishment.** ``parent_observation_id`` links an ``ESTABLISHMENT`` to its
    parent ``LEGAL_ENTITY`` *within the same business*; a ``LEGAL_ENTITY`` has no parent. The single
    sanctioned writer (:class:`~app.services.org_observation.OrgObservationStore`) enforces the shape —
    an establishment must name a parent that exists in this business and is itself a legal entity; a
    legal entity must not name one — so one legal entity can carry many distinct establishments and the
    two are never conflated.

    The external register identifiers (CIN / PAN / GSTIN / …) are kept in a SEPARATE mapping table
    (:class:`OrgExternalId`), not inlined here, so an identity never assumes a single primary
    identifier and the same observation can carry several scheme-specific ids.
    """

    __tablename__ = "org_observations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # legal_entity | establishment — the load-bearing distinction (never conflated).
    entity_type: Mapped[str] = mapped_column(String(16), index=True)
    # An ESTABLISHMENT's parent LEGAL_ENTITY observation id (within THIS business); NULL for a legal
    # entity. Scoped: the writer validates the parent exists in this business and is a legal entity.
    parent_observation_id: Mapped[str | None] = mapped_column(String(36), index=True, default=None)
    # The observation's display name (a company/establishment name — a company fact, not contact data).
    display_name: Mapped[str] = mapped_column(String(300), default="", server_default="")
    # For an establishment, the recognized Indian state/UT (ISO 3166-2:IN code) it operates in; ""
    # when not applicable/known. India-only: an unrecognized code is refused by the store.
    state_code: Mapped[str] = mapped_column(String(8), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class OrgExternalId(TenantOwned, Base):
    """DCA-059 — a SEPARATE external platform-ID mapping for an organization observation (tenant-owned).

    One row maps one external register/platform identifier — a ``scheme`` (:class:`OrgExternalScheme`)
    plus its ``value`` — to one :class:`OrgObservation` within this business. Kept apart from the
    identity node on purpose: an observation can carry several identifiers (a legal entity its CIN and
    PAN; each establishment its own GSTIN), and the identity never has to assume a single primary id.

    ``(business_id, scheme, value)`` is unique, so within ONE business a given external id maps to at
    most one observation (a duplicate is rejected, never silently re-pointed). The key is PER business:
    two businesses may legitimately record the SAME external id against their own observations and
    neither constrains the other — the mapping is scoped, it does NOT make an identifier a global key.

    The ``value`` is a company/establishment register identifier (CIN / GSTIN / …), not personal
    contact data. Tenant-owned: it inherits the DCA-017 ``business_id`` column and the DCA-018 RLS
    policy like every other tenant data table, so one business can never read another's mappings.
    """

    __tablename__ = "org_external_ids"
    # An external id is unique PER business; the same (scheme, value) may recur across tenants.
    __table_args__ = (tenant_unique("scheme", "value", name="uq_org_external_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # The observation this identifier belongs to (an OrgObservation.id within the same business).
    observation_id: Mapped[str] = mapped_column(String(36), index=True)
    # cin | pan | lei | gstin | tan | duns | platform_org_id — the register this id belongs to.
    scheme: Mapped[str] = mapped_column(String(32), index=True)
    # The identifier value, normalized (trimmed + upper-cased) by the store. A company fact, not contact data.
    value: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ContactChannel(str, Enum):
    """DCA-060 — the channel type of a tenant-private contact point (controlled vocabulary).

    A channel names HOW a person would be reached, so a contact point is never an ambiguous bare
    string. India-oriented set:

    - ``EMAIL`` — an email address.
    - ``MOBILE`` — a mobile phone number.
    - ``LANDLINE`` — a fixed-line phone number.
    - ``WHATSAPP`` — a WhatsApp-reachable number.

    An unrecognized channel is refused by the store — a contact point can never carry an unknown
    channel. Recording a contact point is NOT consent to use any of these channels; that stays a
    disabled future capability behind the one send pipeline.
    """

    EMAIL = "email"
    MOBILE = "mobile"
    LANDLINE = "landline"
    WHATSAPP = "whatsapp"


class ContactEvidence(str, Enum):
    """DCA-060 — the identity evidence behind a tenant-private contact point (controlled vocabulary).

    Evidence records WHY this business believes the contact point belongs to the person — never a
    model inference about truth, just the provenance class the operator/import asserted:

    - ``SELF_REPORTED`` — the person gave it directly (e.g. filled a form).
    - ``PUBLIC_LISTING`` — observed on a public website / directory the business has rights to read.
    - ``IMPORTED`` — came in on a customer-supplied list. An imported value is tenant-private and is
      **never** automatically promoted to any shared/global data — promotion would be its own story
      with rights + consent review.
    - ``VERIFIED`` — the channel itself was confirmed (e.g. a replied-to message / OTP).
    - ``INFERRED`` — derived by the operator from other evidence; the weakest class.

    An unrecognized evidence class is refused by the store.
    """

    SELF_REPORTED = "self_reported"
    PUBLIC_LISTING = "public_listing"
    IMPORTED = "imported"
    VERIFIED = "verified"
    INFERRED = "inferred"


class PersonObservation(TenantOwned, Base):
    """DCA-060 — a tenant-private person observation identity (tenant-owned, RLS-protected).

    One row is ONE person this business has recorded while looking at an organization — a contact /
    decision-maker it may later want to reach. It is an *identity node* only: a display name, an
    optional role title, and an optional link to one of THIS business's :class:`OrgObservation`
    rows (the organization the person belongs to). It is NOT a prospect / host record — no send path,
    no acquisition — and creating one grants nothing; those stay disabled future capabilities.

    **Tenant-private, never globally visible.** The person is owned by exactly one ``business_id`` and
    is only ever read within that business (DCA-017/018). The SAME real-world person recorded by two
    businesses yields TWO independent rows — one per tenant — never a shared global node. Even when two
    businesses point their organization observations at the SAME external/global organization id (e.g.
    a shared ``PLATFORM_ORG_ID``), neither business's persons — nor their contact points — are visible
    to the other: the organization mapping is per-business, so it can never expose A's people to B.

    The actual reachable values (emails / phone numbers) live in a SEPARATE tenant-private table
    (:class:`ContactPoint`), not inlined here, so an identity never assumes a single primary contact
    and the value set is kept apart from the identity node.
    """

    __tablename__ = "person_observations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # The organization observation (an OrgObservation.id within THIS business) this person belongs to;
    # NULL when not linked. The writer validates it exists in this business — never a cross-tenant link.
    org_observation_id: Mapped[str | None] = mapped_column(String(36), index=True, default=None)
    # The person's display name. Tenant-private personal data: kept RLS-protected here, never audited.
    display_name: Mapped[str] = mapped_column(String(200), default="", server_default="")
    # The person's role/title at the organization (a business fact, e.g. "Procurement Head"); "" when
    # unknown.
    role_title: Mapped[str] = mapped_column(String(200), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ContactPoint(TenantOwned, Base):
    """DCA-060 — a tenant-private contact point for a person observation (tenant-owned, RLS-protected).

    One row is ONE reachable value — a ``channel`` (:class:`ContactChannel`) plus its ``value`` — for a
    :class:`PersonObservation` within this business, tagged with the identity ``evidence``
    (:class:`ContactEvidence`) that supports it. Kept apart from the identity node on purpose: a person
    can carry several contact points (an email and a mobile), and the identity never has to assume a
    single primary value.

    **Tenant-private; never promoted to shared data.** The value is owned by exactly one ``business_id``
    and is only ever read within that business. There is NO global index keyed on the value, so the same
    email recorded by two businesses stays two independent private rows — one tenant can never retrieve
    another's contact point, even via a shared organization mapping. Nothing in this story copies an
    imported value into any shared/global table; promotion would be its own rights-and-consent story.

    ``(business_id, channel, value)`` is unique, so within ONE business a given contact point is stored
    at most once (a duplicate is rejected, never silently re-pointed). The key is PER business: two
    tenants may legitimately hold the SAME value against their own people and neither constrains the
    other. The ``value`` is tenant-private contact data — RLS-protected here, and NEVER written to the
    audit trail (only opaque references are audited).
    """

    __tablename__ = "contact_points"
    # A contact point is unique PER business; the same (channel, value) may recur across tenants.
    __table_args__ = (tenant_unique("channel", "value", name="uq_contact_point"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # The person this contact point belongs to (a PersonObservation.id within the same business).
    person_observation_id: Mapped[str] = mapped_column(String(36), index=True)
    # email | mobile | landline | whatsapp — the channel this value is reachable on.
    channel: Mapped[str] = mapped_column(String(16), index=True)
    # The reachable value, normalized by the store (trimmed; lower-cased for email). Tenant-private
    # contact data — RLS-protected, never audited, never promoted to shared data.
    value: Mapped[str] = mapped_column(String(320))
    # self_reported | public_listing | imported | verified | inferred — the identity-evidence class.
    evidence: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class FieldSubjectType(str, Enum):
    """DCA-061 — what an observed field is ABOUT (controlled vocabulary).

    A field observation always hangs off exactly one subject in THIS business, so the subject kind is
    never an ambiguous bare id:

    - ``ORGANIZATION`` — one of this business's :class:`OrgObservation` rows (a legal entity or
      establishment).
    - ``PERSON`` — one of this business's :class:`PersonObservation` rows.

    An unrecognized subject type is refused by the store, and the subject id is always resolved WITHIN
    this business (a cross-tenant / unknown subject reads as not-found), so a field can never be
    attached to another tenant's identity.
    """

    ORGANIZATION = "organization"
    PERSON = "person"


class FieldInferenceLabel(str, Enum):
    """DCA-061 — whether an observed field was DIRECTLY observed or INFERRED (controlled vocabulary).

    The label is load-bearing provenance: an inferred value must never be passed off as a directly
    observed fact, so the two are recorded distinctly and carry different required provenance:

    - ``OBSERVED`` — the value was retrieved directly from a catalogued source. It MUST name that
      source (``source_key``) and the specific terms ``source_version`` it came under, plus the
      ACTUAL retrieval reference (``provenance_ref``) that locates the specific fetch — a bare value
      with no source reference is refused rather than silently canonicalized.
    - ``INFERRED`` — the value was DERIVED by the agent from other evidence. It carries no direct
      source fetch, so it must instead name the derivation basis it was computed from
      (``provenance_ref``); it is always labelled ``INFERRED`` so it is never mistaken for an
      observed fact.

    An unrecognized label is refused by the store.
    """

    OBSERVED = "observed"
    INFERRED = "inferred"


class FieldObservation(TenantOwned, Base):
    """DCA-061 — a single observed field about a subject, with REQUIRED provenance (tenant-owned, RLS).

    One row is ONE observation of ONE field (``field_key`` → ``field_value``) about one subject — an
    :class:`OrgObservation` or :class:`PersonObservation` within this business — stamped with the
    provenance that makes it reviewable: the ``source_key`` + ``source_version`` it came from, the
    ACTUAL retrieval/derivation reference (``provenance_ref``), the ``observed_at`` timestamp, the
    collection ``scope`` (India-only: ``nationwide`` or a recognized state/UT), the
    :class:`FieldInferenceLabel` (observed vs inferred), and the rights/retention metadata
    (``rights_basis`` + optional ``retention_until``) under which it was recorded.

    **Provenance is required — an unprovenanced field is rejected, never silently canonicalized.** The
    single sanctioned writer (:class:`~app.services.field_observation.FieldObservationStore`) refuses a
    create that is missing any required provenance BEFORE the session is mutated, so a bare agent-written
    value with no source reference stages no row and is never promoted into the canonical field set.

    **Scoped, never globally visible; append-only history.** The observation is owned by exactly one
    ``business_id`` and read only within it (DCA-017/018). It is NOT deduplicated on ``field_key`` —
    the same field observed again (a later fetch, a different source, an inference) is a NEW evidence
    row, so the provenance history of a field is preserved rather than overwritten. Recording a field
    observation creates no prospect / host record and grants nothing; acquisition and canonicalization
    stay disabled future capabilities.

    This records only the observed field metadata; the ``field_value`` is treated as potentially
    personal/business data and is NEVER written to the audit trail (only opaque references are audited).
    """

    __tablename__ = "field_observations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # organization | person — the kind of subject the field is about (resolved within this business).
    subject_type: Mapped[str] = mapped_column(String(16), index=True)
    # The subject this field is about: an OrgObservation.id or PersonObservation.id in THIS business.
    subject_id: Mapped[str] = mapped_column(String(36), index=True)
    # The observed field name, normalized (trimmed + lower-cased) by the store. Required.
    field_key: Mapped[str] = mapped_column(String(100), index=True)
    # The observed value as recorded. Potentially personal/business data — RLS-protected, never audited.
    field_value: Mapped[str] = mapped_column(Text, default="", server_default="")
    # observed | inferred — load-bearing provenance: an inferred value is never passed off as observed.
    inference_label: Mapped[str] = mapped_column(String(16), index=True)
    # The catalogued Source.key the field came from (required for an OBSERVED field; "" for inferred
    # when no originating source applies). Validated to exist in the platform catalog when supplied.
    source_key: Mapped[str] = mapped_column(String(64), default="", server_default="", index=True)
    # The SourceTerms version the field came under (required for an OBSERVED field; NULL otherwise).
    source_version: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    # The ACTUAL retrieval reference (an OBSERVED field) or derivation basis (an INFERRED field) —
    # an opaque pointer to the specific fetch/computation. Required; a bare value with none is refused.
    provenance_ref: Mapped[str] = mapped_column(String(500), default="", server_default="")
    # When the field was observed at the source / inferred. Required.
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The collection scope the field was observed under. India-only: "nationwide" or a recognized
    # Indian state/UT code (ISO 3166-2:IN). Required and validated by the store.
    scope: Mapped[str] = mapped_column(String(16), default="", server_default="")
    # The rights basis recorded for the field (e.g. the purpose / permitted-operation basis the agent
    # asserts it was collected under). Required, non-empty. This is RECORDED provenance, not an
    # authorization — it grants no acquisition/send right (those gates live elsewhere).
    rights_basis: Mapped[str] = mapped_column(String(200), default="", server_default="")
    # The retention deadline recorded for the field; NULL means no stated limit. Part of the
    # rights/retention metadata the observation carries.
    retention_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    # When this observation row was recorded in the service (distinct from observed_at at the source).
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class EvidenceRetentionMode(str, Enum):
    """DCA-062 — how much of a captured evidence object this service is permitted to retain.

    The mode is DERIVED deterministically from the source's terms (DCA-045) at capture time, never
    chosen by the caller — it records the outcome of the license-aware retention screen:

    - ``RAW_RETAINED`` — the source's terms both GRANT the ``store`` operation AND permit raw
      retention, so the raw payload is kept alongside its reference and digest.
    - ``REFERENCE_ONLY`` — raw retention is prohibited (the ``store`` right is withheld, or the
      retention policy is a no-retention policy, or the retention window is zero days). The raw payload
      is NEVER written; only the locator reference and a non-reversible content digest are kept, so the
      evidence is still traceable/verifiable without holding a prohibited raw copy.
    """

    RAW_RETAINED = "raw_retained"
    REFERENCE_ONLY = "reference_only"


class EvidenceObject(TenantOwned, Base):
    """DCA-062 — a bounded, license-aware evidence object (tenant-owned, RLS-protected).

    One row is ONE piece of evidence this business captured from a catalogued :class:`Source` under a
    specific :class:`SourceTerms` version — the raw material that backs an observation (a fetched
    registry record, a public page, a document). It is stored with **license-aware retention**: the
    single sanctioned writer (:class:`~app.services.evidence_store.EvidenceObjectStore`) consults the
    cited source terms and keeps only what the licence permits.

    The load-bearing guarantee (the DCA-062 acceptance criterion):

    - **A no-retention source leaves no raw content copy.** When the source's terms do NOT permit raw
      retention — the ``store`` operation is withheld, the retention policy is a no-retention policy, or
      the retention window is zero days — the evidence is stored ``REFERENCE_ONLY``: ``raw_content`` is
      left ``NULL`` and never written, while the locator ``content_ref`` and a non-reversible
      ``content_digest`` (a SHA-256 of the captured bytes) ARE kept, so the evidence stays traceable and
      verifiable without retaining a prohibited raw copy. When the terms DO permit it the raw payload is
      kept (``RAW_RETAINED``), bounded by ``retention_until`` when the terms set a max retention window.
      The retention ``mode`` is DERIVED from the terms at capture time, never supplied by the caller.

    The supporting guarantees:

    - **Scoped, never globally visible.** The object is owned by exactly one ``business_id`` and read
      only within it (DCA-017/018). A by-id read of another business's evidence object resolves to
      not-found — a cross-tenant id reads exactly like a non-existent one, never surfaced.

    - **Provenance is required.** Every object cites the ``source_key`` + ``source_version`` it was
      captured from (validated to exist), the actual ``content_ref`` locator, the rights basis, and the
      capture time; an optional subject links it to one of this business's observations.

    - **A denial changes nothing + no host record.** Validation runs BEFORE the session is mutated, so a
      refusal stages no evidence row. Capturing evidence creates no prospect / host record and promotes
      nothing to any shared/canonical table; acquisition stays a disabled future capability.

    The ``raw_content`` is treated as potentially personal/business data — RLS-protected here and NEVER
    written to the audit trail (only opaque references, including the safe digest, are audited).
    """

    __tablename__ = "evidence_objects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # The catalogued Source.key the evidence came from (validated to exist in the platform catalog).
    source_key: Mapped[str] = mapped_column(String(64), index=True)
    # The SourceTerms version the evidence was captured under (validated to exist) — the licence whose
    # retention terms decide whether the raw payload may be kept.
    source_version: Mapped[int] = mapped_column(Integer)
    # Optional link to the subject the evidence is about: an OrgObservation.id or PersonObservation.id
    # in THIS business (resolved within the business; a cross-tenant/unknown subject is refused). "" when
    # the evidence is captured independently of a recorded subject.
    subject_type: Mapped[str] = mapped_column(String(16), default="", server_default="", index=True)
    subject_id: Mapped[str] = mapped_column(String(36), default="", server_default="", index=True)
    # WHAT the evidence is — a short descriptive kind (e.g. "registry_record", "public_listing",
    # "document"), normalized (trimmed + lower-cased) by the store. Required.
    content_kind: Mapped[str] = mapped_column(String(64), index=True)
    # The locator/reference that points at where the raw evidence lives (a record URL / registry ref /
    # object key). ALWAYS kept — this is what is preserved when raw retention is prohibited. Required.
    content_ref: Mapped[str] = mapped_column(String(500), default="", server_default="")
    # A non-reversible SHA-256 (hex) of the captured bytes. ALWAYS kept, in either mode — it lets the
    # evidence be verified / deduplicated without retaining the raw payload. Computed by the store.
    content_digest: Mapped[str] = mapped_column(
        String(64), default="", server_default="", index=True
    )
    # Optional declared media type of the captured content (e.g. "application/json"); "" when unknown.
    content_media_type: Mapped[str] = mapped_column(String(128), default="", server_default="")
    # The raw captured payload — kept ONLY when the licence permits raw retention; NULL (never written)
    # for a REFERENCE_ONLY object. Potentially personal/business data: RLS-protected, never audited.
    raw_content: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    # raw_retained | reference_only — DERIVED from the source terms at capture time (not caller-chosen).
    retention_mode: Mapped[str] = mapped_column(String(16), index=True)
    # A stable code recording WHY the raw payload was dropped (store_not_permitted / no_retention_policy
    # / zero_retention_window), or "" when the raw payload was retained. Auditable why-reference-only.
    retention_reason: Mapped[str] = mapped_column(String(64), default="", server_default="")
    # The rights/retention basis recorded for the capture (e.g. the purpose / permitted-operation basis
    # asserted). Required, non-empty. RECORDED provenance, not an authorization — grants no right.
    rights_basis: Mapped[str] = mapped_column(String(200), default="", server_default="")
    # When the evidence was captured from the source. Required.
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The retention deadline derived from the terms' max-retention window (captured_at + max_days) when
    # the raw payload is kept and a window is set; NULL means no stated raw-retention limit.
    retention_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    # When this evidence row was recorded in the service (distinct from captured_at at the source).
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class FieldCorrection(TenantOwned, Base):
    """DCA-070 — an authorized user's correction of a field value, VERSIONED (tenant-owned, RLS).

    One row is ONE version of a business user's correction of ONE field (``field_key``) about one
    subject — an :class:`OrgObservation` or :class:`PersonObservation` within this business. It is the
    tenant's own authoritative override that sits ON TOP of the DCA-061 provider-observation history
    and the DCA-069 canonical selection: when a *protected* correction exists, it is what the business
    treats as canonical for that field, so a later provider refresh does NOT silently overwrite the
    user's verified value.

    The load-bearing guarantees (the DCA-070 acceptance criteria):

    - **A refresh does not overwrite a protected correction.** The correction is a separate record, not
      an edit of any observation — recording a newer provider observation stages a NEW
      :class:`FieldObservation` row and never touches a correction. The DCA-069 overlay
      (:mod:`app.canonical.correction_overlay`) keeps the protected correction's value canonical over
      the fresher provider value. ``protected_from_refresh`` defaults to ``True`` (the "default refresh
      protection"): a correction is protected unless the user explicitly records an unprotected version.

    - **A correction stays business-private.** The row is owned by exactly one ``business_id`` and read
      only within it (DCA-017/018): a by-id read of another business's correction resolves to not-found,
      and the overlay for a different business (which has no such correction) simply returns the provider
      selection — so the same subject/field can show the corrected value in Business A and the provider
      value in Business B.

    **Versioned, insert-only history.** Each correction of a (subject, field_key) inserts a NEW version
    at ``max(version) + 1`` for that field; prior versions are preserved and never mutated/deleted, so
    the correction history (including who changed it, when, and whether it was protected) is fully
    reviewable. The ACTIVE correction for a field is its highest version. A user re-protects or
    un-protects a field by recording a new version; nothing is edited in place.

    **Authorized + attributed.** A correction may only be recorded by a business user whose membership
    roles grant :attr:`~app.identity.permissions.Scope.FIELD_CORRECT` (a read-only viewer, a non-member,
    and a platform operator all lack it); the single sanctioned writer
    (:class:`~app.services.field_correction.FieldCorrectionStore`) refuses an unauthorized attempt
    BEFORE the session is mutated, so a denial stages no row. ``actor_id`` / ``actor_type`` record WHO
    corrected the field (an opaque subject id, never a name/email), screened by the audit sink in the
    SAME unit of work as the row.

    Recording a correction promotes nothing to any shared/host record and sends nothing — acting on the
    canonical value stays a disabled future capability. The ``corrected_value`` is treated as potentially
    personal/business data: RLS-protected here and NEVER written to the audit trail (only opaque
    references are audited).
    """

    __tablename__ = "field_corrections"
    # A version is unique PER (business, subject, field): the same ordinal recurs across tenants/fields
    # but never within one field of one tenant, so a new version can never overwrite an existing one.
    __table_args__ = (
        tenant_unique(
            "subject_type", "subject_id", "field_key", "version", name="uq_field_correction_version"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # organization | person — the kind of subject the corrected field is about (within this business).
    subject_type: Mapped[str] = mapped_column(String(16), index=True)
    # The subject the field is about: an OrgObservation.id or PersonObservation.id in THIS business.
    subject_id: Mapped[str] = mapped_column(String(36), index=True)
    # The corrected field name, normalized (trimmed + lower-cased) by the store. Required.
    field_key: Mapped[str] = mapped_column(String(100), index=True)
    # The user-supplied corrected value. Potentially personal/business data — RLS-protected, never
    # audited. The business-private authoritative value the overlay keeps canonical when protected.
    corrected_value: Mapped[str] = mapped_column(Text, default="", server_default="")
    # 1-based version ordinal within (business, subject, field); a new correction is max(version)+1.
    version: Mapped[int] = mapped_column(Integer, default=1)
    # DEFAULT refresh protection: a correction is protected from being overwritten by a later provider
    # refresh unless the user explicitly records an UNprotected version. True by default.
    protected_from_refresh: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=sa_true()
    )
    # A short opaque reason/justification for the correction (e.g. "verified_by_customer"); never
    # free-form contact data — screened by the audit sink. Optional.
    reason: Mapped[str] = mapped_column(String(200), default="", server_default="")
    # WHO corrected the field — an opaque subject id (never a name/email) plus its kind, screened by the
    # audit sink in the SAME unit of work as the row.
    actor_id: Mapped[str] = mapped_column(String(128))
    actor_type: Mapped[str] = mapped_column(String(32), default="user", server_default="user")
    # When this correction row was recorded in the service.
    corrected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ConsentAction(str, Enum):
    """DCA-074 — the permission action a consent receipt records (controlled vocabulary).

    A receipt is an evidenced record of ONE permission event; the action names what the subject did,
    never a free-text label:

    - ``GRANT`` — the subject gave permission (opted in) to be contacted on this channel for this
      purpose, under the cited notice/form version.
    - ``CONFIRM`` — a confirmation of a prior grant (e.g. a double opt-in / verified click), recorded
      as its own evidenced event rather than mutating the grant.
    - ``WITHDRAW`` — the subject withdrew a previously granted permission (opted out). Recorded as a
      new receipt, never by deleting the grant, so the permission history stays reviewable.
    - ``DECLINE`` — the subject was offered and declined permission.

    An unrecognized action is refused by the store. Recording a receipt is NOT a send authorization —
    acting on a permission stays a disabled future capability behind the one send pipeline.
    """

    GRANT = "grant"
    CONFIRM = "confirm"
    WITHDRAW = "withdraw"
    DECLINE = "decline"


class ConsentReceipt(TenantOwned, Base):
    """DCA-074 — a scoped, evidenced permission receipt (tenant-owned, RLS-protected).

    One row is ONE evidenced permission event this business captured for one brand it runs — the
    durable record that a specific subject took a specific permission :class:`ConsentAction` on a
    specific reachable :class:`ContactPoint` / channel, for a stated purpose, under a specific
    notice/form version, in a stated language, at a stated time. A permission is therefore a *scoped
    receipt*, never a boolean attribute hung on a contact.

    The load-bearing guarantee (the DCA-074 acceptance criterion):

    - **An imported "subscribed" flag without evidence does not create a valid receipt.** The single
      sanctioned writer (:class:`~app.services.consent_receipt.ConsentReceiptStore`) requires the
      receipt to cite a real, same-business :class:`EvidenceObject` (DCA-062) — the captured proof of
      the permission event (the form submission, the opt-in record). A flag-only import that carries a
      bare ``subscribed = true`` with no evidence is refused BEFORE the session is mutated, so it
      stages no receipt. Permissions are not global contact attributes: a receipt lives only for the
      business + brand + subject/contact point + channel + purpose + notice/form version + action +
      language + time it names.

    The supporting guarantees:

    - **Scoped, never globally visible.** The receipt is owned by exactly one ``business_id`` and read
      only within it (DCA-017/018). A by-id read of another business's receipt resolves to not-found,
      and the SAME contact value recorded by two businesses yields TWO independent receipts (or none) —
      one tenant's permission can never be read by, or asserted for, another.

    - **Grounded in this business's own identities.** The ``contact_point_id`` must name a
      :class:`ContactPoint` in this business; the ``channel`` must match it; the person subject is the
      contact point's owning :class:`PersonObservation`. The cited ``evidence_id`` must resolve within
      this business, and — when that evidence is itself subject-linked — its subject must match.

    - **Append-only history.** A receipt is never edited or deleted; a later confirmation or withdrawal
      is a NEW row, so the full permission timeline for a contact point stays reviewable.

    Each successful record stages an atomic DCA-026 audit entry in the SAME unit of work (SEC-001/
    BR-122). References stay OPAQUE — ids / brand / channel / purpose / action / notice+form version /
    language — and never the raw contact value (that lives RLS-protected on the contact point).
    """

    __tablename__ = "consent_receipts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # The brand (within this business) the permission was given to — a business may run several brands,
    # so a permission is scoped to one of them, never business-wide. Normalized by the store. Required.
    brand_key: Mapped[str] = mapped_column(String(100), index=True)
    # The subject the permission is about: a PersonObservation.id in THIS business (derived from the
    # cited contact point's owner). organization | person — person for a contact-point receipt.
    subject_type: Mapped[str] = mapped_column(String(16), index=True)
    subject_id: Mapped[str] = mapped_column(String(36), index=True)
    # The specific reachable value the permission covers — a ContactPoint.id in THIS business. A
    # permission is per contact point, never a global contact attribute. Required.
    contact_point_id: Mapped[str] = mapped_column(String(36), index=True)
    # email | mobile | landline | whatsapp — the channel the permission covers; must match the cited
    # contact point's channel (the store rejects a mismatch).
    channel: Mapped[str] = mapped_column(String(16), index=True)
    # The stated purpose the permission was given for (e.g. "marketing_email"), normalized by the
    # store. A permission is scoped to one purpose. Required.
    purpose: Mapped[str] = mapped_column(String(100), index=True)
    # grant | confirm | withdraw | decline — the permission action the subject took.
    action: Mapped[str] = mapped_column(String(16), index=True)
    # The notice/privacy-notice version shown when the permission was collected. Required — a receipt
    # with no notice version cannot be reviewed, so it is refused.
    notice_version: Mapped[str] = mapped_column(String(64))
    # The collection form/flow version the permission was collected through. Required.
    form_version: Mapped[str] = mapped_column(String(64))
    # The language the notice/form was presented in (a normalized BCP-47-ish code, e.g. "en", "hi").
    # Required — the language the subject actually consented in is part of the evidence.
    language: Mapped[str] = mapped_column(String(16), index=True)
    # The captured proof of the permission event — an EvidenceObject.id in THIS business (DCA-062).
    # REQUIRED: a flag-only import with no evidence is refused and stages no receipt.
    evidence_id: Mapped[str] = mapped_column(String(36), index=True)
    # When the subject actually took the permission action (distinct from created_at here). Required.
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # When this receipt row was recorded in the service.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class RestrictionRetentionMode(str, Enum):
    """DCA-078 — how long a processing restriction is retained in the ledger.

    A restriction is a durable, append-only ledger entry (never edited in place), so its retention is
    the window the *restriction itself* must be honoured/kept, NOT a data-retention window on any
    prospect:

    - ``UNTIL_LIFTED`` — the restriction stands indefinitely until a later, explicit lift records a new
      ledger version (lifting stays a DISABLED future capability). ``retain_until`` is left ``NULL``.
    - ``UNTIL_DATE`` — the restriction is effective until a stated ``retain_until`` moment, after which a
      future sweep may treat it as lapsed. ``retain_until`` is REQUIRED when this mode is chosen.
    """

    UNTIL_LIFTED = "until_lifted"
    UNTIL_DATE = "until_date"


class RestrictionAuthorityPlane(str, Enum):
    """DCA-078 — which authority plane authorized a restriction (recorded on the ledger row).

    The two planes are disjoint (see :mod:`app.identity.permissions`): a LOCAL (tenant) restriction is
    authorized on the BUSINESS plane; a platform-wide source removal is authorized on the PLATFORM
    plane. Recording the plane on the row makes a restriction's authority auditable at a glance and lets
    local and platform-wide restrictions be told apart even when read together.
    """

    BUSINESS = "business"
    PLATFORM = "platform"


class ProcessingRestriction(TenantOwned, Base):
    """DCA-078 — a LOCAL (tenant) scoped processing restriction, VERSIONED (tenant-owned, RLS).

    One row is ONE version of a restriction a business placed on the agent's processing for a scope it
    owns — any combination of a brand it runs, a channel and a purpose (an empty dimension means "all"
    for that dimension, so a fully-empty scope is a tenant-wide restriction). It is the tenant's own
    durable "do not process this scope" instruction, recorded in an append-only ledger with its
    authority, version and retention metadata.

    The load-bearing guarantees (the DCA-078 acceptance criteria):

    - **Local and platform-wide restrictions are distinguishable and require appropriate authority.**
      This row is the LOCAL kind: it is tenant-owned (scoped to one ``business_id``), stamps
      ``authority_plane = business`` and may only be written by a business user whose membership roles
      grant :attr:`~app.identity.permissions.Scope.PROCESSING_RESTRICT` (a read-only viewer and a
      platform operator both lack it). A platform-wide source removal is the separate
      :class:`PlatformSourceRestriction` kind — control-plane, platform-plane authority, carrying a
      required justification — so the two are never confused and neither authority reaches the other.

    - **A denial changes nothing.** The single sanctioned writer
      (:class:`~app.services.processing_restriction.ProcessingRestrictionStore`) checks authority FIRST
      and validates the scope/retention BEFORE the session is mutated, so a refused restriction stages
      no row.

    **Versioned, insert-only history.** Each restriction of a (brand, channel, purpose) scope inserts a
    NEW version at ``max(version) + 1`` for that scope; prior versions are preserved and never
    mutated/deleted, so the restriction history (who, when, which retention) stays fully reviewable. A
    restriction authorizes nothing and sends nothing — it only ever makes processing MORE restrictive,
    and acting on it stays a disabled future capability behind the one send pipeline.

    Each successful record stages an atomic DCA-026 audit entry in the SAME unit of work (SEC-001/
    BR-122). References stay OPAQUE — ids / brand / channel / purpose / version / plane — never a raw
    contact value.
    """

    __tablename__ = "processing_restrictions"
    # A version is unique PER (business, brand, channel, purpose): the same ordinal recurs across
    # tenants/scopes but never within one scope of one tenant, so a new version can never overwrite one.
    __table_args__ = (
        tenant_unique(
            "scope_brand",
            "scope_channel",
            "scope_purpose",
            "version",
            name="uq_processing_restriction_version",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # The brand (within this business) the restriction covers; "" means ALL brands of the business.
    scope_brand: Mapped[str] = mapped_column(String(100), default="", server_default="", index=True)
    # email | mobile | landline | whatsapp — the channel restricted; "" means ALL channels.
    scope_channel: Mapped[str] = mapped_column(
        String(16), default="", server_default="", index=True
    )
    # The purpose restricted (e.g. "marketing_email"), normalized; "" means ALL purposes.
    scope_purpose: Mapped[str] = mapped_column(
        String(100), default="", server_default="", index=True
    )
    # 1-based version ordinal within (business, brand, channel, purpose); a new restriction is max+1.
    version: Mapped[int] = mapped_column(Integer, default=1)
    # Which authority plane authorized this restriction — always 'business' for a local restriction.
    authority_plane: Mapped[str] = mapped_column(
        String(16), default=RestrictionAuthorityPlane.BUSINESS.value, server_default="business"
    )
    # The explicit permission scope the authorizing principal held (e.g. "processing.restrict").
    authority_scope: Mapped[str] = mapped_column(String(64), default="", server_default="")
    # WHO authorized the restriction — an opaque subject id (never a name/email) plus its kind.
    authorized_by: Mapped[str] = mapped_column(String(128))
    authorized_by_type: Mapped[str] = mapped_column(
        String(32), default="user", server_default="user"
    )
    # A short opaque reason/justification for the restriction (e.g. "subject_request"); optional.
    reason: Mapped[str] = mapped_column(String(300), default="", server_default="")
    # until_lifted | until_date — how long this restriction is retained/effective.
    retention_mode: Mapped[str] = mapped_column(
        String(16),
        default=RestrictionRetentionMode.UNTIL_LIFTED.value,
        server_default=RestrictionRetentionMode.UNTIL_LIFTED.value,
    )
    # When the restriction lapses, when retention_mode is until_date; NULL for until_lifted.
    retain_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When this restriction row was recorded in the service.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PlatformSourceRestriction(Base):
    """DCA-078 — a justified, platform-wide SOURCE removal, VERSIONED (control-plane, NOT tenant-owned).

    One row is ONE version of a platform operator's decision to remove a catalogued :class:`Source`
    across the WHOLE platform — a restriction that applies to every tenant, not one business. It is the
    platform-plane counterpart of the tenant-owned :class:`ProcessingRestriction`: recorded in the same
    append-only restriction ledger, with authority, version and retention metadata, but on the platform
    control plane.

    The load-bearing guarantees (the DCA-078 acceptance criteria):

    - **Local and platform-wide restrictions are distinguishable and require appropriate authority.**
      This row is the PLATFORM kind: it is control-plane data (NOT tenant-owned, carries no
      ``business_id`` and is OUTSIDE tenant RLS, like the DCA-044 source catalog), stamps
      ``authority_plane = platform`` and may only be written by a platform operator holding
      :attr:`~app.identity.permissions.Scope.PLATFORM_SOURCE_RESTRICT`. A tenant administrator holds
      only business-plane scopes, so their roles never authorize a platform-wide removal — it is refused
      before any state is touched. A ``justification`` is REQUIRED (the "justified" in the story): a
      removal that affects every tenant must record WHY.

    - **A denial changes nothing.** The single sanctioned writer
      (:class:`~app.services.processing_restriction.PlatformSourceRestrictionStore`) checks authority
      FIRST, requires a non-empty justification and validates the source exists BEFORE the session is
      mutated, so a refused removal stages no row.

    **Versioned, insert-only history.** Each removal of a ``source_key`` inserts a NEW version at
    ``max(version) + 1`` for that source; prior versions are preserved. The row itself is the durable
    who/when/why record (like the DCA-050 revocation approval), so there is no separate tenant audit
    entry (the audit sink is tenant-scoped and a platform-wide action has no single business scope).

    A restriction only ever makes processing MORE restrictive and authorizes nothing; this story enables
    no source and ships no live adapter. It holds only opaque control-plane metadata — no prospect
    contact data and no secrets.
    """

    __tablename__ = "platform_source_restrictions"
    # A version is unique per source_key across the platform catalog.
    __table_args__ = (
        UniqueConstraint("source_key", "version", name="uq_platform_source_restriction_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # The catalogued Source.key removed platform-wide (validated to exist in the catalog).
    source_key: Mapped[str] = mapped_column(String(64), index=True)
    # 1-based version ordinal within source_key; a new removal is max(version)+1.
    version: Mapped[int] = mapped_column(Integer, default=1)
    # Always 'platform' for a platform-wide removal — the authority plane recorded on the row.
    authority_plane: Mapped[str] = mapped_column(
        String(16), default=RestrictionAuthorityPlane.PLATFORM.value, server_default="platform"
    )
    # The explicit platform scope the operator held (e.g. "platform.source.restrict").
    authority_scope: Mapped[str] = mapped_column(String(64), default="", server_default="")
    # WHO authorized the removal — an opaque operator subject id (never a name/email).
    authorized_by: Mapped[str] = mapped_column(String(128))
    # REQUIRED justification: a platform-wide removal affects every tenant, so it must record WHY.
    justification: Mapped[str] = mapped_column(String(500))
    # until_lifted | until_date — how long this removal is retained/effective.
    retention_mode: Mapped[str] = mapped_column(
        String(16),
        default=RestrictionRetentionMode.UNTIL_LIFTED.value,
        server_default=RestrictionRetentionMode.UNTIL_LIFTED.value,
    )
    retain_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class RestrictionPropagation(TenantOwned, Base):
    """DCA-081 — an append-only *propagation acknowledgment* (work receipt) for a restriction fanout.

    One row records that a recorded restriction (DCA-078) was fanned out INTO one tenant's affected
    recommendations: the restriction it answers, the authority plane it came from, the scope honoured,
    HOW MANY of that tenant's suggestions were revoked and their opaque ids, WHO triggered the
    propagation and WHEN. It is the durable acknowledgment that the invalidation reached that tenant.

    The load-bearing guarantees (the DCA-081 acceptance criteria):

    - **A local block affects only its scope; a platform-wide removal reaches every affected tenant.**
      This row is always scoped to ONE ``business_id`` (it is :class:`TenantOwned`, RLS-protected), so a
      tenant only ever sees the acknowledgments for its OWN revoked suggestions. A LOCAL restriction
      fanout writes exactly one row — for the restricting business. A PLATFORM-WIDE source removal writes
      one row PER affected tenant (each scoped to that tenant), so the platform decision reaches every
      platform-controlled copy while a tenant with no affected copy is never touched (no row for it).

    - **The acknowledgment is a propagation receipt, never a grant.** Recording it changes nothing about
      what may be sent — it only records that affected suggestions were REVOKED (made more restrictive).
      A restriction only ever restricts; acting on a suggestion stays a disabled future capability behind
      the one send pipeline.

    Insert-only: a fanout that revokes ≥1 suggestion for a tenant stages one acknowledgment; a fanout
    that affects nothing for a tenant (a no-op / an idempotent re-run) stages NO row, so re-running a
    fanout leaves no unintended state. ``revoked_candidate_ids`` holds OPAQUE row ids only — never org
    names or contact data. Tenant-owned: it inherits the DCA-017 ``business_id`` column and the DCA-018
    RLS policy like every other tenant data table.
    """

    __tablename__ = "restriction_propagations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # The restriction this acknowledgment answers — a ProcessingRestriction.id (local) or a
    # PlatformSourceRestriction.id (platform-wide). Opaque row key, never free text.
    restriction_id: Mapped[str] = mapped_column(String(36), index=True)
    # The version ordinal of that restriction (so an acknowledgment pins the exact ledger version).
    restriction_version: Mapped[int] = mapped_column(Integer, default=1)
    # Which authority plane the restriction came from — business (local) or platform (platform-wide).
    authority_plane: Mapped[str] = mapped_column(String(16), default="", server_default="")
    # The platform-source key whose removal drove a platform-wide fanout; "" for a local fanout.
    source_key: Mapped[str] = mapped_column(String(64), default="", server_default="")
    # The scope dimensions of a LOCAL restriction, echoed for traceability ("" = all for that
    # dimension). A platform-wide source fanout leaves these empty (it is keyed on the source).
    scope_brand: Mapped[str] = mapped_column(String(100), default="", server_default="")
    scope_channel: Mapped[str] = mapped_column(String(16), default="", server_default="")
    scope_purpose: Mapped[str] = mapped_column(String(100), default="", server_default="")
    # HOW MANY of this tenant's suggestions this fanout revoked, and their opaque row ids.
    revoked_count: Mapped[int] = mapped_column(Integer, default=0)
    revoked_candidate_ids: Mapped[list] = mapped_column(JSON, default=list)
    # WHO triggered the propagation — an opaque subject id (never a name/email) plus its kind.
    acknowledged_by: Mapped[str] = mapped_column(String(128), default="", server_default="")
    acknowledged_by_type: Mapped[str] = mapped_column(
        String(32), default="system", server_default="system"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class GrievanceCategory(str, Enum):
    """DCA-082 — the kind of India privacy grievance a data principal raises (controlled vocabulary).

    A grievance names which obligation it concerns so the fiduciary can route and verify it
    proportionately. These mirror the India DPDP data-principal rights/complaint surface:

    - ``ACCESS`` — a request for a summary of the personal data processed about the principal.
    - ``CORRECTION`` — a request to correct/complete/update inaccurate personal data.
    - ``ERASURE`` — a request to erase personal data (the erasure LOGIC itself is a disabled future
      capability; this story only RECORDS the request).
    - ``CONSENT_WITHDRAWAL`` — a request to withdraw a previously-given consent.
    - ``NOMINATION`` — a nomination of another individual to exercise rights (e.g. on death/incapacity).
    - ``PROCESSING_COMPLAINT`` — a complaint about how the fiduciary is processing personal data.
    - ``OTHER`` — any other grievance not in the controlled set above.

    An unrecognized category is refused by the store. Recording a grievance performs NO erasure and
    authorizes nothing — acting on it stays a disabled future capability.
    """

    ACCESS = "access"
    CORRECTION = "correction"
    ERASURE = "erasure"
    CONSENT_WITHDRAWAL = "consent_withdrawal"
    NOMINATION = "nomination"
    PROCESSING_COMPLAINT = "processing_complaint"
    OTHER = "other"


class GrievanceVerificationLevel(str, Enum):
    """DCA-082 — the PROPORTIONATE identity-verification level a grievance requires before action.

    Verification is proportionate to the sensitivity of the request, not one-size-fits-all:

    - ``ELEVATED`` — categories that expose or alter personal data (access / correction / erasure)
      demand stronger proof of identity, because acting on a mistaken/forged request would itself be a
      personal-data incident.
    - ``STANDARD`` — the remaining categories (consent withdrawal / nomination / complaint / other)
      need ordinary proof.

    The level is DERIVED deterministically from the category by the store (never a free-text input),
    so a reviewer can see WHY a given grievance needs the proof it does.
    """

    STANDARD = "standard"
    ELEVATED = "elevated"


class GrievanceVerificationState(str, Enum):
    """DCA-082 — where a grievance sits on the proportionate-verification path.

    ``UNVERIFIED`` — the requester's identity has not yet been verified to the required level. Every
    grievance is recorded ``UNVERIFIED``; actually verifying a requester (and anything gated on it) is
    a DISABLED future capability, so no path in this story writes ``VERIFIED`` — recording a grievance
    can never, on its own, mark a requester as proven.
    """

    UNVERIFIED = "unverified"
    VERIFIED = "verified"


class GrievanceStatus(str, Enum):
    """DCA-082 — lifecycle state of a recorded grievance.

    ``RECEIVED`` — intake is complete and the grievance is logged with its proportionate verification
    requirement and its reviewed response deadline. The downstream lifecycle (acknowledge / review /
    resolve / reject, and the erasure logic for an erasure request) is a DISABLED future capability, so
    every row written here is ``RECEIVED``; nothing advances it yet.
    """

    RECEIVED = "received"


class PrivacyGrievance(TenantOwned, Base):
    """DCA-082 — a scoped India privacy grievance intake/status record (tenant-owned, RLS-protected).

    One row is ONE grievance a data principal raised with a business (the data fiduciary): what it is
    about (:class:`GrievanceCategory`), the proportionate verification it requires
    (:class:`GrievanceVerificationLevel` + :class:`GrievanceVerificationState`), the reviewed response
    deadline it must be answered by, and the opaque claim-check the requester uses to look up its
    status — never the requester's raw contact.

    The load-bearing guarantees (the DCA-082 acceptance criteria):

    - **No contact enumeration; visible only to the authorized requester or operators.** The row stores
      the requester's contact ONLY as a one-way :attr:`requester_contact_digest` (never the raw value),
      and the service exposes NO lookup-by-contact path, so no caller can enumerate or confirm who has
      filed. A grievance's status is readable two ways and no other: by the REQUESTER presenting the
      single-use-issued claim token whose hash is stored in :attr:`access_token_hash` (a wrong/absent
      token reads exactly like not-found — never an existence oracle), or by an authorized business
      OPERATOR holding the ``grievance.read`` scope for THIS business (a platform operator, who holds no
      membership, cannot; delegated client support is a separate scoped/expiring grant). Operator views
      carry grievance metadata only — never the contact digest or token hash — so authority to handle a
      grievance is never authority to harvest contacts.

    - **Recording a grievance changes nothing else and does no later-story work.** Intake is append-only
      and performs NO erasure, no send and no model call; it only logs the request. Acting on a
      grievance (acknowledge/resolve/erase) stays a DISABLED future capability, so a grievance is always
      recorded ``RECEIVED`` / ``UNVERIFIED``.

    The ``response_deadline_days`` / ``respond_by`` are the CONFIGURABLE, REVIEWED deadline pinned onto
    the row at intake (from the reviewed ``grievance_response_days`` setting), so the deadline is
    reproducible even if the configured policy changes later. Tenant-owned: it inherits the DCA-017
    ``business_id`` column and the DCA-018 RLS policy like every other tenant data table, so one tenant
    can never read another's grievances.
    """

    __tablename__ = "privacy_grievances"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # What the grievance is about (controlled GrievanceCategory); drives the proportionate verification.
    category: Mapped[str] = mapped_column(String(32), index=True)
    # The channel the requester gave a contact on (email | mobile | landline | whatsapp), or "" if none
    # was supplied. Descriptive context for proportionate verification — NOT the contact value itself.
    channel: Mapped[str] = mapped_column(String(16), default="", server_default="")
    # A ONE-WAY digest of the requester's normalized contact — never the raw value, and never exposed in
    # any response. Present so a duplicate submission is recognizable without enabling contact lookup.
    requester_contact_digest: Mapped[str] = mapped_column(
        String(64), default="", server_default="", index=True
    )
    # The requester's own description of the grievance (their words). Tenant-owned, RLS-protected; shown
    # only to an authorized operator and never in a status/list projection. Optional.
    detail: Mapped[str] = mapped_column(Text, default="", server_default="")
    # The PROPORTIONATE verification level required before the grievance may be actioned — derived
    # deterministically from the category by the store (standard | elevated).
    verification_level_required: Mapped[str] = mapped_column(
        String(16),
        default=GrievanceVerificationLevel.STANDARD.value,
        server_default=GrievanceVerificationLevel.STANDARD.value,
    )
    # Where the grievance sits on the verification path — always 'unverified' at intake (verifying a
    # requester is a disabled future capability).
    verification_state: Mapped[str] = mapped_column(
        String(16),
        default=GrievanceVerificationState.UNVERIFIED.value,
        server_default=GrievanceVerificationState.UNVERIFIED.value,
        index=True,
    )
    # Lifecycle state — always 'received' here (advancing it is a disabled future capability).
    status: Mapped[str] = mapped_column(
        String(16),
        default=GrievanceStatus.RECEIVED.value,
        server_default=GrievanceStatus.RECEIVED.value,
        index=True,
    )
    # The CONFIGURABLE, REVIEWED response-deadline policy (in days) pinned onto the row at intake, and
    # the concrete deadline it yields — so the deadline is reproducible even if the policy later changes.
    response_deadline_days: Mapped[int] = mapped_column(Integer)
    respond_by: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The SHA-256 hash of the claim token issued (once) to the requester at intake. The plaintext token
    # is returned to the requester a single time and never stored; status lookup compares a presented
    # token's hash against this, constant-time. Never exposed in any response.
    access_token_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class DataErasureJournal(TenantOwned, Base):
    """DCA-084 — the execution journal of ONE scoped operational-data erasure (tenant-owned, RLS).

    One row is the durable record of a single deletion the service actually executed across a business's
    DCA-owned OPERATIONAL data for one subject — its observations, evidence and projections. Unlike the
    DCA-083 :class:`~app.services.privacy_scope.PrivacyScopeResolver` (which only identifies a scope and
    executes nothing), DCA-084 is the first story that deletes rows; this journal is the "justified
    tombstone reference" the acceptance requires — it survives the erasure and names, in OPAQUE terms,
    WHAT was erased (counts + the erased rows' row ids, grouped by category), WHO executed it, WHEN,
    under WHICH authority, and WHY (the justification / the request it answers).

    The load-bearing guarantees (the DCA-084 acceptance criteria):

    - **The erased operational data is gone; only permitted minimal metadata is retained.** The
      observation / evidence / projection rows for the subject are hard-deleted by the single sanctioned
      executor (:class:`~app.services.data_erasure.OperationalDataEraser`), so they are no longer
      accessible by any tenant-scoped read. What remains is this journal row plus the atomic DCA-026
      audit entry — and both carry ONLY opaque references (row ids, counts, the opaque subject id, a
      justification code), never a contact value, an org/person name or a field value. ``tombstones``
      is the opaque id ledger of the deleted rows, so the erasure stays reviewable without re-exposing
      the personal data it removed.

    - **A denial changes nothing.** The executor checks authority FIRST and resolves the subject WITHIN
      this business BEFORE deleting anything, so an unauthorized caller, an unknown/cross-tenant subject
      or a missing justification each leaves every row — and writes no journal entry — exactly as it was.

    Permitted scope only: the executor erases the DCA's OWN operational observation/evidence/projection
    data for the subject. It deliberately does NOT touch the records a privacy obligation requires be
    retained — the audit trail, consent receipts, restriction ledgers — nor any platform-owned data;
    acting beyond this scope stays a disabled future capability. Tenant-owned: it inherits the DCA-017
    ``business_id`` column and the DCA-018 RLS policy like every other tenant data table, so one tenant
    can never read or drive another's erasures.
    """

    __tablename__ = "data_erasure_journals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # The erased subject — an OrgObservation.id or PersonObservation.id in THIS business (opaque ids).
    subject_type: Mapped[str] = mapped_column(String(16), index=True)
    subject_id: Mapped[str] = mapped_column(String(36), index=True)
    # WHAT KIND of request drove the erasure (e.g. a local grievance erasure, or an operator-initiated
    # erasure) — a short opaque code, descriptive only.
    request_kind: Mapped[str] = mapped_column(String(48), default="", server_default="")
    # The request this erasure answers — an opaque row key (e.g. a PrivacyGrievance.id), or "" when the
    # erasure was initiated directly by an authorized operator.
    request_ref: Mapped[str] = mapped_column(String(64), default="", server_default="")
    # A short opaque justification/reason for the erasure (screened for secret/contact data like the
    # audit trail). REQUIRED — an erasure must record WHY.
    justification: Mapped[str] = mapped_column(String(300))
    # Which authority plane authorized the erasure (always 'business' here) + the explicit scope held.
    authority_plane: Mapped[str] = mapped_column(
        String(16), default="business", server_default="business"
    )
    authority_scope: Mapped[str] = mapped_column(String(64), default="", server_default="")
    # WHO executed the erasure — an opaque subject id (never a name/email) plus its kind.
    executed_by: Mapped[str] = mapped_column(String(128))
    executed_by_type: Mapped[str] = mapped_column(String(32), default="user", server_default="user")
    # HOW MANY rows were deleted, by category (observations / evidence / projections) and in total.
    observations_deleted: Mapped[int] = mapped_column(Integer, default=0)
    evidence_deleted: Mapped[int] = mapped_column(Integer, default=0)
    projections_deleted: Mapped[int] = mapped_column(Integer, default=0)
    total_deleted: Mapped[int] = mapped_column(Integer, default=0)
    # The justified tombstone references: the OPAQUE row ids of every deleted row, grouped by table.
    # Shape: {"observations": {table: [ids]}, "evidence": [ids], "projections": [ids]}. No personal data.
    tombstones: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ExternalDeletionStatus(str, Enum):
    """DCA-085 — the per-destination outcome of propagating an erasure to an external integration.

    An erasure (DCA-084) removes the DCA's OWN operational data. Some of that data may ALSO live in a
    supported external platform/provider integration; this story sends an idempotent deletion/removal
    request through each supported contract and records the outcome here. Four terminal-or-pending
    states, and only these:

    - ``ACKNOWLEDGED`` — the destination accepted the deletion/removal and returned an acknowledgment
      reference. This is the only state that means "the external copy is confirmed removed".
    - ``UNSUPPORTED`` — the destination contract explicitly does NOT support deletion/removal (or does
      not support it for this subject kind). An honest, recorded "cannot be propagated here" — not a
      failure and not a silent success.
    - ``PENDING`` — the request was sent but no acknowledgment was received in time (a timeout). It is
      **pending reconciliation**, NOT a completed deletion: a later reconciliation re-sends the SAME
      idempotent request and only an acknowledgment advances it to ``ACKNOWLEDGED``.
    - ``FAILED`` — the destination explicitly rejected the request (a permanent, non-timeout error).
      Recorded distinctly from a timeout so a reviewer can tell "never acknowledged" from "refused".

    A timeout is deliberately NEVER folded into ``ACKNOWLEDGED`` — treating "we didn't hear back" as
    "deleted" is exactly the fail-open this story forbids.
    """

    ACKNOWLEDGED = "acknowledged"
    UNSUPPORTED = "unsupported"
    PENDING = "pending"
    FAILED = "failed"


class ExternalDeletionPropagation(TenantOwned, Base):
    """DCA-085 — a per-destination deletion-propagation receipt for one erasure (tenant-owned, RLS).

    One row records that a single external integration DESTINATION was asked to delete/remove the data
    for the subject of one :class:`DataErasureJournal`, and WHAT the destination said: an
    acknowledgment, an explicit unsupported state, a pending-reconciliation timeout, or a failure. It
    is the durable, reviewable record that the local erasure (DCA-084) was PROPAGATED outward through
    the supported platform/provider contracts.

    The load-bearing guarantees (the DCA-085 acceptance criteria):

    - **Idempotent; a re-run makes no unintended change.** Each ``(erasure_journal_id, destination)``
      is unique within a business, so propagating the same erasure twice reuses the same row. The
      :attr:`idempotency_key` sent to the external contract is deterministic, so a re-send is
      de-duplicated end to end. A destination already ``ACKNOWLEDGED`` / ``UNSUPPORTED`` is terminal —
      a re-run never re-sends to it and never rewrites it.

    - **A timeout is pending reconciliation, never a completed deletion.** A request that times out is
      recorded ``PENDING`` with its attempt count; it is NOT reported as acknowledged. Reconciliation
      re-sends the same idempotent request and only an acknowledgment moves it to ``ACKNOWLEDGED``.

    The row carries ONLY opaque references — the journal id, the subject id, an opaque destination key,
    an opaque acknowledgment reference and reason code, who requested it and when. It never holds a
    contact value, an org/person name or a field value. Tenant-owned: it inherits the DCA-017
    ``business_id`` column and the DCA-018 RLS policy like every other tenant data table, so one tenant
    can never read or drive another's propagations.
    """

    __tablename__ = "external_deletion_propagations"
    __table_args__ = (
        tenant_unique(
            "erasure_journal_id",
            "destination",
            name="uq_external_deletion_journal_destination",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # The erasure this propagation answers — a DataErasureJournal.id in THIS business (opaque id).
    erasure_journal_id: Mapped[str] = mapped_column(String(36), index=True)
    # The subject the deletion is for — echoed for traceability (opaque ids; mirrors the journal).
    subject_type: Mapped[str] = mapped_column(String(16), index=True)
    subject_id: Mapped[str] = mapped_column(String(36), index=True)
    # The external integration destination the request was sent to — an opaque, controlled contract
    # key (e.g. a platform/provider integration id), never a URL/secret/endpoint.
    destination: Mapped[str] = mapped_column(String(64), index=True)
    # The DETERMINISTIC idempotency key sent to the external contract, so an exact re-send is
    # de-duplicated by the destination. Opaque (derived from the journal id + destination).
    idempotency_key: Mapped[str] = mapped_column(String(128), default="", server_default="")
    # The per-destination outcome (ExternalDeletionStatus).
    status: Mapped[str] = mapped_column(String(16), index=True)
    # The opaque acknowledgment reference the destination returned (present only when ACKNOWLEDGED).
    acknowledgment_ref: Mapped[str] = mapped_column(String(128), default="", server_default="")
    # A stable, opaque reason/detail code for the outcome (e.g. why UNSUPPORTED / FAILED). Never a raw
    # provider message — a controlled code only.
    detail_code: Mapped[str] = mapped_column(String(64), default="", server_default="")
    # HOW MANY dispatch attempts have been made (1 on first send; incremented by each reconciliation).
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # WHO requested the propagation — an opaque subject id (never a name/email) plus its kind.
    requested_by: Mapped[str] = mapped_column(String(128), default="", server_default="")
    requested_by_type: Mapped[str] = mapped_column(
        String(32), default="user", server_default="user"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class RestrictedIdentityOrigin(str, Enum):
    """DCA-086 — the SOURCING origin a reacquisition tombstone speaks for (scope-aware comparison).

    A tombstone forbids *re-acquiring* an identity, but only within the origin it was restricted for —
    so the comparison is scope-aware, never a blanket block:

    - ``PLATFORM`` — the identity was a PLATFORM-sourced record (acquired through a catalogued source /
      the discovery cycle). A platform-sourced restriction blocks a platform-sourced reacquisition of
      the same identity; it is exactly the "restricted platform-sourced record cannot be resurrected"
      guarantee.
    - ``TENANT`` — the identity was the tenant's OWN supplied/first-party data. A tenant-origin
      restriction does NOT block a platform-sourced reacquisition (a different origin / lawful basis),
      and vice-versa. This is what keeps *unrelated lawful scoped data preserved* even when the raw
      identifier happens to collide.
    """

    PLATFORM = "platform"
    TENANT = "tenant"


class ReacquisitionPurpose(str, Enum):
    """DCA-086 — WHY a reacquisition tombstone is retained (a controlled, declared purpose).

    A tombstone is the minimal record kept so a restricted identity cannot be silently re-acquired.
    Because the tombstone still carries a (pseudonymous, NOT anonymous) identity fingerprint, it must
    declare the limited purpose it is retained FOR — it exists ONLY to screen reacquisition, never to
    re-enable contact, scoring or any other processing.
    """

    PREVENT_REACQUISITION = "prevent_reacquisition"


class RestrictedIdentityTombstone(TenantOwned, Base):
    """DCA-086 — a protected minimal tombstone that prevents reacquisition of a restricted identity.

    When a platform-sourced record is restricted/erased, deleting its operational rows is not enough:
    the NEXT discovery cycle would fetch the same identity from the same source and silently resurrect
    it. This tombstone is the durable "do not re-acquire this identity" marker that survives the
    deletion so a reimport is screened out rather than re-suggested.

    **Minimal and protected — a pseudonym, never called anonymous.** The row stores NO raw identifier
    (no CIN/GSTIN/email/phone): only a deterministic ``identity_hash`` (the fingerprint the scope-aware
    comparison matches on), the ``hash_algorithm`` that produced it, the subject kind, the sourcing
    ``origin`` it speaks for, the declared ``purpose`` and ``retention``, and opaque provenance (who/
    when/under which authority, and the erasure/restriction it answers). The hash is a *pseudonym* — it
    is derived from, and re-derivable from, the identifier, so it is treated as personal data: it is
    tenant-owned (RLS-protected like every other tenant table), minimised, retained only for the
    declared purpose and never exported. It is deliberately NOT labelled anonymous.

    **Scope-aware comparison (the load-bearing guarantee).** A tombstone blocks reacquisition only for
    the SAME business, the SAME subject kind, the SAME ``origin`` and the SAME identity fingerprint.
    A different identity, a different origin (a tenant-supplied record vs a platform-sourced one), or a
    different tenant is NOT blocked — unrelated lawful scoped data is preserved. Unresolved scope reads
    nothing (RLS fail-closed), so a tombstone can never leak across tenants.

    **Insert-only, idempotent.** Each ``(subject_type, origin, identity_hash)`` is unique within a
    business, so re-restricting the same identity reuses the existing tombstone rather than duplicating
    it. Lifting a tombstone (re-permitting reacquisition) stays a DISABLED future capability.
    """

    __tablename__ = "restricted_identity_tombstones"
    __table_args__ = (
        tenant_unique(
            "subject_type",
            "origin",
            "identity_hash",
            name="uq_restricted_identity_scope",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # The subject kind the restricted identity is (mirrors FieldSubjectType): organization | person.
    subject_type: Mapped[str] = mapped_column(String(16), index=True)
    # WHICH kind of identifier was fingerprinted (e.g. "org_identifier" / "contact_point") — descriptive
    # provenance for the hash, so a reviewer knows what the fingerprint is OF. A controlled short code.
    identity_scheme: Mapped[str] = mapped_column(String(32), default="", server_default="")
    # The deterministic PSEUDONYMOUS fingerprint of the restricted identity (hex). Derived from the raw
    # identifier; the raw identifier itself is NEVER stored. The scope-aware comparison matches on this.
    identity_hash: Mapped[str] = mapped_column(String(64), index=True)
    # The algorithm/scheme that produced ``identity_hash`` — recorded so the fingerprint is explicitly a
    # documented pseudonym (reproducible from the identifier), not an opaque "anonymous" value.
    hash_algorithm: Mapped[str] = mapped_column(
        String(32), default="sha256", server_default="sha256"
    )
    # The sourcing origin this tombstone speaks for (RestrictedIdentityOrigin) — the scope dimension
    # that keeps the comparison scope-aware (a platform restriction never blocks a tenant-origin record).
    origin: Mapped[str] = mapped_column(String(16), index=True)
    # WHY the tombstone is retained (ReacquisitionPurpose) — the limited, declared purpose.
    purpose: Mapped[str] = mapped_column(
        String(48),
        default=ReacquisitionPurpose.PREVENT_REACQUISITION.value,
        server_default=ReacquisitionPurpose.PREVENT_REACQUISITION.value,
    )
    # How long the tombstone is honoured (RestrictionRetentionMode) — until explicitly lifted
    # (indefinite) or until a stated date, after which a future sweep may treat it as lapsed.
    retention_mode: Mapped[str] = mapped_column(
        String(16),
        default=RestrictionRetentionMode.UNTIL_LIFTED.value,
        server_default=RestrictionRetentionMode.UNTIL_LIFTED.value,
    )
    retain_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # The restriction/erasure this tombstone answers — an opaque row key (e.g. a DataErasureJournal.id
    # or a ProcessingRestriction.id) + a short kind, both descriptive only. Optional.
    origin_kind: Mapped[str] = mapped_column(String(48), default="", server_default="")
    origin_ref: Mapped[str] = mapped_column(String(64), default="", server_default="")
    # Which authority plane authorized the tombstone (always 'business' here) + the explicit scope held.
    authority_plane: Mapped[str] = mapped_column(
        String(16), default="business", server_default="business"
    )
    authority_scope: Mapped[str] = mapped_column(String(64), default="", server_default="")
    # WHO recorded the tombstone — an opaque subject id (never a name/email) plus its kind.
    created_by: Mapped[str] = mapped_column(String(128), default="", server_default="")
    created_by_type: Mapped[str] = mapped_column(String(32), default="user", server_default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class DispatchRecheckConsumption(TenantOwned, Base):
    """DCA-095 — the durable "this dispatch slot was consumed" ledger row.

    An activation reference (DCA-094) proves the DCA's gates were clear for a context when it was
    *issued*. Immediately before a host dispatches, it must RE-ASK the DCA — permission, suppression
    and version state may have moved since issuance. This row is what makes that recheck a *consume*
    rather than a read: a definitive grant records exactly ONE row per ``(business_id,
    idempotency_key)``, and the unique constraint turns a concurrent or retried consume of the same
    dispatch into a single consumption the database itself serialises (why the focused tests use a real
    PostgreSQL, not SQLite).

    **Grants only.** A row exists ONLY for a dispatch the recheck definitively allowed. A denied
    recheck (a withdrawal recorded since issuance, a stale version, an unavailable authority, an
    expired/altered reference) writes NOTHING — a rejection leaves no state, so the denial reproduces
    with no change. A later retry under the SAME key replays this recorded grant verbatim (the dispatch
    already completed); a fresh key after a withdrawal is re-evaluated and blocked.

    **No raw recipient.** Like the reference it answers, the row binds a one-way ``recipient_digest``
    and a ``reference_fingerprint`` (a digest of the consumed reference token), never the address or the
    token itself — safe to keep and inspect. Tenant-owned: it inherits the ``business_id`` column +
    RLS like every other tenant table, so a consumption can never be read or matched across tenants.

    This records that the DCA re-cleared its OWN blockers for exactly one dispatch; it is NOT recipient
    consent and NOT a send authorization — the one send pipeline owns and performs the actual send,
    which stays a disabled future capability.
    """

    __tablename__ = "dispatch_recheck_consumptions"
    __table_args__ = (tenant_unique("idempotency_key", name="uq_dispatch_recheck_idempotency"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # The host-supplied consume-once key: one logical dispatch attempt within the business. The unique
    # constraint above is the serialisation point that makes a retried/concurrent consume idempotent.
    idempotency_key: Mapped[str] = mapped_column(String(128), index=True)
    # The channel the dispatch is on (e.g. "email") — part of the matched binding on a replay.
    channel: Mapped[str] = mapped_column(String(32))
    # The business's OWN resolved contact point id the reference was minted for (opaque row key).
    contact_point_id: Mapped[str] = mapped_column(String(36))
    # One-way digest of the recipient value — never the raw address. Matched on a replay.
    recipient_digest: Mapped[str] = mapped_column(String(64))
    # A digest of the consumed reference token itself (never the token) — so a replay can confirm the
    # SAME reference is being re-presented, and a different reference under a reused key is a conflict.
    reference_fingerprint: Mapped[str] = mapped_column(String(64))
    # Provenance of the grant: the sending provider identity + the campaign revision + the permission /
    # source versions the recheck confirmed were still current at consume time (opaque short strings).
    provider: Mapped[str] = mapped_column(String(64), default="", server_default="")
    campaign_revision: Mapped[str] = mapped_column(String(64), default="", server_default="")
    permission_version: Mapped[str] = mapped_column(String(64), default="", server_default="")
    source_version: Mapped[str] = mapped_column(String(64), default="", server_default="")
    # The suppression snapshot version the recheck cleared against (provenance for audit/repro).
    suppression_version: Mapped[str] = mapped_column(String(64), default="", server_default="")
    # When the dispatch slot was consumed (the authoritative grant moment).
    consumed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class DurableJobRequest(TenantOwned, Base):
    """DCA-099 — a durable, tenant-owned record of ONE requested unit of background work.

    This is the job *store*, not a job *runner*: it persists the intent to do some work so that intent
    survives a restart and can be deduplicated and audited. DCA-099 deliberately does NOT execute jobs —
    rows are only ever created :class:`JobRequestStatus.QUEUED`, and the attempt/status columns are
    bookkeeping scaffolding for a later (currently-disabled) worker capability, never driven here.

    **Deduplicated by logical intent.** ``(business_id, job_type, idempotency_key)`` is unique: the same
    logical work enqueued twice within a business collapses to exactly ONE row. The unique constraint is
    the serialisation point the writer (:class:`~app.jobs.store.JobRequestStore`) relies on, so a retried
    or concurrent enqueue of the same intent resolves to the one job rather than creating duplicates —
    which is why the focused tests exercise it on a real PostgreSQL, not SQLite.

    **No raw PII on the row.** The job carries only a ``payload_ref`` (an opaque pointer to where the
    real payload lives) and a ``payload_digest`` (a one-way fingerprint of that payload), never the
    payload itself. The digest lets a replay confirm the SAME work is being re-enqueued; a *different*
    payload under a reused key is a conflict (rejected, nothing written), never silently re-homed onto
    the first job. ``payload_metadata`` holds only small, non-identifying control fields.

    **Claimed safely across workers (DCA-101).** The ``lease_owner`` / ``lease_expires_at`` /
    ``lease_version`` columns let a worker CLAIM a queued job in a short transaction
    (:class:`~app.jobs.claim.JobClaimStore`): ``SELECT ... FOR UPDATE SKIP LOCKED`` picks one row, the
    row flips to RUNNING with a lease + an incremented fencing token, and the transaction COMMITS —
    releasing the row lock before any handler I/O. A live lease stops a second worker claiming the same
    row; an expired lease lets a crashed worker's job be reclaimed (fencing token bumped again), and a
    stale token is refused. Claiming leases a job; it still EXECUTES nothing — the handler body
    (:meth:`~app.jobs.handlers.JobHandler.run`) is a disabled future capability.

    Tenant-owned: it inherits the DCA-017 ``business_id`` column and the DCA-018 row-level-security
    policy like every other tenant table, so a job can never be read or matched across tenants. Creating
    a job grants nothing and sends nothing — the DCA has no send path (a disabled future capability).
    """

    __tablename__ = "durable_job_requests"
    # One job per logical intent within a business: the same job_type + idempotency key collapses to one
    # row; two tenants (or two job types) may reuse a key without colliding.
    __table_args__ = (tenant_unique("job_type", "idempotency_key", name="uq_durable_job_request"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # The version of the payload contract this job was enqueued under — so a later reader knows how to
    # interpret the referenced payload across contract changes (expand→migrate→contract).
    schema_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    # The logical kind of work (e.g. "prospect.research"). Part of the dedup key; opaque to the store.
    job_type: Mapped[str] = mapped_column(String(64), index=True)
    # The caller-supplied consume-once key for one logical unit of work within the business. The unique
    # constraint above is the serialisation point that makes a retried/concurrent enqueue idempotent.
    idempotency_key: Mapped[str] = mapped_column(String(200), index=True)
    # An opaque pointer to where the real payload lives (NEVER the payload itself, NEVER raw PII).
    payload_ref: Mapped[str] = mapped_column(String(300))
    # A one-way digest (sha256 hex) of the logical payload — matched on a replay to confirm the SAME
    # work; a different digest under a reused key is a conflict. A fingerprint, never the data.
    payload_digest: Mapped[str] = mapped_column(String(64))
    # Small, non-identifying control fields only (never contact data) — e.g. a priority label.
    payload_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    # Always "queued" here — DCA-099 persists requests, it does not run them.
    status: Mapped[str] = mapped_column(
        String(16),
        default=JobRequestStatus.QUEUED.value,
        server_default=JobRequestStatus.QUEUED.value,
    )
    # Attempt bookkeeping for a future worker: how many times a run has been attempted, the ceiling
    # before dead-lettering, and the earliest moment the job may run. The claim path (DCA-101)
    # increments attempt_count as it leases a job; completion/retry stays a disabled future capability.
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, server_default="5")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # DCA-101 lease/fencing. A claim is a SHORT PostgreSQL transaction (SELECT ... FOR UPDATE SKIP
    # LOCKED, then a flip to RUNNING) whose row lock is released at COMMIT — BEFORE any handler I/O —
    # so slow external work never holds a database lock. The lease then marks the claim at the
    # application level (independent of the DB lock): ``lease_owner`` is the worker that holds it and
    # ``lease_expires_at`` is when the claim lapses so a crashed/stalled worker's job can be reclaimed.
    # ``lease_version`` is a monotonic FENCING TOKEN bumped on every (re)claim: a worker holds the
    # token it was handed, and a stale holder (whose lease was superseded) is rejected by a fence check,
    # so two workers can never hold the SAME current claim even across a lease handover. All three are
    # NULL/zero on an unclaimed QUEUED row; nothing here executes a job.
    lease_owner: Mapped[str | None] = mapped_column(String(128), default=None)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    lease_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # DCA-102 bounded-extension ceiling. Stamped at the moment a worker CLAIMS (or reclaims) the row:
    # the absolute latest a heartbeat (:meth:`~app.jobs.claim.JobClaimStore.renew`) may push this claim's
    # ``lease_expires_at`` out to. A renewal can keep a long-but-live job leased, but never past this
    # deadline — so a stuck/looping worker cannot heartbeat a claim forever and starve a reclaim. A
    # reclaim resets it (the new holder gets a fresh lifetime budget); NULL on an unclaimed/legacy row
    # (treated as "no DCA-102 ceiling recorded"). Nothing here executes a job.
    lease_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # The last failure note a future worker recorded; always NULL here (nothing runs).
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    # DCA-103 fenced completion. When a worker COMMITS a result (:meth:`~app.jobs.complete.JobCompletionStore.complete`)
    # the job flips to ``SUCCEEDED`` and its lease is cleared, and these record the committed result — an
    # OPAQUE pointer + one-way digest, NEVER raw PII (screened with the same rule as ``payload_ref``), plus
    # the moment it was committed. All NULL on a queued/running row: a commit is guarded by the current
    # lease fence AND a re-check of the authorization revisions the worker relied on, so a stale/replaced
    # worker (reclaimed job) or one whose source rights changed cannot commit. Committing a result records
    # the terminal status; it still EXECUTES nothing — the handler body stays a disabled future capability.
    result_ref: Mapped[str | None] = mapped_column(String(300), default=None)
    result_digest: Mapped[str | None] = mapped_column(String(64), default=None)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class IdentityMergeStatus(str, Enum):
    """DCA-071 — the lifecycle state of a reviewed identity-merge record (controlled vocabulary).

    - ``ACTIVE`` — a reviewer approved the merge; it is the current link between the two identities.
    - ``REVERSED`` — the merge was later undone. This story records only ``ACTIVE`` merges; reversal
      (flipping an ``ACTIVE`` merge to ``REVERSED``) is the SEPARATE next story (DCA-072), so the column
      exists purely as the lineage DCA-072 will drive — nothing here ever writes ``REVERSED``.
    """

    ACTIVE = "active"
    REVERSED = "reversed"


class IdentityMerge(TenantOwned, Base):
    """DCA-071 — a reviewed, evidenced identity-merge record (tenant-owned, RLS-protected).

    One row is ONE reviewer's approval that two of THIS business's observed identities — two
    :class:`OrgObservation` rows or two :class:`PersonObservation` rows — are the SAME real-world
    entity. It is the human review ACTION that sits on top of a pure matcher *conclusion*: the
    DCA-067/068 matchers only ever return ``MATCH`` / ``NO_MATCH`` / ``REVIEW`` / ``INSUFFICIENT`` and
    merge, transfer and record NOTHING (``auto_merged`` is always ``False``). A merge is never an
    automatic collapse — it is this deliberate, authorized, evidenced record.

    The load-bearing guarantees (the DCA-071 acceptance criteria):

    - **Only a REVIEWED, COMPATIBLE pair is recorded.** The single sanctioned writer
      (:class:`~app.services.identity_merge.IdentityMergeStore`) re-runs the relevant matcher over the
      two STORED observations and refuses unless the conclusion is compatible — a ``MATCH`` or a
      ``REVIEW`` candidate (the "ambiguous" merge a human approves). A ``NO_MATCH`` (a conflicting typed
      registration identifier) or an ``INSUFFICIENT`` pair (no linking evidence at all) is refused BEFORE
      the session is mutated, so an incompatible merge stages no row. ``match_outcome`` /
      ``match_confidence`` / ``evidence`` snapshot exactly the conclusion that justified the merge.

    - **Consent/permission scopes stay SEPARATE.** Recording a merge links two IDENTITIES only; it never
      creates, modifies or transfers any :class:`ConsentReceipt` or :class:`ProcessingRestriction`. The
      two identities' permission state remains exactly as it was — merging identity is not merging
      consent, suppression or restrictions (mirrors the DCA-068 guarantee that identity resolution never
      moves one person's permission onto another).

    - **Authorized by a HUMAN reviewer; an AI suggestion can never execute a merge.** A merge may only be
      recorded by a business user whose membership roles grant
      :attr:`~app.identity.permissions.Scope.IDENTITY_MERGE` AND who is a human user actor — a read-only
      viewer, a non-member, a platform operator and an AI/automation (``service`` / ``system``) actor are
      each refused before any state is read or written (the relevant denial). ``reviewer_id`` /
      ``reviewer_type`` record WHO approved it (an opaque subject id, never a name/email), on the atomic
      DCA-026 audit entry staged in the SAME unit of work as the row.

    - **Business-private, tenant-scoped.** The row is owned by exactly one ``business_id`` and read only
      within it (DCA-017/018). Both merged subjects are resolved WITHIN this business, so a cross-tenant
      or unknown subject reads as not-found and a merge can never link another tenant's identity; a by-id
      read of another business's merge resolves to not-found.

    **Insert-only, idempotent-safe lineage.** ``(business_id, subject_type, primary_subject_id,
    duplicate_subject_id)`` is unique: a pair is merged at most once per business, and re-approving an
    already-merged pair (in either direction) returns the existing row rather than inserting a duplicate.
    The record stores both subject references, the direction (``primary`` = the surviving identity,
    ``duplicate`` = the merged-away one), the justifying evidence and the reviewer — enough that the
    SEPARATE next story (DCA-072) can reverse it by flipping ``status`` to ``reversed``. Reversal is NOT
    implemented here. Acting on a merged identity anywhere downstream (collapsing observations, combining
    records, sending) stays a DISABLED future capability — the record is a reviewed conclusion, nothing
    is sent or granted.
    """

    __tablename__ = "identity_merges"
    # A directed pair is merged at most once PER business; the reverse direction and re-approval are
    # collapsed to the existing row by the store, so a pair is never recorded twice.
    __table_args__ = (
        tenant_unique(
            "subject_type",
            "primary_subject_id",
            "duplicate_subject_id",
            name="uq_identity_merge_pair",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # The version of the merge-record contract this row was written under — so a later reader can
    # interpret the stored evidence/direction across contract changes (expand→migrate→contract).
    schema_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    # organization | person — the kind of identity being merged (both subjects share it). A merge is
    # always within ONE kind: an organization is never merged with a person.
    subject_type: Mapped[str] = mapped_column(String(16), index=True)
    # The SURVIVING identity (an OrgObservation.id or PersonObservation.id in THIS business) the merge
    # keeps as canonical.
    primary_subject_id: Mapped[str] = mapped_column(String(36), index=True)
    # The MERGED-AWAY identity (also in THIS business) recorded as the same entity as the primary.
    duplicate_subject_id: Mapped[str] = mapped_column(String(36), index=True)
    # The matcher conclusion that justified the merge: "match" or "review" (an "ambiguous" candidate a
    # human approved). A "no_match"/"insufficient" pair is refused by the store and never stored here.
    match_outcome: Mapped[str] = mapped_column(String(16), index=True)
    # How strongly the evidence supported the conclusion (the matcher's confidence grade).
    match_confidence: Mapped[str] = mapped_column(String(16), default="", server_default="")
    # A snapshot of the evidence signals the matcher produced (kind/strength/relation/detail) — company
    # facts / mailbox domains only, never a raw personal contact value (the matchers keep these opaque).
    # RLS-protected here; only an opaque signal COUNT is ever written to the audit trail.
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    # active | reversed — ACTIVE on every row written here; reversal to "reversed" is DCA-072 (lineage).
    status: Mapped[str] = mapped_column(
        String(16),
        default=IdentityMergeStatus.ACTIVE.value,
        server_default=IdentityMergeStatus.ACTIVE.value,
        index=True,
    )
    # A short opaque justification for the merge (e.g. "same_cin_verified"); never free-form contact
    # data — screened by the audit sink. Optional.
    reason: Mapped[str] = mapped_column(String(200), default="", server_default="")
    # WHO approved the merge — an opaque human-user subject id (never a name/email) plus its kind,
    # screened by the audit sink in the SAME unit of work as the row.
    reviewer_id: Mapped[str] = mapped_column(String(128))
    reviewer_type: Mapped[str] = mapped_column(String(32), default="user", server_default="user")
    # When the reviewer approved the merge, and when this row was recorded in the service.
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
