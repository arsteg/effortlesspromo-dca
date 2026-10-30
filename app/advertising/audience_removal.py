"""DCA-098 — request removal of a revoked member from a previously uploaded advertising audience.

Key: ``audience-remove`` · source sections 5, 10. Stage: India channel eligibility.

DCA-091 decided whether a customer-list audience *may* be uploaded to an advertising platform (Google
Customer Match): the per-member first-party data-origin gate plus the account-permission gate. This
story is the mirror image on the *way out*: once a member whose identifier was previously uploaded to
an advertising audience has their permission **withdrawn** (consent revoked, newly suppressed, a source
right expired), the DCA must get that member **removed** from the audience so the ad platform stops
targeting them.

The DCA never touches an ad-platform SDK, key, or audience API directly — exactly as it never sends. It
can only ever *REQUEST* a removal through EffortlessPromo (the integration the platform owns), by naming
the previously-uploaded member (as a one-way hashed digest, never the raw identifier) under an
idempotency key. There is deliberately no ``upload`` / ``add`` / ``sync`` / provider method: the DCA can
only ask for a member to be REMOVED; it can never add a member or perform the integration I/O itself. A
removal is expressed as the platform's audience-**update** "remove" operation — the only mutation this
contract models.

Two realities shape the contract, and both are typed here:

- **The withdrawal is authoritative — the prior upload never overrides it.** The moment a member is
  withdrawn, the DCA's own truth is *withdrawn*, independent of what the advertising audience still
  shows. A member that is still PRESENT on the host because a removal has not yet succeeded does NOT get
  treated as active again: :attr:`RemovalReceipt.withdrawal_upheld` is always ``True``. The stale fact
  that the member was once uploaded can never flip the current withdrawal back to membership.
- **Removal failures stay visible and pending — never silently dropped, never faked as done.** A member
  the host could not remove right now (a temporary platform failure), or an entire request to a host
  that is unavailable / stale / non-acknowledging / does not support removal, does NOT disappear and is
  NOT reported as removed. It is labeled :attr:`RemovalItemStatus.PENDING` and stays *owed* so the DCA
  retries it until the host confirms the removal. An unconfirmable removal is never treated as a success
  — that is the fail-open this story forbids.

:func:`authorize_removal` encodes the fail-closed host gate: an absent, unavailable, stale,
removal-unsupporting, non-acknowledging, or audience-blind host capability yields a deny, so the whole
request stays PENDING (visible, retryable) rather than being bypassed. The decision is a pure function of
the capabilities and the moment — it reads nothing and changes nothing, so the allow and every denial
reproduce exactly.

A removal request is not recipient consent and not a send authorization (``authorizes_send`` is always
``False``). Mirrors the DCA-096 delivery and DCA-097 cancellation contracts: typed shapes + a client
``Protocol`` + a deterministic fail-closed decision here, a synthetic fake alongside
(``app/advertising/audience_removal_fake.py``), and a live HTTP adapter deferred to a later story
(blocked on DCA-001 B1 — there is no service-to-service contract into the .NET API yet, and no live
advertising-audience integration exists). The contract stays disabled in production until that adapter
ships; the fake never stands in for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

# A removal-host capability answer older than this is treated as stale and cannot authorize a request.
# Removal rides the same fast-moving consent/suppression truth as delivery/cancellation, so the freshness
# window is deliberately tight: a cached "I acknowledge removals" claim from long ago may no longer hold.
DEFAULT_MAX_CAPABILITIES_AGE = timedelta(hours=1)


def _norm(value: str) -> str:
    """Case-insensitive, whitespace-trimmed key for matching an audience against the host's scope."""
    return value.strip().casefold()


class RemovalHostAvailability(str, Enum):
    """Whether the EffortlessPromo advertising-audience removal contract could be consulted.

    - ``AVAILABLE`` — the platform answered; its capabilities are authoritative as of ``as_of``.
    - ``UNAVAILABLE`` — the contract could not be reached/answered. The DCA has no authoritative view of
      whether a removal would be acknowledged, so it must fail closed — an unavailable host authorizes
      nothing (the capability fields are meaningless here) and the removal stays pending.
    """

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class AudienceMemberState(str, Enum):
    """The host-side state of one previously-uploaded member, which decides the removal outcome.

    - ``PRESENT`` — the member is currently in the uploaded advertising audience and can be removed.
    - ``REMOVED`` — the member has already been removed from the audience (an earlier removal, or never
      present). A removal request over it is a no-op reported as ``NOT_PRESENT``, never fabricated.
    """

    PRESENT = "present"
    REMOVED = "removed"


