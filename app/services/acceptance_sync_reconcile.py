"""DCA-156 — reconcile an UNCERTAIN audience synchronization.

The audience stage's reconciliation step after the DCA-155 sync: a DCA-155 sync attempt against the
DCA-154 :class:`~app.audience.contract.AudienceSyncPort` left the DCA-153
:class:`~app.db.models.AcceptanceIntent` in an IN-DOUBT state — the platform's acknowledgment was lost,
timed out, or came back PENDING (queued, no external ids). The intent is stuck ``pending_sync`` with
``sync_status='pending'`` and nobody knows whether the CRM record was actually written. This service
RESOLVES that one ambiguity, using supported host lookup/receipt semantics, WITHOUT ever creating a
duplicate CRM record and WITHOUT implying success from an unconfirmed outcome.

How the ambiguity is resolved WITHOUT claiming certainty from nothing (the story's one task — "use
supported host lookup/receipt semantics to resolve ambiguous sync outcomes or mark operator review where
unavailable"):

- **Re-drive, don't re-create.** The reconciler RE-DRIVES the DCA-154 port under the SAME stable request
  key the DCA-153 intent was staged under (via :func:`~app.audience.contract.audience_sync_request_from_intent`).
  External ids are DETERMINISTIC in that key, so the platform's receipt lookup can only ever return the
  SAME external record — a retry can NEVER create a second CRM record / a duplicate audience member. This
  is the load-bearing acceptance guarantee.

- **Only an ACKNOWLEDGED re-drive resolves to synced.** If the re-drive comes back ACKNOWLEDGED (external
  ids present), the intent is resolved ``pending_sync -> synced``, the platform's external mapping is
  persisted and ``sync_reconcile_state='resolved'`` — exactly the DCA-155 acknowledged outcome, reached
  under the same key (so a lost ack whose write had in fact succeeded resolves to the SAME record).

- **An unconfirmed re-drive is marked for operator review; it NEVER implies success.** If the re-drive
  does NOT come back acknowledged (still PENDING / an unavailable or unsupported receipt lookup), the
  intent stays ``pending_sync`` (no accepted status, no external ids) and ``sync_reconcile_state`` is set
  to ``needs_review`` — the ambiguity is routed to a human rather than assumed successful. ``needs_review``
  is NOT terminal: a later reconcile may still resolve it once the host acknowledges.

Idempotent, duplicate-safe. An intent already ``synced`` (its external mapping persisted) short-circuits —
no re-drive, no duplicate sync, no second audit — and returns its existing external refs. Every guard is
checked fail-closed, in order, BEFORE any state is mutated, so a refusal (unauthorized / not-found /
not-uncertain / revalidation-denied / production-disabled) changes nothing. The request is re-gated by
:func:`~app.audience.contract.authorize_audience_sync` immediately before the re-drive, so a restricted
CONTACT field, a sendable sync or a disallowed purpose is REFUSED with no port call and no state change —
reconciliation can never silently promote a restricted field.

The service stages into the *caller's* session and never commits — the route/worker owns the unit of
work, so the intent's status advance and its atomic DCA-026 audit entry commit (or roll back) together.
There is no argument to widen the business scope: an unknown / cross-tenant intent reads as not-found.
Reconciling performs NO send, confers NO recipient consent (**acceptance is NOT consent**) and creates NO
audience membership.
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
    AudienceSyncPort,
    AudienceSyncPurpose,
    AudienceSyncStatus,
    audience_sync_request_from_intent,
    authorize_audience_sync,
    build_audience_sync_port,
)
from app.audit.sink import AuditSink
from app.db.models import AcceptanceIntent, AcceptanceIntentStatus
from app.identity.permissions import Scope
from app.services.acceptance_sync import ACCEPTANCE_SYNC_SOURCE, AcceptanceSyncActor

# The audited action code for reconciling an uncertain audience-sync outcome (a controlled code,
# distinct from the DCA-155 ``prospect.suggestion.synced`` so a reconciliation is a separate dimension).
AUDIT_ACTION_RECONCILE = "prospect.suggestion.sync_reconciled"

# The reconciliation states recorded on ``AcceptanceIntent.sync_reconcile_state`` (controlled vocabulary).
# ``RESOLVED`` — a re-drive came back acknowledged, so the intent was resolved to ``synced``.
# ``NEEDS_REVIEW`` — the host's receipt lookup did not confirm the write; routed to operator review (NOT
# terminal — a later reconcile may still resolve it). An empty state means "never reconciled".
RECONCILE_RESOLVED = "resolved"
RECONCILE_NEEDS_REVIEW = "needs_review"

# Stable refusal codes raised (never recorded) for a malformed / ineligible request — each BEFORE any
# state is mutated, so the attempt changes nothing. The not-found code matches DCA-155's so an unknown /
# cross-tenant id reads identically whether synced or reconciled.
REASON_NOT_AUTHORIZED = "acceptance_sync_reconcile_not_authorized"
REASON_INTENT_NOT_FOUND = "acceptance_intent_not_found"
REASON_NOT_UNCERTAIN = "acceptance_sync_not_uncertain"


class AcceptanceSyncReconcileError(Exception):
    """A reconciliation was refused with a stable ``code`` for a malformed / ineligible request (an
    unknown or cross-tenant intent, or an intent that carries no in-doubt outcome to reconcile). Raised
    before the session is mutated, so a refusal changes nothing. ``detail`` names the offending value."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class AcceptanceSyncReconcileAuthorizationError(AcceptanceSyncReconcileError):
    """The caller is not authorized to reconcile an uncertain audience sync.

    A distinct type so an HTTP boundary can map it to a 403 while the other codes map to a 404/409. Raised
    FIRST — before any state is read or written. ``code`` is :data:`REASON_NOT_AUTHORIZED`.
    """

    def __init__(self, message: str) -> None:
        super().__init__(REASON_NOT_AUTHORIZED, message)


