"""DCA-114 — reconcile a reserved hold against the actual cost.

Key: ``budget-settle`` · source sections 8, 13. Stage: Cost reservations and provider accounting.

One capability: once the ACTUAL cost of a run is known (or known to be nothing, or known to be UNKNOWN),
reconcile the DCA-113 :class:`~app.db.models.BudgetReservation` HOLD against it — exactly once — so the
budget ledger reflects what truly happened instead of the conservative hold taken up front. A
reconciliation is NOT a charge and NOT a send — the DCA has no spend or send path — it only ADJUSTS the
ledger, so the freed headroom becomes available to later reservations.

The single sanctioned writer is :class:`BudgetReconciliationStore`; it stages into the *caller's* session
and never commits — the caller owns the unit of work, so the reservation update and its atomic DCA-026
audit entry commit together. There are exactly three terminal outcomes, driven by what is known:

- **SETTLE** — the actual cost is known. The hold is settled at ``min(actual, reserved)`` and the UNUSED
  difference (``reserved - actual``) is RELEASED back to the budget. The actual cost can never be settled
  ABOVE the reserved amount: a hold was admitted only because it fit under the caps at reserve time, so an
  over-run is CLAMPED to the reserved amount and the excess is recorded as ``overspend_minor`` (flagged
  for review), never retroactively charged past a cap the reserve path already enforced.
- **RELEASE** — the run was abandoned / cost nothing. The entire hold is freed (settled amount 0).
- **MARK UNCERTAIN** — the external result is UNKNOWN (a provider call whose outcome we could not
  confirm). The FULL reserved hold is PRESERVED (it keeps counting against the caps) and is neither
  refunded nor retried, so an unknown result can never blindly free budget or double-spend. Resolving an
  uncertain charge later is a separate, still-disabled capability.

The load-bearing guarantees (the DCA-114 acceptance criteria):

- **Idempotent on the provider-call identity; a duplicate settlement never double-spends.** Each
  reconciliation carries an ``outcome_key`` — the provider-call identity. Re-submitting a reconciliation
  for a reservation that is ALREADY reconciled under the SAME ``outcome_key`` returns the existing result
  and changes nothing (a safe replay); a DIFFERENT key against an already-reconciled hold is refused
  (:data:`REASON_ALREADY_RECONCILED`) with no change. A reservation can therefore be reconciled only once.

- **A missing / foreign / already-settled reservation is refused with NO change.** An unknown id (or
  another tenant's id, which reads exactly like not-found) is refused (:data:`REASON_NOT_FOUND`); an
  already-reconciled hold under a new key is refused (:data:`REASON_ALREADY_RECONCILED`). Each refusal is
  raised before the row is mutated, so it stages no update and no audit and leaves every total unchanged.

- **Serialized (transaction-safe).** Before it decides, the writer takes a ``FOR UPDATE`` lock on the
  reservation row, so two concurrent reconciliations of the same hold are serialized by PostgreSQL: the
  first wins and the second sees the committed terminal state (an idempotent replay under the same key, a
  refusal under a different one). A duplicate settlement can therefore never apply twice even under a race.
  (The lock is a no-op on SQLite; the serialization guarantee is a PostgreSQL property proven in the
  Postgres-gated test.)

- **Authorized; a denial changes nothing.** Authority is checked FIRST: a caller whose roles lack
  :attr:`~app.identity.permissions.Scope.BUDGET_MANAGE` (an administrator scope — reconciling spend
  against the budget is a budget-control action, exactly like reserving it) is refused with
  :class:`ReconciliationAuthorizationError` before any state is read or written.

Keeping future/paid capabilities disabled: reconciliation only moves a hold to a terminal ledger state —
it never spends, charges, bills, sends or invokes a model/provider (there is no such path here).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.sink import ActorType, AuditActor, AuditSink
from app.db.models import BudgetReservation, ReservationStatus
from app.identity.permissions import Scope, scopes_for_business_roles

# The audited action code for reconciling a reservation (a controlled code, like the DCA-113 reserve code).
AUDIT_ACTION_RECONCILE = "budget.reservation.reconcile"

# Stable refusal codes. Each is raised BEFORE the row is mutated, so a refusal changes nothing.
REASON_NOT_AUTHORIZED = "reconciliation_not_authorized"
REASON_MISSING_OUTCOME_KEY = "reconciliation_missing_outcome_key"
REASON_MISSING_RESERVATION = "reconciliation_missing_reservation_id"
REASON_INVALID_ACTUAL = "reconciliation_invalid_actual_amount"
REASON_NOT_FOUND = "reconciliation_reservation_not_found"
REASON_ALREADY_RECONCILED = "reconciliation_already_reconciled"


class ReconcileOutcome(str, Enum):
    """What is known about a run's actual cost — the three terminal reconciliation outcomes.

    ``SETTLE`` needs the known actual cost; ``RELEASE`` and ``MARK_UNCERTAIN`` ignore any supplied amount
    (a release costs nothing; an uncertain result HAS no known amount and is never coerced to one)."""

    SETTLE = "settle"
    RELEASE = "release"
    MARK_UNCERTAIN = "mark_uncertain"


# Which reservation status each outcome drives the hold to.
_OUTCOME_STATUS = {
    ReconcileOutcome.SETTLE: ReservationStatus.SETTLED,
    ReconcileOutcome.RELEASE: ReservationStatus.RELEASED,
    ReconcileOutcome.MARK_UNCERTAIN: ReservationStatus.UNCERTAIN,
}


class ReconciliationError(Exception):
    """A reconciliation was refused with a stable ``code``. Raised before the row is mutated, so a
    refusal leaves the reservation (and every budget total) exactly as it was. ``detail`` names the
    offending value when useful."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class ReconciliationAuthorizationError(ReconciliationError):
    """The caller's roles do not grant :attr:`~app.identity.permissions.Scope.BUDGET_MANAGE`.

    A distinct type so an HTTP boundary (where one exists) can map it to a 403 while the other
    :class:`ReconciliationError` codes map to a 404/409/422. Raised FIRST — before anything is read or
    written — so an unauthorized attempt leaves no trace. ``code`` is always :data:`REASON_NOT_AUTHORIZED`.
    """

    def __init__(self, message: str) -> None:
        super().__init__(REASON_NOT_AUTHORIZED, message)


