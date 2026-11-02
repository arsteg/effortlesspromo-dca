"""DCA-153 — record ONE audience-acceptance INTENT over a DCA-145 prospect suggestion.

The audience stage's commit-of-intent step after the DCA-152 preview: a business user has reviewed one
of its own :class:`~app.db.models.ProspectSuggestion` rows and records their INTENT to accept it into
their audience. Recording the intent is **inert**: it persists the decision as a ``pending_sync``
:class:`~app.db.models.AcceptanceIntent`, its atomic DCA-026 audit entry AND a durable DCA-027 outbox
*sync request* — all staged into the *caller's* session so they commit as one unit of work — but it
performs **NO** EffortlessPromo CRM sync / send, confers **NO** recipient consent (**acceptance is NOT
consent**), and creates **NO** audience membership. The actual sync is a later, still-DISABLED story;
billable acceptance / usage emission is a later story too (DCA-157/158) and is NOT done here.

The two load-bearing invariants (the DCA-153 acceptance criteria):

- **Repeated submit creates ONE intent; a transaction failure commits neither the intent NOR its
  audit/outbox.** ``(business_id, suggestion_id)`` is unique, so a subject is accepted ONCE per
  business — re-accepting returns the existing row and stages no duplicate, no second audit and no
  second outbox intent. Like the sibling DCA-145/151 services, the recorder stages into the caller's
  session and never commits, so the route/worker owns the unit of work and a rolled-back transaction
  leaves none of the three rows (the atomic-audit/outbox rule, SEC-001/BR-122).

- **A research-only suggestion never becomes sendable; no audience membership/send is created.** The
  accepted suggestion's ``visibility`` is CAPTURED unchanged; a research-only organization stays
  research-only. The recorded ``provenance`` pins the invariants (``sendable`` / ``synced`` / ``sent`` /
  ``spent`` / ``consentConferred`` / ``membershipCreated`` = ``False``). The ``scored_icp_version`` and
  ``suggestion_schema_version`` are the CAPTURED selection versions, so the intent records exactly what
  was accepted even if the suggestion is later rescored.

Every guard is checked fail-closed, in order, BEFORE the session is mutated, so a refusal changes
nothing: authority FIRST; then the suggestion must exist in THIS business (an unknown / cross-tenant id
reads as not-found); then it must be ACCEPTABLE (a reviewable, unexpired ``suggested`` candidate — a
dismissed / expired / otherwise non-reviewable suggestion is refused). No fetch, model call, spend, send
or auto-sync happens.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.sink import ActorType, AuditActor, AuditSink
from app.db.models import (
    AcceptanceIntent,
    AcceptanceIntentStatus,
    ProspectSuggestion,
    SuggestionVisibility,
)
from app.identity.permissions import Scope, scopes_for_business_roles
from app.outbox.sink import OutboxSink
from app.services.prospect_suggestion import DISMISSED_STATUS, VISIBLE_STATUS

# The version of the acceptance-intent contract rows are written under (expand→migrate→contract).
ACCEPTANCE_SCHEMA_VERSION = 1

# The audited action code for recording an acceptance intent (a controlled code).
AUDIT_ACTION_ACCEPT = "prospect.suggestion.accepted"

# The DCA-027 outbox event type for the durable sync REQUEST staged with an acceptance intent. It is a
# controlled code naming the logical intent; the actual relay/sync stays a disabled future capability.
OUTBOX_EVENT_ACCEPTANCE = "audience.acceptance.pending_sync"

# Where an acceptance intent records its origin on the provenance.
ACCEPTANCE_SOURCE = "acceptance_intent"

# Stable refusal codes raised (never recorded) for a malformed / ineligible request — each BEFORE any
# state is mutated, so the attempt changes nothing.
REASON_NOT_AUTHORIZED = "acceptance_intent_not_authorized"
REASON_SUGGESTION_NOT_FOUND = "prospect_suggestion_not_found"
REASON_SUGGESTION_NOT_ACCEPTABLE = "prospect_suggestion_not_acceptable"


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalize a possibly-naive timestamp to UTC (SQLite returns naive datetimes)."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class AcceptanceIntentError(Exception):
    """An acceptance was refused with a stable ``code`` for a malformed / ineligible request (an
    unknown or cross-tenant suggestion, or a suggestion that is not an acceptable reviewable candidate).
    Raised before the session is mutated, so a refusal changes nothing. ``detail`` names the offending
    value."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class AcceptanceIntentAuthorizationError(AcceptanceIntentError):
    """The caller is not authorized to record an acceptance intent.

    A distinct type so an HTTP boundary can map it to a 403 while the other codes map to a 404/409.
    Raised FIRST — before any state is read or written. ``code`` is :data:`REASON_NOT_AUTHORIZED`.
    """

    def __init__(self, message: str) -> None:
        super().__init__(REASON_NOT_AUTHORIZED, message)


