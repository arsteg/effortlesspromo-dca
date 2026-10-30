"""DCA-113 — reserve spend atomically.

Key: ``budget-reserve`` · source sections 8, 13. Stage: Cost reservations and provider accounting.

One capability: ATOMICALLY RESERVE a precise amount of spend against a business's in-force budget caps,
so that concurrent reservations can never collectively overshoot any cap. A reservation is a HOLD, not a
charge and not a send — the DCA has no spend or send path — it only sets aside headroom. It builds
directly on the DCA-112 estimator (a run is estimated, then its cost is reserved) and the DCA-042
:class:`~app.db.models.BudgetLimit` caps.

The single sanctioned writer is :class:`BudgetReservationStore`; it stages into the *caller's* session
and never commits — the caller owns the unit of work, so the reservation row and its atomic DCA-026
audit entry commit together. The load-bearing guarantees (the DCA-113 acceptance criteria):

- **No reservation can overshoot any cap; a refusal changes nothing.** A reservation is admitted only if
  the amount fits, in PRECISE INR minor units (paise), within EVERY cap in force: the per-run cap, a
  platform per-reservation hard ceiling, the day's remaining daily cap, the month's remaining monthly
  cap, an optional per-provider cap, and a platform daily hard ceiling. A request that would exceed ANY
  cap is refused with a stable code BEFORE any row is staged, so a denial stages no hold and no audit and
  leaves every total exactly as it was — and it makes no external call (there is none to make here).

- **Concurrent reservations serialize (transaction-safe).** Before it sums existing ``ACTIVE`` holds and
  decides, :meth:`BudgetReservationStore.reserve` takes a ``FOR UPDATE`` lock on the business's latest
  :class:`~app.db.models.BudgetLimit` row. Every concurrent reservation for the same business contends
  for that one row, so PostgreSQL serializes them: the second transaction blocks until the first commits,
  then reads the first's committed hold in its sums. Two reservations can therefore never both be
  admitted past a cap, and the total reserved never exceeds the limit even under a race. (The lock is a
  no-op on SQLite, which has no row locking; the serialization guarantee is a PostgreSQL property, proven
  in the Postgres-gated tests. SQLite still exercises the pure cap arithmetic and the idempotency path.)

- **Idempotent on a key.** ``(business_id, idempotency_key)`` is unique. Re-submitting the same key —
  including a concurrent duplicate that races past the fast-path check — returns the EXISTING reservation
  and stages no second row and no second hold, so a retry is always safe.

- **Authorized; a denial changes nothing.** Authority is checked FIRST: a caller whose roles lack
  :attr:`~app.identity.permissions.Scope.BUDGET_MANAGE` (an administrator scope — reserving against the
  spend budget is a budget-control action) is refused with :class:`ReservationAuthorizationError` before
  any state is read or written. The automated worker path that would reserve on the agent's behalf runs
  under a system actor and is a separate, still-disabled capability.

Keeping future/paid capabilities disabled: a reservation HOLDS budget, it never spends, charges, bills or
sends, and it never invokes a model/provider. Releasing / settling a hold (and recording actual provider
spend) are later, disabled stories; this writer only ever inserts an ``ACTIVE`` hold.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.sink import ActorType, AuditActor, AuditSink
from app.db.models import BudgetLimit, BudgetReservation, ReservationStatus
from app.identity.permissions import Scope, scopes_for_business_roles

# The default provider / cost bucket a reservation is attributed to when the caller names none.
DEFAULT_PROVIDER = "platform"

# Platform-set HARD ceilings — defence-in-depth bounds, in the spirit of the DCA-112 rate card's
# ``max_units_per_operation`` / ``max_total_operational_minor`` caps. No per-business limit and no
# supplied provider cap can authorize a reservation beyond these, so a single reservation and one
# business's reservations in one civil day are ALWAYS bounded, even if a (mis)configured BudgetLimit is
# enormous. Conservative placeholder values in INR minor units (paise); a real platform cap is data for a
# later story.
PLATFORM_PER_RESERVATION_CEILING_MINOR = 100_000_000  # ₹1,000,000 — one single reservation
PLATFORM_DAILY_CEILING_MINOR = 1_000_000_000  # ₹10,000,000 — one business, one civil day

# The audited action code for taking a reservation (a controlled code, like the other DCA writes).
AUDIT_ACTION_RESERVE = "budget.reservation.reserve"

# Stable refusal codes. Each is raised BEFORE the session is mutated, so a refusal stages no hold.
REASON_NOT_AUTHORIZED = "reservation_not_authorized"
REASON_MISSING_KEY = "reservation_missing_idempotency_key"
REASON_INVALID_AMOUNT = "reservation_invalid_amount"
REASON_NO_BUDGET = "reservation_no_budget_configured"
REASON_PER_RUN_EXCEEDED = "reservation_per_run_cap_exceeded"
REASON_DAILY_EXCEEDED = "reservation_daily_cap_exceeded"
REASON_MONTHLY_EXCEEDED = "reservation_monthly_cap_exceeded"
REASON_PROVIDER_EXCEEDED = "reservation_provider_cap_exceeded"
REASON_PLATFORM_EXCEEDED = "reservation_platform_cap_exceeded"


class ReservationError(Exception):
    """A reservation was refused with a stable ``code``. Raised before the session is mutated, so a
    refusal leaves no hold and no partial state. ``detail`` names the offending value when useful."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class ReservationAuthorizationError(ReservationError):
    """The caller's roles do not grant :attr:`~app.identity.permissions.Scope.BUDGET_MANAGE`.

    A distinct type so an HTTP boundary (where one exists) can map it to a 403 while the other
    :class:`ReservationError` codes map to a 404/409/422. Raised FIRST — before anything is read or
    written — so an unauthorized attempt leaves no trace and changes nothing. ``code`` is always
    :data:`REASON_NOT_AUTHORIZED`.
    """

    def __init__(self, message: str) -> None:
        super().__init__(REASON_NOT_AUTHORIZED, message)