@dataclass(frozen=True)
class ReconciliationActor:
    """The business principal reconciling a reservation.

    ``subject`` is an opaque actor id (never a name/email) recorded on the reservation and its atomic
    audit entry. ``roles`` are the caller's verified business-plane membership roles (never a bare token
    claim). :meth:`may_reconcile` grants the action only when those roles yield
    :attr:`~app.identity.permissions.Scope.BUDGET_MANAGE` — an ordinary acting user and a read-only
    viewer do not, and a platform operator holds no business scope for the tenant."""

    subject: str
    roles: tuple[str, ...] = ()
    actor_type: str = "user"

    def may_reconcile(self) -> bool:
        return Scope.BUDGET_MANAGE in scopes_for_business_roles(self.roles)

    def as_actor(self) -> AuditActor:
        return AuditActor(id=self.subject, type=ActorType(self.actor_type))


@dataclass
class ReconciliationRequest:
    """One reconciliation as supplied by the caller.

    ``reservation_id`` names the DCA-113 hold to reconcile (within this business). ``outcome_key`` is the
    provider-call identity — the per-reservation idempotency token (a retry with the same key is a replay,
    never a second adjustment). ``outcome`` selects SETTLE / RELEASE / MARK_UNCERTAIN. ``actual_minor`` is
    the known actual cost in INR **minor units (paise)** — REQUIRED (and non-negative) for SETTLE, ignored
    for RELEASE and MARK_UNCERTAIN (a release costs nothing; an uncertain result has no known amount)."""

    reservation_id: str
    outcome_key: str
    outcome: ReconcileOutcome
    actual_minor: int | None = None


@dataclass(frozen=True)
class ReconciliationResult:
    """The inspectable outcome of a reconciliation (or of an idempotent replay of a prior one).

    ``replayed`` is ``True`` when the reservation was ALREADY reconciled under this ``outcome_key`` and the
    stored terminal state was returned unchanged (a safe duplicate settlement). ``released_minor`` is the
    headroom freed back to the budget; ``settled_minor`` is what the hold now counts toward the caps (0 for
    a release, ``None`` for an uncertain hold whose full amount is preserved); ``overspend_minor`` is the
    flagged, clamped over-run (0 unless the actual cost exceeded the reserved amount)."""

    reservation_id: str
    outcome: str
    status: str
    reserved_minor: int
    actual_minor: int | None
    settled_minor: int | None
    released_minor: int
    overspend_minor: int
    reconciliation_key: str
    replayed: bool


