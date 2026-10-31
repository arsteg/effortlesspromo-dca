"""DCA-116 — reconcile an ambiguous (UNCERTAIN) provider outcome.

Key: ``provider-reconcile`` · source sections 8, 13. Stage: Cost reservations and provider accounting.

One capability: resolve a previously AMBIGUOUS provider outcome — a DCA-115
:class:`~app.db.models.ProviderCallRecord` left ``UNCERTAIN`` (dispatched then unconfirmed, a billable
cost MAY exist) together with the DCA-113 :class:`~app.db.models.BudgetReservation` hold DCA-114 preserved
as ``UNCERTAIN`` — once the truth is known, so the ledger stops carrying an unknown indefinitely. This is
the capability DCA-114 deferred ("resolving an uncertain charge later is a separate, still-disabled
capability"): now enabled, but strictly as a LEDGER/RECORD adjustment — it never performs, retries, spends,
charges, bills, sends or invokes a model/provider (the DCA has no such path).

How ambiguity is resolved WITHOUT claiming certainty from nothing (the story's one task — "query supported
provider status/receipts or route unsupported cases to review before retrying chargeable work"):

- **Supported provider** (one whose status/receipt can be consulted — a synthetic allowlist here;
  wiring a real provider's receipt lookup is a later, still-disabled capability). The caller supplies the
  receipt/status EVIDENCE:

  - *It DID happen* → the provider call is resolved ``CONFIRMED`` and the tied UNCERTAIN hold is SETTLED at
    the actual cost (the unused difference released), exactly like a DCA-114 settle.
  - *It did NOT happen* → the provider call is resolved ``VOID`` and the tied UNCERTAIN hold is RELEASED in
    full, freeing the preserved headroom back to the budget.

- **Unsupported provider** (no receipt/status lookup) → the outcome CANNOT be confirmed, so it is routed to
  ``NEEDS_REVIEW``: nothing is settled, released or retried. A human resolves it later. This is the
  load-bearing acceptance guarantee — *reconciliation never claims certainty without evidence, and there is
  no uncontrolled repeat charge.*

The single sanctioned writer is :class:`UncertainOutcomeResolver`; it stages into the *caller's* session
and never commits — the caller owns the unit of work, so the provider-call record, the reservation update
and their atomic DCA-026 audit entry commit together. It mirrors the DCA-113/114/115 store conventions
exactly.

The load-bearing guarantees (the DCA-116 acceptance criteria):

- **Only an ambiguous outcome is resolvable; a resolution is recorded as a SEPARATE dimension.** The
  resolver acts ONLY on a record whose DCA-115 ``status`` is ``UNCERTAIN`` (:data:`REASON_NOT_UNCERTAIN`
  otherwise). The terminal ``UNCERTAIN`` lifecycle status is NEVER reopened — the confirmed truth is
  recorded in the DCA-116 ``resolution`` dimension and the tied hold is settled/released. A succeeded /
  failed / pending / in-flight call carries no ambiguity to reconcile.

- **Certainty needs evidence; no uncontrolled repeat charge.** A supported provider is resolved only when
  the receipt/status evidence says whether it happened (:data:`REASON_NO_EVIDENCE` otherwise); an
  unsupported provider is routed to review, never auto-confirmed and never retried.

- **Idempotent; a conflicting resolution is refused with NO change.** Each resolution carries a
  ``resolution_key`` (the receipt/evidence identity). Re-resolving under the SAME key returns the existing
  outcome unchanged (a safe replay); a DIFFERENT key against an already-FINAL resolution (confirmed/void)
  is refused (:data:`REASON_ALREADY_RESOLVED`) with no change. ``NEEDS_REVIEW`` is deliberately NOT final,
  so a reviewed call may still be resolved later with real evidence.

- **Serialized (transaction-safe).** Both the provider-call record and (when present) the tied reservation
  are re-read ``FOR UPDATE`` before the decision, so two concurrent resolutions of the same ambiguity
  serialize on PostgreSQL: the first wins and the rest see the committed final state (an idempotent replay
  under the same key, a refusal under a different one). A duplicate settlement can never apply twice.
  (The lock is a no-op on SQLite; the serialization guarantee is a PostgreSQL property proven in the
  Postgres-gated test.)

- **Authorized; a denial changes nothing.** Authority is checked FIRST: a caller whose roles lack
  :attr:`~app.identity.permissions.Scope.BUDGET_MANAGE` (reconciling an uncertain charge is a
  budget/accounting-control action, exactly like reserving or reconciling spend) is refused with
  :class:`OutcomeResolutionAuthorizationError` before any state is read or written.

Only opaque references, codes and integer amounts are persisted — never a raw request/response payload.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.sink import ActorType, AuditActor, AuditSink
from app.cost.provider_call import DEFAULT_PROVIDER
from app.db.models import (
    BudgetReservation,
    ProviderCallRecord,
    ProviderCallResolution,
    ProviderCallStatus,
    ReservationStatus,
)
from app.identity.permissions import Scope, scopes_for_business_roles

# The providers whose status/receipt can be consulted to CONFIRM an ambiguous outcome. A synthetic
# allowlist (data, not a live integration): the platform/synthetic bucket is reconcilable; any other
# provider is routed to human review until a later, still-disabled capability wires its real receipt
# lookup. The default bucket matches the DCA-113/115 default provider.
DEFAULT_RECONCILABLE_PROVIDERS: frozenset[str] = frozenset({DEFAULT_PROVIDER})

# The audited action code for resolving an ambiguous provider outcome (a controlled code, like the
# DCA-113/114/115 codes).
AUDIT_ACTION_RESOLVE = "provider.outcome.resolve"

# Stable refusal codes. Each is raised BEFORE the session is mutated, so a refusal changes nothing.
REASON_NOT_AUTHORIZED = "provider_outcome_not_authorized"
REASON_MISSING_KEY = "provider_outcome_missing_call_key"
REASON_MISSING_RESOLUTION_KEY = "provider_outcome_missing_resolution_key"
REASON_NOT_FOUND = "provider_outcome_not_found"
REASON_NOT_UNCERTAIN = "provider_outcome_not_uncertain"
REASON_NO_EVIDENCE = "provider_outcome_no_evidence"
REASON_INVALID_ACTUAL = "provider_outcome_invalid_actual_amount"
REASON_ALREADY_RESOLVED = "provider_outcome_already_resolved"


class OutcomeResolutionError(Exception):
    """An ambiguous-outcome resolution was refused with a stable ``code``. Raised before any row is
    mutated, so a refusal leaves the provider-call record and the tied reservation (and every budget
    total) exactly as they were. ``detail`` names the offending value when useful."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class OutcomeResolutionAuthorizationError(OutcomeResolutionError):
    """The caller's roles do not grant :attr:`~app.identity.permissions.Scope.BUDGET_MANAGE`.

    A distinct type so an HTTP boundary (where one exists) can map it to a 403 while the other
    :class:`OutcomeResolutionError` codes map to a 404/409/422. Raised FIRST — before anything is read or
    written — so an unauthorized attempt leaves no trace. ``code`` is always :data:`REASON_NOT_AUTHORIZED`.
    """

    def __init__(self, message: str) -> None:
        super().__init__(REASON_NOT_AUTHORIZED, message)