@dataclass(frozen=True)
class ReservationActor:
    """The business principal taking a reservation.

    ``subject`` is an opaque actor id (never a name/email) recorded on the reservation and its atomic
    audit entry. ``roles`` are the caller's business-plane membership roles (from the verified
    :class:`~app.identity.tenant_context.TenantContext`, NEVER a bare token claim). :meth:`may_reserve`
    grants the action only when those roles yield :attr:`~app.identity.permissions.Scope.BUDGET_MANAGE` —
    an ordinary acting user and a read-only viewer do not, and a platform operator holds no business
    scope for the tenant.
    """

    subject: str
    roles: tuple[str, ...] = ()
    actor_type: str = "user"

    def may_reserve(self) -> bool:
        return Scope.BUDGET_MANAGE in scopes_for_business_roles(self.roles)

    def as_actor(self) -> AuditActor:
        return AuditActor(id=self.subject, type=ActorType(self.actor_type))


@dataclass
class ReservationRequest:
    """One reservation as supplied by the caller.

    ``idempotency_key`` is the per-business dedup token (a retry with the same key is a replay, never a
    second hold). ``amount_minor`` is the amount to hold, in INR **minor units (paise)** — a positive
    integer. ``provider`` names the cost bucket the hold is attributed to (defaults to the platform
    bucket). ``provider_cap_minor`` optionally imposes an ADDITIONAL per-provider ceiling on that
    provider's holds for the civil day (e.g. a vendor sub-budget); when ``None`` no provider cap is
    enforced. The daily / monthly / per-run caps always come from the business's in-force BudgetLimit.
    """

    idempotency_key: str
    amount_minor: int
    provider: str = DEFAULT_PROVIDER
    provider_cap_minor: int | None = None


@dataclass(frozen=True)
class ReservationWindow:
    """A read-only snapshot of a business's reservation state for the current civil day / month.

    Inspection helper (not a hold): it resolves the in-force caps and sums the ``ACTIVE`` holds in the
    current windows, so a reviewer / manual check can see the remaining headroom without reserving.
    """

    configured: bool
    budget_version: int | None
    timezone: str | None
    day_key: str | None
    month_key: str | None
    daily_cap_minor: int | None
    monthly_cap_minor: int | None
    per_run_cap_minor: int | None
    day_reserved_minor: int
    month_reserved_minor: int

    @property
    def daily_remaining_minor(self) -> int | None:
        if self.daily_cap_minor is None:
            return None
        return max(0, self.daily_cap_minor - self.day_reserved_minor)

    @property
    def monthly_remaining_minor(self) -> int | None:
        if self.monthly_cap_minor is None:
            return None
        return max(0, self.monthly_cap_minor - self.month_reserved_minor)


