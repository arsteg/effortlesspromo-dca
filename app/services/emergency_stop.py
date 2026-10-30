"""DCA-111 — enforce hierarchical emergency stops (the kill-switch ledger + resolver).

Key: ``kill-switch`` · source sections 2, 3, 8. Stage: Durable Python execution.

One capability: a HIERARCHICAL emergency stop over the DCA's own background-work machinery, with two
tiers whose authority planes are disjoint and where the higher tier DOMINATES the lower:

- :class:`PlatformEmergencyStopStore` — the PLATFORM-WIDE switch (top tier). A platform OPERATOR holding
  :attr:`~app.identity.permissions.Scope.PLATFORM_EMERGENCY_STOP` sets/clears it; while ACTIVE it pauses
  ALL tenants' background work, regardless of any per-business state. Control-plane (NOT tenant-owned),
  platform-plane authority; the row itself is the durable who/when/why record (like the DCA-078 platform
  source removal), so there is no tenant audit entry.

- :class:`BusinessEmergencyStopStore` — the PER-BUSINESS switch (lower tier). A business ADMINISTRATOR
  holding :attr:`~app.identity.permissions.Scope.EMERGENCY_STOP` sets/clears it for ITS OWN business;
  while ACTIVE it pauses only that business's background work, leaving every other (un-stopped) business
  running. Tenant-owned (RLS-protected), business-plane authority; each set/clear stages an atomic
  DCA-026 audit entry in the SAME unit of work.

The load-bearing guarantee (the DCA-111 acceptance criteria):

- **A pause stops new relevant side effects at checkpoints while unrelated allowed work continues.** The
  :class:`EmergencyStopResolver` computes the effective block for a business as *platform ACTIVE OR this
  business ACTIVE* — and the worker (DCA-109) consults it at its claim/maintenance checkpoints, so a
  stopped scope's durable jobs are never claimed/advanced and its due schedules are never enqueued, while
  an un-stopped business keeps working. The platform tier dominates: a platform stop blocks a business
  even when that business's own latest state is ``LIFTED``.

- **Setting/clearing is authority-gated, and the relevant denial changes nothing.** Each writer checks
  authority FIRST (platform scope for the platform stop, the business admin scope for a business stop)
  and requires a non-empty, opaque ``reason`` BEFORE the session is mutated — so a refused set/clear
  stages no row and leaves the current effective state exactly as it was. A business administrator can
  never set the platform stop and a platform operator (holding no membership) can never set a client's
  business stop — the planes are disjoint (:mod:`app.identity.permissions`).

Both ledgers are **versioned, insert-only**: each set/clear inserts a NEW version at ``max(version)+1``
for its scope, so the switch is toggled WITHOUT deleting history (the story's "without deleting existing
records"); the CURRENT effective state is the latest version's ``state``. Both writers stage into the
caller's session and never commit; the caller owns the unit of work.

**Execution stays a disabled future capability.** A stop makes the agent do LESS; it never enables a
fetch, a model call, a result commit or a send. The DCA has no send path, and acceptance of a prospect is
not recipient consent — a stop only ever tightens that, it never loosens it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.sink import ActorType, AuditActor, AuditSink, assert_opaque_text
from app.db.models import (
    BusinessEmergencyStop,
    EmergencyStopState,
    PlatformEmergencyStop,
    RestrictionAuthorityPlane,
)
from app.identity.permissions import Scope, scopes_for_business_roles, scopes_for_platform_roles

# The fixed logical scope key for the single platform-wide switch (it versions like the other ledgers).
PLATFORM_SCOPE_KEY = "platform"

# Audited action codes for the per-business tier (controlled codes, like the other DCA writes). The
# platform tier is recorded by the row itself (no tenant audit — a platform action has no business scope).
AUDIT_ACTION_SET = "emergency_stop.set"
AUDIT_ACTION_CLEAR = "emergency_stop.clear"

# Stable refusal codes. Each is raised BEFORE the session is mutated, so a refusal stages no row.
REASON_NOT_AUTHORIZED = "emergency_stop_not_authorized"
REASON_NOT_PLATFORM_OPERATOR = "emergency_stop_not_platform_operator"
REASON_MISSING_REASON = "emergency_stop_missing_reason"


class EmergencyStopError(Exception):
    """A set/clear was refused with a stable ``code`` for an INPUT problem (e.g. a missing reason).

    Raised before the row is staged, so a refusal leaves the current effective state exactly as it was.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EmergencyStopAuthorizationError(EmergencyStopError):
    """The caller is not authorized to set/clear this emergency stop.

    A distinct type so an HTTP boundary (where one exists) can map it to a 403. Raised BEFORE anything is
    read or written, so an unauthorized attempt leaves no trace and changes nothing. ``code`` is
    :data:`REASON_NOT_AUTHORIZED` (business tier) or :data:`REASON_NOT_PLATFORM_OPERATOR` (platform tier).
    """


