"""DCA-115 — track the lifecycle of an external provider call.

Key: ``provider-call`` · source sections 8, 13. Stage: Cost reservations and provider accounting.

One capability: persist the lifecycle of ONE external provider call — its intent, request identity,
source/version, outcome/usage and ambiguous completion state — so cost accounting and the DCA-114
reconciliation have an AUTHORITATIVE record of what happened. **No actual provider/model/network/paid
call is ever made here** — this records lifecycle ONLY; performing the call stays a disabled future
capability. Nothing here spends, charges, bills, sends or invokes a model.

The single sanctioned writer is :class:`ProviderCallStore`; it stages into the *caller's* session and
never commits — the caller owns the unit of work, so the record (or a transition) and its atomic DCA-026
audit entry commit together. It mirrors the DCA-113 :class:`~app.cost.reserve.BudgetReservationStore`
and DCA-114 :class:`~app.cost.reconcile.BudgetReconciliationStore` conventions exactly.

The load-bearing guarantees (the DCA-115 acceptance criteria):

- **A safe state machine; an invalid or duplicate transition changes nothing.** A provider call moves
  only along valid edges of :class:`~app.db.models.ProviderCallStatus` (:data:`VALID_TRANSITIONS`):
  ``PENDING`` → ``IN_FLIGHT`` / ``FAILED``; ``IN_FLIGHT`` → ``SUCCEEDED`` / ``FAILED`` / ``UNCERTAIN``;
  the terminal states accept nothing. An invalid target, a self/duplicate transition, or any transition
  out of a terminal state is refused (:data:`REASON_INVALID_TRANSITION`) BEFORE the row is mutated, so a
  refusal stages no change and no audit and leaves the record exactly as it was. On PostgreSQL a
  ``FOR UPDATE`` lock serializes concurrent transitions on the same record, so a duplicate can never both
  apply (a no-op on SQLite; the serialization guarantee is a PostgreSQL property).

- **A timeout after possible external acceptance is distinguishable from a confirmed no-call failure.**
  ``UNCERTAIN`` (dispatched then unconfirmed — a billable cost MAY exist) is a different terminal state
  from ``FAILED`` (a CONFIRMED no-call failure — no cost occurred). ``dispatched`` records whether
  external acceptance was ever possible, and :attr:`~app.db.models.ProviderCallRecord.cost_possible`
  surfaces the distinction cost accounting / reconciliation reads.

- **Idempotent on the call key.** ``(business_id, call_key)`` is unique: re-recording the same call key
  returns the EXISTING record and stages no second row — a retry of the intent is always safe.

- **Authorized; a denial changes nothing.** Authority is checked FIRST: a caller whose roles lack
  :attr:`~app.identity.permissions.Scope.BUDGET_MANAGE` (recording provider-call cost lifecycle is a
  budget/accounting-control action, exactly like reserving or reconciling spend) is refused with
  :class:`ProviderCallAuthorizationError` before any state is read or written.

Only opaque references, codes and integer counts are persisted — never a raw request/response payload.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.sink import ActorType, AuditActor, AuditSink
from app.db.models import ProviderCallRecord, ProviderCallStatus
from app.identity.permissions import Scope, scopes_for_business_roles

# The default provider / cost bucket a call is attributed to when the caller names none (matches the
# DCA-113 reservation default).
DEFAULT_PROVIDER = "platform"

# The audited action codes for recording a provider call and for advancing its lifecycle (controlled
# codes, like the DCA-113/114 budget codes).
AUDIT_ACTION_RECORD = "provider.call.record"
AUDIT_ACTION_TRANSITION = "provider.call.transition"

# Stable refusal codes. Each is raised BEFORE the session is mutated, so a refusal changes nothing.
REASON_NOT_AUTHORIZED = "provider_call_not_authorized"
REASON_MISSING_KEY = "provider_call_missing_call_key"
REASON_MISSING_CAPABILITY = "provider_call_missing_capability"
REASON_NOT_FOUND = "provider_call_not_found"
REASON_INVALID_TRANSITION = "provider_call_invalid_transition"

# The ONE safe state machine (adjacency). A target not listed for the current status is refused as an
# invalid transition; a terminal status maps to the empty set, so any transition out of it (including a
# duplicate / self transition) is refused with no change.
VALID_TRANSITIONS: dict[ProviderCallStatus, frozenset[ProviderCallStatus]] = {
    ProviderCallStatus.PENDING: frozenset(
        {ProviderCallStatus.IN_FLIGHT, ProviderCallStatus.FAILED}
    ),
    ProviderCallStatus.IN_FLIGHT: frozenset(
        {ProviderCallStatus.SUCCEEDED, ProviderCallStatus.FAILED, ProviderCallStatus.UNCERTAIN}
    ),
    ProviderCallStatus.SUCCEEDED: frozenset(),
    ProviderCallStatus.FAILED: frozenset(),
    ProviderCallStatus.UNCERTAIN: frozenset(),
}


class ProviderCallError(Exception):
    """A provider-call operation was refused with a stable ``code``. Raised before the session is mutated,
    so a refusal leaves the record exactly as it was. ``detail`` names the offending value when useful."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class ProviderCallAuthorizationError(ProviderCallError):
    """The caller's roles do not grant :attr:`~app.identity.permissions.Scope.BUDGET_MANAGE`.

    A distinct type so an HTTP boundary (where one exists) can map it to a 403 while the other
    :class:`ProviderCallError` codes map to a 404/409/422. Raised FIRST — before anything is read or
    written — so an unauthorized attempt leaves no trace. ``code`` is always :data:`REASON_NOT_AUTHORIZED`.
    """

    def __init__(self, message: str) -> None:
        super().__init__(REASON_NOT_AUTHORIZED, message)