class BudgetReservationStore:
    """Tenant-scoped reader/writer for budget reservations, bound to one business.

    Construct with the active session and the verified business scope (a route resolves it from the
    membership-verified tenant context). Every query is confined to that business — there is no argument
    to widen the scope. The store stages into the caller's session and never commits; the caller owns the
    unit of work (and, on PostgreSQL, the open transaction that the serializing ``FOR UPDATE`` lock lives
    in). Reserving holds budget and runs/sends nothing.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def get(self, reservation_id: str) -> BudgetReservation | None:
        """Fetch one reservation by id WITHIN this business. Another business's id (or an unknown id)
        resolves to ``None`` — a cross-tenant id reads exactly like not-found."""
        if not reservation_id:
            return None
        stmt = (
            select(BudgetReservation)
            .where(BudgetReservation.business_id == self._business_id)
            .where(BudgetReservation.id == reservation_id)
        )
        return self._session.scalars(stmt).one_or_none()

    def by_key(self, idempotency_key: str) -> BudgetReservation | None:
        """The reservation for one idempotency key within this business, or ``None``. A key another
        business recorded is invisible here (the query is business-scoped)."""
        key = (idempotency_key or "").strip()
        if not key:
            return None
        stmt = (
            select(BudgetReservation)
            .where(BudgetReservation.business_id == self._business_id)
            .where(BudgetReservation.idempotency_key == key)
        )
        return self._session.scalars(stmt).one_or_none()

    def active(self) -> list[BudgetReservation]:
        """Every ACTIVE reservation for this business, oldest first — the reviewable hold ledger."""
        stmt = (
            select(BudgetReservation)
            .where(BudgetReservation.business_id == self._business_id)
            .where(BudgetReservation.status == ReservationStatus.ACTIVE.value)
            .order_by(BudgetReservation.created_at, BudgetReservation.id)
        )
        return list(self._session.scalars(stmt).all())

    def window(self, now: datetime | None = None) -> ReservationWindow:
        """A read-only snapshot of the in-force caps and the current-window ACTIVE hold sums.

        Reads the latest :class:`~app.db.models.BudgetLimit` WITHOUT locking (pure inspection), so it
        never serializes against a concurrent reserve. When no budget is configured it reports
        ``configured=False`` with only the raw sums.
        """
        moment = now or datetime.now(UTC)
        limit = self._latest_limit()
        if limit is None:
            return ReservationWindow(
                configured=False,
                budget_version=None,
                timezone=None,
                day_key=None,
                month_key=None,
                daily_cap_minor=None,
                monthly_cap_minor=None,
                per_run_cap_minor=None,
                day_reserved_minor=0,
                month_reserved_minor=0,
            )
        day_key, month_key = _window_keys(moment, limit.timezone)
        return ReservationWindow(
            configured=True,
            budget_version=limit.version,
            timezone=limit.timezone,
            day_key=day_key,
            month_key=month_key,
            daily_cap_minor=limit.daily_cap_minor,
            monthly_cap_minor=limit.monthly_cap_minor,
            per_run_cap_minor=limit.per_run_cap_minor,
            day_reserved_minor=self._reserved_total(day_key=day_key),
            month_reserved_minor=self._reserved_total(month_key=month_key),
        )

    # -- the one write path -------------------------------------------------------------------------

    def reserve(
        self,
        request: ReservationRequest,
        actor: ReservationActor,
        *,
        now: datetime | None = None,
    ) -> BudgetReservation:
        """Authorize, gate and atomically stage a reservation; return the (uncommitted) hold.

        Order (each gate leaves NO state behind when it refuses):

        1. **Authority FIRST.** A caller lacking
           :attr:`~app.identity.permissions.Scope.BUDGET_MANAGE` is refused
           (:class:`ReservationAuthorizationError`) before any state is read or written.
        2. **Shape.** An empty idempotency key (:data:`REASON_MISSING_KEY`) or a non-positive amount
           (:data:`REASON_INVALID_AMOUNT`) raises before anything is read.
        3. **Idempotent replay (fast path).** An existing key returns the existing hold, no new row.
        4. **Serialize.** A ``FOR UPDATE`` lock is taken on the business's latest BudgetLimit row so
           concurrent reservations for this business are serialized; a re-check of the key under the lock
           returns the existing hold if a concurrent duplicate just committed (idempotent under a race).
           No configured budget → :data:`REASON_NO_BUDGET` (fail-closed; a reservation needs caps).
        5. **Caps.** The amount must fit within the per-run cap, the platform per-reservation ceiling,
           the day's remaining daily cap, the month's remaining monthly cap, an optional provider cap,
           and the platform daily ceiling — each a stable refusal code. Sums count only ACTIVE holds in
           the caps' own civil day/month (India-local).

        On success an ``ACTIVE`` hold is staged with the pinned ``budget_version`` and window keys, plus
        an atomic DCA-026 audit entry (opaque references only) in the SAME unit of work. The caller owns
        the transaction. Reserving holds budget and spends/sends nothing.
        """
        moment = now or datetime.now(UTC)

        # 1) Authority FIRST — an unauthorized caller is refused with no state touched.
        if not actor.may_reserve():
            raise ReservationAuthorizationError(
                "reserving spend requires a business administrator holding "
                f"'{Scope.BUDGET_MANAGE.value}'; an ordinary acting user, a read-only viewer or a "
                "platform operator (holding no business scope) cannot perform it"
            )

        # 2) Shape — before any state is read.
        key = (request.idempotency_key or "").strip()
        if not key:
            raise ReservationError(REASON_MISSING_KEY, "an idempotency_key is required")
        amount = int(request.amount_minor)
        if amount <= 0:
            raise ReservationError(
                REASON_INVALID_AMOUNT,
                f"reservation amount must be a positive number of INR minor units (got {amount})",
                detail=str(amount),
            )
        provider = (request.provider or DEFAULT_PROVIDER).strip() or DEFAULT_PROVIDER

        # 3) Idempotent replay (fast path) — an existing key returns the existing hold, nothing staged.
        existing = self.by_key(key)
        if existing is not None:
            return existing

        # 4) Serialize — lock the business's latest BudgetLimit row so concurrent reservations contend
        #    for the SAME row (PostgreSQL serializes them; a no-op on SQLite). Re-check the key under the
        #    lock in case a concurrent duplicate committed between the fast-path read and the lock.
        limit = self._lock_latest_limit()
        existing = self.by_key(key)
        if existing is not None:
            return existing
        if limit is None:
            raise ReservationError(
                REASON_NO_BUDGET,
                "the business has no configured spending limits; spend cannot be reserved until "
                "budget caps are set (DCA-042)",
            )

        day_key, month_key = _window_keys(moment, limit.timezone)

        # 5) Caps — all evaluated before any row is staged, cheapest/single-amount checks first.
        if amount > limit.per_run_cap_minor:
            raise ReservationError(
                REASON_PER_RUN_EXCEEDED,
                f"reservation {amount} exceeds the per-run cap ({limit.per_run_cap_minor})",
                detail=str(limit.per_run_cap_minor),
            )
        if amount > PLATFORM_PER_RESERVATION_CEILING_MINOR:
            raise ReservationError(
                REASON_PLATFORM_EXCEEDED,
                f"reservation {amount} exceeds the platform per-reservation ceiling "
                f"({PLATFORM_PER_RESERVATION_CEILING_MINOR})",
                detail=str(PLATFORM_PER_RESERVATION_CEILING_MINOR),
            )

        day_reserved = self._reserved_total(day_key=day_key)
        month_reserved = self._reserved_total(month_key=month_key)

        if request.provider_cap_minor is not None:
            provider_reserved = self._reserved_total(day_key=day_key, provider=provider)
            if provider_reserved + amount > request.provider_cap_minor:
                raise ReservationError(
                    REASON_PROVIDER_EXCEEDED,
                    f"reservation {amount} for provider '{provider}' would exceed its daily provider "
                    f"cap ({request.provider_cap_minor}); {provider_reserved} already reserved today",
                    detail=provider,
                )
        if day_reserved + amount > limit.daily_cap_minor:
            raise ReservationError(
                REASON_DAILY_EXCEEDED,
                f"reservation {amount} would exceed the daily cap ({limit.daily_cap_minor}); "
                f"{day_reserved} already reserved today",
                detail=str(limit.daily_cap_minor),
            )
        if month_reserved + amount > limit.monthly_cap_minor:
            raise ReservationError(
                REASON_MONTHLY_EXCEEDED,
                f"reservation {amount} would exceed the monthly cap ({limit.monthly_cap_minor}); "
                f"{month_reserved} already reserved this month",
                detail=str(limit.monthly_cap_minor),
            )
        if day_reserved + amount > PLATFORM_DAILY_CEILING_MINOR:
            raise ReservationError(
                REASON_PLATFORM_EXCEEDED,
                f"reservation {amount} would exceed the platform daily ceiling "
                f"({PLATFORM_DAILY_CEILING_MINOR})",
                detail=str(PLATFORM_DAILY_CEILING_MINOR),
            )

        # 6) Stage the ACTIVE hold + an atomic audit entry in the SAME unit of work.
        reservation = BudgetReservation(
            business_id=self._business_id,
            idempotency_key=key,
            amount_minor=amount,
            currency=limit.currency,
            provider=provider,
            budget_version=limit.version,
            day_key=day_key,
            month_key=month_key,
            timezone=limit.timezone,
            status=ReservationStatus.ACTIVE.value,
            actor_id=actor.subject,
            actor_type=actor.actor_type,
            created_at=moment,
        )
        self._session.add(reservation)

        # Atomic audit (SEC-001/BR-122): staged into the caller's session so it commits with the hold.
        # References stay OPAQUE — ids / key / integer amount / provider bucket / pinned version / window
        # keys; never any raw contact value or secret. Reserving holds budget and runs/sends nothing.
        AuditSink(self._session, business_id=self._business_id, actor=actor.as_actor()).record(
            action=AUDIT_ACTION_RESERVE,
            reason=f"reserved {amount} INR minor units against budget version {limit.version}",
            references={
                "reservationId": reservation.id,
                "idempotencyKey": key,
                "amountMinor": amount,
                "currency": limit.currency,
                "provider": provider,
                "budgetVersion": limit.version,
                "dayKey": day_key,
                "monthKey": month_key,
            },
        )
        return reservation

    # -- internals ----------------------------------------------------------------------------------

    def _latest_limit(self) -> BudgetLimit | None:
        """The business's highest-version (in-force) BudgetLimit, or ``None`` — WITHOUT a lock."""
        stmt = (
            select(BudgetLimit)
            .where(BudgetLimit.business_id == self._business_id)
            .order_by(BudgetLimit.version.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).first()

    def _lock_latest_limit(self) -> BudgetLimit | None:
        """The business's in-force BudgetLimit, locked ``FOR UPDATE`` to serialize concurrent reserves.

        On PostgreSQL the row lock makes every concurrent reservation for this business contend for the
        SAME row, so they are serialized (the second blocks until the first commits, then sees its hold).
        On SQLite ``with_for_update`` is silently ignored (no row locking) — the cap arithmetic and the
        idempotency path still hold; only the cross-transaction serialization is a PostgreSQL property.
        """
        stmt = (
            select(BudgetLimit)
            .where(BudgetLimit.business_id == self._business_id)
            .order_by(BudgetLimit.version.desc())
            .limit(1)
            .with_for_update()
        )
        return self._session.scalars(stmt).first()

    def _reserved_total(
        self,
        *,
        day_key: str | None = None,
        month_key: str | None = None,
        provider: str | None = None,
    ) -> int:
        """Sum of ACTIVE reservation amounts for this business in the given window (exact minor units)."""
        stmt = (
            select(func.coalesce(func.sum(BudgetReservation.amount_minor), 0))
            .where(BudgetReservation.business_id == self._business_id)
            .where(BudgetReservation.status == ReservationStatus.ACTIVE.value)
        )
        if day_key is not None:
            stmt = stmt.where(BudgetReservation.day_key == day_key)
        if month_key is not None:
            stmt = stmt.where(BudgetReservation.month_key == month_key)
        if provider is not None:
            stmt = stmt.where(BudgetReservation.provider == provider)
        return int(self._session.scalar(stmt) or 0)


def _as_utc(value: datetime) -> datetime:
    """Normalize a (possibly naive, SQLite-returned) datetime to UTC-aware."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _window_keys(moment: datetime, timezone: str) -> tuple[str, str]:
    """The civil (day, month) keys for ``moment`` in ``timezone`` — e.g. ("2026-10-31", "2026-10").

    The caps' daily/monthly windows are India-local civil windows, so the key is computed in the caps'
    own zone, not UTC: the same instant can fall in a different civil day in a different zone.
    """
    local = _as_utc(moment).astimezone(ZoneInfo(timezone))
    return local.strftime("%Y-%m-%d"), local.strftime("%Y-%m")