class RemovalItemStatus(str, Enum):
    """The outcome of trying to remove ONE previously-uploaded audience member.

    - ``REMOVED`` — the member was ``PRESENT`` in the audience and the host removed it.
    - ``NOT_PRESENT`` — the host has no such member in the audience (already removed, or never uploaded).
      Reported, never fabricated as a removal.
    - ``PENDING`` — the removal could NOT be confirmed right now (the host is unavailable/stale/
      non-acknowledging, or a per-member temporary platform failure). The member stays *owed* and the
      DCA must retry; the prior upload never overrides the withdrawal while the removal is pending.
    """

    REMOVED = "removed"
    NOT_PRESENT = "not_present"
    PENDING = "pending"


class RemovalCapabilities(BaseModel):
    """An immutable, point-in-time view of what the EffortlessPromo removal contract offers a business.

    ``acknowledges`` is the load-bearing field: whether the platform GUARANTEES it will ACKNOWLEDGE each
    removal request. Without that guarantee the DCA could never know a revoked member was actually
    removed, so an unacknowledged host leaves the removal PENDING — never a silent success.
    ``supports_removal`` states whether the host offers a removal (audience-update "remove") path at all.
    ``supported_audiences`` is the affected scope (e.g. ``google_customer_match``). ``as_of`` is when the
    platform produced the answer (its freshness); staleness is measured from it. ``synthetic`` marks a
    non-production fixture. ``host_owns_audience`` is always ``True`` and states the ownership boundary at
    the type level: the platform owns the advertising-audience integration; the DCA only ever requests a
    removal. When ``availability`` is ``UNAVAILABLE`` the capability fields are not authoritative and the
    gate refuses regardless of them.
    """

    model_config = ConfigDict(frozen=True)

    business_id: str = Field(min_length=1)
    availability: RemovalHostAvailability
    version: str = Field(min_length=1)
    as_of: datetime
    supports_removal: bool = False
    acknowledges: bool = False
    supported_audiences: tuple[str, ...] = ()
    host_owns_audience: bool = True
    synthetic: bool = False

    @property
    def is_available(self) -> bool:
        return self.availability is RemovalHostAvailability.AVAILABLE

    @property
    def offers_removal(self) -> bool:
        """Does the host expose a removal path at all? Only meaningful on an available answer."""
        return self.is_available and self.supports_removal

    @property
    def acknowledges_removals(self) -> bool:
        """Does the host GUARANTEE it will acknowledge each removal? Only meaningful on an available
        answer; the gate checks availability/freshness/support first. ``False`` ⇒ the removal stays
        PENDING, because an unacknowledged removal can never be confirmed."""
        return self.offers_removal and self.acknowledges

    def supports_audience(self, audience: str) -> bool:
        target = _norm(audience)
        return any(_norm(a) == target for a in self.supported_audiences)

    def age(self, *, now: datetime) -> timedelta:
        return now - self.as_of


class AudienceMemberRef(BaseModel):
    """A pointer to ONE previously-uploaded audience member to remove — provenance only, no raw PII.

    The member is named ONLY by its one-way ``member_digest`` — the SHA-256 hashed identifier that was
    uploaded (exactly as DCA-091 hashes it), never the raw email/phone. ``key_type`` is which identifier
    it is keyed on (``email`` / ``phone``). ``source_membership_id`` optionally pins the host-side
    membership record. There is no content and no raw identifier: the DCA names a member to remove, it
    never carries the person's address.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    member_digest: str = Field(min_length=1, max_length=128)
    key_type: str = Field(min_length=1)
    source_membership_id: str = ""


class RemovalRequest(BaseModel):
    """A request to REMOVE revoked members from a previously-uploaded advertising audience THROUGH
    EffortlessPromo, because their permission was withdrawn.

    ``removal_key`` is the consume-once idempotency key for this removal operation itself, so a retried
    request is the SAME logical removal and returns the SAME acknowledgment once it completes.
    ``withdrawal_reason`` is WHY the permission is no longer valid (consent withdrawn, newly suppressed,
    source right expired) — audit provenance, never itself a decision. ``audience_channel`` is the
    advertising audience the members live in (e.g. ``google_customer_match``). ``audience_name`` is the
    named audience for provenance. ``members`` are the hashed-member pointers to remove. ``correlation_id``
    is carried through so the (future) HTTP call and its logs share one id. There is no payload/body: the
    DCA names members to remove; it never composes content and can never add a member.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    business_id: str = Field(min_length=1)
    removal_key: str = Field(min_length=1, max_length=128)
    withdrawal_reason: str = Field(min_length=1)
    audience_channel: str = Field(min_length=1)
    members: tuple[AudienceMemberRef, ...] = Field(min_length=1)
    audience_name: str = ""
    correlation_id: str | None = None


