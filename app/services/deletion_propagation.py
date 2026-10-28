"""DCA-085 — propagate deletion to external integrations (the deletion-propagation writer).

Key: ``deletion-remote`` · source sections 5, 10, 11.

The DCA-084 :class:`~app.services.data_erasure.OperationalDataEraser` erases the DCA's OWN operational
data and writes a :class:`~app.db.models.DataErasureJournal`. Some of that subject's data may ALSO live
in a supported external integration (a platform/provider destination). This story is the propagation:
a single sanctioned writer that, for one erasure journal, sends an **idempotent** deletion/removal
request through each supported destination contract and records the outcome per destination as an
:class:`~app.db.models.ExternalDeletionPropagation` — an acknowledgment, an explicit unsupported state,
a pending-reconciliation timeout, or a failure.

The load-bearing guarantees (the DCA-085 acceptance criteria):

- **A timeout is pending reconciliation, never a completed deletion.** A destination that does not
  acknowledge in time (:class:`~app.integration.deletion.DeletionTimeout`) is recorded
  ``PENDING`` — it is explicitly NOT counted as acknowledged/completed. :meth:`DeletionPropagator.reconcile`
  re-sends the SAME idempotent request and only an acknowledgment advances it to ``ACKNOWLEDGED``.

- **Idempotent; a re-run makes no unintended change.** Each ``(journal, destination)`` is one row
  (unique within the business) carrying a deterministic
  :func:`~app.integration.deletion.build_idempotency_key`, so a re-send is de-duplicated end to end. A
  destination already ``ACKNOWLEDGED`` / ``UNSUPPORTED`` is terminal — a re-run never re-sends to it and
  never rewrites it.

- **Authority first, and a denial changes nothing.** :meth:`DeletionPropagator.propagate` /
  :meth:`DeletionPropagator.reconcile` check the caller's authority BEFORE anything is read, sent or
  written, so a relevant denial is pure and reproducible (it sends no request and stages no row). The
  authority is the same reviewed-obligation administrator scope that authorized the erasure
  (:attr:`~app.identity.permissions.Scope.DATA_ERASE`) — an ordinary user, a read-only viewer and a
  platform operator (holding no membership) all lack it.

Every writer stages into the *caller's* session and performs no DB I/O of its own (never commits /
flushes / opens a transaction): the propagation rows and their atomic DCA-026 audit entries all commit
together with the caller's unit of work. The destination call itself is the external contract's I/O,
injected via the :class:`~app.integration.deletion.DeletionDestinationClient` port (the synthetic fake
in dev/test; the contract stays disabled in production until the live adapter ships). Every reference
recorded is OPAQUE — the journal id, the subject id, an opaque destination key, an opaque acknowledgment
ref and reason code — never a contact value, a name or a field value. Propagating a deletion only ever
REMOVES data; it grants nothing and sends no outreach (the DCA has no send path).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.sink import AuditActor, AuditSink
from app.db.models import DataErasureJournal, ExternalDeletionPropagation, ExternalDeletionStatus
from app.identity.permissions import Scope
from app.integration.deletion import (
    DeletionContractDisabled,
    DeletionDestinationClient,
    DeletionDispatchRequest,
    DeletionOutcome,
    DeletionRejected,
    DeletionTimeout,
    build_idempotency_key,
)
from app.services.data_erasure import ErasurePrincipal

# The per-destination statuses that are TERMINAL — a re-run never re-sends to a destination in one of
# these. PENDING and FAILED are NOT terminal: PENDING is reconciled (re-sent) and FAILED may be retried.
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {ExternalDeletionStatus.ACKNOWLEDGED.value, ExternalDeletionStatus.UNSUPPORTED.value}
)

# The audited action codes (controlled codes, like the other DCA writes).
AUDIT_ACTION_PROPAGATE = "external_deletion.dispatched"
AUDIT_ACTION_RECONCILE = "external_deletion.reconciled"

# Stable refusal / detail codes. Each refusal is raised BEFORE any send or write, so it stages nothing.
REASON_NOT_AUTHORIZED = "deletion_propagation_not_authorized"
REASON_JOURNAL_SCOPE_MISMATCH = "deletion_propagation_journal_scope_mismatch"
DETAIL_TIMEOUT = "destination_timeout_pending_reconciliation"


class DeletionPropagationError(Exception):
    """A propagation was refused with a stable ``code``. Raised before anything is sent or staged, so
    a refusal leaves no request, no row and no partial state."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DeletionPropagationAuthorizationError(DeletionPropagationError):
    """The caller is not authorized to propagate (or reconcile) this erasure's external deletions.

    A distinct type so an HTTP boundary can map it to a 403. Raised before anything is read, sent or
    written. ``code`` is :data:`REASON_NOT_AUTHORIZED`.
    """

    def __init__(self) -> None:
        super().__init__(
            REASON_NOT_AUTHORIZED,
            "propagating an erasure to external integrations requires a business administrator "
            f"holding '{Scope.DATA_ERASE.value}' (the same authority that executed the erasure); an "
            "ordinary user, a read-only viewer and a platform operator all lack it",
        )