def _require_reason(reason: str) -> str:
    """Normalize + screen a non-empty opaque reason, or raise before the session is touched."""
    text = (reason or "").strip()
    if not text:
        raise EmergencyStopError(
            REASON_MISSING_REASON,
            "setting or clearing an emergency stop requires a non-empty reason (an opaque "
            "code/explanation of why the switch is being toggled)",
        )
    # Screen like an audit reference: it is persisted and must carry no raw contact value / secret.
    assert_opaque_text("reason", text)
    return text


# --------------------------------------------------------------------------------------------------
# PER-BUSINESS stop — business-plane authority, tenant-owned, RLS-protected.
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BusinessStopPrincipal:
    """The business principal setting/clearing a PER-BUSINESS emergency stop.

    ``subject`` is an opaque actor id (never a name/email) recorded on the row and its atomic audit
    entry. ``roles`` are the caller's business-plane membership roles (from the verified
    :class:`~app.identity.tenant_context.TenantContext`, NEVER a bare token claim).
    :meth:`may_control` grants the action only when those roles yield
    :attr:`~app.identity.permissions.Scope.EMERGENCY_STOP` — a read-only viewer and an ordinary acting
    user do not, and a platform operator holds no business scope for the tenant.
    """

    subject: str
    roles: tuple[str, ...] = ()
    actor_type: str = "user"

    def may_control(self) -> bool:
        return Scope.EMERGENCY_STOP in scopes_for_business_roles(self.roles)

    def as_actor(self) -> AuditActor:
        return AuditActor(id=self.subject, type=ActorType(self.actor_type))


