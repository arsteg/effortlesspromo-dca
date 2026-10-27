"""DCA-047 — restrict source access per business (narrow platform-approved rights).

:class:`BusinessSourceEntitlementStore` is the single sanctioned writer of
:class:`~app.db.models.BusinessSourceEntitlement`. It lets ONE business record, per catalog source,
the field/operation/purpose rights it grants ITSELF — always a *narrowing* of the platform-approved
rights, never a widening.

The load-bearing guarantee (the DCA-047 acceptance criterion):

- **A business cannot enable a source or operation denied at platform level.** The platform ceiling
  is the in-force :class:`~app.db.models.SourceTermsApproval` (DCA-046). Before anything is staged
  the store:

  1. refuses to entitle a source that has NO in-force platform approval
     (:data:`REASON_SOURCE_NOT_PLATFORM_APPROVED`) — a business can never enable a source the platform
     has not approved (or whose approval has lapsed);
  2. refuses any source-wide operation the approval does not grant
     (:data:`REASON_OPERATION_DENIED_AT_PLATFORM`);
  3. refuses any per-field right the approval does not grant for that field
     (:data:`REASON_FIELD_RIGHT_DENIED_AT_PLATFORM`);
  4. refuses any purpose the approved terms version does not document
     (:data:`REASON_PURPOSE_DENIED_AT_PLATFORM`).

  Each refusal is raised BEFORE the session is mutated, so a rejected broadening leaves no row and no
  partial state. On success the stored row carries ONLY the validated (narrowed) rights, so an
  entitlement can never silently exceed the platform grant.

Authority is the business-plane administrator scope
:attr:`~app.identity.permissions.Scope.SOURCE_APPROVE` ("a business-local intent to enable a data
source"): an ordinary business user cannot configure an entitlement, and a platform operator holds no
business scope for the tenant, so neither can configure it either. The check runs FIRST, before any
state is read or written.

Like the DCA-042 budget store, this is tenant-owned data (per ``business_id``, RLS-protected): the
store is bound to one verified business scope and every query is confined to it. It stages into the
caller's session and never commits — the caller owns the unit of work, so the entitlement row and its
atomic DCA-026 audit entry commit together.

Entitling a source grants NO acquisition right on its own: no discovery path consults this table, and
the source catalog entry staying ENABLED (DCA-044) remains a disabled future capability. So an
entitlement records WHAT the business has opted into, never authorizing acquisition by itself —
future capabilities stay disabled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.sink import ActorType, AuditActor, AuditSink
from app.db.models import BusinessSourceEntitlement, SourceOperation, SourceTermsApproval
from app.identity.permissions import Scope, scopes_for_business_roles
from app.services.source_approval import SourceApprovalStore
from app.services.source_terms import SourceTermsStore

# The controlled vocabulary of operations an entitlement may grant a right for (shared with DCA-045).
OPERATIONS: tuple[str, ...] = tuple(op.value for op in SourceOperation)

# The audited action code for configuring a per-business source entitlement.
AUDIT_ACTION_SET_ENTITLEMENT = "source.entitlement.set"

# Authorization refusal (raised FIRST, before any state is read or written).
REASON_NOT_BUSINESS_ADMINISTRATOR = "not_business_administrator"

# Validation / narrowing refusals (each raised BEFORE the session is mutated, so a refusal stages
# nothing). These are the DCA-047 denials: a business cannot enable a source or grant a right the
# platform withheld.
REASON_MISSING_SOURCE = "missing_source_key"
REASON_UNKNOWN_OPERATION = "unknown_operation"
REASON_SOURCE_NOT_PLATFORM_APPROVED = "source_not_platform_approved"
REASON_OPERATION_DENIED_AT_PLATFORM = "operation_denied_at_platform"
REASON_FIELD_RIGHT_DENIED_AT_PLATFORM = "field_right_denied_at_platform"
REASON_PURPOSE_DENIED_AT_PLATFORM = "purpose_denied_at_platform"

# Denials a consumer sees when a narrowed right is evaluated (not a staging error).
REASON_SOURCE_NOT_ENTITLED = "source_not_entitled"
REASON_OPERATION_NOT_ENTITLED = "operation_not_entitled"


class BusinessSourceEntitlementAuthorizationError(Exception):
    """The caller is not authorized to configure a business source entitlement.

    Raised BEFORE any state is read or staged (an ordinary user or a non-member platform operator
    learns nothing and changes nothing). ``code`` is :data:`REASON_NOT_BUSINESS_ADMINISTRATOR`.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = REASON_NOT_BUSINESS_ADMINISTRATOR