class RemovalItemOutcome(BaseModel):
    """What happened to ONE member in a removal request.

    ``status`` is one of the :class:`RemovalItemStatus` values. ``removed`` is ``True`` only for a member
    that was present and was actually removed. ``pending`` is ``True`` when the removal could not be
    confirmed and stays owed (visible, must retry) — a pending item is never counted as removed.
    ``note`` is a human-readable explanation. Only the ``member_digest`` names the member — never a raw
    identifier.
    """

    model_config = ConfigDict(frozen=True)

    member_digest: str = Field(min_length=1)
    status: RemovalItemStatus
    removed: bool
    pending: bool
    note: str = ""


class RemovalReceipt(BaseModel):
    """What the EffortlessPromo removal contract returns for a request. Never a send/upload confirmation.

    ``acknowledged`` is whether the host ACKNOWLEDGED the removal request (``False`` when the host is
    unavailable/stale/non-acknowledging). ``status`` is one of :data:`COMPLETED` / :data:`PENDING`:
    ``COMPLETED`` only when the host acknowledged AND every member is ``REMOVED`` or ``NOT_PRESENT`` with
    nothing left owed; ``PENDING`` whenever any member could not be confirmed removed (host disabled, or a
    per-member transient failure) — those stay visible and retryable. ``reason`` is a stable code (a
    :class:`RemovalDecision` reason on a host disable, else ``ok``). ``outcomes`` is the per-member result
    (always populated — a pending request still lists its owed members, so a failure is never silently
    dropped). ``acknowledgment_reference`` is an opaque handle the platform returns for a COMPLETED
    request (empty otherwise); it is deterministic in the removal key so a replay returns the same handle.
    ``removal_key`` / ``audience_channel`` echo the request.
    """

    model_config = ConfigDict(frozen=True)

    business_id: str = Field(min_length=1)
    removal_key: str = Field(min_length=1)
    audience_channel: str = Field(min_length=1)
    status: str
    reason: str
    acknowledged: bool = False
    outcomes: tuple[RemovalItemOutcome, ...] = ()
    acknowledgment_reference: str = ""
    synthetic: bool = False

    # stable status strings the caller reads uniformly
    COMPLETED: ClassVar[str] = "completed"
    PENDING: ClassVar[str] = "pending"

    @property
    def removed_count(self) -> int:
        return sum(1 for o in self.outcomes if o.status is RemovalItemStatus.REMOVED)

    @property
    def not_present_count(self) -> int:
        return sum(1 for o in self.outcomes if o.status is RemovalItemStatus.NOT_PRESENT)

    @property
    def pending_count(self) -> int:
        return sum(1 for o in self.outcomes if o.status is RemovalItemStatus.PENDING)

    @property
    def has_pending(self) -> bool:
        """Whether any member's removal is still owed (visible and must be retried)."""
        return self.pending_count > 0

    @property
    def withdrawal_upheld(self) -> bool:
        """The prior upload NEVER overrides the current withdrawal. A member whose removal is still
        pending is treated as withdrawn, not reinstated — so this is always ``True``, even on a
        PENDING/disabled receipt. The stale fact that the member was once uploaded cannot flip the
        current withdrawal back to membership."""
        return True

    @property
    def authorizes_send(self) -> bool:
        """A removal receipt never authorizes a send or an upload — it only reports what was removed (or
        still owed). Kept explicit so no caller mistakes it for a send/upload decision."""
        return False