@dataclass(frozen=True)
class OutcomeResolutionActor:
    """The business principal reconciling an ambiguous provider outcome.

    ``subject`` is an opaque actor id (never a name/email) recorded on the record / reservation and their
    atomic audit entry. ``roles`` are the caller's verified business-plane membership roles (never a bare
    token claim). :meth:`may_resolve` grants the action only when those roles yield
    :attr:`~app.identity.permissions.Scope.BUDGET_MANAGE` — an ordinary acting user and a read-only viewer
    do not, and a platform operator holds no business scope for the tenant."""

    subject: str
    roles: tuple[str, ...] = ()
    actor_type: str = "user"

    def may_resolve(self) -> bool:
        return Scope.BUDGET_MANAGE in scopes_for_business_roles(self.roles)

    def as_actor(self) -> AuditActor:
        return AuditActor(id=self.subject, type=ActorType(self.actor_type))


@dataclass
class OutcomeEvidence:
    """The receipt/status evidence for ONE ambiguous-outcome resolution, as supplied by the caller.

    ``call_key`` identifies the UNCERTAIN provider-call record WITHIN this business. ``resolution_key`` is
    the receipt/evidence identity — the per-record idempotency token (a retry with the same key is a
    replay, never a second adjustment). ``happened`` is the confirmed truth from a SUPPORTED provider's
    receipt: ``True`` = the call DID happen (settle the tied hold at ``actual_minor``), ``False`` = it did
    NOT happen (release the tied hold), ``None`` = no evidence supplied (refused for a supported provider;
    ignored when the provider is unsupported and routed to review). ``actual_minor`` is the known actual
    cost in INR **minor units (paise)** — REQUIRED (and non-negative) when ``happened`` is ``True`` AND a
    tied UNCERTAIN hold must be settled. ``outcome_code`` is an opaque receipt/evidence code recorded on
    the resolution."""

    call_key: str
    resolution_key: str
    happened: bool | None = None
    actual_minor: int | None = None
    outcome_code: str | None = None


