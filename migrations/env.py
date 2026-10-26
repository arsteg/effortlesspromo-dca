"""Alembic environment for the DCA-owned database/schema only.

Resolves the database URL and target schema from DCA settings at runtime (never from alembic.ini),
and refuses to migrate into a host-owned schema (``schema_guard.resolve_dca_schema``). All objects —
including Alembic's own ``alembic_version`` table — live in the DCA schema; the connection's
``search_path`` is pinned to it so model DDL (which is schema-agnostic) always lands in DCA territory
and never in ``public`` or a platform schema.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool, text

import app.db.models  # noqa: F401 — import for the side effect of populating Base.metadata
from app.core.config import get_settings
from app.db.base import Base
from app.db.schema_guard import resolve_dca_schema

settings = get_settings()
# GUARD (runs before any DDL): a host-owned schema setting raises here and aborts the migration.
DCA_SCHEMA = resolve_dca_schema(settings.db_schema)
DATABASE_URL = settings.database_url or ""

target_metadata = Base.metadata


def _require_url() -> str:
    if not DATABASE_URL:
        raise RuntimeError("EFFORTLESSPROMO_DCA_DATABASE_URL is required to run migrations")
    return DATABASE_URL


def run_migrations_offline() -> None:
    context.configure(
        url=_require_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        version_table_schema=DCA_SCHEMA,
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(_require_url(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        # Create and pin the DCA-owned schema; everything below operates only inside it.
        connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{DCA_SCHEMA}"'))
        connection.execute(text(f'SET search_path TO "{DCA_SCHEMA}"'))
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table_schema=DCA_SCHEMA,
            include_schemas=False,
        )
        with context.begin_transaction():
            context.run_migrations()
        # The CREATE SCHEMA / SET search_path above already opened a transaction, so Alembic ran in
        # "shared connection" mode and left the commit to us — persist it explicitly.
        connection.commit()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
