"""DCA-155 — synchronize ONE accepted record THROUGH EffortlessPromo.

The audience stage's sync step after the DCA-153 acceptance: a business user has recorded a
``pending_sync`` :class:`~app.db.models.AcceptanceIntent` (and, with it, a durable DCA-027 outbox sync
*request*). This service drives that ONE accepted record through the DCA-154
:class:`~app.audience.contract.AudienceSyncPort` — against the synthetic fake today; the live adapter
into the platform's CRM stays a disabled future capability (DCA-001 B1) and production refuses it
outright (:func:`~app.audience.fake.build_audience_sync_port`). Syncing hands the platform a MINIMIZED
:class:`~app.audience.contract.AudienceSyncRequest` under the SAME stable request key the intent was
staged under, and applies the platform's :class:`~app.audience.contract.AudienceSyncAck` back onto the
intent. It performs NO send, confers NO recipient consent (**acceptance is NOT consent**) and creates
NO audience membership — a research-only organization stays research-only.

The two load-bearing invariants (the DCA-155 acceptance criteria):

- **A timeout / lost acknowledgment remains PENDING reconciliation; no accepted status before the
  required acknowledgment.** Only an ACKNOWLEDGED ack (external ids present) advances the intent to
  ``synced`` and persists the platform's external record mapping. A PENDING hand-off (queued, no external
  ids — exactly how a lost acknowledgment is modelled) leaves the intent ``pending_sync``, records only
  that a sync is pending reconciliation, and persists NO external ids. The status never advances on
  anything short of an acknowledgment.

- **Revalidation immediately before sync; a restricted field is never promoted.** The request is
  re-gated by the contract's own :func:`~app.audience.contract.authorize_audience_sync` immediately
  before it is handed to the port, so a sendable sync, a restricted CONTACT field under a research-only
  acceptance, or a disallowed purpose is REFUSED with no port call and no state change — the sync can
  never SILENTLY promote a restricted field.

Idempotent on the request key: an intent already ``synced`` (its external mapping persisted) short-
circuits — no second port call, no duplicate sync, no second audit — so replaying a sync returns the
SAME external record refs. Every guard is checked fail-closed, in order, BEFORE any state is mutated,
so a refusal (unauthorized / not-found / revalidation-denied / production-disabled) changes nothing.

The service stages into the *caller's* session and never commits — the route/worker owns the unit of
work, so the intent's status advance and its atomic DCA-026 audit entry commit (or roll back) together.
There is no argument to widen the business scope: an unknown / cross-tenant intent reads as not-found.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audience import (
    AudienceField,
    AudienceSyncAck,
    AudienceSyncDecision,
    AudienceSyncPort,
    AudienceSyncPurpose,
    AudienceSyncStatus,
    audience_sync_request_from_intent,
    authorize_audience_sync,
    build_audience_sync_port,
)
from app.audit.sink import ActorType, AuditActor, AuditSink
from app.db.models import AcceptanceIntent, AcceptanceIntentStatus
from app.identity.permissions import Scope, scopes_for_business_roles

# The audited action code for applying an audience-sync result onto an acceptance intent (controlled).
AUDIT_ACTION_SYNC = "prospect.suggestion.synced"

# Where a sync records its origin on the provenance / audit references.
ACCEPTANCE_SYNC_SOURCE = "acceptance_sync"

# Stable refusal codes raised (never recorded) for a malformed / ineligible request — each BEFORE any
# state is mutated, so the attempt changes nothing.
REASON_NOT_AUTHORIZED = "acceptance_sync_not_authorized"
REASON_INTENT_NOT_FOUND = "acceptance_intent_not_found"


class AcceptanceSyncError(Exception):
    """A sync was refused with a stable ``code`` for a malformed / ineligible request (an unknown or
    cross-tenant intent). Raised before the session is mutated, so a refusal changes nothing.
    ``detail`` names the offending value."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class AcceptanceSyncAuthorizationError(AcceptanceSyncError):
    """The caller is not authorized to synchronize an accepted record.

    A distinct type so an HTTP boundary can map it to a 403 while the other code maps to a 404. Raised
    FIRST — before any state is read or written. ``code`` is :data:`REASON_NOT_AUTHORIZED`.
    """

    def __init__(self, message: str) -> None:
        super().__init__(REASON_NOT_AUTHORIZED, message)