@dataclass
class DestinationOutcome:
    """The outcome of propagating to ONE destination, for an inspectable record (no personal data)."""

    destination: str
    status: str
    propagation_id: str = ""
    acknowledgment_ref: str = ""
    detail_code: str = ""
    attempts: int = 0
    # True when the destination was already terminal and this call re-used the existing row without
    # re-sending (the idempotent no-op path).
    reused: bool = False

    @property
    def acknowledged(self) -> bool:
        return self.status == ExternalDeletionStatus.ACKNOWLEDGED.value

    @property
    def pending(self) -> bool:
        return self.status == ExternalDeletionStatus.PENDING.value


@dataclass
class PropagationResult:
    """What one propagate/reconcile call decided across all destinations (opaque references only)."""

    journal_id: str
    outcomes: list[DestinationOutcome] = field(default_factory=list)

    def _count(self, status: ExternalDeletionStatus) -> int:
        return sum(1 for o in self.outcomes if o.status == status.value)

    @property
    def acknowledged_count(self) -> int:
        return self._count(ExternalDeletionStatus.ACKNOWLEDGED)

    @property
    def unsupported_count(self) -> int:
        return self._count(ExternalDeletionStatus.UNSUPPORTED)

    @property
    def pending_count(self) -> int:
        return self._count(ExternalDeletionStatus.PENDING)

    @property
    def failed_count(self) -> int:
        return self._count(ExternalDeletionStatus.FAILED)

    @property
    def is_complete(self) -> bool:
        """True only when EVERY destination reached a terminal state (acknowledged / unsupported).

        A pending (timeout) or failed destination means the propagation is NOT complete — the explicit
        "a timeout is pending reconciliation, not a completed deletion" guarantee at the result level.
        """
        return bool(self.outcomes) and all(o.status in TERMINAL_STATUSES for o in self.outcomes)


