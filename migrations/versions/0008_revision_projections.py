"""revision projections — the DCA-031 revision-aware projection tables (tenant-owned, RLS-protected)

Revision ID: 0008_revision_projections
Revises: 0007_inbox_receipts
Create Date: 2026-10-26

Adds two tenant-owned tables:

- ``revision_projections`` — one row per aggregate ``(business_id, aggregate_type, aggregate_id)``
  holding the highest CONTIGUOUS ``applied_version``, its opaque projected ``state``, and a
  ``current``/``gapped`` status. The unique constraint makes one projection per aggregate per tenant.
- ``revision_pending_events`` — out-of-order revisions buffered ahead of a missing predecessor,
  unique on ``(business_id, aggregate_type, aggregate_id, aggregate_version)`` so a re-delivery is
  not double-buffered.

Together they let a receiver reject a stale (older) revision and keep a gap visible until the missing
revision arrives and the buffered events are reconciled in order.

Dual-path safe (same guard style as 0005/0006/0007):

- On a FRESH database the metadata baseline (0001) already created both tables and 0002 already built
  their RLS policies (both are ``TenantOwned``, so both are in ``tenant_owned_tables()``), so
  everything below is a no-op.
- On a database that reached 0007 BEFORE these tables existed, each table is created here and its RLS
  policy / grants are added here, so it is protected exactly like the other tenant data tables.

The unique constraints travel with the table definitions (``tenant_unique`` on the models), so they
are created on both paths.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.models  # noqa: F401 — populate Base.metadata
from app.core.config import get_settings
from app.db.models import RevisionPendingEvent, RevisionProjection
from app.db.rls import APP_ROLE, POLICY_NAME, RLS_GUC
from app.db.schema_guard import resolve_dca_schema

revision: str = "0008_revision_projections"
down_revision: str | None = "0007_inbox_receipts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = {
    "revision_projections": RevisionProjection,
    "revision_pending_events": RevisionPendingEvent,
}


def _schema() -> str:
    return resolve_dca_schema(get_settings().db_schema)


def _has_table(table: str, schema: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table, schema=schema)


def _has_policy(table: str, schema: str) -> bool:
    row = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM pg_policies WHERE schemaname = :s "
                "AND tablename = :t AND policyname = :p"
            ),
            {"s": schema, "t": table, "p": POLICY_NAME},
        )
        .first()
    )
    return row is not None


def upgrade() -> None:
    schema = _schema()
    for table, model in _TABLES.items():
        if not _has_table(table, schema):
            # Create exactly this table (search_path is pinned to the DCA schema by env.py). Its
            # tenant_unique constraint is part of the table definition.
            model.__table__.create(bind=op.get_bind(), checkfirst=True)

        if not _has_policy(table, schema):
            op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
            op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
            op.execute(
                f"""
                CREATE POLICY {POLICY_NAME} ON "{table}"
                    USING (business_id = current_setting('{RLS_GUC}', true))
                    WITH CHECK (business_id = current_setting('{RLS_GUC}', true));
                """
            )
            op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON "{table}" TO {APP_ROLE}')


def downgrade() -> None:
    # Intentionally a no-op, same rationale as 0005/0006/0007: teardown of these tables and their
    # policies is owned by the migrations that own the metadata-driven baseline (0002 drops the
    # policies, 0001's ``drop_all`` drops the tables). Dropping here would race 0002.down's ALTER.
    # Leaving the unused tables on a single-step rollback is safe (expand→contract, BR-201).
    pass