@dataclass(frozen=True)
class AcceptanceIntentActor:
    """The business principal recording an acceptance intent.

    ``subject`` is an opaque actor id (never a name/email) recorded on the intent + its atomic audit
    entry. ``roles`` are the caller's verified business-plane membership roles (from the tenant context,
    NEVER a bare token claim). :meth:`may_accept` grants the action only when those roles yield
    :attr:`~app.identity.permissions.Scope.CANDIDATE_ACCEPT` — an acting user and an administrator hold
    it; a read-only viewer (who may only VIEW candidates) and a platform operator (holding no business
    scope) cannot perform it.
    """

    subject: str
    roles: tuple[str, ...] = ()
    actor_type: str = "user"

    def may_accept(self) -> bool:
        return Scope.CANDIDATE_ACCEPT in scopes_for_business_roles(self.roles)

    def as_audit_actor(self) -> AuditActor:
        return AuditActor(id=self.subject, type=ActorType(self.actor_type))


@dataclass
class AcceptanceIntentResult:
    """The inert, inspectable outcome of one accept attempt.

    ``intent`` is the staged :class:`~app.db.models.AcceptanceIntent`. ``already_accepted`` is ``True``
    when this was an idempotent no-op (the suggestion had already been accepted for this business). The
    remaining fields echo the intent's load-bearing attributes — the captured ``visibility`` constraint,
    whether it is ``sendable`` (always ``False`` here), the captured selection versions and the stable
    ``outbox_event_id`` of the staged sync request. Nothing here is ever synced, sent, spent or consented.
    """

    business_id: str
    intent: AcceptanceIntent
    visibility: str = SuggestionVisibility.RESEARCH_ONLY.value
    scored_icp_version: int | None = None
    suggestion_schema_version: int = 1
    outbox_event_id: str = ""
    already_accepted: bool = False

    @property
    def sendable(self) -> bool:
        """Whether the accepted suggestion is a sendable recipient — always ``False`` for research-only."""
        return self.visibility == SuggestionVisibility.SENDABLE.value


