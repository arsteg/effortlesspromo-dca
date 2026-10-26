"""DCA-018 — PostgreSQL row-level security for tenant-owned tables.

Defense-in-depth beneath the DCA-017 :class:`~app.db.tenancy.ScopedRepository`: even a direct query
that bypasses the repository cannot cross a tenant boundary, because the database itself filters every
row by a *transaction-local* tenant setting.

The pieces:

- **Which tables** — exactly the :class:`~app.db.tenancy.TenantOwned` set (:func:`tenant_owned_tables`).
  The platform-owned registration catalog (``tenant_agents``, ``icp_profiles``) is deliberately
  *outside* tenant RLS: it is control-plane data the platform manages over the service-auth path,
  not tenant operational data, so it is accessed separately and never granted to the app role.
- **Who** — a non-login, ``NOBYPASSRLS`` application role (:data:`APP_ROLE`). The service's runtime
  login user is a member of it and is itself non-superuser, so RLS is always enforced for app
  queries. Superuser/owner connections (migrations, ops) still bypass, by design.
- **How the scope is set** — a transaction-local GUC (:data:`RLS_GUC`) set via ``set_config(..., true)``
  (the parameterisable form of ``SET LOCAL``). Because it is transaction-scoped it is reset at
  COMMIT/ROLLBACK, so a pooled connection never carries one tenant's scope into the next
  transaction. With the GUC unset the policy predicate is ``business_id = NULL`` → no rows
  (fail-closed): an unscoped query reads nothing.
"""

from __future__ import annotations

from sqlalchemy import Connection, Table, text

from app.db.base import Base
from app.db.tenancy import TenantOwned

# Custom GUC holding the active tenant for the current transaction. A dotted name is required for a
# user-defined setting; ``current_setting(name, true)`` returns NULL (not an error) when it is unset.
RLS_GUC = "dca.business_id"

# The non-login, non-bypass application role the RLS policies are written for.
APP_ROLE = "dca_app"

# The tenant policy name, uniform across the protected tables.
POLICY_NAME = "tenant_isolation"


def tenant_owned_tables() -> list[str]:
    """Table names of every mapped model that inherits :class:`TenantOwned`, sorted.

    This is the single source of truth for *which* tables RLS covers — the migration builds its
    policies from the same set, and a test asserts the two never drift, so adding a tenant-owned
    model can never silently ship without RLS.
    """
    names = {
        mapper.local_table.name
        for mapper in Base.registry.mappers
        if issubclass(mapper.class_, TenantOwned) and isinstance(mapper.local_table, Table)
    }
    return sorted(names)


def set_local_tenant(connection: Connection, business_id: str) -> None:
    """Bind the current transaction to one tenant via a transaction-local GUC.

    Uses ``set_config(name, value, is_local => true)`` — the function form of ``SET LOCAL`` that
    accepts a bound parameter, so the business id can never be interpolated into SQL. Must run inside
    an open transaction; the setting is discarded when that transaction ends, which is exactly why a
    reused pooled connection does not retain the previous tenant's scope.
    """
    connection.execute(
        text("SELECT set_config(:name, :value, true)"),
        {"name": RLS_GUC, "value": business_id},
    )
