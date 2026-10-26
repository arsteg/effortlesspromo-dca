"""DCA-017 — tenant ownership enforced in persistence.

Two pieces, composed on the verified tenant context from DCA-016:

- :class:`TenantOwned` — the declarative mixin every ordinary tenant-owned row inherits. It supplies
  the ``business_id`` column (the platform's business GUID as a string) with a consistent shape, and
  :func:`tenant_unique` builds the *composite* uniqueness that tenant data needs: an identifier is
  unique **within a business**, never globally, so one tenant's value can never collide with or leak
  another's.
- :class:`ScopedRepository` — the ONLY sanctioned read/write path for tenant-owned rows. It is bound
  to a :class:`~app.identity.tenant_context.TenantContext` (which exists only after membership was
  verified), so every query is filtered to that business and every write is stamped with it. A
  by-id lookup of another business's row returns ``None`` (indistinguishable from not-found, never a
  cross-tenant read); a write carrying a conflicting ``business_id`` is rejected outright.

The repository does app-level scoping; it is not a substitute for database RLS. It guarantees that
*ordinary* repository methods cannot cross a tenant boundary — deliberate cross-tenant reads must
bypass it explicitly (there is no such path in the service today).
"""

from __future__ import annotations

from typing import Generic, TypeVar

from sqlalchemy import String, UniqueConstraint
from sqlalchemy.orm import Mapped, Session, mapped_column
from sqlalchemy.sql import Select, select

from app.identity.tenant_context import TenantContext


class TenantOwnershipError(Exception):
    """A write targeted a business other than the active, verified tenant context.

    Raised BEFORE the session is touched, so a rejected write leaves no row and no partial state.
    The HTTP layer (where one exists) collapses it to a generic 403 — it is a tenant-isolation
    violation, not a validation error.
    """


class TenantOwned:
    """Declarative mixin for a row owned by exactly one business.

    ``business_id`` is indexed and NOT NULL — a tenant-owned row without an owner is never valid.
    Writes never set it by hand; :class:`ScopedRepository` stamps it from the verified context.
    """

    business_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)


def tenant_unique(*columns: str, name: str) -> UniqueConstraint:
    """A composite uniqueness constraint scoped to the owning business.

    ``tenant_unique("org_identifier", name="uq_candidate_org")`` yields
    ``UniqueConstraint("business_id", "org_identifier")`` — the identifier is unique *per business*,
    so two tenants may legitimately hold the same external id and neither constrains the other.
    """
    return UniqueConstraint("business_id", *columns, name=name)


T = TypeVar("T", bound=TenantOwned)


class ScopedRepository(Generic[T]):
    """Tenant-scoped persistence for one :class:`TenantOwned` model, bound to a verified context.

    Every method is confined to ``context.business_id``. There is no argument to widen the scope and
    no method that reads another business's rows — that is the point. Deliberate cross-tenant access
    (none exists in the service today) would have to build its own query without this helper.
    """

    def __init__(self, session: Session, context: TenantContext, model: type[T]) -> None:
        self._session = session
        self._business_id = context.business_id
        self._model = model

    @property
    def business_id(self) -> str:
        return self._business_id

    def _reject_conflict(self, supplied: str | None) -> None:
        # A body/caller-supplied owner that is neither empty nor the active tenant is a conflict:
        # we never let a request assert ownership of a different (or any) business.
        if supplied not in (None, "", self._business_id):
            raise TenantOwnershipError(
                "supplied business_id does not match the active tenant context"
            )

    def add(self, entity: T) -> T:
        """Stamp an already-constructed entity with the active tenant and stage it for insert.

        If the entity already carries a *different* ``business_id`` (e.g. deserialized from a request
        body), the write is rejected rather than silently re-homed.
        """
        self._reject_conflict(getattr(entity, "business_id", None))
        entity.business_id = self._business_id
        self._session.add(entity)
        return entity

    def create(self, **kwargs: object) -> T:
        """Construct, stamp, and stage a new row. A conflicting ``business_id`` kwarg is rejected."""
        self._reject_conflict(kwargs.pop("business_id", None))  # type: ignore[arg-type]
        # SQLAlchemy's declarative constructor takes column kwargs; the TenantOwned bound doesn't
        # declare them, so the call-arg check can't see them.
        entity = self._model(business_id=self._business_id, **kwargs)  # type: ignore[call-arg]
        self._session.add(entity)
        return entity

    def get(self, entity_id: object) -> T | None:
        """Fetch by primary key WITHIN this tenant. Another business's id resolves to ``None`` —
        a cross-tenant id is reported exactly like a non-existent one, never surfaced."""
        entity = self._session.get(self._model, entity_id)
        if entity is None or entity.business_id != self._business_id:
            return None
        return entity

    def select(self) -> Select[tuple[T]]:
        """A SELECT pre-filtered to this tenant — the base for any further ``.where(...)`` the caller
        adds. The business filter cannot be removed by the caller."""
        return select(self._model).where(self._model.business_id == self._business_id)

    def list(self) -> list[T]:
        return list(self._session.scalars(self.select()).all())