@runtime_checkable
class RemovalPort(Protocol):
    """Port the DCA uses to REQUEST removal of a revoked member from an advertising audience.

    Synchronous, matching the DCA-096 delivery and DCA-097 cancellation ports and the other
    service-to-service clients. There is exactly one method — ``request_removal`` — and deliberately no
    ``upload`` / ``add`` / ``sync`` / provider / audience-I/O method: the DCA can only ever REQUEST a
    removal; the platform owns the advertising-audience integration and performs (or defers) it. The only
    implementation today is the dev/test fake; the live HTTP adapter is a later story (blocked on
    DCA-001 B1) and until it ships the contract stays disabled in production.
    """

    def request_removal(self, request: RemovalRequest) -> RemovalReceipt: ...


@dataclass(frozen=True)
class RemovalDecision:
    """Whether the host's capabilities permit REQUESTING a removal. ``reason`` is a stable code.

    An allow is permission only to hand the removal request to the platform — it does not itself remove
    anything (the host does), and it is never recipient consent or a send/upload authorization
    (``authorizes_send`` is always ``False``). A deny does NOT mean "do nothing": the removal is still
    owed and the request stays PENDING (visible, retryable) — the withdrawal is never abandoned.
    """

    allowed: bool
    reason: str

    # stable reason codes
    OK = "ok"
    HOST_UNAVAILABLE = "removal_host_unavailable"
    STALE = "removal_capabilities_stale"
    REMOVAL_NOT_SUPPORTED = "removal_not_supported"
    ACKNOWLEDGMENT_NOT_GUARANTEED = "removal_acknowledgment_unavailable"
    AUDIENCE_NOT_SUPPORTED = "removal_audience_not_supported"

    @property
    def authorizes_send(self) -> bool:
        return False


def authorize_removal(
    capabilities: RemovalCapabilities | None,
    audience_channel: str,
    *,
    now: datetime | None = None,
    max_age: timedelta = DEFAULT_MAX_CAPABILITIES_AGE,
) -> RemovalDecision:
    """Decide whether the EffortlessPromo removal contract permits requesting a removal right now.

    Fail-closed at each step: absent/unavailable → stale → removal-not-supported → acknowledgment-not-
    guaranteed → audience-not-supported → allowed. The decision is a pure function of the capabilities,
    the audience, and the moment — it reads nothing and changes nothing, so the allow and every denial are
    perfectly reproducible.

    A deny never means the withdrawal is dropped: the caller keeps the removal PENDING and retries. The
    acknowledgment step is the load-bearing one — a host that does not guarantee it will acknowledge a
    removal leaves the removal unconfirmable, so it must stay pending rather than be treated as done.
    """
    moment = now or datetime.now(UTC)

    # Absent or unavailable: no authoritative view of whether a removal would be acknowledged.
    if capabilities is None or not capabilities.is_available:
        return RemovalDecision(False, RemovalDecision.HOST_UNAVAILABLE)

    # Stale: fetched too long ago. Removal rides fast-moving consent/suppression truth, so an old answer
    # can no longer be trusted — refuse (the removal stays pending).
    if capabilities.age(now=moment) > max_age:
        return RemovalDecision(False, RemovalDecision.STALE)

    # The host must actually offer a removal (audience-update "remove") path.
    if not capabilities.offers_removal:
        return RemovalDecision(False, RemovalDecision.REMOVAL_NOT_SUPPORTED)

    # The host must GUARANTEE acknowledgment — an unacknowledged removal can never be confirmed, so the
    # DCA would not know the revoked member was removed. Keep it pending rather than bypass.
    if not capabilities.acknowledges_removals:
        return RemovalDecision(False, RemovalDecision.ACKNOWLEDGMENT_NOT_GUARANTEED)

    # The host's removal contract must cover the advertising audience in question.
    if not capabilities.supports_audience(audience_channel):
        return RemovalDecision(False, RemovalDecision.AUDIENCE_NOT_SUPPORTED)

    # Cleared to REQUEST removal. Not a removal by itself, not consent, not a send/upload.
    return RemovalDecision(True, RemovalDecision.OK)
