"""Guard: migrations may only ever run inside the DCA-owned schema.

The DCA has its own database (polyrepo rule, ADR-0005 / DCA boundary) and, within it, owns exactly
one logical schema (``EFFORTLESSPROMO_DCA_DB_SCHEMA``, default ``dca``). Alembic resolves the target
schema through :func:`resolve_dca_schema`, which **refuses** to migrate into a PostgreSQL system
schema or any schema reserved for the platform / host. A misconfiguration fails before a single DDL
statement runs, so the DCA can never create or mutate tables in a schema it does not own.
"""

from __future__ import annotations

# PostgreSQL system schemas + schemas reserved for the platform (the .NET system of record) / host.
# Matching is case-insensitive; any ``pg_*`` name is reserved by PostgreSQL as well.
HOST_OWNED_SCHEMAS: frozenset[str] = frozenset(
    {
        "pg_catalog",
        "pg_toast",
        "information_schema",
        "platform",
        "effortlesspromo",
        "host",
        "public",  # the shared default schema is never assumed to be DCA-owned; name the DCA schema
    }
)


class HostSchemaError(RuntimeError):
    """Raised when migrations are pointed at a schema the DCA does not own."""


def resolve_dca_schema(db_schema: str | None) -> str:
    """Return the validated DCA-owned schema name, or raise if it is empty or host-owned."""
    name = (db_schema or "").strip()
    if not name:
        raise HostSchemaError(
            "EFFORTLESSPROMO_DCA_DB_SCHEMA must name the DCA-owned schema (it is empty)"
        )
    if name.lower() in HOST_OWNED_SCHEMAS or name.lower().startswith("pg_"):
        raise HostSchemaError(
            f"refusing to migrate into host-owned schema '{name}': the DCA owns only its own schema "
            "(set EFFORTLESSPROMO_DCA_DB_SCHEMA to a DCA-owned name)"
        )
    return name
