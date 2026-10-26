"""audit events — the DCA-026 transactional audit table (tenant-owned, RLS-protected)

Revision ID: 0005_audit_events
Revises: 0004_tenant_data_mode
Create Date: 2026-10-26

Adds ``audit_events`` — a tenant-owned table holding actor, scope, action, reason and opaque
references for each audited mutation. Dual-path safe (same guard style as 0004):

- On a FRESH database the metadata-driven baseline (0001) already created ``audit_events`` and 0002
  already built its RLS policy (``audit_events`` is a ``TenantOwned`` table, so it is in
  ``tenant_owned_tables()``), so everything below is a no-op.
- On a database that reached 0004 BEFORE this table existed, the table is created here and its RLS
  policy / grants are added here, so it is protected exactly like the other tenant data tables.

RLS is applied explicitly rather than left implicit: a tenant-owned audit trail that any tenant could
read would defeat the point.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.models  # noqa: F401 — populate Base.metadata
from app.core.config import get_settings
from app.db.models import AuditEvent
from app.db.rls import APP_ROLE, POLICY_NAME, RLS_GUC
from app.db.schema_guard import resolve_dca_schema

revision: str = "0005_audit_events"
down_revision: str | None = "0004_tenant_data_mode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "audit_events"


def _schema() -> str:
    return resolve_dca_schema(get_settings().db_schema)


def _has_table(schema: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(_TABLE, schema=schema)


def _has_policy(schema: str) -> bool:
    row = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM pg_policies WHERE schemaname = :s "
                "AND tablename = :t AND policyname = :p"
            ),
            {"s": schema, "t": _TABLE, "p": POLICY_NAME},
        )
        .first()
    )
    return row is not None


def upgrade() -> None:
    schema = _schema()
    if not _has_table(schema):
        # Create exactly this table (search_path is pinned to the DCA schema by env.py).
        AuditEvent.__table__.create(bind=op.get_bind(), checkfirst=True)

    if not _has_policy(schema):
        op.execute(f'ALTER TABLE "{_TABLE}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{_TABLE}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f"""
            CREATE POLICY {POLICY_NAME} ON "{_TABLE}"
                USING (business_id = current_setting('{RLS_GUC}', true))
                WITH CHECK (business_id = current_setting('{RLS_GUC}', true));
            """
        )
        op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON "{_TABLE}" TO {APP_ROLE}')


def downgrade() -> None:
    # Intentionally a no-op. Teardown of ``audit_events`` and its policy is owned by the migrations
    # that own the metadata-driven baseline: on a full downgrade, 0002 drops the policy (the table
    # still exists at that point) and 0001's ``drop_all`` drops the table. Dropping them here would
    # race those: 0005.down runs BEFORE 0002.down, so it would remove the table out from under
    # 0002.down's ``ALTER TABLE``. Leaving the (now-unused) table on a single-step rollback is safe
    # (expand→contract, BR-201); a later contract migration removes it when the model does.
    pass