@dataclass(frozen=True)
class ProviderCallActor:
    """The business principal recording / advancing a provider call.

    ``subject`` is an opaque actor id (never a name/email) recorded on the row and its atomic audit entry.
    ``roles`` are the caller's verified business-plane membership roles (never a bare token claim).
    :meth:`may_track` grants the action only when those roles yield
    :attr:`~app.identity.permissions.Scope.BUDGET_MANAGE` — an ordinary acting user and a read-only viewer
    do not, and a platform operator holds no business scope for the tenant."""

    subject: str
    roles: tuple[str, ...] = ()
    actor_type: str = "user"

    def may_track(self) -> bool:
        return Scope.BUDGET_MANAGE in scopes_for_business_roles(self.roles)

    def as_actor(self) -> AuditActor:
        return AuditActor(id=self.subject, type=ActorType(self.actor_type))


@dataclass
class ProviderCallIntent:
    """The intent/identity of ONE provider call, as supplied when it is first recorded.

    ``call_key`` is the per-business dedup token / call identity (a retry with the same key is a replay,
    never a second record). ``capability`` is the AI capability the call is for (required). ``request_id``
    is the gateway idempotency identity (X-Request-Id) the call would be issued under. ``intent`` is an
    optional opaque purpose code. ``provider`` / ``model`` / ``source_version`` name the source and its
    version. ``reservation_id`` optionally links the DCA-113 hold the call draws on. All opaque — never a
    raw payload or secret."""

    call_key: str
    capability: str
    request_id: str = ""
    intent: str = ""
    provider: str = DEFAULT_PROVIDER
    model: str = ""
    source_version: str = ""
    reservation_id: str | None = None