@dataclass
class AudienceSyncReconcileResult:
    """The inspectable outcome of one reconciliation attempt.

    ``resolved`` is ``True`` when this call drove the intent to ``synced`` (an acknowledged re-drive);
    ``needs_review`` when it routed an unconfirmed outcome to operator review; ``already_resolved`` when
    the intent was already ``synced`` and this was an idempotent no-op; ``refused`` when the revalidation
    gate refused the request before any port call. ``attempted`` reports whether the port was re-driven.
    ``status`` / ``sync_status`` / ``reconcile_state`` echo the intent's state after the attempt, and
    ``external_ref`` the persisted external mapping (present only when resolved / already synced).
    ``reason`` is a stable code. Nothing here is ever sent, spent or consented — ``confers_consent`` is
    always ``False``.
    """

    business_id: str
    intent: AcceptanceIntent
    status: str
    sync_status: str
    reconcile_state: str
    attempted: bool
    resolved: bool
    needs_review: bool
    already_resolved: bool
    refused: bool
    reason: str
    external_ref: Any = None  # ExternalRecordRef | None

    @property
    def confers_consent(self) -> bool:
        """Reconciling an uncertain sync confers NO recipient consent. Always ``False``."""
        return False


class UncertainAudienceSyncReconciler:
    """Reconcile ONE uncertain audience synchronization, for ONE business (tenant-scoped).

    Construct with the active session, the verified business scope and the deployment settings (used to
    resolve the audience-sync port via the DCA-154 build factory, which refuses production). Every query
    is confined to this business — there is no argument to widen the scope — so a reconciliation can never
    reach another tenant's intent, and an unknown / cross-tenant id reads as not-found. The reconciler
    stages into the caller's session and never commits; it re-drives ONE in-doubt record under its stable
    request key and sends / spends / confers nothing.
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
        cross-tenant id reads exactly like not-found (a reconciliation can never target another
        tenant's row)."""
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

    # -- the one reconcile path ---------------------------------------------------------------------

    def reconcile(
        self,
        intent_id: str,
        actor: AcceptanceSyncActor,
        *,
        fields: tuple[AudienceField, ...] = (),
        purpose: AudienceSyncPurpose = AudienceSyncPurpose.RESEARCH,
        correlation_id: str | None = None,
        now: datetime | None = None,
    ) -> AudienceSyncReconcileResult:
        """Reconcile the uncertain sync for ``intent_id`` for this business.

        Order (each gate leaves prior state unchanged when it refuses):

        1. **Authority FIRST.** A caller lacking :attr:`~app.identity.permissions.Scope.CANDIDATE_ACCEPT`
           is refused (:class:`AcceptanceSyncReconcileAuthorizationError`) before any state is read —
           reconciling an accepted record is the same authority as syncing it.
        2. **The intent exists in THIS business.** Otherwise refused (:data:`REASON_INTENT_NOT_FOUND`) —
           an unknown or cross-tenant id reads as not-found.
        3. **Idempotent terminal replay.** An intent already ``synced`` (its external mapping persisted)
           returns its existing refs unchanged — no re-drive, no duplicate sync, no second audit.
        4. **Uncertainty gate.** Only an in-doubt intent (``pending_sync`` with ``sync_status='pending'``)
           carries anything to reconcile; anything else is refused (:data:`REASON_NOT_UNCERTAIN`).
        5. **Revalidate immediately before the re-drive.** The request (built from the intent under its
           stable key) is re-gated by :func:`~app.audience.contract.authorize_audience_sync`; a restricted
           CONTACT field, a sendable sync or a disallowed purpose is REFUSED with no port call and no state
           change — reconciliation never silently promotes a restricted field.
        6. **Re-drive + resolve.** The port (production-refused via the build factory) is RE-DRIVEN under
           the SAME request key (deterministic external ids, so never a duplicate record). An ACKNOWLEDGED
           re-drive resolves the intent to ``synced`` and persists the external mapping; an unconfirmed one
           (still PENDING / unavailable lookup) leaves it ``pending_sync`` and marks it ``needs_review``
           for operator review — it NEVER implies success. Both stage an atomic DCA-026 audit entry.

        Reconciling performs NO send, confers NO recipient consent and creates NO audience membership.
        """
        moment = now or datetime.now(UTC)

        # 1) Authority FIRST — an unauthorized caller is refused with no state touched.
        if not actor.may_sync():
            raise AcceptanceSyncReconcileAuthorizationError(
                "reconciling an uncertain audience sync requires a business user holding "
                f"'{Scope.CANDIDATE_ACCEPT.value}'; a read-only viewer or a platform operator "
                "(holding no business scope) cannot perform it"
            )

        # 2) The intent must exist WITHIN this business (an unknown / cross-tenant id is not-found).
        intent = self._intent(intent_id)
        if intent is None:
            raise AcceptanceSyncReconcileError(
                REASON_INTENT_NOT_FOUND,
                "no such acceptance intent in this business (an unknown or cross-tenant id is "
                "not-found)",
                detail=intent_id,
            )

        # 3) Idempotent terminal replay — an already-synced intent returns its persisted mapping unchanged.
        if intent.status == AcceptanceIntentStatus.SYNCED.value and intent.external_crm_record_id:
            return AudienceSyncReconcileResult(
                business_id=self._business_id,
                intent=intent,
                status=intent.status,
                sync_status=intent.sync_status,
                reconcile_state=intent.sync_reconcile_state,
                attempted=False,
                resolved=False,
                needs_review=False,
                already_resolved=True,
                refused=False,
                reason=AudienceSyncStatus.ACKNOWLEDGED.value,
                external_ref=self._external_ref_of(intent),
            )

        # 4) Uncertainty gate — only an in-doubt (pending-reconciliation) outcome is reconcilable. A
        #    never-attempted ('') or refused ('disabled') intent carries no ambiguity to resolve.
        if not (
            intent.status == AcceptanceIntentStatus.PENDING_SYNC.value
            and intent.sync_status == AudienceSyncStatus.PENDING.value
        ):
            raise AcceptanceSyncReconcileError(
                REASON_NOT_UNCERTAIN,
                f"acceptance intent '{intent.id}' is status='{intent.status}' "
                f"sync_status='{intent.sync_status}', not an in-doubt (pending-reconciliation) sync; "
                "only an uncertain outcome can be reconciled",
                detail=intent.sync_status,
            )

        # 5) Revalidate immediately before the re-drive — refuse (no port call, no state change) rather
        #    than silently promote a restricted field / a sendable sync / a disallowed purpose.
        request = audience_sync_request_from_intent(
            intent, purpose=purpose, fields=fields, correlation_id=correlation_id
        )
        decision = authorize_audience_sync(request)
        if not decision.allowed:
            return AudienceSyncReconcileResult(
                business_id=self._business_id,
                intent=intent,
                status=intent.status,
                sync_status=intent.sync_status,
                reconcile_state=intent.sync_reconcile_state,
                attempted=False,
                resolved=False,
                needs_review=False,
                already_resolved=False,
                refused=True,
                reason=decision.reason,
                external_ref=None,
            )

        # 6) Resolve the port (production-refused here, before any mutation) and RE-DRIVE under the SAME
        #    request key — deterministic external ids, so a retry can never create a duplicate record.
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
    ) -> AudienceSyncReconcileResult:
        """Apply one re-driven :class:`~app.audience.contract.AudienceSyncAck` onto the uncertain intent +
        stage its atomic audit entry. ACKNOWLEDGED resolves the intent to ``synced`` and persists the
        external mapping; an unconfirmed re-drive marks ``needs_review`` and NEVER implies success."""
        acknowledged = ack.is_acknowledged
        intent.last_sync_attempt_at = moment
        intent.last_reconciled_at = moment
        intent.reconcile_attempt_count = (intent.reconcile_attempt_count or 0) + 1

        if acknowledged:
            ref = ack.external_ref
            assert ref is not None  # the ack model guarantees ids exactly when acknowledged
            intent.status = AcceptanceIntentStatus.SYNCED.value
            intent.sync_status = AudienceSyncStatus.ACKNOWLEDGED.value
            intent.sync_reconcile_state = RECONCILE_RESOLVED
            intent.external_crm_record_id = ref.crm_record_id
            intent.external_audience_id = ref.audience_id
            intent.external_version = ref.external_version
            intent.synced_at = moment
            # Flip ONLY the 'synced' invariant; reconciling is still never a send / spend / consent.
            intent.provenance = {
                **(intent.provenance or {}),
                "synced": True,
                "syncSource": ACCEPTANCE_SYNC_SOURCE,
                "externalCrmRecordId": ref.crm_record_id,
                "externalAudienceId": ref.audience_id,
            }
            resolved, needs_review = True, False
            reason = ack.reason
        else:
            # The host's receipt/status lookup did NOT confirm the write (still PENDING / unavailable).
            # The intent stays pending_sync — no accepted status, no external ids — and the ambiguity is
            # routed to operator review rather than assumed successful (unavailable lookup != success).
            intent.sync_status = ack.status.value
            intent.sync_reconcile_state = RECONCILE_NEEDS_REVIEW
            resolved, needs_review = False, True
            reason = ack.reason

        self._session.flush()

        # Atomic audit (SEC-001/BR-122): staged into the caller's session so it commits with the state
        # change. References stay OPAQUE — the intent/suggestion/subject ids, the request key and the
        # platform's external ids — never a contact value. Reconciling never sends / charges / consents.
        AuditSink(
            self._session, business_id=self._business_id, actor=actor.as_audit_actor()
        ).record(
            action=AUDIT_ACTION_RECONCILE,
            reason=(
                f"reconciled uncertain audience sync for acceptance intent '{intent.id}' as "
                f"{intent.sync_reconcile_state} (re-drive {ack.status.value}; no send, no consent, "
                "no membership)"
            ),
            references={
                "acceptanceIntentId": intent.id,
                "prospectSuggestionId": intent.suggestion_id,
                "subjectId": intent.subject_id,
                "subjectType": intent.subject_type,
                "visibility": intent.visibility,
                "requestKey": ack.request_key,
                "reDriveStatus": ack.status.value,
                "reconcileState": intent.sync_reconcile_state,
                "reconcileAttemptCount": intent.reconcile_attempt_count,
                "resolved": resolved,
                "needsReview": needs_review,
                "crmRecordId": intent.external_crm_record_id or None,
                "audienceId": intent.external_audience_id or None,
                "externalVersion": intent.external_version or None,
                "synthetic": ack.synthetic,
                "consentConferred": False,
                "sent": False,
            },
        )

        return AudienceSyncReconcileResult(
            business_id=self._business_id,
            intent=intent,
            status=intent.status,
            sync_status=intent.sync_status,
            reconcile_state=intent.sync_reconcile_state,
            attempted=True,
            resolved=resolved,
            needs_review=needs_review,
            already_resolved=False,
            refused=False,
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
    "AUDIT_ACTION_RECONCILE",
    "RECONCILE_RESOLVED",
    "RECONCILE_NEEDS_REVIEW",
    "REASON_NOT_AUTHORIZED",
    "REASON_INTENT_NOT_FOUND",
    "REASON_NOT_UNCERTAIN",
    "AcceptanceSyncReconcileError",
    "AcceptanceSyncReconcileAuthorizationError",
    "AudienceSyncReconcileResult",
    "UncertainAudienceSyncReconciler",
]
