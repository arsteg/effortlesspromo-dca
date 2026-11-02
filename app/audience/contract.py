"""DCA-154 — the audience-sync contract: sync an accepted prospect ONLY through EffortlessPromo.

Key: ``crm-port`` · source sections 7, 10, 12, 13. Stage: User selection and EffortlessPromo sync.

The DCA never owns a CRM or an audience. The EffortlessPromo platform is the system of record for
every business's CRM records and marketing audiences; the DCA can only ever ASK the platform to take
one accepted prospect into that system of record. So the only shape the DCA may legitimately define
here is a *request-sync* port: it hands the platform a DCA-153 :class:`~app.db.models.AcceptanceIntent`
(the thing a business user chose to accept) under a stable request key, and the platform's own CRM
re-gates and performs the write. There is deliberately no ``create``/``upsert``/``promote`` method and
no audience store — requesting a sync through EffortlessPromo is all the DCA is ever allowed to do.

Four things make a sync request legitimate and inspectable, and all four are typed here:

- **External IDs.** An acknowledged sync returns the platform's own :class:`ExternalRecordRef` (the CRM
  record id + audience id) — opaque handles the DCA keeps so it never re-syncs the same subject and can
  reconcile later. The DCA refers to the subject only by its OWN ids (the intent / subject id); the
  external ids are minted and owned by the platform.
- **Field visibility.** Every carried :class:`AudienceField` declares its :class:`AudienceFieldClass`.
  A ``research_only`` acceptance may carry ORGANIZATION-class facts only; a CONTACT-class (restricted
  recipient) field can NEVER ride along — :func:`authorize_audience_sync` refuses the whole request
  rather than silently stripping or promoting it. Acceptance is NOT recipient consent, so an accepted
  research-only organization can never become a sendable contact through this contract.
- **Purpose.** A request names its :class:`AudienceSyncPurpose`. A ``research_only`` subject may be
  synced for ``research`` only; a ``marketing`` purpose would require recipient consent the DCA does not
  hold, so it is refused.
- **Stable request key + acknowledgment result.** The request carries the SAME key the acceptance
  intent was staged under, so a retried sync is the SAME logical write, never a duplicate CRM record.
  The platform answers with an :class:`AudienceSyncAck` whose ``status`` distinguishes a PENDING hand-off
  (taken into the CRM's queue, no external ids yet) from an ACKNOWLEDGED one (external ids present) — the
  two outcomes are deliberately NOT the same, so a caller never mistakes "queued" for "written".

Mirrors the DCA-096 delivery and DCA-037 platform-profile contracts: typed shapes + a client
``Protocol`` + a deterministic fail-closed decision here, a synthetic fake alongside
(``app/audience/fake.py``), and a live HTTP adapter deferred to a later story (blocked on DCA-001 B1 —
there is no service-to-service contract into the .NET API / its CRM yet). The contract stays DISABLED in
production until that adapter ships; the fake never stands in for it. Authority is always the platform's
(blocker B3): ``X-Service-Auth`` authenticates the caller service, NOT a tenant, so a sync only ever
targets a business the DCA has itself authorized and is scoped to that one ``business_id``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:  # annotation only — the contract keeps no runtime dependency on the ORM model.
    from app.db.models import AcceptanceIntent

# The two visibility values an acceptance intent can carry (the DCA-145/153 controlled vocabulary),
# restated here so the contract validates against them WITHOUT importing the ORM at runtime. A
# research-only organization is never a sendable recipient.
VISIBILITY_RESEARCH_ONLY = "research_only"
VISIBILITY_SENDABLE = "sendable"
_ALLOWED_VISIBILITIES = frozenset({VISIBILITY_RESEARCH_ONLY, VISIBILITY_SENDABLE})


class AudienceFieldClass(str, Enum):
    """How restricted a single synced field is (controlled vocabulary).

    - ``ORGANIZATION`` — an organization-level fact (business type, sector, website host, …). Not a
      recipient and not restricted contact PII; it may ride along with a ``research_only`` sync.
    - ``CONTACT`` — a restricted recipient field (an email/phone/address — carried only ever as a
      one-way digest, never raw). It represents a sendable recipient and therefore requires recipient
      consent the DCA does not hold. It can NEVER accompany a ``research_only`` acceptance; the contract
      refuses the whole request rather than silently promoting it.
    """

    ORGANIZATION = "organization"
    CONTACT = "contact"


class AudienceSyncPurpose(str, Enum):
    """The declared purpose a sync is requested for (controlled vocabulary).

    - ``RESEARCH`` — populate the business's CRM with an organization it may research/qualify. The only
      purpose a ``research_only`` acceptance may be synced for.
    - ``MARKETING`` — add a sendable recipient to a marketing audience. Requires recipient consent
      (acceptance is NOT consent), so no path in this service produces it; it stays refused here.
    """

    RESEARCH = "research"
    MARKETING = "marketing"


class AudienceSyncStatus(str, Enum):
    """The outcome of a sync request (controlled vocabulary). PENDING and ACKNOWLEDGED differ on purpose.

    - ``PENDING`` — the platform TOOK the request into its CRM's queue but has not yet written the
      record; no external ids exist yet. A hand-off, never a completed write.
    - ``ACKNOWLEDGED`` — the platform wrote the record and returned its :class:`ExternalRecordRef`
      (external ids present). Only this terminal status carries external ids.
    - ``DISABLED`` — the request was refused (a restricted field, a disallowed purpose, or a sendable
      sync); nothing was taken into any CRM and no external ids were minted.
    """

    PENDING = "pending"
    ACKNOWLEDGED = "acknowledged"
    DISABLED = "disabled"


class ExternalRecordRef(BaseModel):
    """The platform's OWN external ids for a synced subject — minted and owned by EffortlessPromo.

    The DCA never invents these; it keeps them only to reconcile and to avoid re-syncing the same
    subject. ``crm_record_id`` is the platform CRM record; ``audience_id`` is the audience the record
    belongs to; ``external_version`` is the source version the ids were read at (provenance). Present
    ONLY on an ACKNOWLEDGED ack — a PENDING hand-off has no external ids yet.
    """

    model_config = ConfigDict(frozen=True)

    crm_record_id: str = Field(min_length=1)
    audience_id: str = Field(min_length=1)
    external_version: str = ""


class AudienceField(BaseModel):
    """One minimized field offered for sync, tagged with how restricted it is.

    ``value_digest`` is a MINIMIZED, one-way representation — never a raw contact value (an email/phone
    is only ever carried as a digest, exactly as the DCA-096 delivery context carries ``recipient_digest``).
    ``field_class`` is load-bearing: it is what :func:`authorize_audience_sync` checks to refuse a
    restricted CONTACT field under a ``research_only`` acceptance. The platform maps digests back to its
    own records via its external ids; the DCA holds no raw contact.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1)
    field_class: AudienceFieldClass
    value_digest: str = Field(min_length=1)