class DeletionPropagator:
    """The single sanctioned writer that propagates an erasure to external integrations (DCA-085).

    Bound to one verified business scope and a :class:`~app.integration.deletion.DeletionDestinationClient`
    (the synthetic fake in dev/test). Every query/write is confined to that business — there is no
    argument to widen the scope. Stages into the caller's session and performs no DB I/O of its own.
    """

    def __init__(
        self, session: Session, business_id: str, client: DeletionDestinationClient | None
    ) -> None:
        if client is None:
            # Fail-closed: the contract is disabled (production, no live adapter). A propagator can
            # never be built against a missing client — the route refuses before reaching here.
            raise DeletionContractDisabled(
                "external-deletion contract is disabled: no destination client is configured"
            )
        self._session = session
        self._business_id = business_id
        self._client = client

    # -- the propagate path -------------------------------------------------------------------------

    def propagate(
        self,
        journal: DataErasureJournal,
        principal: ErasurePrincipal,
        *,
        destinations: tuple[str, ...] | None = None,
        now: datetime | None = None,
    ) -> PropagationResult:
        """Send a deletion request to each supported destination for ``journal`` (uncommitted result).

        Authorization is checked FIRST (:class:`DeletionPropagationAuthorizationError` before anything
        is read/sent/written). The journal must belong to this business. For each supported
        destination: a destination already ``ACKNOWLEDGED`` / ``UNSUPPORTED`` is reused as-is (no
        re-send — idempotent); otherwise an idempotent deletion request is sent and the outcome
        recorded — ``ACKNOWLEDGED`` (with the opaque ack ref), ``UNSUPPORTED``, ``PENDING`` on a
        timeout (pending reconciliation, never completed), or ``FAILED`` on a permanent rejection. Each
        dispatch stages/updates one :class:`~app.db.models.ExternalDeletionPropagation` row and one
        atomic DCA-026 audit entry in the caller's unit of work.
        """
        if not principal.may_erase():
            raise DeletionPropagationAuthorizationError()
        if journal.business_id != self._business_id:
            # Defensive: the route resolves the journal within this business, so this is unreachable
            # through the API; it fail-closes a direct misuse before any send/write.
            raise DeletionPropagationError(
                REASON_JOURNAL_SCOPE_MISMATCH,
                "the erasure journal does not belong to this business scope",
            )

        moment = now or datetime.now(UTC)
        targets = (
            destinations if destinations is not None else self._client.supported_destinations()
        )
        result = PropagationResult(journal_id=journal.id)

        for destination in sorted(set(targets)):
            existing = self._existing(journal.id, destination)
            if existing is not None and existing.status in TERMINAL_STATUSES:
                # Terminal already — idempotent no-op: no re-send, no rewrite, no new audit.
                result.outcomes.append(self._reused_outcome(existing))
                continue
            result.outcomes.append(
                self._dispatch(
                    journal,
                    destination,
                    existing=existing,
                    actor=principal.as_actor(),
                    action=AUDIT_ACTION_PROPAGATE,
                    now=moment,
                )
            )
        return result

    # -- the reconcile path (re-attempt PENDING destinations) ---------------------------------------

    def reconcile(
        self,
        principal: ErasurePrincipal,
        *,
        journal_id: str | None = None,
        now: datetime | None = None,
    ) -> PropagationResult:
        """Re-send the SAME idempotent request for every PENDING destination in this business.

        Authorization is checked FIRST (same scope as :meth:`propagate`). Only ``PENDING`` rows are
        re-attempted (a timeout is the "pending reconciliation" case); a terminal destination is left
        exactly as it is. A re-send carries the SAME deterministic idempotency key, so a destination
        that has recovered acknowledges it exactly once. ``journal_id`` narrows the reconciliation to
        one erasure; omitted, it reconciles every pending destination in the business. Stages into the
        caller's session; returns the reconciled outcomes (empty when nothing was pending).
        """
        if not principal.may_erase():
            raise DeletionPropagationAuthorizationError()

        moment = now or datetime.now(UTC)
        stmt = (
            select(ExternalDeletionPropagation)
            .where(ExternalDeletionPropagation.business_id == self._business_id)
            .where(ExternalDeletionPropagation.status == ExternalDeletionStatus.PENDING.value)
            .order_by(ExternalDeletionPropagation.created_at, ExternalDeletionPropagation.id)
        )
        if journal_id is not None:
            stmt = stmt.where(ExternalDeletionPropagation.erasure_journal_id == journal_id)

        pending = list(self._session.scalars(stmt).all())
        result = PropagationResult(journal_id=journal_id or "")
        for row in pending:
            journal = self._session.get(DataErasureJournal, row.erasure_journal_id)
            result.outcomes.append(
                self._dispatch(
                    journal,
                    row.destination,
                    existing=row,
                    actor=principal.as_actor(),
                    action=AUDIT_ACTION_RECONCILE,
                    now=moment,
                    subject_type=row.subject_type,
                    subject_id=row.subject_id,
                )
            )
        return result

    # -- internals ----------------------------------------------------------------------------------

    def _existing(self, journal_id: str, destination: str) -> ExternalDeletionPropagation | None:
        return self._session.scalars(
            select(ExternalDeletionPropagation)
            .where(ExternalDeletionPropagation.business_id == self._business_id)
            .where(ExternalDeletionPropagation.erasure_journal_id == journal_id)
            .where(ExternalDeletionPropagation.destination == destination)
        ).one_or_none()

    @staticmethod
    def _reused_outcome(row: ExternalDeletionPropagation) -> DestinationOutcome:
        return DestinationOutcome(
            destination=row.destination,
            status=row.status,
            propagation_id=row.id,
            acknowledgment_ref=row.acknowledgment_ref,
            detail_code=row.detail_code,
            attempts=row.attempts,
            reused=True,
        )

    def _dispatch(
        self,
        journal: DataErasureJournal | None,
        destination: str,
        *,
        existing: ExternalDeletionPropagation | None,
        actor: AuditActor,
        action: str,
        now: datetime,
        subject_type: str | None = None,
        subject_id: str | None = None,
    ) -> DestinationOutcome:
        """Send one idempotent deletion request and stage/update its row + audit entry.

        A timeout becomes ``PENDING`` (pending reconciliation — never acknowledged); a permanent
        rejection becomes ``FAILED``; an acknowledgment / unsupported response is recorded as such.
        The idempotency key is deterministic, so a reconciliation re-send carries the same key.
        """
        # At least one of ``journal`` / ``existing`` is always present: propagate passes the resolved
        # journal; reconcile passes the existing row (its journal may have been erased since). The
        # journal id comes from whichever is available.
        journal_id = journal.id if journal is not None else None
        if journal_id is None:
            assert existing is not None  # reconcile always carries the existing row
            journal_id = existing.erasure_journal_id
        s_type = (
            subject_type if subject_type is not None else (journal.subject_type if journal else "")
        )
        s_id = subject_id if subject_id is not None else (journal.subject_id if journal else "")
        idem_key = build_idempotency_key(journal_id, destination)

        request = DeletionDispatchRequest(
            business_id=self._business_id,
            erasure_journal_id=journal_id,
            subject_type=s_type,
            subject_id=s_id,
            destination=destination,
            idempotency_key=idem_key,
        )

        status, ack_ref, detail_code = self._invoke(request)

        row = existing
        if row is None:
            row = ExternalDeletionPropagation(
                business_id=self._business_id,
                erasure_journal_id=request.erasure_journal_id,
                subject_type=s_type,
                subject_id=s_id,
                destination=destination,
                idempotency_key=idem_key,
                status=status.value,
                acknowledgment_ref=ack_ref,
                detail_code=detail_code,
                attempts=1,
                requested_by=actor.id,
                requested_by_type=actor.type.value,
                created_at=now,
                updated_at=now,
            )
            self._session.add(row)
        else:
            row.status = status.value
            row.acknowledgment_ref = ack_ref
            row.detail_code = detail_code
            row.attempts = (row.attempts or 0) + 1
            row.updated_at = now

        self._audit(row, action=action, actor=actor)

        return DestinationOutcome(
            destination=destination,
            status=status.value,
            propagation_id=row.id,
            acknowledgment_ref=ack_ref,
            detail_code=detail_code,
            attempts=row.attempts,
        )

    def _invoke(self, request: DeletionDispatchRequest) -> tuple[ExternalDeletionStatus, str, str]:
        """Call the destination contract and map its response/exception to a recorded status.

        A :class:`~app.integration.deletion.DeletionTimeout` is mapped to ``PENDING`` — explicitly NOT
        an acknowledgment — so a timeout is pending reconciliation, never a completed deletion.
        """
        try:
            response = self._client.dispatch(request)
        except DeletionTimeout:
            return ExternalDeletionStatus.PENDING, "", DETAIL_TIMEOUT
        except DeletionRejected as exc:
            return ExternalDeletionStatus.FAILED, "", exc.reason

        if response.outcome is DeletionOutcome.ACKNOWLEDGED:
            return (
                ExternalDeletionStatus.ACKNOWLEDGED,
                response.acknowledgment_ref,
                response.detail_code,
            )
        # UNSUPPORTED — an explicit, recorded "cannot be propagated here".
        return (
            ExternalDeletionStatus.UNSUPPORTED,
            "",
            response.detail_code or "deletion_not_supported",
        )

    def _audit(self, row: ExternalDeletionPropagation, *, action: str, actor: AuditActor) -> None:
        """Stage one atomic DCA-026 audit entry for a dispatch (opaque references only)."""
        AuditSink(self._session, business_id=self._business_id, actor=actor).record(
            action=action,
            reason=(
                f"propagated erasure '{row.erasure_journal_id}' to destination "
                f"'{row.destination}': {row.status}"
            ),
            references={
                "erasureJournalId": row.erasure_journal_id,
                "subjectType": row.subject_type,
                "subjectId": row.subject_id,
                "destination": row.destination,
                "status": row.status,
                "acknowledgmentRef": row.acknowledgment_ref,
                "detailCode": row.detail_code,
                "attempts": row.attempts,
            },
        )
