"""DCA-157 — emit a BILLABLE acceptance ONLY AFTER the audience sync was ACKNOWLEDGED.

The audience stage's metering step after the DCA-155 sync / DCA-156 reconcile: a DCA-153
:class:`~app.db.models.AcceptanceIntent` has been driven to ``synced`` by an ACKNOWLEDGED platform
acknowledgment (its external record mapping persisted). This service records the durable, idempotent
fact that the accepted prospect is BILLABLE — a usage fact the platform will meter — WITHOUT ever
calling a live billing API and WITHOUT charging anything. It stages a durable DCA-027 outbox usage
record alongside the ledger entry (:class:`~app.db.models.BillableAcceptance`), so the later, still-
DISABLED delivery story (DCA-158) has something to relay; delivery / live billing stays disabled here.

The load-bearing invariant (the DCA-157 acceptance criterion): **a prospect is billable EXACTLY once,
ONLY on acknowledgment — never on a pending / uncertain / failed sync, and never double-charged on a
replay / reconcile.**

- **Only an ACKNOWLEDGED sync is billable.** The billable event is emitted ONLY when the intent is
  ``synced`` with ``sync_status='acknowledged'`` and the platform's external mapping present. A
  ``pending_sync`` intent, a ``pending`` (lost-acknowledgment) hand-off, an in-doubt ``needs_review``
  reconcile outcome or any other unacknowledged / failed state is REFUSED with no entry emitted — an
  excluded / failed / pending record is never charged.

- **Billable once; never double-charged.** ``(business_id, acceptance_intent_id)`` is unique and the
  ``billing_key`` is the STABLE logical billing key (deterministic in business + intent), so a duplicate
  acknowledgment — a replay, or a DCA-156 reconcile that re-resolved the SAME intent under the SAME
  request key — short-circuits on the existing entry: no second ledger row, no second usage record, no
  second audit. Exactly one billable event per accepted prospect.

A billable fact is NOT a send / spend / consent: emitting it confers NO recipient consent (acceptance
is NOT consent), performs NO send and makes NO live billing call (DCA-158 delivery stays disabled).

Every guard is checked fail-closed, in order, BEFORE any state is mutated, so a refusal (unauthorized /
not-found / not-acknowledged) changes nothing. The service stages into the *caller's* session and never
commits — the route/worker owns the unit of work, so the ledger entry, its DCA-027 usage record and its
atomic DCA-026 audit entry commit (or roll back) together. There is no argument to widen the business
scope: an unknown / cross-tenant intent reads as not-found.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audience import AudienceSyncStatus
from app.audit.sink import AuditSink
from app.db.models import (
    AcceptanceIntent,
    AcceptanceIntentStatus,
    BillableAcceptance,
    BillableAcceptanceStatus,
    billable_acceptance_key,
)
from app.identity.permissions import Scope
from app.outbox.sink import OutboxSink
from app.services.acceptance_sync import AcceptanceSyncActor

# The billable-acceptance ledger contract version (expand→migrate→contract).
BILLABLE_ACCEPTANCE_SCHEMA_VERSION = 1

# The audited action code for emitting a billable acceptance (a controlled code, distinct from the
# DCA-155 sync / DCA-156 reconcile codes so billing is a separate audited dimension).
AUDIT_ACTION_BILLABLE = "prospect.suggestion.billable_accepted"

# The DCA-027 outbox event type for the durable billing-usage record staged with a billable acceptance.
# A controlled code naming the logical usage intent; the actual delivery to platform billing stays a
# disabled future capability (DCA-158).
OUTBOX_EVENT_BILLABLE = "billing.usage.acceptance"

# Where a billable acceptance records its origin on the provenance / audit references.
BILLABLE_ACCEPTANCE_SOURCE = "billable_acceptance"

# Stable refusal codes raised (never recorded) for a malformed / ineligible request — each BEFORE any
# state is mutated, so the attempt changes nothing. The not-found code matches DCA-155/156's so an
# unknown / cross-tenant id reads identically whether synced, reconciled or metered.
REASON_NOT_AUTHORIZED = "billable_acceptance_not_authorized"
REASON_INTENT_NOT_FOUND = "acceptance_intent_not_found"
REASON_NOT_ACKNOWLEDGED = "acceptance_sync_not_acknowledged"


class BillableAcceptanceError(Exception):
    """A billable-acceptance emission was refused with a stable ``code`` for a malformed / ineligible
    request (an unknown or cross-tenant intent, or an intent whose sync is not yet acknowledged). Raised
    before the session is mutated, so a refusal changes nothing. ``detail`` names the offending value."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class BillableAcceptanceAuthorizationError(BillableAcceptanceError):
    """The caller is not authorized to emit a billable acceptance.

    A distinct type so an HTTP boundary can map it to a 403 while the other codes map to a 404/409.
    Raised FIRST — before any state is read or written. ``code`` is :data:`REASON_NOT_AUTHORIZED`.
    """

    def __init__(self, message: str) -> None:
        super().__init__(REASON_NOT_AUTHORIZED, message)


