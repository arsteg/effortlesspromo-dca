"""DCA-107 — the authorized per-business recurring-schedule store.

:class:`RecurringScheduleStore` is the single sanctioned writer of
:class:`~app.db.models.RecurringSchedule`. It persists, per business, an ACTIVE recurring schedule and
its DUE time (``next_run_at``) as durable PostgreSQL data, pinned to the versioned profile references it
was created against.

The load-bearing guarantees (the DCA-107 acceptance criteria):

- **A schedule survives a process restart and implies NO endless per-tenant process.** The schedule is
  just a row: storing it writes durable state (``next_run_at`` and the cadence) and does NOTHING else —
  it starts no thread, opens no loop, enqueues no job and performs no I/O. A restarted scheduler simply
  re-reads the same persisted ``next_run_at``. Advancing a due schedule and uniquely enqueuing each
  cycle is the SEPARATE next story (DCA-108) and stays a DISABLED future capability here; nothing in this
  store ever executes a schedule.

- **Only a confirmed, entitled business, with versioned profile references.** :meth:`create` refuses to
  store a schedule unless, at creation time, the business has a CONFIRMED active ICP (DCA-040) AND a
  currently-entitled cached snapshot (DCA-023). The schedule snapshots ``icp_version`` (the confirmed
  active ICP version) and optionally links ``profile_version`` (the DCA-036 business-profile draft
  version), so it always records WHICH versioned profile it targets.

- **Authorized; a denial changes nothing.** Authority is checked FIRST: a caller whose roles lack
  :attr:`~app.identity.permissions.Scope.SCHEDULE_MANAGE` (an administrator scope) is refused with
  :class:`ScheduleAuthorizationError` before any state is read or written. Every other gate (shape,
  confirmed, entitled, duplicate key, unknown profile version) also raises BEFORE the session is mutated,
  so a refused create stages no row and leaves no partial state — the "relevant denial" reproduces
  exactly. ``actor`` records WHO stored it (opaque id + kind) on the atomic DCA-026 audit entry in the
  SAME unit of work as the row.

- **One schedule per logical key per business.** ``(business_id, schedule_key)`` is unique; re-creating
  an existing key is a :class:`ScheduleConflict` (updating/advancing it is DCA-108), never a silent
  overwrite. The store is tenant-scoped (DCA-017 scoping + DCA-018 RLS beneath it): every read is
  confined to the bound business and a by-id read of another business's schedule resolves to not-found.

The store stages into the *caller's* session and never commits — the caller owns the unit of work, so
the schedule row and its atomic audit entry commit together. Storing a schedule grants nothing and sends
nothing: the DCA has no send path, and a schedule is only a durable record of a recurring intent.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.sink import ActorType, AuditActor, AuditSink
from app.db.models import (
    BusinessProfileDraft,
    EntitlementProjection,
    RecurringSchedule,
    ScheduleStatus,
)
from app.entitlements import EntitlementStatus
from app.identity.permissions import Scope, scopes_for_business_roles
from app.services.icp_confirmation import IcpConfirmationStore

# The audited action code for storing a recurring schedule (a controlled code, like the other writes).
AUDIT_ACTION_STORE_SCHEDULE = "schedule.store"

# Stable refusal codes. Each is raised BEFORE the session is mutated, so a refusal stages no row.
REASON_NOT_AUTHORIZED = "schedule_not_authorized"
REASON_MISSING_SCHEDULE_KEY = "missing_schedule_key"
REASON_INVALID_INTERVAL = "invalid_interval"
REASON_MISSING_NEXT_RUN = "missing_next_run_at"
REASON_NOT_CONFIRMED = "schedule_business_not_confirmed"
REASON_NOT_ENTITLED = "schedule_business_not_entitled"
REASON_PROFILE_VERSION_NOT_FOUND = "profile_version_not_found"
REASON_ALREADY_EXISTS = "schedule_already_exists"


class ScheduleError(Exception):
    """A schedule store/create was refused with a stable code. Raised before the session is mutated, so
    a refusal leaves no row and no partial state. ``detail`` names the offending value when useful."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class ScheduleAuthorizationError(ScheduleError):
    """The caller's roles do not grant :attr:`~app.identity.permissions.Scope.SCHEDULE_MANAGE`.

    A distinct type so the HTTP boundary (where one exists) can map it to a 403 while the other
    :class:`ScheduleError` codes map to a 404/409/422. Raised FIRST — before anything is read or written.
    """

    def __init__(self, message: str) -> None:
        super().__init__(REASON_NOT_AUTHORIZED, message)