@dataclass(frozen=True)
class AcceptanceSyncActor:
    """The business principal synchronizing an accepted record.

    ``subject`` is an opaque actor id (never a name/email) recorded on the atomic audit entry. ``roles``
    are the caller's verified business-plane membership roles (from the tenant context, NEVER a bare
    token claim). :meth:`may_sync` grants the action only when those roles yield
    :attr:`~app.identity.permissions.Scope.CANDIDATE_ACCEPT` — syncing an accepted record is the positive
    counterpart of accepting it, so it demands the same acting-user authority; a read-only viewer and a
    platform operator (holding no business scope) cannot perform it.
    """

    subject: str
    roles: tuple[str, ...] = ()
    actor_type: str = "user"

    def may_sync(self) -> bool:
        return Scope.CANDIDATE_ACCEPT in scopes_for_business_roles(self.roles)

    def as_audit_actor(self) -> AuditActor:
        return AuditActor(id=self.subject, type=ActorType(self.actor_type))


@dataclass
class AcceptanceSyncResult:
    """The inspectable outcome of one sync attempt.

    ``ack`` is the platform's :class:`~app.audience.contract.AudienceSyncAck` (``None`` only when the
    revalidation gate refused the request before any port call). ``accepted`` is whether the platform
    TOOK the request; ``acknowledged`` whether it WROTE the record (and so advanced the intent to
    ``synced``). ``already_synced`` is ``True`` when this was an idempotent no-op (the intent was already
    synced). ``status`` / ``sync_status`` echo the intent's state after the attempt, and
    ``external_ref`` the persisted external mapping (present only when acknowledged). ``reason`` is a
    stable code. Nothing here is ever sent, spent or consented — ``confers_consent`` is always ``False``.
    """

    business_id: str
    intent: AcceptanceIntent
    status: str
    sync_status: str
    accepted: bool
    acknowledged: bool
    already_synced: bool
    reason: str
    external_ref: Any = None  # ExternalRecordRef | None

    @property
    def confers_consent(self) -> bool:
        """Synchronizing an accepted record confers NO recipient consent. Always ``False``."""
        return False