class AcceptanceIntentRecorder:
    """Record ONE audience-acceptance intent over a prospect suggestion, for ONE business.

    Construct with the active session and the verified business scope (a route/worker resolves it from
    the membership-verified tenant context). Every query is confined to this business — there is no
    argument to widen the scope — so an intent can never reach another tenant's suggestion or any
    shared/global master, and an unknown / cross-tenant id reads as not-found. The recorder stages into
    the caller's session and never commits; it records an intent only and syncs/sends/spends nothing.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def _suggestion(self, suggestion_id: str) -> ProspectSuggestion | None:
        """Fetch one suggestion by id WITHIN this business, or ``None`` — an unknown or cross-tenant id
        reads exactly like not-found (an acceptance can never target another tenant's row)."""
        if not suggestion_id:
            return None
        stmt = (
            select(ProspectSuggestion)
            .where(ProspectSuggestion.business_id == self._business_id)
            .where(ProspectSuggestion.id == suggestion_id)
        )
        return self._session.scalars(stmt).one_or_none()

    def intent_for(self, suggestion_id: str) -> AcceptanceIntent | None:
        """The acceptance intent for one suggestion within this business, or ``None`` (the idempotency
        read; a cross-tenant / unknown id reads as not-found)."""
        if not suggestion_id:
            return None
        stmt = (
            select(AcceptanceIntent)
            .where(AcceptanceIntent.business_id == self._business_id)
            .where(AcceptanceIntent.suggestion_id == suggestion_id)
        )
        return self._session.scalars(stmt).one_or_none()

    # -- the one accept path ------------------------------------------------------------------------

    def accept(
        self,
        suggestion_id: str,
        actor: AcceptanceIntentActor,
        *,
        now: datetime | None = None,
    ) -> AcceptanceIntentResult:
        """Record the acceptance intent for ``suggestion_id`` for this business.

        Order (each gate leaves prior state unchanged when it refuses):

        1. **Authority FIRST.** A caller lacking :attr:`~app.identity.permissions.Scope.CANDIDATE_ACCEPT`
           is refused (:class:`AcceptanceIntentAuthorizationError`) before any state is read.
        2. **The suggestion exists in THIS business.** Otherwise refused
           (:data:`REASON_SUGGESTION_NOT_FOUND`) — an unknown or cross-tenant id reads as not-found.
        3. **The suggestion is ACCEPTABLE.** It must be a reviewable, unexpired ``suggested`` candidate;
           a dismissed / expired / otherwise non-reviewable suggestion is refused
           (:data:`REASON_SUGGESTION_NOT_ACCEPTABLE`) — acting on a stale/invalid candidate is denied.
        4. **Idempotent replay.** An existing intent for this suggestion is returned unchanged (no
           duplicate, no new audit, no new outbox) — a suggestion is accepted once per business.
        5. **Record.** The ``pending_sync`` intent is staged with the CAPTURED visibility + selection
           versions, together with its atomic DCA-026 audit entry and a durable DCA-027 outbox sync
           REQUEST — all in the caller's unit of work, so they commit (or roll back) together.

        Recording reads an already-permitted suggestion and performs NO fetch, model call, spend, send,
        CRM sync or recipient consent. A research-only suggestion stays research-only and joins no
        audience; future capabilities (the actual sync, billing) stay disabled.
        """
        moment = now or datetime.now(UTC)

        # 1) Authority FIRST — an unauthorized caller is refused with no state touched.
        if not actor.may_accept():
            raise AcceptanceIntentAuthorizationError(
                "recording an acceptance intent requires a business user holding "
                f"'{Scope.CANDIDATE_ACCEPT.value}'; a read-only viewer or a platform operator "
                "(holding no business scope) cannot perform it"
            )

        # 2) The suggestion must exist WITHIN this business (an unknown / cross-tenant id is not-found).
        suggestion = self._suggestion(suggestion_id)
        if suggestion is None:
            raise AcceptanceIntentError(
                REASON_SUGGESTION_NOT_FOUND,
                "no such prospect suggestion in this business (an unknown or cross-tenant id is "
                "not-found)",
                detail=suggestion_id,
            )

        # 3) The suggestion must be an ACCEPTABLE reviewable candidate — a reviewable 'suggested' status,
        #    not dismissed and not expired. (An already-accepted intent for it is handled in step 4.)
        expires_at = _as_utc(suggestion.expires_at)
        fresh = expires_at is None or expires_at > moment
        existing = self.intent_for(suggestion_id)
        if existing is None and (suggestion.status != VISIBLE_STATUS or not fresh):
            why = (
                "dismissed"
                if suggestion.status == DISMISSED_STATUS
                else ("expired" if suggestion.status == VISIBLE_STATUS else "not_reviewable")
            )
            raise AcceptanceIntentError(
                REASON_SUGGESTION_NOT_ACCEPTABLE,
                f"prospect suggestion '{suggestion_id}' is not an acceptable candidate ({why}); it "
                "must be a reviewable, unexpired 'suggested' candidate to accept",
                detail=why,
            )

        # 4) Idempotent replay — a suggestion is accepted ONCE per business; return the existing row.
        if existing is not None:
            return AcceptanceIntentResult(
                business_id=self._business_id,
                intent=existing,
                visibility=existing.visibility,
                scored_icp_version=existing.scored_icp_version,
                suggestion_schema_version=existing.suggestion_schema_version,
                outbox_event_id=existing.outbox_event_id,
                already_accepted=True,
            )

        # 5) Record the pending_sync intent + its atomic audit + its durable outbox sync request.
        intent, outbox_event_id = self._stage_intent(suggestion, actor, moment)
        return AcceptanceIntentResult(
            business_id=self._business_id,
            intent=intent,
            visibility=suggestion.visibility,
            scored_icp_version=suggestion.scored_icp_version,
            suggestion_schema_version=suggestion.schema_version,
            outbox_event_id=outbox_event_id,
        )

    # -- internals ----------------------------------------------------------------------------------

    def _stage_intent(
        self,
        suggestion: ProspectSuggestion,
        actor: AcceptanceIntentActor,
        moment: datetime,
    ) -> tuple[AcceptanceIntent, str]:
        """Stage the intent row + its atomic DCA-026 audit entry + its DCA-027 outbox sync request in
        the caller's unit of work. Returns the (uncommitted) intent and the staged outbox event id."""
        # The durable sync REQUEST — a PENDING outbox intent (never delivered here). Its stable event id
        # is derived from (business, type, suggestion_id), so re-staging the same logical intent is
        # constrained by the database rather than silently doubled. Payload is opaque references only.
        outbox = OutboxSink(self._session, business_id=self._business_id).enqueue(
            event_type=OUTBOX_EVENT_ACCEPTANCE,
            dedup_key=suggestion.id,
            payload={
                "suggestionId": suggestion.id,
                "runId": suggestion.run_id,
                "subjectId": suggestion.subject_id,
                "subjectType": suggestion.subject_type,
                "visibility": suggestion.visibility,
                "scoredIcpVersion": suggestion.scored_icp_version,
                "intentStatus": AcceptanceIntentStatus.PENDING_SYNC.value,
                # The invariants travel on the sync request too: a later (disabled) sync must honour that
                # acceptance conferred no consent and no sendability.
                "sendable": False,
                "consentConferred": False,
            },
        )

        provenance = {
            "source": ACCEPTANCE_SOURCE,
            "suggestionId": suggestion.id,
            "runId": suggestion.run_id,
            "subjectType": suggestion.subject_type,
            "scoredIcpVersion": suggestion.scored_icp_version,
            "suggestionSchemaVersion": suggestion.schema_version,
            "outboxEventId": outbox.event_id,
            # Load-bearing invariants: an acceptance intent records a decision + a pending sync request
            # ONLY. It is never auto-synced/sent/charged, confers no consent, and creates no membership.
            "accepted": True,
            "sendable": False,
            "synced": False,
            "sent": False,
            "spent": False,
            "consentConferred": False,
            "membershipCreated": False,
        }
        intent = AcceptanceIntent(
            business_id=self._business_id,
            suggestion_id=suggestion.id,
            run_id=suggestion.run_id,
            subject_type=suggestion.subject_type,
            subject_id=suggestion.subject_id,
            visibility=suggestion.visibility,
            status=AcceptanceIntentStatus.PENDING_SYNC.value,
            scored_icp_version=suggestion.scored_icp_version,
            suggestion_schema_version=suggestion.schema_version,
            match_score=suggestion.match_score,
            schema_version=ACCEPTANCE_SCHEMA_VERSION,
            outbox_event_id=outbox.event_id,
            accepted_by=actor.subject,
            accepted_by_type=actor.actor_type,
            provenance=provenance,
            created_at=moment,
        )
        self._session.add(intent)
        # Flush so the intent's generated id is populated BEFORE it is referenced on the atomic audit
        # entry (and in the route's response). A flush is NOT a commit — it stays inside the caller's
        # unit of work, so a later rollback still discards the intent, its audit and its outbox request.
        self._session.flush()

        # Atomic audit (SEC-001/BR-122): staged into the caller's session so it commits with the row +
        # the outbox request. References stay OPAQUE — the suggestion/run/subject ids, visibility,
        # captured versions and the outbox event id — never a contact value. Recording an intent accepts,
        # it never syncs / sends / charges / consents.
        AuditSink(
            self._session, business_id=self._business_id, actor=actor.as_audit_actor()
        ).record(
            action=AUDIT_ACTION_ACCEPT,
            reason=(
                f"recorded pending_sync acceptance intent for suggestion '{suggestion.id}' "
                "(no CRM sync, no send, no consent)"
            ),
            references={
                "acceptanceIntentId": intent.id,
                "prospectSuggestionId": suggestion.id,
                "runId": suggestion.run_id,
                "subjectId": suggestion.subject_id,
                "subjectType": suggestion.subject_type,
                "visibility": suggestion.visibility,
                "sendable": False,
                "consentConferred": False,
                "membershipCreated": False,
                "scoredIcpVersion": suggestion.scored_icp_version,
                "suggestionSchemaVersion": suggestion.schema_version,
                "outboxEventId": outbox.event_id,
                "intentStatus": AcceptanceIntentStatus.PENDING_SYNC.value,
            },
        )
        return intent, outbox.event_id


__all__ = [
    "ACCEPTANCE_SCHEMA_VERSION",
    "AUDIT_ACTION_ACCEPT",
    "OUTBOX_EVENT_ACCEPTANCE",
    "ACCEPTANCE_SOURCE",
    "REASON_NOT_AUTHORIZED",
    "REASON_SUGGESTION_NOT_FOUND",
    "REASON_SUGGESTION_NOT_ACCEPTABLE",
    "AcceptanceIntentError",
    "AcceptanceIntentAuthorizationError",
    "AcceptanceIntentActor",
    "AcceptanceIntentResult",
    "AcceptanceIntentRecorder",
]