@dataclass
class ProviderCallTransition:
    """One lifecycle transition as supplied by the caller.

    ``call_key`` identifies the record WITHIN this business. ``to_status`` is the target state; only a
    valid successor of the current state is admitted. ``outcome_code`` is an opaque outcome/denial code
    recorded on a terminal transition. The token counts are numeric USAGE recorded only on a ``SUCCEEDED``
    outcome (a failed call has no usage; an uncertain result's usage is unknown and never fabricated)."""

    call_key: str
    to_status: ProviderCallStatus
    outcome_code: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class ProviderCallStore:
    """Tenant-scoped reader/writer for provider-call lifecycle records, bound to one business.

    Construct with the active session and the verified business scope (a route resolves it from the
    membership-verified tenant context). Every query is confined to that business — there is no argument
    to widen the scope, so another tenant's call key / id reads exactly like not-found. The store stages
    into the caller's session and never commits; the caller owns the unit of work (and, on PostgreSQL, the
    open transaction that the serializing ``FOR UPDATE`` lock lives in). Recording lifecycle spends and
    sends nothing, and never performs the provider call itself.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def get(self, record_id: str) -> ProviderCallRecord | None:
        """Fetch one record by id WITHIN this business. Another business's id (or an unknown id) resolves
        to ``None`` — a cross-tenant id reads exactly like not-found."""
        if not record_id:
            return None
        stmt = (
            select(ProviderCallRecord)
            .where(ProviderCallRecord.business_id == self._business_id)
            .where(ProviderCallRecord.id == record_id)
        )
        return self._session.scalars(stmt).one_or_none()

    def by_key(self, call_key: str) -> ProviderCallRecord | None:
        """The record for one call key within this business, or ``None``. A key another business recorded
        is invisible here (the query is business-scoped)."""
        key = (call_key or "").strip()
        if not key:
            return None
        stmt = (
            select(ProviderCallRecord)
            .where(ProviderCallRecord.business_id == self._business_id)
            .where(ProviderCallRecord.call_key == key)
        )
        return self._session.scalars(stmt).one_or_none()

    # -- write paths --------------------------------------------------------------------------------

    def record(
        self,
        intent: ProviderCallIntent,
        actor: ProviderCallActor,
        *,
        now: datetime | None = None,
    ) -> ProviderCallRecord:
        """Authorize, gate and atomically stage a PENDING provider-call record; return the (uncommitted) row.

        Order (each gate leaves NO state behind when it refuses):

        1. **Authority FIRST.** A caller lacking :attr:`~app.identity.permissions.Scope.BUDGET_MANAGE` is
           refused (:class:`ProviderCallAuthorizationError`) before any state is read or written.
        2. **Shape.** An empty ``call_key`` (:data:`REASON_MISSING_KEY`) or empty ``capability``
           (:data:`REASON_MISSING_CAPABILITY`) raises before anything is read.
        3. **Idempotent replay.** An existing call key returns the existing record, no new row.

        On success a ``PENDING`` record is staged plus an atomic DCA-026 audit entry (opaque references
        only) in the SAME unit of work. The caller owns the transaction. Recording performs no call.
        """
        moment = now or datetime.now(UTC)

        # 1) Authority FIRST — an unauthorized caller is refused with no state touched.
        if not actor.may_track():
            raise ProviderCallAuthorizationError(
                "recording a provider-call lifecycle requires a business administrator holding "
                f"'{Scope.BUDGET_MANAGE.value}'; an ordinary acting user, a read-only viewer or a "
                "platform operator (holding no business scope) cannot perform it"
            )

        # 2) Shape — before any state is read.
        key = (intent.call_key or "").strip()
        if not key:
            raise ProviderCallError(REASON_MISSING_KEY, "a call_key is required")
        capability = (intent.capability or "").strip()
        if not capability:
            raise ProviderCallError(REASON_MISSING_CAPABILITY, "a capability is required")
        provider = (intent.provider or DEFAULT_PROVIDER).strip() or DEFAULT_PROVIDER

        # 3) Idempotent replay — an existing key returns the existing record, nothing staged.
        existing = self.by_key(key)
        if existing is not None:
            return existing

        record = ProviderCallRecord(
            business_id=self._business_id,
            call_key=key,
            request_id=(intent.request_id or "").strip(),
            capability=capability,
            intent=(intent.intent or "").strip(),
            provider=provider,
            model=(intent.model or "").strip(),
            source_version=(intent.source_version or "").strip(),
            reservation_id=intent.reservation_id,
            status=ProviderCallStatus.PENDING.value,
            dispatched=False,
            actor_id=actor.subject,
            actor_type=actor.actor_type,
            created_at=moment,
            updated_at=moment,
        )
        self._session.add(record)

        # Atomic audit (SEC-001/BR-122): staged into the caller's session so it commits with the record.
        # References stay OPAQUE — ids / key / request identity / capability / source — never a payload.
        AuditSink(self._session, business_id=self._business_id, actor=actor.as_actor()).record(
            action=AUDIT_ACTION_RECORD,
            reason=f"recorded provider call intent for capability '{capability}' as pending",
            references={
                "providerCallId": record.id,
                "callKey": key,
                "requestId": record.request_id,
                "capability": capability,
                "provider": provider,
                "model": record.model,
                "sourceVersion": record.source_version,
                "reservationId": intent.reservation_id,
                "status": ProviderCallStatus.PENDING.value,
            },
        )
        return record

    def transition(
        self,
        change: ProviderCallTransition,
        actor: ProviderCallActor,
        *,
        now: datetime | None = None,
    ) -> ProviderCallRecord:
        """Authorize, gate and atomically advance ONE record along a valid edge; return the (uncommitted) row.

        Order (each gate leaves NO state behind when it refuses):

        1. **Authority FIRST.** A caller lacking :attr:`~app.identity.permissions.Scope.BUDGET_MANAGE` is
           refused (:class:`ProviderCallAuthorizationError`) before any state is read or written.
        2. **Shape.** An empty ``call_key`` (:data:`REASON_MISSING_KEY`) raises before anything is read.
        3. **Load + serialize.** The record is fetched WITHIN this business (an unknown/foreign call key →
           :data:`REASON_NOT_FOUND`) and re-read under a ``FOR UPDATE`` lock so concurrent transitions of
           the same record serialize.
        4. **Valid-transition gate.** The target must be a valid successor of the CURRENT status
           (:data:`VALID_TRANSITIONS`). An invalid target, a self/duplicate transition, or any transition
           out of a terminal state is refused (:data:`REASON_INVALID_TRANSITION`) with no change.
        5. **Apply.** The status is advanced; ``dispatched`` / ``dispatched_at`` are set on the move to
           ``IN_FLIGHT``; ``outcome_code`` / ``resolved_at`` (and, for ``SUCCEEDED`` only, the token usage)
           are set on a terminal move. An atomic DCA-026 audit entry (opaque references only) is staged in
           the SAME unit of work.

        The caller owns the transaction. A transition records lifecycle and performs no call.
        """
        moment = now or datetime.now(UTC)

        # 1) Authority FIRST — an unauthorized caller is refused with no state touched.
        if not actor.may_track():
            raise ProviderCallAuthorizationError(
                "advancing a provider-call lifecycle requires a business administrator holding "
                f"'{Scope.BUDGET_MANAGE.value}'; an ordinary acting user, a read-only viewer or a "
                "platform operator (holding no business scope) cannot perform it"
            )

        # 2) Shape — before any state is read.
        key = (change.call_key or "").strip()
        if not key:
            raise ProviderCallError(REASON_MISSING_KEY, "a call_key is required")

        # 3) Load WITHIN this business (foreign/unknown key → not-found), then re-read under FOR UPDATE.
        if self.by_key(key) is None:
            raise ProviderCallError(
                REASON_NOT_FOUND,
                "no provider-call record with that call key exists for this business",
                detail=key,
            )
        record = self._lock(key)
        if record is None:  # pragma: no cover - vanished between the read and the lock
            raise ProviderCallError(
                REASON_NOT_FOUND,
                "no provider-call record with that call key exists for this business",
                detail=key,
            )

        current = ProviderCallStatus(record.status)
        target = change.to_status
        # 4) Valid-transition gate — an invalid OR duplicate transition is refused with no change.
        if target not in VALID_TRANSITIONS[current]:
            raise ProviderCallError(
                REASON_INVALID_TRANSITION,
                f"{current.value} -> {target.value} is not a valid provider-call transition",
                detail=f"{current.value}->{target.value}",
            )

        # 5) Apply the valid transition.
        record.status = target.value
        record.updated_at = moment
        if target is ProviderCallStatus.IN_FLIGHT:
            record.dispatched = True
            record.dispatched_at = moment
        if target in (
            ProviderCallStatus.SUCCEEDED,
            ProviderCallStatus.FAILED,
            ProviderCallStatus.UNCERTAIN,
        ):
            record.outcome_code = (change.outcome_code or "").strip() or None
            record.resolved_at = moment
        if target is ProviderCallStatus.SUCCEEDED:
            # Usage is recorded ONLY on a confirmed success (numeric counts, never raw text).
            record.prompt_tokens = change.prompt_tokens
            record.completion_tokens = change.completion_tokens
            record.total_tokens = change.total_tokens

        # Atomic audit (SEC-001/BR-122): staged into the caller's session so it commits with the update.
        AuditSink(self._session, business_id=self._business_id, actor=actor.as_actor()).record(
            action=AUDIT_ACTION_TRANSITION,
            reason=f"advanced provider call {current.value} -> {target.value}",
            references={
                "providerCallId": record.id,
                "callKey": key,
                "fromStatus": current.value,
                "toStatus": target.value,
                "dispatched": record.dispatched,
                "outcomeCode": record.outcome_code,
                "totalTokens": record.total_tokens,
                "costPossible": record.cost_possible,
            },
        )
        return record

    # -- internals ----------------------------------------------------------------------------------

    def _lock(self, call_key: str) -> ProviderCallRecord | None:
        """Re-read the record ``FOR UPDATE`` so concurrent transitions of the same call serialize.

        On PostgreSQL the row lock makes the second concurrent transition block until the first commits,
        then see its committed state — so a duplicate transition is refused as invalid, never applied
        twice. On SQLite ``with_for_update`` is silently ignored (no row locking); the valid-transition
        gate still holds via the stored status."""
        stmt = (
            select(ProviderCallRecord)
            .where(ProviderCallRecord.business_id == self._business_id)
            .where(ProviderCallRecord.call_key == call_key)
            .with_for_update()
        )
        return self._session.scalars(stmt).one_or_none()
