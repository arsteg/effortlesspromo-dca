"""DCA-080 — apply platform suppression events into the scoped local projection.

Key: ``suppression-sync`` · source sections 5, 10, 11.

The DCA-079 contract is the *pull* side of host-owned suppression — the DCA asks the platform "is this
contact point suppressed right now?". This story is the *push* side: the host platform emits
authoritative suppression events (an unsubscribe recorded, a bounce, a complaint, or a clear) and the
DCA consumes them into a **tenant-scoped local projection** so its view can be kept current between
lookups. Suppression and recipient consent stay the host's to own; this only mirrors the host's
authoritative decisions, it never originates one.

A suppression event is consumed only when it is **verified**. Verification is the two gates already
built, run upstream before an event ever reaches this consumer:

- **transport authentication** (DCA-030 ``verify_webhook``) — the event provably came from the
  platform and was not replayed/tampered; and
- **envelope validation** (DCA-029 ``parse_envelope``) — a supported, well-formed, minimized,
  opaque-only :class:`~app.integration.envelope.IntegrationEventEnvelope`.

:class:`PlatformSuppressionProjector` is the single sanctioned consumer. It applies a verified event
under **two guards**, and like the DCA-026/027/028/031 writers it stages everything into the
*caller's* session and performs no I/O (the caller owns the unit of work):

- **Revision guard (DCA-031).** Each suppressed ``(contact_ref, channel)`` is one aggregate with its
  own monotonic ``aggregate_version``. A delayed/duplicate event whose version is ``<=`` the applied
  version is rejected **STALE** and mutates nothing, so *a stale "clear" can never clear a newer
  "block"* (nor a stale "block" resurrect over a newer "clear"). An out-of-order successor is buffered
  as a visible GAP, never applied over the gap.
- **Inbox guard (DCA-028).** The event's ``event_id`` is recorded as a processed
  :class:`~app.db.models.InboxReceipt`, so an exact re-delivery is de-duplicated (the local effect is
  applied exactly once) and a failed unit of work rolls the receipt back for a safe retry.

**Authority is checked first, and fail-closed — a tenant event cannot widen its own authority.** The
projection is scoped to the verified :class:`~app.identity.tenant_context.TenantContext`; the scope is
taken from that context, never from the event. An event is refused (DENIED) with no state change when:

- its ``business_id`` is not the consuming tenant's scope — a tenant can never write another tenant's
  suppression projection (``scope_mismatch``); or
- it does not assert **platform** authority (``authority_plane != "platform"``) — suppression is
  host-owned, so an event carrying tenant-plane authority is a tenant attempting to assert an
  authority it does not hold and is rejected (``authority_not_platform``); or
- it is not a recognized suppression event / is missing the ``(contact_ref, channel, state)`` it must
  carry (``unrecognized_event_type`` / ``malformed_suppression_payload``).

Every denial is pure and reproducible: authority and shape are validated BEFORE the session is touched,
so a refused event stages no row. The payload is opaque only (a ``contact_ref`` is a host-supplied
hashed/tokenized handle, never a raw contact point) — enforced by the envelope upstream and re-screened
by the revision/inbox writers. Applying a suppression decision authorizes nothing and sends nothing;
acting on suppression stays a disabled future capability behind the one send pipeline, and this consumer
is wired into no live route yet (the inbound transport remains disabled).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from sqlalchemy.orm import Session

from app.db.models import RestrictionAuthorityPlane
from app.identity.tenant_context import TenantContext
from app.inbox.sink import InboxSink
from app.integration.envelope import IntegrationEventEnvelope
from app.projection.revision import RevisionDecision, RevisionProjector

# The controlled event type a host suppression event carries. Only this is consumed; any other event
# type is refused so this consumer stays narrow.
SUPPRESSION_EVENT_TYPE = "platform.suppression.updated"

# The aggregate type recorded in the DCA-031 projection for a suppressed contact point on a channel.
SUPPRESSION_AGGREGATE_TYPE = "platform-suppression"

# WHERE a suppression event came from — the inbox natural-key source half. Combined with the envelope's
# ``event_id`` it de-duplicates an exact re-delivery (the DCA-028 inbox guard).
SUPPRESSION_EVENT_SOURCE = "effortlesspromo-platform-suppression"

# The opaque separator joining ``contact_ref`` and ``channel`` into one aggregate id (the ASCII unit
# separator — never present in a ref/channel, so the two halves can't collide).
_AGG_SEP = "\x1f"


class SuppressionState(str, Enum):
    """The suppression decision a host event projects for a ``(contact_ref, channel)``.

    ``BLOCKED`` — the contact point is suppressed on the channel (unsubscribe / bounce / complaint /
    do-not-contact). ``CLEARED`` — a prior suppression no longer applies per the host. Neither is
    recipient consent; a clear only removes the suppression blocker, exactly like the DCA-079 gate.
    """

    BLOCKED = "blocked"
    CLEARED = "cleared"


class SuppressionSyncDecision(str, Enum):
    """The outcome of consuming one verified suppression event."""

    APPLIED = "applied"  # the event advanced the projection (revision guard: next contiguous)
    STALE = "stale"  # an older/duplicate revision — rejected, projection unchanged
    GAP = "gap"  # ahead of a missing predecessor — buffered, not applied over the gap
    DUPLICATE = "duplicate"  # an exact event_id re-delivery — inbox guard, no-op
    DENIED = "denied"  # authority/shape refused BEFORE any state change


# Stable reason codes (for callers / audit); never echo a payload value.
REASON_OK = "ok"
REASON_STALE = "stale_revision_newer_projection_preserved"
REASON_GAP = "revision_gap_buffered"
REASON_DUPLICATE = "duplicate_event_already_processed"
REASON_SCOPE_MISMATCH = "scope_mismatch"
REASON_AUTHORITY_NOT_PLATFORM = "authority_not_platform"
REASON_UNRECOGNIZED_EVENT_TYPE = "unrecognized_event_type"
REASON_MALFORMED_PAYLOAD = "malformed_suppression_payload"


@dataclass(frozen=True)
class SuppressionSyncOutcome:
    """What consuming one event decided, with enough detail to reproduce and audit it (no raw data).

    ``state`` is the projected suppression decision AFTER a successful apply (``None`` on a denial or a
    no-op). ``applied_version`` is the projection's version after the call (unchanged on STALE/GAP/
    DENIED/DUPLICATE). ``contact_ref`` / ``channel`` echo the opaque handles the event addressed.
    """

    decision: SuppressionSyncDecision
    reason: str
    contact_ref: str = ""
    channel: str = ""
    state: SuppressionState | None = None
    incoming_version: int = 0
    applied_version: int = 0

    @property
    def applied(self) -> bool:
        return self.decision is SuppressionSyncDecision.APPLIED

    @property
    def denied(self) -> bool:
        return self.decision is SuppressionSyncDecision.DENIED

    @property
    def duplicate(self) -> bool:
        return self.decision is SuppressionSyncDecision.DUPLICATE

    @property
    def stale(self) -> bool:
        return self.decision is SuppressionSyncDecision.STALE

    @property
    def changed_state(self) -> bool:
        """Did this call mutate the projection? True only on APPLIED — a denial/stale/gap/dup never
        clears or sets a block, which is exactly the acceptance guarantee."""
        return self.decision is SuppressionSyncDecision.APPLIED


class PlatformSuppressionProjector:
    """Consume verified host suppression events into one tenant's local projection (single writer).

    Bind it to a verified :class:`~app.identity.tenant_context.TenantContext` via :meth:`for_context`
    (scope = the context's business). The scope is taken from the context, never from the event, so an
    event can never be applied to — or read from — another tenant's projection. The consumer stages
    into the caller's session and never commits/flushes/opens a transaction of its own; the caller
    commits the revision change and the inbox receipt together, so a replay or a failed apply is clean.
    """

    def __init__(self, session: Session, *, business_id: str | None = None) -> None:
        self._session = session
        self._business_id = business_id
        self._revision = RevisionProjector(session, business_id=business_id)
        self._inbox = InboxSink(session, business_id=business_id)

    @classmethod
    def for_context(cls, session: Session, context: TenantContext) -> PlatformSuppressionProjector:
        """Bind the consumer to a verified tenant context (scope = the context's business)."""
        return cls(session, business_id=context.business_id)

    def _scope(self) -> str:
        if not self._business_id:
            raise ValueError("suppression projection requires a business scope")
        return self._business_id

    # -- read helpers (tenant-scoped; make the local restriction inspectable) ----------------------

    def current(self, *, contact_ref: str, channel: str) -> SuppressionState | None:
        """Return the projected suppression state for ``(contact_ref, channel)`` in this tenant.

        ``BLOCKED`` / ``CLEARED`` per the highest contiguous revision applied, or ``None`` if the
        aggregate has never been seen (or is anchored at a gap with nothing applied). Read-only.
        """
        proj = self._revision.projection(
            aggregate_type=SUPPRESSION_AGGREGATE_TYPE,
            aggregate_id=self._aggregate_id(contact_ref, channel),
        )
        if proj is None or proj.applied_version == 0:
            return None
        raw = (proj.state or {}).get("state")
        try:
            return SuppressionState(raw)
        except ValueError:  # pragma: no cover - state is always written from the enum
            return None

    def is_blocked(self, *, contact_ref: str, channel: str) -> bool:
        """Is ``(contact_ref, channel)`` currently suppressed per the local projection?"""
        return self.current(contact_ref=contact_ref, channel=channel) is SuppressionState.BLOCKED

    # -- the one write path -------------------------------------------------------------------------

    def apply(self, envelope: IntegrationEventEnvelope) -> SuppressionSyncOutcome:
        """Apply one VERIFIED suppression event and return the decision (no commit; no I/O).

        ``envelope`` must already be transport-authenticated (DCA-030) and envelope-validated
        (DCA-029) — that is what "verified" means here. Authority and shape are checked FIRST and the
        event is refused (DENIED) before the session is touched if it targets another tenant, does not
        assert platform authority, or is not a well-formed suppression event. A recognized event is
        then de-duplicated by the DCA-028 inbox guard and applied by the DCA-031 revision guard, with
        both the revision change and the inbox receipt staged into the caller's unit of work.
        """
        scope = self._scope()

        # (1) Authority + shape — pure, fail-closed, BEFORE any state is touched. A tenant event can
        #     never widen its authority: wrong tenant, non-platform plane, or an unrecognized/
        #     malformed event is refused and stages nothing.
        guard = self._guard(envelope, scope)
        if guard is not None:
            return guard
        contact_ref, channel, state, reason = self._read_payload(envelope)

        # (2) Inbox guard — an exact re-delivery of this event is already processed: no-op. Scoped to
        #     the tenant, so another tenant's identical event id never masks this one.
        if (
            self._inbox.already_processed(
                event_source=SUPPRESSION_EVENT_SOURCE, event_id=envelope.event_id
            )
            is not None
        ):
            return SuppressionSyncOutcome(
                decision=SuppressionSyncDecision.DUPLICATE,
                reason=REASON_DUPLICATE,
                contact_ref=contact_ref,
                channel=channel,
                incoming_version=envelope.aggregate_version,
            )

        # (3) Revision guard — apply the event in version order. A stale version is rejected and the
        #     newer block/clear is preserved; a gap is buffered, never applied over.
        outcome = self._revision.apply(
            aggregate_type=SUPPRESSION_AGGREGATE_TYPE,
            aggregate_id=self._aggregate_id(contact_ref, channel),
            version=envelope.aggregate_version,
            state={
                "state": state.value,
                "contact_ref": contact_ref,
                "channel": channel,
                "reason": reason,
            },
            event_id=envelope.event_id,
            occurred_at=envelope.occurred_at,
        )

        # (4) Record the inbox receipt in the SAME unit of work, so a later exact re-delivery is
        #     de-duplicated regardless of how the revision guard decided this one.
        self._inbox.register(
            event_source=SUPPRESSION_EVENT_SOURCE,
            event_id=envelope.event_id,
            event_type=SUPPRESSION_EVENT_TYPE,
            payload={
                "aggregate_version": envelope.aggregate_version,
                "channel": channel,
                "decision": outcome.decision.value,
            },
        )

        return self._to_outcome(outcome, contact_ref, channel, state)

    # -- internals ----------------------------------------------------------------------------------

    @staticmethod
    def _aggregate_id(contact_ref: str, channel: str) -> str:
        return f"{contact_ref.strip()}{_AGG_SEP}{channel.strip().casefold()}"

    def _guard(
        self, envelope: IntegrationEventEnvelope, scope: str
    ) -> SuppressionSyncOutcome | None:
        """Refuse an event that would let a tenant widen its authority or that is not a suppression
        event. Returns a DENIED outcome, or ``None`` when the event is authorized and well-formed."""
        # Cross-tenant: the event's declared scope must be the tenant we are applying under. The scope
        # is the verified context's business — an event for another business is refused outright.
        if envelope.business_id != scope:
            return SuppressionSyncOutcome(
                decision=SuppressionSyncDecision.DENIED,
                reason=REASON_SCOPE_MISMATCH,
                channel="",
                incoming_version=envelope.aggregate_version,
            )

        # Not a suppression event — this consumer is deliberately narrow.
        if envelope.event_type != SUPPRESSION_EVENT_TYPE:
            return SuppressionSyncOutcome(
                decision=SuppressionSyncDecision.DENIED,
                reason=REASON_UNRECOGNIZED_EVENT_TYPE,
                incoming_version=envelope.aggregate_version,
            )

        # Authority plane: suppression is host-owned, so only an event asserting PLATFORM authority is
        # accepted. A tenant-plane event is a tenant attempting to assert an authority it does not hold
        # — refuse rather than mirror it into the authoritative projection.
        plane = str((envelope.payload or {}).get("authority_plane", "")).strip().casefold()
        if plane != RestrictionAuthorityPlane.PLATFORM.value:
            return SuppressionSyncOutcome(
                decision=SuppressionSyncDecision.DENIED,
                reason=REASON_AUTHORITY_NOT_PLATFORM,
                incoming_version=envelope.aggregate_version,
            )

        # Shape: a suppression event must address a contact_ref on a channel with a known state.
        payload = envelope.payload or {}
        contact_ref = str(payload.get("contact_ref", "")).strip()
        channel = str(payload.get("channel", "")).strip()
        raw_state = str(payload.get("state", "")).strip().casefold()
        if not contact_ref or not channel or raw_state not in {s.value for s in SuppressionState}:
            return SuppressionSyncOutcome(
                decision=SuppressionSyncDecision.DENIED,
                reason=REASON_MALFORMED_PAYLOAD,
                contact_ref=contact_ref,
                channel=channel,
                incoming_version=envelope.aggregate_version,
            )
        return None

    @staticmethod
    def _read_payload(
        envelope: IntegrationEventEnvelope,
    ) -> tuple[str, str, SuppressionState, str]:
        payload = envelope.payload or {}
        contact_ref = str(payload.get("contact_ref", "")).strip()
        channel = str(payload.get("channel", "")).strip()
        state = SuppressionState(str(payload.get("state", "")).strip().casefold())
        reason = str(payload.get("reason", "")).strip()
        return contact_ref, channel, state, reason

    @staticmethod
    def _to_outcome(
        outcome, contact_ref: str, channel: str, state: SuppressionState
    ) -> SuppressionSyncOutcome:
        mapping = {
            RevisionDecision.APPLIED: (SuppressionSyncDecision.APPLIED, REASON_OK),
            RevisionDecision.STALE: (SuppressionSyncDecision.STALE, REASON_STALE),
            RevisionDecision.GAP: (SuppressionSyncDecision.GAP, REASON_GAP),
        }
        decision, reason = mapping[outcome.decision]
        return SuppressionSyncOutcome(
            decision=decision,
            reason=reason,
            contact_ref=contact_ref,
            channel=channel,
            # Only an applied event changed the projected state; a stale/gap event leaves it untouched.
            state=state if decision is SuppressionSyncDecision.APPLIED else None,
            incoming_version=outcome.incoming_version,
            applied_version=outcome.applied_version,
        )