class BusinessSourceEntitlementError(Exception):
    """An entitlement was refused with a stable code. Raised before the session is mutated, so a
    refusal leaves no row and no partial state. ``detail`` names the offending value."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class BusinessEntitler:
    """The business principal configuring an entitlement.

    ``roles`` are the caller's business-plane membership roles (from the verified
    :class:`~app.identity.tenant_context.TenantContext`, never a bare header).
    :meth:`BusinessSourceEntitlementStore.set_entitlement` grants the action only when these roles
    yield :attr:`~app.identity.permissions.Scope.SOURCE_APPROVE` — an ordinary user's roles do not,
    and a platform operator holds no business scope for the tenant. ``subject`` is an opaque actor id
    recorded on the entitlement and its atomic audit entry.
    """

    subject: str
    roles: tuple[str, ...] = ()

    def may_configure(self) -> bool:
        return Scope.SOURCE_APPROVE in scopes_for_business_roles(self.roles)


@dataclass
class BusinessSourceEntitlementInput:
    """One business source entitlement as supplied by the caller.

    ``operations`` is the business's narrowed source-wide grant per operation; ``field_rights`` are
    per-field overrides; ``purposes`` the purposes the business limits its use to. Each is validated
    to be a SUBSET of the platform-approved rights before anything is staged — a request to grant a
    right the platform denies is refused. ``enabled`` is the business's own enable intent; it can only
    be set against a source with an in-force platform approval.
    """

    source_key: str
    enabled: bool = False
    operations: dict[str, bool] = field(default_factory=dict)
    field_rights: dict[str, dict[str, bool]] = field(default_factory=dict)
    purposes: list[str] = field(default_factory=list)


def _normalize_operation(value: str) -> str:
    """Return the canonical operation value, or raise :data:`REASON_UNKNOWN_OPERATION`."""
    token = (value or "").strip().lower()
    try:
        return SourceOperation(token).value
    except ValueError as exc:
        raise BusinessSourceEntitlementError(
            REASON_UNKNOWN_OPERATION,
            f"unknown operation '{value}'; expected one of: {', '.join(OPERATIONS)}",
            detail=value,
        ) from exc


def _ceiling_allows(approval: SourceTermsApproval, field_name: str | None, operation: str) -> bool:
    """True if the platform approval grants ``operation`` (on ``field_name`` when given).

    Mirrors the DCA-045/046 evaluation: a per-field override wins over the source-wide default, and an
    operation absent from both is denied (deny-by-default). This is the ceiling the business narrows.
    """
    if field_name is not None:
        overrides = (approval.approved_field_rights or {}).get(field_name, {})
        if operation in overrides:
            return bool(overrides[operation])
    return bool((approval.approved_operation_rights or {}).get(operation, False))


class BusinessSourceEntitlementStore:
    """Tenant-scoped reader/writer for per-business source entitlements, bound to one business.

    Construct with the active session and the verified business scope (the route resolves it from the
    membership-verified tenant context). Every query is confined to that business.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def versions(self, source_key: str) -> list[BusinessSourceEntitlement]:
        """All entitlement versions for this business + source, oldest first (prior versions kept)."""
        stmt = (
            select(BusinessSourceEntitlement)
            .where(BusinessSourceEntitlement.business_id == self._business_id)
            .where(BusinessSourceEntitlement.source_key == (source_key or "").strip())
            .order_by(BusinessSourceEntitlement.version)
        )
        return list(self._session.scalars(stmt).all())

    def latest(self, source_key: str) -> BusinessSourceEntitlement | None:
        """The highest-version (in-force) entitlement for this business + source, or ``None``."""
        stmt = (
            select(BusinessSourceEntitlement)
            .where(BusinessSourceEntitlement.business_id == self._business_id)
            .where(BusinessSourceEntitlement.source_key == (source_key or "").strip())
            .order_by(BusinessSourceEntitlement.version.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).one_or_none()

    def permits(
        self,
        source_key: str,
        field_name: str,
        operation: str,
        *,
        now: datetime | None = None,
    ) -> tuple[bool, str | None]:
        """Decide whether this business may use ``operation`` on ``field_name`` of ``source_key``.

        The decision is the INTERSECTION of the platform ceiling and the business narrowing: allowed
        only when (a) the source has an in-force platform approval that grants the right AND (b) the
        business's latest entitlement is enabled and grants it too. Returns ``(allowed, reason)`` with
        a stable reason on denial. A pure, side-effect-free read.
        """
        op = _normalize_operation(operation)
        approval = self._in_force_approval(source_key, now=now)
        if approval is None or not _ceiling_allows(approval, field_name, op):
            # Denied (or never granted) at the platform level — the ceiling itself withholds it.
            return False, (
                REASON_SOURCE_NOT_PLATFORM_APPROVED
                if approval is None
                else REASON_OPERATION_DENIED_AT_PLATFORM
            )
        entitlement = self.latest(source_key)
        if entitlement is None or not entitlement.enabled:
            return False, REASON_SOURCE_NOT_ENTITLED
        overrides = (entitlement.field_rights or {}).get(field_name, {})
        granted = (
            bool(overrides[op])
            if op in overrides
            else bool((entitlement.operation_rights or {}).get(op, False))
        )
        return (granted, None) if granted else (False, REASON_OPERATION_NOT_ENTITLED)

    # -- the platform ceiling -----------------------------------------------------------------------

    def in_force_approval(
        self, source_key: str, *, now: datetime | None = None
    ) -> SourceTermsApproval | None:
        """The in-force platform approval ceiling for ``source_key`` at ``now``, or ``None``.

        A public read over :meth:`_in_force_approval` for composing consumers (the DCA-048 source
        preflight), so a reader can resolve the ceiling without reaching into a private method. A
        pure, side-effect-free read.
        """
        return self._in_force_approval(source_key, now=now)

    def approved_purposes(self, approval: SourceTermsApproval) -> set[str]:
        """The purposes the terms version this ``approval`` endorses documents (the purpose ceiling).

        A public read over :meth:`_approved_purposes` for the DCA-048 preflight, so the purpose
        ceiling can be evaluated without reaching into a private method.
        """
        return self._approved_purposes(approval)

    def _in_force_approval(
        self, source_key: str, *, now: datetime | None = None
    ) -> SourceTermsApproval | None:
        """The highest-version in-force platform approval for ``source_key``, or ``None``.

        The source is "approved at the platform level" only while such an approval is in force
        (DCA-046 ``is_in_force``: active AND inside its validity window). A lapsed or revoked approval
        yields ``None`` — so a business can never entitle a source whose approval has expired.
        """
        moment = now or datetime.now(UTC)
        catalog = SourceApprovalStore(self._session)
        in_force = [a for a in catalog.approvals(source_key) if catalog.is_in_force(a, now=moment)]
        if not in_force:
            return None
        return max(in_force, key=lambda a: a.terms_version)

    # -- the one write path -------------------------------------------------------------------------

    def _next_version(self, source_key: str) -> int:
        current = self._session.scalar(
            select(func.max(BusinessSourceEntitlement.version))
            .where(BusinessSourceEntitlement.business_id == self._business_id)
            .where(BusinessSourceEntitlement.source_key == source_key)
        )
        return int(current or 0) + 1

    def set_entitlement(
        self,
        data: BusinessSourceEntitlementInput,
        principal: BusinessEntitler,
        *,
        now: datetime | None = None,
    ) -> BusinessSourceEntitlement:
        """Validate and stage a NEW entitlement version; return it (uncommitted — caller owns the UoW).

        Authorization is checked FIRST: only a business administrator holding
        :attr:`~app.identity.permissions.Scope.SOURCE_APPROVE` may configure an entitlement — an
        ordinary user or a non-member is refused before any state is read or written. Then the request
        is narrowed against the platform ceiling (the in-force DCA-046 approval): an unapproved source,
        or any operation / field right / purpose the approval does not grant, is refused with a stable
        code before a row is staged. On success a new version is inserted (prior versions preserved),
        carrying ONLY the validated (narrowed) rights, plus an atomic DCA-026 audit entry in the SAME
        unit of work.
        """
        # 1) Authority FIRST — an ordinary user / non-member is refused with no state touched.
        if not principal.may_configure():
            raise BusinessSourceEntitlementAuthorizationError(
                "configuring a source entitlement requires a business administrator "
                f"({Scope.SOURCE_APPROVE.value}); an ordinary user cannot perform it"
            )

        source_key = (data.source_key or "").strip()
        if not source_key:
            raise BusinessSourceEntitlementError(
                REASON_MISSING_SOURCE, "a source_key is required for an entitlement"
            )

        # 2) The source must be approved at the platform level (an in-force DCA-046 approval). A
        #    business can never enable a source the platform has not approved (or whose approval lapsed).
        approval = self._in_force_approval(source_key, now=now)
        if approval is None:
            raise BusinessSourceEntitlementError(
                REASON_SOURCE_NOT_PLATFORM_APPROVED,
                f"source '{source_key}' has no in-force platform approval; a business cannot enable "
                "a source denied (or not yet approved) at the platform level",
                detail=source_key,
            )

        # 3) Narrow: every source-wide operation the business grants must be granted by the approval.
        operation_rights: dict[str, bool] = {}
        for raw_op, granted in (data.operations or {}).items():
            op = _normalize_operation(raw_op)
            want = bool(granted)
            if want and not _ceiling_allows(approval, None, op):
                raise BusinessSourceEntitlementError(
                    REASON_OPERATION_DENIED_AT_PLATFORM,
                    f"operation '{op}' is not granted by the platform approval for '{source_key}'; "
                    "a business entitlement can only narrow the approved rights, never widen them",
                    detail=op,
                )
            operation_rights[op] = want

        # 4) Narrow: every per-field right the business grants must be granted by the approval too.
        field_rights: dict[str, dict[str, bool]] = {}
        for raw_field, ops in (data.field_rights or {}).items():
            name = (raw_field or "").strip()
            if not name:
                continue
            overrides: dict[str, bool] = {}
            for raw_op, granted in (ops or {}).items():
                op = _normalize_operation(raw_op)
                want = bool(granted)
                if want and not _ceiling_allows(approval, name, op):
                    raise BusinessSourceEntitlementError(
                        REASON_FIELD_RIGHT_DENIED_AT_PLATFORM,
                        f"right '{op}' on field '{name}' is not granted by the platform approval "
                        f"for '{source_key}'; a business entitlement can only narrow it",
                        detail=name,
                    )
                overrides[op] = want
            field_rights[name] = overrides

        # 5) Narrow: every purpose the business limits to must be one the approved terms document.
        approved_purposes = self._approved_purposes(approval)
        purposes: list[str] = []
        for raw in data.purposes or []:
            purpose = (raw or "").strip()
            if not purpose or purpose in purposes:
                continue
            if purpose not in approved_purposes:
                raise BusinessSourceEntitlementError(
                    REASON_PURPOSE_DENIED_AT_PLATFORM,
                    f"purpose '{purpose}' is not documented by the platform approval for "
                    f"'{source_key}'; a business entitlement can only narrow the approved purposes",
                    detail=purpose,
                )
            purposes.append(purpose)

        # All checks passed — stage the narrowed entitlement and its atomic audit entry.
        actor = AuditActor(id=principal.subject, type=ActorType.USER)
        version = self._next_version(source_key)
        entitlement = BusinessSourceEntitlement(
            business_id=self._business_id,
            source_key=source_key,
            version=version,
            enabled=bool(data.enabled),
            operation_rights=operation_rights,
            field_rights=field_rights,
            purposes=purposes,
            approved_terms_version=approval.terms_version,
            actor_id=actor.id,
            actor_type=actor.type.value,
        )
        self._session.add(entitlement)

        # Atomic audit (SEC-001/BR-122 discipline): staged into the caller's session so it commits with
        # the entitlement row. References are opaque (source key + version + boolean grants).
        # References are kept OPAQUE: field names (e.g. "email") would trip the audit sink's
        # secret/contact screen, so we record the granted operation NAMES and plain COUNTS rather than
        # the raw per-field maps — enough to audit the change without staging any sensitive key.
        AuditSink(self._session, business_id=self._business_id, actor=actor).record(
            action=AUDIT_ACTION_SET_ENTITLEMENT,
            reason=f"source entitlement set to version {version} for '{source_key}'",
            references={
                "sourceKey": source_key,
                "version": version,
                "enabled": bool(data.enabled),
                "approvedTermsVersion": approval.terms_version,
                "grantedOperations": sorted(
                    op for op, granted in operation_rights.items() if granted
                ),
                "fieldOverrideCount": len(field_rights),
                "purposeCount": len(purposes),
            },
        )
        return entitlement

    def _approved_purposes(self, approval: SourceTermsApproval) -> set[str]:
        """The purposes documented by the terms version this approval endorses (the purpose ceiling)."""
        terms = SourceTermsStore(self._session).get_version(
            approval.source_key, approval.terms_version
        )
        return {str(p) for p in (terms.purposes or [])} if terms is not None else set()