@dataclass
class BillableAcceptanceResult:
    """The inspectable outcome of one billable-acceptance emission attempt.

    ``emitted`` is ``True`` when this call recorded a NEW billable ledger entry (the acknowledged, not-
    yet-metered case); ``already_emitted`` when the accepted prospect was already billable and this was
    an idempotent no-op (no second entry / usage record / audit). ``entry`` is the (uncommitted) ledger
    row — the existing one on a replay. ``billing_key`` is the stable logical billing key. Nothing here
    is ever sent, spent, charged or consented — ``confers_consent`` / ``charged`` are always ``False``.
    """

    business_id: str
    entry: BillableAcceptance
    billing_key: str
    status: str
    outbox_event_id: str
    emitted: bool
    already_emitted: bool

    @property
    def confers_consent(self) -> bool:
        """Emitting a billable acceptance confers NO recipient consent. Always ``False``."""
        return False

    @property
    def charged(self) -> bool:
        """This story makes NO live billing call and charges nothing (DCA-158 delivery is disabled)."""
        return False


class BillableAcceptanceEmitter:
    """Emit ONE billable acceptance for an ACKNOWLEDGED audience sync, for ONE business (tenant-scoped).

    Construct with the active session and the verified business scope. Every query is confined to this
    business — there is no argument to widen the scope — so an emission can never reach another tenant's
    intent, and an unknown / cross-tenant id reads as not-found. The emitter stages into the caller's
    session and never commits; it records ONE billable ledger entry (+ its DCA-027 usage record + its
    atomic audit) and sends / spends / charges / confers nothing.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def _intent(self, intent_id: str) -> AcceptanceIntent | None:
        """Fetch one acceptance intent by id WITHIN this business, or ``None`` — an unknown or
        cross-tenant id reads exactly like not-found (an emission can never target another tenant's
        row)."""
        if not intent_id:
            return None
        stmt = (
            select(AcceptanceIntent)
            .where(AcceptanceIntent.business_id == self._business_id)
            .where(AcceptanceIntent.id == intent_id)
        )
        return self._session.scalars(stmt).one_or_none()

    def entry_for(self, intent_id: str) -> BillableAcceptance | None:
        """The billable-acceptance ledger entry for one intent within this business, or ``None`` (the
        idempotency read; a cross-tenant / unknown id reads as not-found)."""
        if not intent_id:
            return None
        stmt = (
            select(BillableAcceptance)
            .where(BillableAcceptance.business_id == self._business_id)
            .where(BillableAcceptance.acceptance_intent_id == intent_id)
        )
        return self._session.scalars(stmt).one_or_none()

    # -- the one emit path --------------------------------------------------------------------------

    def emit(
        self,
        intent_id: str,
        actor: AcceptanceSyncActor,
        *,
        now: datetime | None = None,
    ) -> BillableAcceptanceResult:
        """Emit the billable acceptance for ``intent_id`` for this business.

        Order (each gate leaves prior state unchanged when it refuses):

        1. **Authority FIRST.** A caller lacking :attr:`~app.identity.permissions.Scope.CANDIDATE_ACCEPT`
           is refused (:class:`BillableAcceptanceAuthorizationError`) before any state is read — metering
           an accepted record is the same authority as accepting / syncing it.
        2. **The intent exists in THIS business.** Otherwise refused (:data:`REASON_INTENT_NOT_FOUND`) —
           an unknown or cross-tenant id reads as not-found.
        3. **Idempotent replay.** An existing ledger entry for this intent is returned unchanged (no
           second entry, no second usage record, no second audit) — a prospect is billable once per
           business, never double-charged on a replay / reconcile.
        4. **Acknowledgment gate.** The sync must be ACKNOWLEDGED — the intent ``synced`` with
           ``sync_status='acknowledged'`` and the external mapping present. A pending / uncertain
           (``needs_review``) / ``pending_sync`` / failed intent is REFUSED (:data:`REASON_NOT_ACKNOWLEDGED`)
           with no entry emitted — an excluded / failed / pending record is never charged.
        5. **Record.** The ``recorded`` ledger entry is staged under the stable logical billing key,
           together with a durable DCA-027 outbox usage record and its atomic DCA-026 audit entry — all
           in the caller's unit of work, so they commit (or roll back) together.

        Emitting makes NO live billing call, charges NOTHING, performs NO send and confers NO recipient
        consent. Delivery of usage to platform billing stays a disabled future capability (DCA-158).
        """
        moment = now or datetime.now(UTC)

        # 1) Authority FIRST — an unauthorized caller is refused with no state touched.
        if not actor.may_sync():
            raise BillableAcceptanceAuthorizationError(
                "emitting a billable acceptance requires a business user holding "
                f"'{Scope.CANDIDATE_ACCEPT.value}'; a read-only viewer or a platform operator "
                "(holding no business scope) cannot perform it"
            )

        # 2) The intent must exist WITHIN this business (an unknown / cross-tenant id is not-found).
        intent = self._intent(intent_id)
        if intent is None:
            raise BillableAcceptanceError(
                REASON_INTENT_NOT_FOUND,
                "no such acceptance intent in this business (an unknown or cross-tenant id is "
                "not-found)",
                detail=intent_id,
            )

        # 3) Idempotent replay — a prospect is billable ONCE per business; return the existing entry
        #    unchanged. A duplicate acknowledgment (replay / reconcile) never double-charges.
        existing = self.entry_for(intent_id)
        if existing is not None:
            return BillableAcceptanceResult(
                business_id=self._business_id,
                entry=existing,
                billing_key=existing.billing_key,
                status=existing.status,
                outbox_event_id=existing.outbox_event_id,
                emitted=False,
                already_emitted=True,
            )

        # 4) Acknowledgment gate — ONLY an acknowledged sync is billable. A pending / uncertain / failed /
        #    pending_sync intent carries no billable fact: refuse, emit nothing.
        if not (
            intent.status == AcceptanceIntentStatus.SYNCED.value
            and intent.sync_status == AudienceSyncStatus.ACKNOWLEDGED.value
            and intent.external_crm_record_id
        ):
            raise BillableAcceptanceError(
                REASON_NOT_ACKNOWLEDGED,
                f"acceptance intent '{intent.id}' is status='{intent.status}' "
                f"sync_status='{intent.sync_status}', not an ACKNOWLEDGED sync; a billable acceptance "
                "is emitted only after the required acknowledgment (never on a pending / uncertain / "
                "failed sync)",
                detail=intent.sync_status,
            )

        # 5) Record the billable ledger entry + its durable outbox usage record + its atomic audit.
        return self._record(intent, actor, moment)

    # -- internals ----------------------------------------------------------------------------------

    def _record(
        self,
        intent: AcceptanceIntent,
        actor: AcceptanceSyncActor,
        moment: datetime,
    ) -> BillableAcceptanceResult:
        """Stage the billable ledger entry + its DCA-027 outbox usage record + its atomic DCA-026 audit
        entry in the caller's unit of work. Returns the (uncommitted) ledger entry + staged outbox id."""
        billing_key = billable_acceptance_key(self._business_id, intent.id)

        # The durable billing-usage RECORD — a PENDING outbox intent (never delivered here). Its stable
        # event id is derived from (business, type, billing_key), so re-staging the same logical usage is
        # constrained by the database rather than silently doubled. Payload is opaque references only.
        outbox = OutboxSink(self._session, business_id=self._business_id).enqueue(
            event_type=OUTBOX_EVENT_BILLABLE,
            dedup_key=billing_key,
            payload={
                "acceptanceIntentId": intent.id,
                "suggestionId": intent.suggestion_id,
                "subjectId": intent.subject_id,
                "subjectType": intent.subject_type,
                "billingKey": billing_key,
                "externalCrmRecordId": intent.external_crm_record_id,
                "intentStatus": intent.status,
                "syncStatus": intent.sync_status,
                # The invariants travel on the usage record too: a later (disabled) billing-delivery story
                # must honour that a billable fact is not a charge / send / consent until it delivers.
                "billable": True,
                "delivered": False,
                "consentConferred": False,
            },
        )

        provenance = {
            "source": BILLABLE_ACCEPTANCE_SOURCE,
            "acceptanceIntentId": intent.id,
            "suggestionId": intent.suggestion_id,
            "subjectType": intent.subject_type,
            "billingKey": billing_key,
            "externalCrmRecordId": intent.external_crm_record_id,
            "outboxEventId": outbox.event_id,
            # Load-bearing invariants: a billable acceptance records a usage FACT + a pending delivery
            # record ONLY. It makes no live billing call and charges nothing (DCA-158 delivery disabled),
            # confers no consent, performs no send, and creates no membership.
            "billable": True,
            "delivered": False,
            "sent": False,
            "spent": False,
            "consentConferred": False,
            "membershipCreated": False,
        }
        entry = BillableAcceptance(
            business_id=self._business_id,
            acceptance_intent_id=intent.id,
            suggestion_id=intent.suggestion_id,
            subject_type=intent.subject_type,
            subject_id=intent.subject_id,
            billing_key=billing_key,
            external_crm_record_id=intent.external_crm_record_id,
            outbox_event_id=outbox.event_id,
            schema_version=BILLABLE_ACCEPTANCE_SCHEMA_VERSION,
            status=BillableAcceptanceStatus.RECORDED.value,
            recorded_by=actor.subject,
            recorded_by_type=actor.actor_type,
            provenance=provenance,
            created_at=moment,
        )
        self._session.add(entry)
        # Flush so the entry's generated id is populated BEFORE it is referenced on the atomic audit
        # entry. A flush is NOT a commit — it stays inside the caller's unit of work, so a later rollback
        # still discards the entry, its outbox usage record and its audit.
        self._session.flush()

        # Atomic audit (SEC-001/BR-122): staged into the caller's session so it commits with the entry +
        # the usage record. References stay OPAQUE — the intent / suggestion / subject ids, the billing
        # key, the platform's external id and the outbox event id — never a contact value. Metering is
        # never a send / charge / consent.
        AuditSink(
            self._session, business_id=self._business_id, actor=actor.as_audit_actor()
        ).record(
            action=AUDIT_ACTION_BILLABLE,
            reason=(
                f"recorded billable acceptance for intent '{intent.id}' after acknowledgment "
                "(no live billing call, no charge, no send, no consent)"
            ),
            references={
                "billableAcceptanceId": entry.id,
                "acceptanceIntentId": intent.id,
                "prospectSuggestionId": intent.suggestion_id,
                "subjectId": intent.subject_id,
                "subjectType": intent.subject_type,
                "billingKey": billing_key,
                "externalCrmRecordId": intent.external_crm_record_id,
                "outboxEventId": outbox.event_id,
                "status": BillableAcceptanceStatus.RECORDED.value,
                "billable": True,
                "delivered": False,
                "charged": False,
                "consentConferred": False,
                "sent": False,
            },
        )

        return BillableAcceptanceResult(
            business_id=self._business_id,
            entry=entry,
            billing_key=billing_key,
            status=entry.status,
            outbox_event_id=outbox.event_id,
            emitted=True,
            already_emitted=False,
        )


__all__ = [
    "BILLABLE_ACCEPTANCE_SCHEMA_VERSION",
    "AUDIT_ACTION_BILLABLE",
    "OUTBOX_EVENT_BILLABLE",
    "BILLABLE_ACCEPTANCE_SOURCE",
    "REASON_NOT_AUTHORIZED",
    "REASON_INTENT_NOT_FOUND",
    "REASON_NOT_ACKNOWLEDGED",
    "BillableAcceptanceError",
    "BillableAcceptanceAuthorizationError",
    "BillableAcceptanceResult",
    "BillableAcceptanceEmitter",
]