class ScheduleConflict(ScheduleError):
    """A schedule already exists for this ``(business, schedule_key)``.

    Raised before any new row is written, so a conflict changes nothing — the existing schedule is never
    silently overwritten. Advancing / updating a schedule is the separate DCA-108 capability.
    """

    def __init__(self, schedule_key: str) -> None:
        super().__init__(
            REASON_ALREADY_EXISTS,
            f"a recurring schedule '{schedule_key}' already exists for this business",
            detail=schedule_key,
        )


@dataclass(frozen=True)
class ScheduleManager:
    """The business principal storing a schedule.

    ``subject`` is an opaque actor id (never a name/email) recorded on the schedule and its atomic audit
    entry. ``roles`` are the caller's business-plane membership roles (from the verified
    :class:`~app.identity.tenant_context.TenantContext`, NEVER a bare token claim). :meth:`may_manage`
    grants the action only when those roles yield
    :attr:`~app.identity.permissions.Scope.SCHEDULE_MANAGE` — an ordinary acting user and a read-only
    viewer do not, and a platform operator holds no business scope for the tenant.
    """

    subject: str
    roles: tuple[str, ...] = ()
    actor_type: str = "user"

    def may_manage(self) -> bool:
        return Scope.SCHEDULE_MANAGE in scopes_for_business_roles(self.roles)

    def as_actor(self) -> AuditActor:
        return AuditActor(id=self.subject, type=ActorType(self.actor_type))


@dataclass
class ScheduleInput:
    """One recurring schedule as supplied by the caller.

    ``schedule_key`` is the logical kind of recurring work (the per-business dedup key).
    ``interval_seconds`` is the cadence (positive). ``next_run_at`` is the first DUE time to persist.
    ``profile_version`` optionally links the DCA-036 business-profile draft version this schedule was
    authored against (validated to exist for this business when supplied); the confirmed active ICP
    version is snapshotted automatically and is NOT a caller argument.
    """

    schedule_key: str
    interval_seconds: int
    next_run_at: datetime
    profile_version: int | None = None