class BudgetReconciliationStore:
    """Tenant-scoped reconciler for budget reservations, bound to one business.

    Construct with the active session and the verified business scope (a route resolves it from the
    membership-verified tenant context). Every query is confined to that business — there is no argument
    to widen the scope, so another tenant's reservation id reads exactly like not-found. The store stages
    into the caller's session and never commits; the caller owns the unit of work (and, on PostgreSQL, the
    open transaction that the serializing ``FOR UPDATE`` lock lives in). Reconciling adjusts the ledger and
    spends/sends nothing.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    def reconcile(
        self,
        request: ReconciliationRequest,
        actor: ReconciliationActor,
        *,
        now: datetime | None = None,
    ) -> ReconciliationResult:
        """Authorize, gate and atomically reconcile ONE reservation; return the (uncommitted) result.

        Order (each gate leaves NO state behind when it refuses):

        1. **Authority FIRST.** A caller lacking
           :attr:`~app.identity.permissions.Scope.BUDGET_MANAGE` is refused
           (:class:`ReconciliationAuthorizationError`) before any state is read or written.
        2. **Shape.** An empty ``reservation_id`` (:data:`REASON_MISSING_RESERVATION`), an empty
           ``outcome_key`` (:data:`REASON_MISSING_OUTCOME_KEY`) or — for a SETTLE — a missing/negative
           ``actual_minor`` (:data:`REASON_INVALID_ACTUAL`) raises before anything is read.
        3. **Load + serialize.** The reservation is fetched WITHIN this business (an unknown/foreign id →
           :data:`REASON_NOT_FOUND`) and re-read under a ``FOR UPDATE`` lock so concurrent reconciliations
           of the same hold serialize.
        4. **Idempotent replay / reconciled-once.** If the hold is already reconciled: the SAME
           ``outcome_key`` returns the existing result unchanged (a safe duplicate settlement); a DIFFERENT
           key is refused (:data:`REASON_ALREADY_RECONCILED`) with no change.
        5. **Apply.** An ACTIVE hold is driven to its terminal state (settle at the clamped actual with the
           unused difference released, release the whole hold, or preserve the full hold as uncertain) and
           an atomic DCA-026 audit entry (opaque references only) is staged in the SAME unit of work.

        The caller owns the transaction. Reconciling adjusts the ledger and spends/sends nothing.
        """
        moment = now or datetime.now(UTC)

        # 1) Authority FIRST — an unauthorized caller is refused with no state touched.
        if not actor.may_reconcile():
            raise ReconciliationAuthorizationError(
                "reconciling spend requires a business administrator holding "
                f"'{Scope.BUDGET_MANAGE.value}'; an ordinary acting user, a read-only viewer or a "
                "platform operator (holding no business scope) cannot perform it"
            )

        # 2) Shape — before any state is read.
        reservation_id = (request.reservation_id or "").strip()
        if not reservation_id:
            raise ReconciliationError(REASON_MISSING_RESERVATION, "a reservation_id is required")
        outcome_key = (request.outcome_key or "").strip()
        if not outcome_key:
            raise ReconciliationError(
                REASON_MISSING_OUTCOME_KEY,
                "an outcome_key (the provider-call identity) is required for idempotency",
            )
        actual: int | None = None
        if request.outcome is ReconcileOutcome.SETTLE:
            if request.actual_minor is None:
                raise ReconciliationError(
                    REASON_INVALID_ACTUAL,
                    "settling requires the known actual cost in INR minor units",
                )
            actual = int(request.actual_minor)
            if actual < 0:
                raise ReconciliationError(
                    REASON_INVALID_ACTUAL,
                    f"actual cost must be a non-negative number of INR minor units (got {actual})",
                    detail=str(actual),
                )

        # 3) Load WITHIN this business (foreign/unknown id → not-found), then re-read under FOR UPDATE.
        if self._get(reservation_id) is None:
            raise ReconciliationError(
                REASON_NOT_FOUND,
                "no reservation with that id exists for this business",
                detail=reservation_id,
            )
        reservation = self._lock(reservation_id)
        if reservation is None:  # pragma: no cover - vanished between the read and the lock
            raise ReconciliationError(
                REASON_NOT_FOUND,
                "no reservation with that id exists for this business",
                detail=reservation_id,
            )

        # 4) Idempotent replay / reconciled-once — an already-terminal hold is never adjusted twice.
        if reservation.status in ReservationStatus.reconciled_values():
            if reservation.reconciliation_key == outcome_key:
                return self._result(reservation, replayed=True)
            raise ReconciliationError(
                REASON_ALREADY_RECONCILED,
                f"reservation {reservation_id} is already reconciled ({reservation.status}) under a "
                "different provider-call identity; it can be reconciled only once",
                detail=reservation.status,
            )

        # 5) Apply the terminal outcome to the ACTIVE hold.
        reserved = int(reservation.amount_minor)
        if request.outcome is ReconcileOutcome.SETTLE:
            assert actual is not None  # guaranteed by the shape gate above
            settled = min(actual, reserved)  # never settle above the cap-admitted hold
            overspend = max(0, actual - reserved)
            reservation.actual_minor = actual
            reservation.settled_amount_minor = settled
            reservation.overspend_minor = overspend
        elif request.outcome is ReconcileOutcome.RELEASE:
            reservation.actual_minor = 0
            reservation.settled_amount_minor = 0
            reservation.overspend_minor = 0
        else:  # MARK_UNCERTAIN — preserve the full hold, record no actual, refund nothing.
            reservation.actual_minor = None
            reservation.settled_amount_minor = None
            reservation.overspend_minor = 0

        reservation.status = _OUTCOME_STATUS[request.outcome].value
        reservation.reconciliation_key = outcome_key
        reservation.reconciled_at = moment
        reservation.reconciled_by_actor_id = actor.subject
        reservation.reconciled_by_actor_type = actor.actor_type

        result = self._result(reservation, replayed=False)

        # Atomic audit (SEC-001/BR-122): staged into the caller's session so it commits with the update.
        # References stay OPAQUE — ids / keys / integer amounts / status; never a raw contact value or
        # secret. Reconciling adjusts the ledger and spends/sends nothing.
        AuditSink(self._session, business_id=self._business_id, actor=actor.as_actor()).record(
            action=AUDIT_ACTION_RECONCILE,
            reason=(
                f"reconciled reservation as {reservation.status}: reserved {reserved}, "
                f"settled {result.settled_minor}, released {result.released_minor}, "
                f"overspend {result.overspend_minor}"
            ),
            references={
                "reservationId": reservation.id,
                "outcomeKey": outcome_key,
                "outcome": request.outcome.value,
                "status": reservation.status,
                "reservedMinor": reserved,
                "actualMinor": result.actual_minor,
                "settledMinor": result.settled_minor,
                "releasedMinor": result.released_minor,
                "overspendMinor": result.overspend_minor,
                "budgetVersion": reservation.budget_version,
                "dayKey": reservation.day_key,
                "monthKey": reservation.month_key,
            },
        )
        return result

    # -- internals ----------------------------------------------------------------------------------

    def _get(self, reservation_id: str) -> BudgetReservation | None:
        """Fetch one reservation by id WITHIN this business (a foreign/unknown id reads as ``None``)."""
        stmt = (
            select(BudgetReservation)
            .where(BudgetReservation.business_id == self._business_id)
            .where(BudgetReservation.id == reservation_id)
        )
        return self._session.scalars(stmt).one_or_none()

    def _lock(self, reservation_id: str) -> BudgetReservation | None:
        """Re-read the reservation ``FOR UPDATE`` so concurrent reconciliations of the same hold serialize.

        On PostgreSQL the row lock makes the second concurrent reconciliation block until the first
        commits, then see its committed terminal state — so a duplicate settlement is an idempotent replay
        (same key) or a refusal (different key), never a second adjustment. On SQLite ``with_for_update``
        is silently ignored (no row locking); the idempotency path still holds via the stored key."""
        stmt = (
            select(BudgetReservation)
            .where(BudgetReservation.business_id == self._business_id)
            .where(BudgetReservation.id == reservation_id)
            .with_for_update()
        )
        return self._session.scalars(stmt).one_or_none()

    def _result(self, reservation: BudgetReservation, *, replayed: bool) -> ReconciliationResult:
        """Build the inspectable result from a reservation's (terminal) state — the released headroom is
        ``reserved - effective`` (what the hold no longer counts toward the caps)."""
        reserved = int(reservation.amount_minor)
        if reservation.status == ReservationStatus.RELEASED.value:
            effective = 0
        elif reservation.status == ReservationStatus.SETTLED.value:
            effective = int(
                reservation.settled_amount_minor
                if reservation.settled_amount_minor is not None
                else reserved
            )
        else:  # ACTIVE / UNCERTAIN — the full reserved hold is still counted.
            effective = reserved
        settled = None if reservation.status == ReservationStatus.UNCERTAIN.value else effective
        return ReconciliationResult(
            reservation_id=reservation.id,
            outcome=reservation.status,
            status=reservation.status,
            reserved_minor=reserved,
            actual_minor=reservation.actual_minor,
            settled_minor=settled,
            released_minor=max(0, reserved - effective),
            overspend_minor=int(reservation.overspend_minor or 0),
            reconciliation_key=reservation.reconciliation_key or "",
            replayed=replayed,
        )
