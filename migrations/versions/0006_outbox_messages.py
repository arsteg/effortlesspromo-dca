"""outbox messages — the DCA-027 transactional outbox table (tenant-owned, RLS-protected)

Revision ID: 0006_outbox_messages
Revises: 0005_audit_events
Create Date: 2026-10-26

Adds ``outbox_messages`` — a tenant-owned table holding a stable event id, event type, logical-intent
key, payload-contract version, opaque payload and a ``pending``/``delivered`` status for each
integration intent staged with a local change. Dual-path safe (same guard style as 0005):

- On a FRESH database the metadata-driven baseline (0001) already created ``outbox_messages`` and
  0002 already built its RLS policy (``outbox_messages`` is a ``TenantOwned`` table, so it is in
  ``tenant_owned_tables()``), so everything below is a no-op.
- On a database that reached 0005 BEFORE this table existed, the table is created here and its RLS
  policy / grants are added here, so it is protected exactly like the other tenant data tables.

The ``(business_id, event_id)`` unique constraint — which makes a duplicate logical intent a
constraint violation rather than a silent double — travels with the table definition
(``tenant_unique`` on the model), so it is created on both paths.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.models  # noqa: F401 — populate Base.metadata
from app.core.config import get_settings
from app.db.models import OutboxMessage
from app.db.rls import APP_ROLE, POLICY_NAME, RLS_GUC
from app.db.schema_guard import resolve_dca_schema

revision: str = "0006_outbox_messages"
down_revision: str | None = "0005_audit_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "outbox_messages"


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
        # tenant_unique constraint on (business_id, event_id) is part of the table definition.
        OutboxMessage.__table__.create(bind=op.get_bind(), checkfirst=True)

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
    # Intentionally a no-op, same rationale as 0005: teardown of ``outbox_messages`` and its policy is
    # owned by the migrations that own the metadata-driven baseline (0002 drops the policy, 0001's
    # ``drop_all`` drops the table). Dropping here would race 0002.down's ALTER. Leaving the unused
    # table on a single-step rollback is safe (expand→contract, BR-201).
    pass
