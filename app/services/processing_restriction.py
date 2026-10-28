"""DCA-078 — store scoped processing restrictions (the restriction ledger).

Two single-sanctioned writers, one per authority plane, mirror the two restriction kinds:

- :class:`ProcessingRestrictionStore` — the LOCAL (tenant) ledger. It lets an AUTHORIZED business
  user record a scoped "do not process this scope" restriction for a scope the business owns: any
  combination of a brand it runs, a channel and a purpose (an empty dimension means "all" for that
  dimension, so a fully-empty scope is a tenant-wide restriction). Tenant-owned (RLS-protected),
  business-plane authority.

- :class:`PlatformSourceRestrictionStore` — the PLATFORM-WIDE source-removal ledger. It lets a
  platform OPERATOR record a justified removal of a catalogued :class:`~app.db.models.Source` across
  the whole platform (every tenant). Control-plane (NOT tenant-owned), platform-plane authority, with
  a REQUIRED justification.

The load-bearing guarantees (the DCA-078 acceptance criteria):

- **Local and platform-wide restrictions are distinguishable and require appropriate authority.** The
  two kinds are distinct records on distinct planes: a local restriction is tenant-owned and may only
  be written by a business user holding :attr:`~app.identity.permissions.Scope.PROCESSING_RESTRICT`; a
  platform-wide source removal is control-plane and may only be written by a platform operator holding
  :attr:`~app.identity.permissions.Scope.PLATFORM_SOURCE_RESTRICT`. The planes are disjoint, so a
  tenant administrator can never perform a platform-wide removal and a platform operator (holding no
  membership) can never place a client's local restriction. Each row stamps its ``authority_plane`` so
  the two are told apart even when read together.

- **A denial changes nothing.** Each writer checks authority FIRST and validates the scope / retention
  / justification BEFORE the session is mutated, so a refused restriction stages NO row and leaves no
  partial state.

Both ledgers are **versioned, insert-only**: each restriction of a scope inserts a NEW version at
``max(version) + 1`` for that scope; prior versions are preserved and never mutated/deleted, so the
restriction history (who, when, which retention) stays reviewable. Lifting a restriction is a later,
DISABLED capability.

A restriction only ever makes processing MORE restrictive and authorizes nothing: recording one
creates no prospect / host record and sends nothing — acting on it stays a disabled future capability
behind the one send pipeline. The local writer stages an atomic DCA-026 audit entry (opaque
references only) in the SAME unit of work; the platform writer has no single business scope, so — like
the DCA-050 revocation — the row itself is the durable who/when/why record. Both writers stage into
the caller's session and never commit; the caller owns the unit of work.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.sink import ActorType, AuditActor, AuditSink
from app.db.models import (
    ContactChannel,
    PlatformSourceRestriction,
    ProcessingRestriction,
    RestrictionAuthorityPlane,
    RestrictionRetentionMode,
    Source,
)
from app.identity.permissions import Scope, scopes_for_business_roles, scopes_for_platform_roles

# The controlled vocabulary of channels a local restriction may name (empty => all channels).
RESTRICTION_CHANNELS: tuple[str, ...] = tuple(c.value for c in ContactChannel)
# The controlled vocabulary of retention modes.
RETENTION_MODES: tuple[str, ...] = tuple(m.value for m in RestrictionRetentionMode)

# Audited action code for recording a LOCAL processing restriction (a controlled code).
AUDIT_ACTION_RECORD_RESTRICTION = "processing.restriction.record"

# Stable refusal codes. Each is raised BEFORE the session is mutated, so a refusal stages no row.
REASON_NOT_AUTHORIZED = "processing_restriction_not_authorized"
REASON_NOT_PLATFORM_OPERATOR = "not_platform_operator"
REASON_UNKNOWN_CHANNEL = "unknown_channel"
REASON_UNKNOWN_RETENTION_MODE = "unknown_retention_mode"
REASON_MISSING_RETAIN_UNTIL = "missing_retain_until"
REASON_UNEXPECTED_RETAIN_UNTIL = "unexpected_retain_until"
REASON_MISSING_SOURCE = "missing_source"
REASON_SOURCE_NOT_FOUND = "source_not_found"
REASON_MISSING_JUSTIFICATION = "missing_justification"


class ProcessingRestrictionError(Exception):
    """A restriction write was refused with a stable code. Raised before the session is mutated, so a
    refusal leaves no row and no partial state. ``detail`` names the offending value when useful."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class ProcessingRestrictionAuthorizationError(ProcessingRestrictionError):
    """The caller is not authorized to place this restriction.

    A distinct type so the HTTP boundary (where one exists) can map it to a 403 while the other codes
    map to a 404/422. Raised before anything is read or written. ``code`` is
    :data:`REASON_NOT_AUTHORIZED` (local) or :data:`REASON_NOT_PLATFORM_OPERATOR` (platform-wide).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


def _normalize_channel(value: str) -> str:
    """Normalize a channel; "" (all channels) passes through, a non-empty value must be a known channel."""
    token = (value or "").strip().lower()
    if not token:
        return ""
    try:
        return ContactChannel(token).value
    except ValueError as exc:
        raise ProcessingRestrictionError(
            REASON_UNKNOWN_CHANNEL,
            f"unknown channel '{value}'; expected one of: {', '.join(RESTRICTION_CHANNELS)} (or empty "
            "for all channels)",
            detail=value,
        ) from exc


def _normalize_retention(
    retention_mode: str, retain_until: datetime | None
) -> tuple[str, datetime | None]:
    """Validate the retention metadata and return the normalized (mode, retain_until).

    ``until_date`` REQUIRES a ``retain_until``; ``until_lifted`` must NOT carry one (it is indefinite).
    An unknown mode is refused. Each refusal is raised before the session is mutated.
    """
    token = (retention_mode or "").strip().lower() or RestrictionRetentionMode.UNTIL_LIFTED.value
    try:
        mode = RestrictionRetentionMode(token)
    except ValueError as exc:
        raise ProcessingRestrictionError(
            REASON_UNKNOWN_RETENTION_MODE,
            f"unknown retention_mode '{retention_mode}'; expected one of: {', '.join(RETENTION_MODES)}",
            detail=retention_mode,
        ) from exc
    if mode is RestrictionRetentionMode.UNTIL_DATE and retain_until is None:
        raise ProcessingRestrictionError(
            REASON_MISSING_RETAIN_UNTIL,
            "retention_mode 'until_date' requires a retain_until timestamp",
        )
    if mode is RestrictionRetentionMode.UNTIL_LIFTED and retain_until is not None:
        raise ProcessingRestrictionError(
            REASON_UNEXPECTED_RETAIN_UNTIL,
            "retention_mode 'until_lifted' is indefinite and must not carry a retain_until",
        )
    return mode.value, retain_until


# --------------------------------------------------------------------------------------------------
# LOCAL (tenant) restrictions — business-plane authority, tenant-owned, RLS-protected.
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RestrictionPrincipal:
    """The business principal placing a LOCAL restriction.

    ``subject`` is an opaque actor id (never a name/email) recorded on the restriction and its atomic
    audit entry. ``roles`` are the caller's business-plane membership roles (from the verified
    :class:`~app.identity.tenant_context.TenantContext`, NEVER a bare token claim). :meth:`may_restrict`
    grants the action only when those roles yield
    :attr:`~app.identity.permissions.Scope.PROCESSING_RESTRICT` — a read-only viewer's roles do not,
    and a platform operator holds no business scope for the tenant.
    """

    subject: str
    roles: tuple[str, ...] = ()
    actor_type: str = "user"

    def may_restrict(self) -> bool:
        return Scope.PROCESSING_RESTRICT in scopes_for_business_roles(self.roles)

    def as_actor(self) -> AuditActor:
        return AuditActor(id=self.subject, type=ActorType(self.actor_type))


@dataclass
class ProcessingRestrictionInput:
    """One local restriction as supplied by the caller.

    ``brand_key`` / ``channel`` / ``purpose`` are the scope dimensions — each OPTIONAL, where an empty
    value means "all" for that dimension (so leaving all three empty is a tenant-wide restriction).
    ``channel``, when set, must be a known :class:`~app.db.models.ContactChannel`. ``reason`` is a short
    opaque justification (optional). ``retention_mode`` is ``until_lifted`` (default, indefinite) or
    ``until_date`` (then ``retain_until`` is required).
    """

    brand_key: str = ""
    channel: str = ""
    purpose: str = ""
    reason: str = ""
    retention_mode: str = RestrictionRetentionMode.UNTIL_LIFTED.value
    retain_until: datetime | None = None


class ProcessingRestrictionStore:
    """Tenant-scoped reader/writer for LOCAL processing restrictions, bound to one business.

    Construct with the active session and the verified business scope (a route resolves it from the
    membership-verified tenant context). Every query is confined to that business — there is no
    argument to widen the scope.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def get(self, restriction_id: str) -> ProcessingRestriction | None:
        """Fetch one restriction by id WITHIN this business. Another business's id (or an unknown id)
        resolves to ``None`` — a cross-tenant id reads exactly like not-found."""
        if not restriction_id:
            return None
        stmt = (
            select(ProcessingRestriction)
            .where(ProcessingRestriction.business_id == self._business_id)
            .where(ProcessingRestriction.id == restriction_id)
        )
        return self._session.scalars(stmt).one_or_none()

    def restrictions(self) -> list[ProcessingRestriction]:
        """Every local restriction for this business, oldest first — the reviewable ledger."""
        stmt = (
            select(ProcessingRestriction)
            .where(ProcessingRestriction.business_id == self._business_id)
            .order_by(ProcessingRestriction.created_at, ProcessingRestriction.id)
        )
        return list(self._session.scalars(stmt).all())

    def history_for_scope(
        self, *, brand_key: str = "", channel: str = "", purpose: str = ""
    ) -> list[ProcessingRestriction]:
        """Every version for one (brand, channel, purpose) scope in this business, oldest first."""
        brand = (brand_key or "").strip().lower()
        chan = _normalize_channel(channel)
        purp = (purpose or "").strip().lower()
        stmt = (
            select(ProcessingRestriction)
            .where(ProcessingRestriction.business_id == self._business_id)
            .where(ProcessingRestriction.scope_brand == brand)
            .where(ProcessingRestriction.scope_channel == chan)
            .where(ProcessingRestriction.scope_purpose == purp)
            .order_by(ProcessingRestriction.version)
        )
        return list(self._session.scalars(stmt).all())

    # -- the one write path -------------------------------------------------------------------------

    def _next_version(self, brand: str, chan: str, purp: str) -> int:
        current = self._session.scalar(
            select(func.max(ProcessingRestriction.version))
            .where(ProcessingRestriction.business_id == self._business_id)
            .where(ProcessingRestriction.scope_brand == brand)
            .where(ProcessingRestriction.scope_channel == chan)
            .where(ProcessingRestriction.scope_purpose == purp)
        )
        return int(current or 0) + 1

    def restrict(
        self,
        data: ProcessingRestrictionInput,
        principal: RestrictionPrincipal,
        *,
        now: datetime | None = None,
    ) -> ProcessingRestriction:
        """Authorize, validate and stage a NEW restriction version; return the (uncommitted) row.

        Authorization is checked FIRST: a caller whose roles lack
        :attr:`~app.identity.permissions.Scope.PROCESSING_RESTRICT` is refused with
        :class:`ProcessingRestrictionAuthorizationError` before any state is read or written (the
        relevant denial). Then the scope + retention are validated before the session is mutated — an
        unknown channel or an inconsistent retention (``until_date`` with no ``retain_until``, or
        ``until_lifted`` with one) each raise with a stable code and stage NO row. On success a new
        version is inserted at ``max(version)+1`` for the (brand, channel, purpose) scope, preserving
        every prior version, plus an atomic DCA-026 audit entry (opaque references only) in the SAME
        unit of work. The caller owns the transaction.
        """
        moment = now or datetime.now(UTC)

        # 1) Authority FIRST — an unauthorized caller is refused with no state touched.
        if not principal.may_restrict():
            raise ProcessingRestrictionAuthorizationError(
                REASON_NOT_AUTHORIZED,
                "placing a processing restriction requires a business user holding "
                f"'{Scope.PROCESSING_RESTRICT.value}'; a read-only viewer or a non-member cannot "
                "perform it, and a platform operator holds no business scope for this tenant",
            )

        brand = (data.brand_key or "").strip().lower()
        chan = _normalize_channel(data.channel)
        purp = (data.purpose or "").strip().lower()
        mode, retain_until = _normalize_retention(data.retention_mode, data.retain_until)

        version = self._next_version(brand, chan, purp)
        restriction = ProcessingRestriction(
            id=str(uuid.uuid4()),
            business_id=self._business_id,
            scope_brand=brand,
            scope_channel=chan,
            scope_purpose=purp,
            version=version,
            authority_plane=RestrictionAuthorityPlane.BUSINESS.value,
            authority_scope=Scope.PROCESSING_RESTRICT.value,
            authorized_by=principal.subject,
            authorized_by_type=principal.actor_type,
            reason=(data.reason or "").strip(),
            retention_mode=mode,
            retain_until=retain_until,
            created_at=moment,
        )
        self._session.add(restriction)

        # Atomic audit (SEC-001/BR-122): staged into the caller's session so it commits with the row.
        # References stay OPAQUE — ids, scope dimensions, version, plane — never a raw contact value.
        AuditSink(self._session, business_id=self._business_id, actor=principal.as_actor()).record(
            action=AUDIT_ACTION_RECORD_RESTRICTION,
            reason=f"recorded processing restriction '{restriction.id}' (version {version})",
            references={
                "restrictionId": restriction.id,
                "authorityPlane": RestrictionAuthorityPlane.BUSINESS.value,
                "authorityScope": Scope.PROCESSING_RESTRICT.value,
                "scopeBrand": brand,
                "scopeChannel": chan,
                "scopePurpose": purp,
                "version": version,
                "retentionMode": mode,
            },
        )
        return restriction