class RecurringScheduleStore:
    """Tenant-scoped reader/writer for per-business recurring schedules, bound to one business.

    Construct with the active session and the verified business scope (a route resolves it from the
    membership-verified tenant context). Every query is confined to that business — there is no argument
    to widen the scope.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def get(self, schedule_id: str) -> RecurringSchedule | None:
        """Fetch one schedule by id WITHIN this business. Another business's id (or an unknown id)
        resolves to ``None`` — a cross-tenant id reads exactly like not-found."""
        if not schedule_id:
            return None
        stmt = (
            select(RecurringSchedule)
            .where(RecurringSchedule.business_id == self._business_id)
            .where(RecurringSchedule.id == schedule_id)
        )
        return self._session.scalars(stmt).one_or_none()

    def by_key(self, schedule_key: str) -> RecurringSchedule | None:
        """The schedule for one logical key within this business, or ``None``. A key another business
        recorded is invisible here (the query is business-scoped)."""
        key = (schedule_key or "").strip()
        if not key:
            return None
        stmt = (
            select(RecurringSchedule)
            .where(RecurringSchedule.business_id == self._business_id)
            .where(RecurringSchedule.schedule_key == key)
        )
        return self._session.scalars(stmt).one_or_none()

    def schedules(self) -> list[RecurringSchedule]:
        """Every schedule stored for this business, oldest first."""
        stmt = (
            select(RecurringSchedule)
            .where(RecurringSchedule.business_id == self._business_id)
            .order_by(RecurringSchedule.created_at, RecurringSchedule.id)
        )
        return list(self._session.scalars(stmt).all())

    def due(self, now: datetime) -> list[RecurringSchedule]:
        """Active schedules whose persisted ``next_run_at`` is at or before ``now`` — a READ-ONLY view.

        This only *inspects* which schedules are due; it NEVER advances ``next_run_at`` and NEVER
        enqueues anything (that is the disabled DCA-108 capability). It exists so a human / future reader
        can see due times without mutating state.
        """
        stmt = (
            select(RecurringSchedule)
            .where(RecurringSchedule.business_id == self._business_id)
            .where(RecurringSchedule.status == ScheduleStatus.ACTIVE.value)
            .order_by(RecurringSchedule.next_run_at, RecurringSchedule.id)
        )
        return [
            row for row in self._session.scalars(stmt).all() if not _after(row.next_run_at, now)
        ]

    # -- the one write path -------------------------------------------------------------------------

    def create(
        self,
        data: ScheduleInput,
        manager: ScheduleManager,
        *,
        now: datetime | None = None,
    ) -> RecurringSchedule:
        """Authorize, gate and stage a NEW active schedule; return the (uncommitted) row.

        Order of gates (each leaves NO state behind when it refuses):

        1. **Authority FIRST.** A caller whose roles lack
           :attr:`~app.identity.permissions.Scope.SCHEDULE_MANAGE` is refused
           (:class:`ScheduleAuthorizationError`) before any state is read or written.
        2. **Shape.** An empty schedule key, a non-positive interval, or a missing ``next_run_at`` each
           raise a stable code and stage no row.
        3. **Confirmed.** The business must have a CONFIRMED active ICP (DCA-040); otherwise
           :data:`REASON_NOT_CONFIRMED`. The confirmed version is snapshotted as ``icp_version``.
        4. **Entitled.** The business must have a currently-entitled cached snapshot (DCA-023); otherwise
           :data:`REASON_NOT_ENTITLED`. Read-only — no live/network resolution is performed here.
        5. **Profile version.** If ``profile_version`` is supplied it must name an existing
           business-profile draft version of this business; otherwise :data:`REASON_PROFILE_VERSION_NOT_FOUND`.
        6. **Duplicate key.** A schedule already present for this ``(business, schedule_key)`` is a
           :class:`ScheduleConflict` — never an overwrite.

        On success a new ``ACTIVE`` schedule is staged with the persisted ``next_run_at`` and the pinned
        profile versions, plus an atomic DCA-026 audit entry (opaque references only) in the SAME unit of
        work. Storing the schedule runs NOTHING and sends nothing. The caller owns the transaction.
        """
        moment = now or datetime.now(UTC)

        # 1) Authority FIRST — an unauthorized caller is refused with no state touched.
        if not manager.may_manage():
            raise ScheduleAuthorizationError(
                "storing a recurring schedule requires a business administrator holding "
                f"'{Scope.SCHEDULE_MANAGE.value}'; an ordinary user, a read-only viewer or a non-member "
                "cannot perform it"
            )

        # 2) Shape — before any state is read.
        schedule_key = (data.schedule_key or "").strip()
        if not schedule_key:
            raise ScheduleError(REASON_MISSING_SCHEDULE_KEY, "a schedule_key is required")
        if data.interval_seconds is None or int(data.interval_seconds) <= 0:
            raise ScheduleError(
                REASON_INVALID_INTERVAL,
                f"interval_seconds must be a positive number of seconds (got {data.interval_seconds})",
                detail=str(data.interval_seconds),
            )
        if data.next_run_at is None:
            raise ScheduleError(REASON_MISSING_NEXT_RUN, "a next_run_at (due time) is required")

        # 3) Confirmed — the business must have a confirmed active ICP; snapshot its version.
        confirmation = IcpConfirmationStore(self._session, self._business_id).active()
        if confirmation is None:
            raise ScheduleError(
                REASON_NOT_CONFIRMED,
                "the business has no confirmed active ICP; a schedule can only be stored for a "
                "confirmed business (DCA-040)",
            )
        icp_version = confirmation.version

        # 4) Entitled — the business must have a currently-entitled cached snapshot (read-only).
        if not self._is_entitled(moment):
            raise ScheduleError(
                REASON_NOT_ENTITLED,
                "the business is not currently entitled; a schedule can only be stored for an entitled "
                "business (DCA-023)",
            )

        # 5) Profile version — if supplied, it must exist for this business.
        profile_version = data.profile_version
        if profile_version is not None and not self._profile_version_exists(profile_version):
            raise ScheduleError(
                REASON_PROFILE_VERSION_NOT_FOUND,
                f"no business-profile draft version {profile_version} exists for this business",
                detail=str(profile_version),
            )

        # 6) Duplicate key — a schedule already present for this key is a conflict (never overwrite).
        if self.by_key(schedule_key) is not None:
            raise ScheduleConflict(schedule_key)

        schedule = RecurringSchedule(
            id=str(uuid.uuid4()),
            business_id=self._business_id,
            schedule_key=schedule_key,
            interval_seconds=int(data.interval_seconds),
            next_run_at=data.next_run_at,
            status=ScheduleStatus.ACTIVE.value,
            icp_version=icp_version,
            profile_version=profile_version,
            actor_id=manager.subject,
            actor_type=manager.actor_type,
            created_at=moment,
            updated_at=moment,
        )
        self._session.add(schedule)

        # Atomic audit (SEC-001/BR-122): staged into the caller's session so it commits with the row.
        # References stay OPAQUE — ids / schedule key / cadence / pinned versions / due time — never any
        # raw value. Storing the schedule enqueues nothing and runs nothing.
        AuditSink(self._session, business_id=self._business_id, actor=manager.as_actor()).record(
            action=AUDIT_ACTION_STORE_SCHEDULE,
            reason=f"stored recurring schedule '{schedule_key}'",
            references={
                "scheduleId": schedule.id,
                "scheduleKey": schedule_key,
                "intervalSeconds": int(data.interval_seconds),
                "icpVersion": icp_version,
                "profileVersion": profile_version,
                "nextRunAt": _isoformat(data.next_run_at),
            },
        )
        return schedule

    # -- internals ----------------------------------------------------------------------------------

    def _is_entitled(self, now: datetime) -> bool:
        """Whether the DCA's own cached entitlement snapshot (DCA-023) currently authorizes this business.

        A READ-ONLY check against the bounded :class:`~app.db.models.EntitlementProjection` cache — it
        performs NO live/network resolution (that is the DCA-023 gate's job). The business is entitled
        only while a cached projection exists, is ACTIVE, and has not passed its ``expires_at``. A missing,
        non-active or lapsed projection fails closed (not entitled).
        """
        row = self._session.scalar(
            select(EntitlementProjection).where(
                EntitlementProjection.business_id == self._business_id
            )
        )
        if row is None or row.status != EntitlementStatus.ACTIVE.value:
            return False
        return row.expires_at is None or _after(row.expires_at, now)

    def _profile_version_exists(self, version: int) -> bool:
        stmt = (
            select(BusinessProfileDraft.id)
            .where(BusinessProfileDraft.business_id == self._business_id)
            .where(BusinessProfileDraft.version == version)
            .limit(1)
        )
        return self._session.scalars(stmt).first() is not None


def _after(moment: datetime, now: datetime) -> bool:
    """True if ``moment`` is strictly after ``now``, tolerating SQLite's naive datetimes."""
    return _as_utc(moment) > _as_utc(now)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _isoformat(value: datetime) -> str:
    return _as_utc(value).isoformat()