class BusinessEmergencyStopStore:
    """Tenant-scoped reader/writer for a business's own emergency stop, bound to one business.

    Construct with the active session and the verified business scope. Every query is confined to that
    business — there is no argument to widen the scope. Stages into the caller's session and never
    commits; the set/clear and its atomic DCA-026 audit entry commit together in the caller's unit of
    work. Setting a stop runs nothing and sends nothing."""

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    @property
    def business_id(self) -> str:
        return self._business_id

    def current(self) -> BusinessEmergencyStop | None:
        """The latest (highest-version) ledger row for this business, or ``None`` if never set."""
        stmt = (
            select(BusinessEmergencyStop)
            .where(BusinessEmergencyStop.business_id == self._business_id)
            .order_by(BusinessEmergencyStop.version.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).first()

    def is_stopped(self) -> bool:
        """True iff this business's CURRENT effective state is ``ACTIVE`` (its latest event is a stop)."""
        row = self.current()
        return row is not None and row.state == EmergencyStopState.ACTIVE.value

    def history(self) -> list[BusinessEmergencyStop]:
        """Every ledger version for this business, oldest first — the reviewable switch history."""
        stmt = (
            select(BusinessEmergencyStop)
            .where(BusinessEmergencyStop.business_id == self._business_id)
            .order_by(BusinessEmergencyStop.version)
        )
        return list(self._session.scalars(stmt).all())

    def _next_version(self) -> int:
        current = self._session.scalar(
            select(func.max(BusinessEmergencyStop.version)).where(
                BusinessEmergencyStop.business_id == self._business_id
            )
        )
        return int(current or 0) + 1

    def set_stop(
        self, principal: BusinessStopPrincipal, *, reason: str, now: datetime | None = None
    ) -> BusinessEmergencyStop:
        """PAUSE this business's background work: stage a new ``ACTIVE`` version (+ atomic audit)."""
        return self._record(EmergencyStopState.ACTIVE, principal, reason=reason, now=now)

    def clear_stop(
        self, principal: BusinessStopPrincipal, *, reason: str, now: datetime | None = None
    ) -> BusinessEmergencyStop:
        """RESUME this business's background work: stage a new ``LIFTED`` version (+ atomic audit)."""
        return self._record(EmergencyStopState.LIFTED, principal, reason=reason, now=now)

    def _record(
        self,
        state: EmergencyStopState,
        principal: BusinessStopPrincipal,
        *,
        reason: str,
        now: datetime | None,
    ) -> BusinessEmergencyStop:
        moment = now or datetime.now(UTC)
        # 1) Authority FIRST — an unauthorized caller is refused with no state touched.
        if not principal.may_control():
            raise EmergencyStopAuthorizationError(
                REASON_NOT_AUTHORIZED,
                "setting or clearing a business emergency stop requires a business administrator holding "
                f"'{Scope.EMERGENCY_STOP.value}'; a read-only viewer, an ordinary acting user or a "
                "platform operator (holding no business scope) cannot perform it",
            )
        reason_text = _require_reason(reason)

        version = self._next_version()
        row = BusinessEmergencyStop(
            id=str(uuid.uuid4()),
            business_id=self._business_id,
            version=version,
            state=state.value,
            authority_plane=RestrictionAuthorityPlane.BUSINESS.value,
            authority_scope=Scope.EMERGENCY_STOP.value,
            authorized_by=principal.subject,
            authorized_by_type=principal.actor_type,
            reason=reason_text,
            created_at=moment,
        )
        self._session.add(row)

        # Atomic audit (SEC-001/BR-122): staged into the caller's session so it commits with the row.
        # References stay OPAQUE — ids, the plane/scope, the resulting state and version; never contact data.
        action = AUDIT_ACTION_SET if state is EmergencyStopState.ACTIVE else AUDIT_ACTION_CLEAR
        AuditSink(self._session, business_id=self._business_id, actor=principal.as_actor()).record(
            action=action,
            reason=reason_text,
            references={
                "emergencyStopId": row.id,
                "authorityPlane": RestrictionAuthorityPlane.BUSINESS.value,
                "authorityScope": Scope.EMERGENCY_STOP.value,
                "state": state.value,
                "version": version,
            },
        )
        return row


# --------------------------------------------------------------------------------------------------
# PLATFORM-WIDE stop — platform-plane authority, control-plane (NOT tenant-owned).
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PlatformStopPrincipal:
    """The platform principal setting/clearing the PLATFORM-WIDE emergency stop.

    ``roles`` are the caller's PLATFORM-plane roles (from the authenticated, signed principal — never a
    bare header). :meth:`may_control` grants the action only when these roles yield
    :attr:`~app.identity.permissions.Scope.PLATFORM_EMERGENCY_STOP`; a business administrator's business
    roles never do. ``subject`` is an opaque operator id recorded as the authorizer on the row.
    """

    subject: str
    roles: tuple[str, ...] = ()

    def may_control(self) -> bool:
        return Scope.PLATFORM_EMERGENCY_STOP in scopes_for_platform_roles(self.roles)


class PlatformEmergencyStopStore:
    """The single sanctioned writer/reader of the platform-wide emergency stop (DCA-111).

    Platform control-plane; session-only (stages into the caller's session, never commits). The row
    itself is the durable who/when/why record, so — like the DCA-078 platform source removal — there is
    no separate tenant audit entry (the audit sink is tenant-scoped and a platform-wide action has no
    single business scope)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def current(self) -> PlatformEmergencyStop | None:
        """The latest (highest-version) platform ledger row, or ``None`` if never set."""
        stmt = (
            select(PlatformEmergencyStop)
            .where(PlatformEmergencyStop.scope_key == PLATFORM_SCOPE_KEY)
            .order_by(PlatformEmergencyStop.version.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).first()

    def is_stopped(self) -> bool:
        """True iff the platform-wide CURRENT effective state is ``ACTIVE`` (the latest event is a stop)."""
        row = self.current()
        return row is not None and row.state == EmergencyStopState.ACTIVE.value

    def history(self) -> list[PlatformEmergencyStop]:
        """Every platform ledger version, oldest first — the reviewable switch history."""
        stmt = (
            select(PlatformEmergencyStop)
            .where(PlatformEmergencyStop.scope_key == PLATFORM_SCOPE_KEY)
            .order_by(PlatformEmergencyStop.version)
        )
        return list(self._session.scalars(stmt).all())

    def _next_version(self) -> int:
        current = self._session.scalar(
            select(func.max(PlatformEmergencyStop.version)).where(
                PlatformEmergencyStop.scope_key == PLATFORM_SCOPE_KEY
            )
        )
        return int(current or 0) + 1

    def set_stop(
        self, principal: PlatformStopPrincipal, *, reason: str, now: datetime | None = None
    ) -> PlatformEmergencyStop:
        """PAUSE ALL tenants' background work: stage a new ``ACTIVE`` platform version."""
        return self._record(EmergencyStopState.ACTIVE, principal, reason=reason, now=now)

    def clear_stop(
        self, principal: PlatformStopPrincipal, *, reason: str, now: datetime | None = None
    ) -> PlatformEmergencyStop:
        """RESUME tenants (except those still paused by their own business stop): stage ``LIFTED``."""
        return self._record(EmergencyStopState.LIFTED, principal, reason=reason, now=now)

    def _record(
        self,
        state: EmergencyStopState,
        principal: PlatformStopPrincipal,
        *,
        reason: str,
        now: datetime | None,
    ) -> PlatformEmergencyStop:
        moment = now or datetime.now(UTC)
        # 1) Authority FIRST — a business admin (or any non-operator) is refused with no state touched.
        if not principal.may_control():
            raise EmergencyStopAuthorizationError(
                REASON_NOT_PLATFORM_OPERATOR,
                "setting or clearing the platform-wide emergency stop requires a platform operator "
                f"('{Scope.PLATFORM_EMERGENCY_STOP.value}'); a business administrator cannot perform it",
            )
        reason_text = _require_reason(reason)

        version = self._next_version()
        row = PlatformEmergencyStop(
            id=str(uuid.uuid4()),
            scope_key=PLATFORM_SCOPE_KEY,
            version=version,
            state=state.value,
            authority_plane=RestrictionAuthorityPlane.PLATFORM.value,
            authority_scope=Scope.PLATFORM_EMERGENCY_STOP.value,
            authorized_by=principal.subject,
            reason=reason_text,
            created_at=moment,
        )
        self._session.add(row)
        return row


# --------------------------------------------------------------------------------------------------
# The hierarchy resolver — the read the worker consults at its checkpoints.
# --------------------------------------------------------------------------------------------------


class EmergencyStopResolver:
    """Read-only resolver of the HIERARCHICAL effective block for a business (the worker's checkpoint read).

    Bound to one session. :meth:`is_claiming_blocked` is the single question the DCA-109 worker asks
    before it claims/advances a business's jobs or runs that business's maintenance ticks: the block is
    the hierarchy *platform ACTIVE OR this business ACTIVE*, so the platform tier DOMINATES — a
    platform-wide stop blocks every business regardless of its own latest state, while a per-business stop
    blocks only that one. It is fail-closed in spirit (a stop can only ADD a block), reads no payload and
    touches nothing."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def is_platform_stopped(self) -> bool:
        return PlatformEmergencyStopStore(self._session).is_stopped()

    def is_business_stopped(self, business_id: str) -> bool:
        return BusinessEmergencyStopStore(self._session, business_id).is_stopped()

    def is_claiming_blocked(self, business_id: str) -> bool:
        """True iff the platform-wide stop is active OR this business's own stop is active."""
        return self.is_platform_stopped() or self.is_business_stopped(business_id)