# --------------------------------------------------------------------------------------------------
# PLATFORM-WIDE source removal — platform-plane authority, control-plane (NOT tenant-owned).
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PlatformRestrictionPrincipal:
    """The platform principal placing a PLATFORM-WIDE source removal.

    ``roles`` are the caller's PLATFORM-plane roles (from the authenticated, signed principal — never a
    bare header). :meth:`may_restrict_platform` grants the action only when these roles yield
    :attr:`~app.identity.permissions.Scope.PLATFORM_SOURCE_RESTRICT`; a tenant administrator's business
    roles never do. ``subject`` is an opaque operator id recorded as the authorizer.
    """

    subject: str
    roles: tuple[str, ...] = ()

    def may_restrict_platform(self) -> bool:
        return Scope.PLATFORM_SOURCE_RESTRICT in scopes_for_platform_roles(self.roles)


class PlatformSourceRestrictionStore:
    """The single sanctioned writer of a platform-wide source removal (DCA-078).

    Platform control-plane; session-only (stages into the caller's session, never commits). The row
    itself is the durable who/when/why/justification record, so — like the DCA-050 revocation — there
    is no separate tenant audit entry (the audit sink is tenant-scoped and a platform-wide action has
    no single business scope).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- reads (platform-wide) ----------------------------------------------------------------------

    def get(self, restriction_id: str) -> PlatformSourceRestriction | None:
        if not restriction_id:
            return None
        stmt = select(PlatformSourceRestriction).where(
            PlatformSourceRestriction.id == restriction_id
        )
        return self._session.scalars(stmt).one_or_none()

    def restrictions(self) -> list[PlatformSourceRestriction]:
        """Every platform-wide source removal, oldest first."""
        stmt = select(PlatformSourceRestriction).order_by(
            PlatformSourceRestriction.created_at, PlatformSourceRestriction.id
        )
        return list(self._session.scalars(stmt).all())

    def for_source(self, source_key: str) -> list[PlatformSourceRestriction]:
        """Every version recorded for one source, oldest first."""
        key = (source_key or "").strip()
        stmt = (
            select(PlatformSourceRestriction)
            .where(PlatformSourceRestriction.source_key == key)
            .order_by(PlatformSourceRestriction.version)
        )
        return list(self._session.scalars(stmt).all())

    # -- the one write path -------------------------------------------------------------------------

    def _next_version(self, source_key: str) -> int:
        current = self._session.scalar(
            select(func.max(PlatformSourceRestriction.version)).where(
                PlatformSourceRestriction.source_key == source_key
            )
        )
        return int(current or 0) + 1

    def restrict_source(
        self,
        source_key: str,
        principal: PlatformRestrictionPrincipal,
        *,
        justification: str,
        retention_mode: str = RestrictionRetentionMode.UNTIL_LIFTED.value,
        retain_until: datetime | None = None,
        now: datetime | None = None,
    ) -> PlatformSourceRestriction:
        """Authorize, validate and stage a NEW platform-wide source-removal version; return the row.

        Authorization is checked FIRST: only a platform operator holding
        :attr:`~app.identity.permissions.Scope.PLATFORM_SOURCE_RESTRICT` may remove a source platform-
        wide — a tenant administrator (any non-operator) is refused before any state is read or written.
        Then a non-empty ``justification`` is required (a platform-wide removal affects every tenant, so
        it must record WHY), the source must exist in the catalog, and the retention metadata is
        validated — each refusal is raised before the session is mutated, so it stages NO row. On
        success a new version is inserted at ``max(version)+1`` for the source, preserving every prior
        version. The caller owns the transaction.
        """
        moment = now or datetime.now(UTC)

        # 1) Authority FIRST — a tenant admin (or any non-operator) is refused with no state touched.
        if not principal.may_restrict_platform():
            raise ProcessingRestrictionAuthorizationError(
                REASON_NOT_PLATFORM_OPERATOR,
                "removing a source platform-wide requires a platform operator "
                f"('{Scope.PLATFORM_SOURCE_RESTRICT.value}'); a tenant administrator cannot perform it",
            )

        key = (source_key or "").strip()
        if not key:
            raise ProcessingRestrictionError(
                REASON_MISSING_SOURCE, "a platform-wide removal must name the source to remove"
            )

        justification_text = (justification or "").strip()
        if not justification_text:
            raise ProcessingRestrictionError(
                REASON_MISSING_JUSTIFICATION,
                "a platform-wide source removal must carry a non-empty justification (it affects "
                "every tenant)",
            )

        if (
            self._session.scalars(select(Source.id).where(Source.key == key).limit(1)).first()
            is None
        ):
            raise ProcessingRestrictionError(
                REASON_SOURCE_NOT_FOUND,
                f"source '{key}' does not exist in the platform catalog; nothing to remove",
                detail=key,
            )

        mode, normalized_retain = _normalize_retention(retention_mode, retain_until)

        version = self._next_version(key)
        restriction = PlatformSourceRestriction(
            id=str(uuid.uuid4()),
            source_key=key,
            version=version,
            authority_plane=RestrictionAuthorityPlane.PLATFORM.value,
            authority_scope=Scope.PLATFORM_SOURCE_RESTRICT.value,
            authorized_by=principal.subject,
            justification=justification_text,
            retention_mode=mode,
            retain_until=normalized_retain,
            created_at=moment,
        )
        self._session.add(restriction)
        return restriction