class AudienceSyncRequest(BaseModel):
    """A request to sync one accepted prospect THROUGH EffortlessPromo — a stable key + its minimized facts.

    ``request_key`` is the SAME stable key the DCA-153 acceptance intent was staged under (its outbox
    event id), so a retried sync is the SAME logical write and can never create a second CRM record.
    ``intent_id`` / ``subject_id`` are the DCA's OWN references to the accepted thing; ``visibility`` is
    CAPTURED from the intent (never widened here) and ``purpose`` declares why the sync is asked for.
    ``fields`` are the minimized facts offered; a CONTACT-class field present under a ``research_only``
    visibility is refused by the gate, never silently carried. ``correlation_id`` is carried through so
    the (future) HTTP call and its logs share one id. There is no raw contact and no message content —
    the DCA requests a CRM write of an already-decided acceptance; the platform owns the write.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    business_id: str = Field(min_length=1)
    request_key: str = Field(min_length=1, max_length=128)
    intent_id: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    subject_type: str = "organization"
    visibility: str = Field(min_length=1)
    purpose: AudienceSyncPurpose = AudienceSyncPurpose.RESEARCH
    fields: tuple[AudienceField, ...] = ()
    correlation_id: str | None = None

    @field_validator("visibility")
    @classmethod
    def _known_visibility(cls, value: str) -> str:
        if value not in _ALLOWED_VISIBILITIES:
            raise ValueError(
                f"visibility must be one of {sorted(_ALLOWED_VISIBILITIES)}; got {value!r}"
            )
        return value

    @property
    def has_restricted_fields(self) -> bool:
        """Whether any offered field is a restricted CONTACT field (the promotion the gate refuses)."""
        return any(f.field_class is AudienceFieldClass.CONTACT for f in self.fields)

    @property
    def confers_consent(self) -> bool:
        """Requesting a sync is NEVER recipient consent. Kept explicit so no caller mistakes it."""
        return False


class AudienceSyncAck(BaseModel):
    """What the EffortlessPromo CRM returns for a sync request. Never recipient consent, never a send.

    ``accepted`` is whether the platform TOOK the request (PENDING or ACKNOWLEDGED) — a refused request
    is ``accepted=False`` with ``status=DISABLED``. ``status`` distinguishes a PENDING hand-off (queued,
    no external ids) from an ACKNOWLEDGED write (external ids present) — the two outcomes differ by
    construction. ``external_ref`` is present ONLY on an ACKNOWLEDGED ack (enforced below). ``reason`` is
    a stable code (an :class:`AudienceSyncDecision` reason on a disable). ``request_key`` echoes the
    request's key so a replay is recognizable.
    """

    model_config = ConfigDict(frozen=True)

    accepted: bool
    status: AudienceSyncStatus
    reason: str
    request_key: str = Field(min_length=1)
    external_ref: ExternalRecordRef | None = None
    synthetic: bool = False

    @model_validator(mode="after")
    def _external_ids_only_when_acknowledged(self) -> AudienceSyncAck:
        # External ids exist exactly when the platform actually wrote the record. A PENDING hand-off or a
        # DISABLED refusal must NEVER carry them — so "queued" / "refused" can never masquerade as "written".
        if self.status is AudienceSyncStatus.ACKNOWLEDGED and self.external_ref is None:
            raise ValueError("an acknowledged sync must carry its external record ids")
        if self.status is not AudienceSyncStatus.ACKNOWLEDGED and self.external_ref is not None:
            raise ValueError("only an acknowledged sync may carry external record ids")
        return self

    @property
    def is_acknowledged(self) -> bool:
        return self.status is AudienceSyncStatus.ACKNOWLEDGED

    @property
    def is_pending(self) -> bool:
        return self.status is AudienceSyncStatus.PENDING

    @property
    def confers_consent(self) -> bool:
        """Accepting a sync request confers NO recipient consent. Always ``False``."""
        return False


@runtime_checkable
class AudienceSyncPort(Protocol):
    """Port the DCA uses to REQUEST an audience/CRM sync through EffortlessPromo.

    Synchronous, matching the other service-to-service clients (identity/entitlement/delivery). There is
    exactly one method — ``sync`` — and deliberately no ``create`` / ``upsert`` / ``promote`` / audience
    method: the DCA can only ever REQUEST a sync; the platform's CRM performs the write. The only
    implementation today is the dev/test fake; the live HTTP adapter is a later story (DCA-001 B1) and
    until it ships the contract stays disabled in production.
    """

    def sync(self, request: AudienceSyncRequest) -> AudienceSyncAck: ...


@dataclass(frozen=True)
class AudienceSyncDecision:
    """Whether a sync request may be handed to the platform. ``reason`` is a stable code.

    An allow is permission only to REQUEST the CRM write — it is NOT recipient consent and NOT a send
    (``confers_consent`` is always ``False``). The platform still re-gates at write time, and the actual
    sync stays a disabled future capability. Fail-closed: a sendable sync, a restricted contact field, or
    a disallowed purpose each DENIES rather than being silently promoted.
    """

    allowed: bool
    reason: str

    OK: ClassVar[str] = "ok"
    SENDABLE_SYNC_DISABLED: ClassVar[str] = "sendable_audience_sync_disabled"
    RESTRICTED_FIELD_NOT_PERMITTED: ClassVar[str] = "restricted_contact_field_not_permitted"
    PURPOSE_NOT_PERMITTED: ClassVar[str] = "audience_sync_purpose_not_permitted"
    UNKNOWN_VISIBILITY: ClassVar[str] = "audience_sync_visibility_unknown"

    @property
    def confers_consent(self) -> bool:
        """Clearing the sync gate is not recipient consent. Kept explicit for every caller."""
        return False


def authorize_audience_sync(request: AudienceSyncRequest) -> AudienceSyncDecision:
    """Decide whether ``request`` may be handed to the platform's CRM — a pure, reproducible function.

    Fail-closed, in order (each step leaves nothing changed — the function reads and mutates nothing):

    1. **Known visibility.** An unrecognized visibility is refused (defence in depth; the request model
       already validates it).
    2. **Sendable sync is disabled.** A ``sendable`` subject would be a recipient, which requires consent
       no path produces — refused (the future capability stays off).
    3. **No restricted contact field.** A ``research_only`` acceptance carrying a CONTACT-class field is
       refused outright — the contract can never SILENTLY promote a restricted field (the DCA-154
       acceptance criterion).
    4. **Permitted purpose.** A ``research_only`` subject may be synced for ``research`` only; any other
       purpose is refused (marketing requires consent).

    Otherwise the request may be REQUESTED — not consent, not a send; the platform's CRM still owns the
    write, which stays a disabled future capability.
    """
    if request.visibility not in _ALLOWED_VISIBILITIES:
        return AudienceSyncDecision(False, AudienceSyncDecision.UNKNOWN_VISIBILITY)

    # A sendable subject is a recipient — it requires recipient consent the DCA does not hold. Disable
    # the sync rather than let an accepted prospect become a sendable CRM contact.
    if request.visibility == VISIBILITY_SENDABLE:
        return AudienceSyncDecision(False, AudienceSyncDecision.SENDABLE_SYNC_DISABLED)

    # research_only: a restricted CONTACT field can NEVER ride along — refuse the whole request rather
    # than silently stripping or promoting it (the load-bearing acceptance criterion).
    if request.has_restricted_fields:
        return AudienceSyncDecision(False, AudienceSyncDecision.RESTRICTED_FIELD_NOT_PERMITTED)

    # research_only may be synced for research only; marketing would need consent that does not exist.
    if request.purpose is not AudienceSyncPurpose.RESEARCH:
        return AudienceSyncDecision(False, AudienceSyncDecision.PURPOSE_NOT_PERMITTED)

    return AudienceSyncDecision(True, AudienceSyncDecision.OK)


def audience_sync_request_from_intent(
    intent: AcceptanceIntent,
    *,
    purpose: AudienceSyncPurpose = AudienceSyncPurpose.RESEARCH,
    fields: tuple[AudienceField, ...] = (),
    correlation_id: str | None = None,
) -> AudienceSyncRequest:
    """Build a sync request from a DCA-153 :class:`~app.db.models.AcceptanceIntent` — the thing accepted.

    Captures the intent's business, subject and visibility UNCHANGED (the sync can never widen what was
    accepted) and reuses the intent's stable outbox event id as the request key, so a retried sync is the
    SAME logical write. Falls back to the intent id when (defensively) no outbox event id was recorded.
    Pure: reads the intent's attributes only; stages, syncs and sends nothing.
    """
    return AudienceSyncRequest(
        business_id=intent.business_id,
        request_key=intent.outbox_event_id or intent.id,
        intent_id=intent.id,
        subject_id=intent.subject_id,
        subject_type=intent.subject_type,
        visibility=intent.visibility,
        purpose=purpose,
        fields=tuple(fields),
        correlation_id=correlation_id,
    )
