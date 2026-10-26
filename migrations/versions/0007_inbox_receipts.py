"""inbox receipts — the DCA-028 idempotent-receiver table (tenant-owned, RLS-protected)

Revision ID: 0007_inbox_receipts
Revises: 0006_outbox_messages
Create Date: 2026-10-26

Adds ``inbox_receipts`` — a tenant-owned table holding, per inbound event, its external source/id
natural key, an optional event type, an opaque payload, a ``pending``/``processed`` status and the
received/processed timestamps. One receipt per handled event makes a replay a constraint violation
rather than a second application of the local change. Dual-path safe (same guard style as 0005/0006):

- On a FRESH database the metadata-driven baseline (0001) already created ``inbox_receipts`` and 0002
  already built its RLS policy (``inbox_receipts`` is a ``TenantOwned`` table, so it is in
  ``tenant_owned_tables()``), so everything below is a no-op.
- On a database that reached 0006 BEFORE this table existed, the table is created here and its RLS
  policy / grants are added here, so it is protected exactly like the other tenant data tables.

The ``(business_id, event_source, event_id)`` unique constraint — which makes a replayed event a
constraint violation rather than a silent double — travels with the table definition
(``tenant_unique`` on the model), so it is created on both paths.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.models  # noqa: F401 — populate Base.metadata
from app.core.config import get_settings
from app.db.models import InboxReceipt
from app.db.rls import APP_ROLE, POLICY_NAME, RLS_GUC
from app.db.schema_guard import resolve_dca_schema

revision: str = "0007_inbox_receipts"
down_revision: str | None = "0006_outbox_messages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "inbox_receipts"


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
        # Create exactly this table (search_path is pinned to the DCA schema by env.py). The
        # tenant_unique constraint on (business_id, event_source, event_id) is part of the table def.
        InboxReceipt.__table__.create(bind=op.get_bind(), checkfirst=True)

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
    # Intentionally a no-op, same rationale as 0005/0006: teardown of ``inbox_receipts`` and its
    # policy is owned by the migrations that own the metadata-driven baseline (0002 drops the policy,
    # 0001's ``drop_all`` drops the table). Dropping here would race 0002.down's ALTER. Leaving the
    # unused table on a single-step rollback is safe (expand→contract, BR-201).
    pass