class AcceptanceRecordSynchronizer:
    """Synchronize ONE accepted record THROUGH EffortlessPromo, for ONE business (tenant-scoped).

    Construct with the active session, the verified business scope and the deployment settings (used to
    resolve the audience-sync port via the DCA-154 build factory, which refuses production). Every query
    is confined to this business — there is no argument to widen the scope — so a sync can never reach
    another tenant's intent, and an unknown / cross-tenant id reads as not-found. The synchronizer stages
    into the caller's session and never commits; it syncs ONE accepted record and sends / spends / confers
    nothing.
    """

    def __init__(
        self,
        session: Session,
        business_id: str,
        settings: Any,
        *,
        port: AudienceSyncPort | None = None,
    ) -> None:
        self._session = session
        self._business_id = business_id
        self._settings = settings
        # An injected port is a dev/test seam (to drive the fake's acknowledge knob); production still
        # resolves through the build factory below, which refuses the fake outright.
        self._port = port

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def _intent(self, intent_id: str) -> AcceptanceIntent | None:
        """Fetch one acceptance intent by id WITHIN this business, or ``None`` — an unknown or
        cross-tenant id reads exactly like not-found (a sync can never target another tenant's row)."""
        if not intent_id:
            return None
        stmt = (
            select(AcceptanceIntent)
            .where(AcceptanceIntent.business_id == self._business_id)
            .where(AcceptanceIntent.id == intent_id)
        )
        return self._session.scalars(stmt).one_or_none()

    def _resolve_port(self) -> AudienceSyncPort:
        """Resolve the audience-sync port. Production is refused through the DCA-154 build factory (the
        live adapter is unbuilt, B1); otherwise an injected test port or the synthetic fake is used."""
        if getattr(self._settings, "is_production", False):
            # Honour the contract's production refusal even if a port was injected — the disabled-in-
            # production rule is absolute (build_audience_sync_port raises AudienceSyncContractDisabled).
            return build_audience_sync_port(self._settings)
        return self._port or build_audience_sync_port(self._settings)

    # -- the one sync path --------------------------------------------------------------------------

    def sync(
        self,
        intent_id: str,
        actor: AcceptanceSyncActor,
        *,
        fields: tuple[AudienceField, ...] = (),
        purpose: AudienceSyncPurpose = AudienceSyncPurpose.RESEARCH,
        correlation_id: str | None = None,
        now: datetime | None = None,
    ) -> AcceptanceSyncResult:
        """Synchronize the accepted record for ``intent_id`` THROUGH EffortlessPromo, for this business.

        Order (each gate leaves prior state unchanged when it refuses):

        1. **Authority FIRST.** A caller lacking :attr:`~app.identity.permissions.Scope.CANDIDATE_ACCEPT`
           is refused (:class:`AcceptanceSyncAuthorizationError`) before any state is read.
        2. **The intent exists in THIS business.** Otherwise refused (:data:`REASON_INTENT_NOT_FOUND`) —
           an unknown or cross-tenant id reads as not-found.
        3. **Idempotent replay.** An intent already ``synced`` (its external mapping persisted) returns
           its existing refs unchanged — no port call, no duplicate sync, no second audit.
        4. **Revalidate immediately before sync.** The request (built from the intent under its stable
           key) is re-gated by :func:`~app.audience.contract.authorize_audience_sync`; a sendable sync, a
           restricted CONTACT field, or a disallowed purpose is REFUSED with no port call and no state
           change — the sync never silently promotes a restricted field.
        5. **Request + apply.** The port (production-refused via the build factory) is asked to sync; the
           ack is applied back onto the intent — ACKNOWLEDGED advances it to ``synced`` and persists the
           external mapping; PENDING leaves it ``pending_sync`` (a hand-off awaiting reconciliation). Both
           stage an atomic DCA-026 audit entry in the caller's unit of work.

        Syncing performs NO send, confers NO recipient consent and creates NO audience membership.
        """
        moment = now or datetime.now(UTC)

        # 1) Authority FIRST — an unauthorized caller is refused with no state touched.
        if not actor.may_sync():
            raise AcceptanceSyncAuthorizationError(
                "synchronizing an accepted record requires a business user holding "
                f"'{Scope.CANDIDATE_ACCEPT.value}'; a read-only viewer or a platform operator "
                "(holding no business scope) cannot perform it"
            )

        # 2) The intent must exist WITHIN this business (an unknown / cross-tenant id is not-found).
        intent = self._intent(intent_id)
        if intent is None:
            raise AcceptanceSyncError(
                REASON_INTENT_NOT_FOUND,
                "no such acceptance intent in this business (an unknown or cross-tenant id is "
                "not-found)",
                detail=intent_id,
            )

        # 3) Idempotent replay — an already-synced intent returns its persisted mapping unchanged.
        if intent.status == AcceptanceIntentStatus.SYNCED.value and intent.external_crm_record_id:
            return AcceptanceSyncResult(
                business_id=self._business_id,
                intent=intent,
                status=intent.status,
                sync_status=intent.sync_status,
                accepted=True,
                acknowledged=True,
                already_synced=True,
                reason=AudienceSyncDecision.OK,
                external_ref=self._external_ref_of(intent),
            )

        # 4) Revalidate immediately before sync — refuse (no port call, no state change) rather than
        #    silently promote a restricted field / a sendable sync / a disallowed purpose.
        request = audience_sync_request_from_intent(
            intent, purpose=purpose, fields=fields, correlation_id=correlation_id
        )
        decision = authorize_audience_sync(request)
        if not decision.allowed:
            return AcceptanceSyncResult(
                business_id=self._business_id,
                intent=intent,
                status=intent.status,
                sync_status=AudienceSyncStatus.DISABLED.value,
                accepted=False,
                acknowledged=False,
                already_synced=False,
                reason=decision.reason,
                external_ref=None,
            )

        # 5) Resolve the port (production-refused here, before any mutation) and request the sync.
        port = self._resolve_port()
        ack = port.sync(request)
        return self._apply(intent, ack, actor, moment)

    # -- internals ----------------------------------------------------------------------------------

    def _apply(
        self,
        intent: AcceptanceIntent,
        ack: AudienceSyncAck,
        actor: AcceptanceSyncActor,
        moment: datetime,
    ) -> AcceptanceSyncResult:
        """Apply one :class:`~app.audience.contract.AudienceSyncAck` onto the intent + stage its atomic
        audit entry. ACKNOWLEDGED advances the intent to ``synced`` and persists the external mapping;
        anything short of an acknowledgment leaves it ``pending_sync`` (pending reconciliation)."""
        acknowledged = ack.is_acknowledged
        intent.last_sync_attempt_at = moment

        if acknowledged:
            ref = ack.external_ref
            assert ref is not None  # the ack model guarantees ids exactly when acknowledged
            intent.status = AcceptanceIntentStatus.SYNCED.value
            intent.sync_status = AudienceSyncStatus.ACKNOWLEDGED.value
            intent.external_crm_record_id = ref.crm_record_id
            intent.external_audience_id = ref.audience_id
            intent.external_version = ref.external_version
            intent.synced_at = moment
            # Flip ONLY the 'synced' invariant; a sync is still never a send / spend / consent.
            intent.provenance = {
                **(intent.provenance or {}),
                "synced": True,
                "syncSource": ACCEPTANCE_SYNC_SOURCE,
                "externalCrmRecordId": ref.crm_record_id,
                "externalAudienceId": ref.audience_id,
            }
            reason = ack.reason
        else:
            # A PENDING hand-off (queued, no external ids) — exactly how a lost acknowledgment is
            # modelled. The intent stays pending_sync: no accepted status before acknowledgment, no
            # external ids persisted, no membership. Only the pending-reconciliation marker is recorded.
            intent.sync_status = AudienceSyncStatus.PENDING.value
            reason = ack.reason

        self._session.flush()

        # Atomic audit (SEC-001/BR-122): staged into the caller's session so it commits with the status
        # advance. References stay OPAQUE — the intent/suggestion/subject ids, the request key and the
        # platform's external ids — never a contact value. Syncing never sends / charges / consents.
        AuditSink(
            self._session, business_id=self._business_id, actor=actor.as_audit_actor()
        ).record(
            action=AUDIT_ACTION_SYNC,
            reason=(
                f"applied {ack.status.value} audience-sync for acceptance intent '{intent.id}' "
                "(no send, no consent, no membership)"
            ),
            references={
                "acceptanceIntentId": intent.id,
                "prospectSuggestionId": intent.suggestion_id,
                "subjectId": intent.subject_id,
                "subjectType": intent.subject_type,
                "visibility": intent.visibility,
                "requestKey": ack.request_key,
                "syncStatus": ack.status.value,
                "accepted": ack.accepted,
                "acknowledged": acknowledged,
                "crmRecordId": intent.external_crm_record_id or None,
                "audienceId": intent.external_audience_id or None,
                "externalVersion": intent.external_version or None,
                "synthetic": ack.synthetic,
                "consentConferred": False,
                "sent": False,
            },
        )

        return AcceptanceSyncResult(
            business_id=self._business_id,
            intent=intent,
            status=intent.status,
            sync_status=intent.sync_status,
            accepted=ack.accepted,
            acknowledged=acknowledged,
            already_synced=False,
            reason=reason,
            external_ref=ack.external_ref,
        )

    @staticmethod
    def _external_ref_of(intent: AcceptanceIntent):
        """Rebuild the persisted :class:`~app.audience.contract.ExternalRecordRef` from the intent's
        stored columns (for an idempotent replay) — or ``None`` when none is persisted."""
        from app.audience import ExternalRecordRef

        if not intent.external_crm_record_id:
            return None
        return ExternalRecordRef(
            crm_record_id=intent.external_crm_record_id,
            audience_id=intent.external_audience_id,
            external_version=intent.external_version,
        )


__all__ = [
    "AUDIT_ACTION_SYNC",
    "ACCEPTANCE_SYNC_SOURCE",
    "REASON_NOT_AUTHORIZED",
    "REASON_INTENT_NOT_FOUND",
    "AcceptanceSyncError",
    "AcceptanceSyncAuthorizationError",
    "AcceptanceSyncActor",
    "AcceptanceSyncResult",
    "AcceptanceRecordSynchronizer",
]