@dataclass(frozen=True)
class OutcomeResolutionResult:
    """The inspectable outcome of a resolution (or of an idempotent replay of a prior one).

    ``supported`` reports whether the provider's receipt could be consulted. ``routed_to_review`` is
    ``True`` when an unsupported provider was deferred to human review (nothing settled/released/retried).
    ``resolution`` is the DCA-116 :class:`~app.db.models.ProviderCallResolution` value now recorded.
    ``call_status`` is the (unchanged) DCA-115 lifecycle status — still ``uncertain``. ``settled_minor`` is
    what the tied hold now counts toward the caps (``None`` when no hold was settled), ``released_minor``
    the headroom freed back to the budget, ``overspend_minor`` the flagged clamped over-run. ``replayed``
    is ``True`` when the prior stored resolution was returned unchanged."""

    call_key: str
    provider: str
    supported: bool
    resolution: str
    call_status: str
    reservation_id: str | None
    reservation_status: str | None
    settled_minor: int | None
    released_minor: int
    overspend_minor: int
    resolution_key: str
    replayed: bool
    routed_to_review: bool


class UncertainOutcomeResolver:
    """Tenant-scoped resolver for ambiguous (UNCERTAIN) provider outcomes, bound to one business.

    Construct with the active session, the verified business scope (a route resolves it from the
    membership-verified tenant context) and — optionally — the set of providers whose receipts can be
    consulted (defaults to :data:`DEFAULT_RECONCILABLE_PROVIDERS`). Every query is confined to that
    business — there is no argument to widen the scope, so another tenant's call key / reservation id reads
    exactly like not-found. The store stages into the caller's session and never commits; the caller owns
    the unit of work (and, on PostgreSQL, the open transaction that the serializing ``FOR UPDATE`` locks
    live in). Resolving adjusts the record/ledger and performs/retries/spends/sends nothing.
    """

    def __init__(
        self,
        session: Session,
        business_id: str,
        *,
        reconcilable_providers: frozenset[str] | None = None,
    ) -> None:
        self._session = session
        self._business_id = business_id
        self._reconcilable = (
            reconcilable_providers
            if reconcilable_providers is not None
            else DEFAULT_RECONCILABLE_PROVIDERS
        )

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def by_key(self, call_key: str) -> ProviderCallRecord | None:
        """The provider-call record for one call key within this business, or ``None``. A key another
        business recorded is invisible here (the query is business-scoped)."""
        key = (call_key or "").strip()
        if not key:
            return None
        stmt = (
            select(ProviderCallRecord)
            .where(ProviderCallRecord.business_id == self._business_id)
            .where(ProviderCallRecord.call_key == key)
        )
        return self._session.scalars(stmt).one_or_none()

    def supports(self, provider: str) -> bool:
        """Whether ``provider``'s status/receipt can be consulted to CONFIRM an outcome (DCA-116)."""
        return (provider or "").strip() in self._reconcilable

    # -- the one write path -------------------------------------------------------------------------

    def resolve(
        self,
        evidence: OutcomeEvidence,
        actor: OutcomeResolutionActor,
        *,
        now: datetime | None = None,
    ) -> OutcomeResolutionResult:
        """Authorize, gate and atomically reconcile ONE ambiguous outcome; return the (uncommitted) result.

        Order (each gate leaves NO state behind when it refuses):

        1. **Authority FIRST.** A caller lacking :attr:`~app.identity.permissions.Scope.BUDGET_MANAGE` is
           refused (:class:`OutcomeResolutionAuthorizationError`) before any state is read or written.
        2. **Shape.** An empty ``call_key`` (:data:`REASON_MISSING_KEY`) or empty ``resolution_key``
           (:data:`REASON_MISSING_RESOLUTION_KEY`) raises before anything is read.
        3. **Load + serialize.** The record is fetched WITHIN this business (unknown/foreign key →
           :data:`REASON_NOT_FOUND`) and re-read ``FOR UPDATE``.
        4. **Ambiguity gate.** The record must be ``UNCERTAIN`` (:data:`REASON_NOT_UNCERTAIN` otherwise) —
           only an ambiguous outcome carries anything to reconcile.
        5. **Idempotency / already-final.** If the resolution is already FINAL: the SAME ``resolution_key``
           replays the stored outcome unchanged; a DIFFERENT key is refused
           (:data:`REASON_ALREADY_RESOLVED`). ``NEEDS_REVIEW`` is not final and may be re-evaluated.
        6. **Supported vs review.** An unsupported provider is routed to ``NEEDS_REVIEW`` (nothing settled /
           released / retried). A supported provider needs the receipt evidence (:data:`REASON_NO_EVIDENCE`
           otherwise): confirmed-happened settles the tied UNCERTAIN hold at the actual cost (a settle needs
           a non-negative ``actual_minor`` — :data:`REASON_INVALID_ACTUAL`), confirmed-not-happened releases
           it in full.
        7. **Apply.** The ``resolution`` dimension is recorded (the DCA-115 ``UNCERTAIN`` status is never
           reopened), the tied hold is settled/released, and an atomic DCA-026 audit entry (opaque
           references only) is staged in the SAME unit of work.

        The caller owns the transaction. Resolving adjusts the record/ledger and performs/retries/spends/
        sends nothing.
        """
        moment = now or datetime.now(UTC)

        # 1) Authority FIRST — an unauthorized caller is refused with no state touched.
        if not actor.may_resolve():
            raise OutcomeResolutionAuthorizationError(
                "reconciling an ambiguous provider outcome requires a business administrator holding "
                f"'{Scope.BUDGET_MANAGE.value}'; an ordinary acting user, a read-only viewer or a "
                "platform operator (holding no business scope) cannot perform it"
            )

        # 2) Shape — before any state is read.
        key = (evidence.call_key or "").strip()
        if not key:
            raise OutcomeResolutionError(REASON_MISSING_KEY, "a call_key is required")
        resolution_key = (evidence.resolution_key or "").strip()
        if not resolution_key:
            raise OutcomeResolutionError(
                REASON_MISSING_RESOLUTION_KEY,
                "a resolution_key (the receipt/evidence identity) is required for idempotency",
            )

        # 3) Load WITHIN this business (foreign/unknown key → not-found), then re-read under FOR UPDATE.
        if self.by_key(key) is None:
            raise OutcomeResolutionError(
                REASON_NOT_FOUND,
                "no provider-call record with that call key exists for this business",
                detail=key,
            )
        record = self._lock_record(key)
        if record is None:  # pragma: no cover - vanished between the read and the lock
            raise OutcomeResolutionError(
                REASON_NOT_FOUND,
                "no provider-call record with that call key exists for this business",
                detail=key,
            )

        # 4) Ambiguity gate — only an UNCERTAIN outcome carries anything to reconcile.
        if record.status != ProviderCallStatus.UNCERTAIN.value:
            raise OutcomeResolutionError(
                REASON_NOT_UNCERTAIN,
                f"provider call is '{record.status}', not an ambiguous (uncertain) outcome; only an "
                "uncertain outcome can be reconciled",
                detail=record.status,
            )

        # 5) Idempotency / already-final — a FINAL resolution is never changed; review is not final.
        if record.resolution in ProviderCallResolution.final_values():
            if record.resolution_key == resolution_key:
                return self._result(record, replayed=True)
            raise OutcomeResolutionError(
                REASON_ALREADY_RESOLVED,
                f"the ambiguous outcome for call '{key}' is already resolved "
                f"({record.resolution}) under a different evidence identity; it is reconciled only once",
                detail=record.resolution,
            )

        provider = record.provider
        supported = self.supports(provider)

        # 6a) Unsupported provider → route to human review; settle/release/retry NOTHING.
        if not supported:
            # An already-review record under the same key is a clean replay (no second audit).
            if (
                record.resolution == ProviderCallResolution.NEEDS_REVIEW.value
                and record.resolution_key == resolution_key
            ):
                return self._result(record, replayed=True)
            self._apply_resolution(
                record,
                ProviderCallResolution.NEEDS_REVIEW,
                resolution_key,
                (evidence.outcome_code or "").strip() or "unsupported_provider",
                actor,
                moment,
            )
            self._audit(record, None, actor, provider, supported=False)
            return self._result(record, replayed=False)

        # 6b) Supported provider → certainty needs evidence.
        if evidence.happened is None:
            raise OutcomeResolutionError(
                REASON_NO_EVIDENCE,
                "a supported provider outcome can be confirmed only with receipt/status evidence "
                "(happened=True/False); none was supplied",
            )

        reservation = self._lock_reservation(record.reservation_id)
        tied = reservation is not None and reservation.status == ReservationStatus.UNCERTAIN.value

        if evidence.happened:
            # It DID happen → confirm the cost and SETTLE the tied uncertain hold at the actual cost.
            if tied:
                assert reservation is not None  # narrowed by ``tied``
                if evidence.actual_minor is None:
                    raise OutcomeResolutionError(
                        REASON_INVALID_ACTUAL,
                        "settling a confirmed uncertain hold requires the known actual cost in INR "
                        "minor units",
                    )
                actual = int(evidence.actual_minor)
                if actual < 0:
                    raise OutcomeResolutionError(
                        REASON_INVALID_ACTUAL,
                        f"actual cost must be a non-negative number of INR minor units (got {actual})",
                        detail=str(actual),
                    )
                self._settle_reservation(reservation, actual, resolution_key, actor, moment)
            resolution = ProviderCallResolution.CONFIRMED
            default_code = "receipt_confirmed"
        else:
            # It did NOT happen → void the cost and RELEASE the tied uncertain hold in full.
            if tied:
                assert reservation is not None  # narrowed by ``tied``
                self._release_reservation(reservation, resolution_key, actor, moment)
            resolution = ProviderCallResolution.VOID
            default_code = "receipt_absent"

        self._apply_resolution(
            record,
            resolution,
            resolution_key,
            (evidence.outcome_code or "").strip() or default_code,
            actor,
            moment,
        )
        self._audit(record, reservation if tied else None, actor, provider, supported=True)
        return self._result(record, replayed=False)

    # -- internals ----------------------------------------------------------------------------------

    def _lock_record(self, call_key: str) -> ProviderCallRecord | None:
        """Re-read the provider-call record ``FOR UPDATE`` so concurrent resolutions serialize.

        On PostgreSQL the row lock makes the second concurrent resolution block until the first commits,
        then see its committed final state — so a duplicate is an idempotent replay (same key) or a
        refusal (different key), never a second adjustment. On SQLite ``with_for_update`` is silently
        ignored (no row locking); the idempotency path still holds via the stored resolution/key."""
        stmt = (
            select(ProviderCallRecord)
            .where(ProviderCallRecord.business_id == self._business_id)
            .where(ProviderCallRecord.call_key == call_key)
            .with_for_update()
        )
        return self._session.scalars(stmt).one_or_none()

    def _lock_reservation(self, reservation_id: str | None) -> BudgetReservation | None:
        """Re-read the tied reservation ``FOR UPDATE`` WITHIN this business, or ``None`` when unlinked /
        foreign / unknown (a cross-tenant id reads exactly like not-found)."""
        if not reservation_id:
            return None
        stmt = (
            select(BudgetReservation)
            .where(BudgetReservation.business_id == self._business_id)
            .where(BudgetReservation.id == reservation_id)
            .with_for_update()
        )
        return self._session.scalars(stmt).one_or_none()

    def _apply_resolution(
        self,
        record: ProviderCallRecord,
        resolution: ProviderCallResolution,
        resolution_key: str,
        outcome_code: str,
        actor: OutcomeResolutionActor,
        moment: datetime,
    ) -> None:
        """Record the DCA-116 resolution dimension on the record (the UNCERTAIN status is never reopened)."""
        record.resolution = resolution.value
        record.resolution_key = resolution_key
        record.resolution_outcome_code = outcome_code
        record.resolution_actor_id = actor.subject
        record.resolution_actor_type = actor.actor_type
        record.resolution_at = moment
        record.updated_at = moment

    def _settle_reservation(
        self,
        reservation: BudgetReservation,
        actual: int,
        resolution_key: str,
        actor: OutcomeResolutionActor,
        moment: datetime,
    ) -> None:
        """Settle the tied UNCERTAIN hold at the clamped actual cost (unused difference released, over-run
        clamped and flagged — never charged past the cap), exactly like a DCA-114 settle."""
        reserved = int(reservation.amount_minor)
        settled = min(actual, reserved)
        reservation.actual_minor = actual
        reservation.settled_amount_minor = settled
        reservation.overspend_minor = max(0, actual - reserved)
        reservation.status = ReservationStatus.SETTLED.value
        reservation.reconciliation_key = resolution_key
        reservation.reconciled_at = moment
        reservation.reconciled_by_actor_id = actor.subject
        reservation.reconciled_by_actor_type = actor.actor_type

    def _release_reservation(
        self,
        reservation: BudgetReservation,
        resolution_key: str,
        actor: OutcomeResolutionActor,
        moment: datetime,
    ) -> None:
        """Release the tied UNCERTAIN hold in full (counts 0), freeing the preserved headroom."""
        reservation.actual_minor = 0
        reservation.settled_amount_minor = 0
        reservation.overspend_minor = 0
        reservation.status = ReservationStatus.RELEASED.value
        reservation.reconciliation_key = resolution_key
        reservation.reconciled_at = moment
        reservation.reconciled_by_actor_id = actor.subject
        reservation.reconciled_by_actor_type = actor.actor_type

    def _audit(
        self,
        record: ProviderCallRecord,
        reservation: BudgetReservation | None,
        actor: OutcomeResolutionActor,
        provider: str,
        *,
        supported: bool,
    ) -> None:
        """Stage the atomic DCA-026 audit entry (opaque references only) in the caller's session."""
        AuditSink(self._session, business_id=self._business_id, actor=actor.as_actor()).record(
            action=AUDIT_ACTION_RESOLVE,
            reason=(
                f"resolved ambiguous provider outcome as {record.resolution} "
                f"(provider supported={supported})"
            ),
            references={
                "providerCallId": record.id,
                "callKey": record.call_key,
                "provider": provider,
                "supported": supported,
                "resolution": record.resolution,
                "resolutionKey": record.resolution_key,
                "outcomeCode": record.resolution_outcome_code,
                "callStatus": record.status,
                "reservationId": reservation.id if reservation is not None else None,
                "reservationStatus": reservation.status if reservation is not None else None,
                "settledMinor": (
                    reservation.settled_amount_minor if reservation is not None else None
                ),
            },
        )

    def _result(self, record: ProviderCallRecord, *, replayed: bool) -> OutcomeResolutionResult:
        """Build the inspectable result from a record's (resolved) state and its tied reservation."""
        reservation = (
            self._load_reservation(record.reservation_id) if record.reservation_id else None
        )
        settled: int | None = None
        released = 0
        overspend = 0
        reservation_status: str | None = None
        if reservation is not None:
            reservation_status = reservation.status
            reserved = int(reservation.amount_minor)
            if reservation.status == ReservationStatus.SETTLED.value:
                settled = int(
                    reservation.settled_amount_minor
                    if reservation.settled_amount_minor is not None
                    else reserved
                )
                released = max(0, reserved - settled)
                overspend = int(reservation.overspend_minor or 0)
            elif reservation.status == ReservationStatus.RELEASED.value:
                settled = 0
                released = reserved
        return OutcomeResolutionResult(
            call_key=record.call_key,
            provider=record.provider,
            supported=self.supports(record.provider),
            resolution=record.resolution,
            call_status=record.status,
            reservation_id=record.reservation_id,
            reservation_status=reservation_status,
            settled_minor=settled,
            released_minor=released,
            overspend_minor=overspend,
            resolution_key=record.resolution_key or "",
            replayed=replayed,
            routed_to_review=record.resolution == ProviderCallResolution.NEEDS_REVIEW.value,
        )

    def _load_reservation(self, reservation_id: str) -> BudgetReservation | None:
        """Fetch the tied reservation by id WITHIN this business (foreign/unknown id reads as ``None``)."""
        stmt = (
            select(BudgetReservation)
            .where(BudgetReservation.business_id == self._business_id)
            .where(BudgetReservation.id == reservation_id)
        )
        return self._session.scalars(stmt).one_or_none()
