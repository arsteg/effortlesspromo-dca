"""recurring schedules — the DCA-107 per-business recurring-schedule store (tenant-owned, RLS)

Revision ID: 0042_recurring_schedules
Revises: 0041_identity_merges
Create Date: 2026-10-31

Adds ``recurring_schedules`` (one durable, tenant-owned row per business recurring intent: a schema
version, the logical ``schedule_key``, the cadence ``interval_seconds``, the persisted DUE time
``next_run_at``, the active/paused status, the versioned profile references ``icp_version`` /
``profile_version``, and the opaque actor). The table is tenant-owned, so each row is scoped to one
``business_id`` and carries NO cross-tenant visibility. ``(business_id, schedule_key)`` is unique — one
schedule per logical key within a business. Storing a schedule is durable DATA only; nothing executes it
(a disabled future capability).

Dual-path safe (same guard style as 0028/0032/0033/0037/0041):

- On a FRESH database the metadata baseline (0001) already created the table and 0002 already built its
  RLS policy (it is a ``TenantOwned`` table, so it is in ``tenant_owned_tables()``), so everything below
  is a no-op.
- On a database that reached 0041 BEFORE this table existed, it is created here and its RLS policy /
  grant is added here, so it is protected exactly like the other tenant data tables.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.models  # noqa: F401 — populate Base.metadata
from app.core.config import get_settings
from app.db.models import RecurringSchedule
from app.db.rls import APP_ROLE, POLICY_NAME, RLS_GUC
from app.db.schema_guard import resolve_dca_schema

revision: str = "0042_recurring_schedules"
down_revision: str | None = "0041_identity_merges"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (("recurring_schedules", RecurringSchedule),)


def _schema() -> str:
    return resolve_dca_schema(get_settings().db_schema)


def _has_table(schema: str, table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table, schema=schema)


def _has_policy(schema: str, table: str) -> bool:
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
    for table, model in _TABLES:
        if not _has_table(schema, table):
            # Create exactly this table (search_path is pinned to the DCA schema by env.py).
            model.__table__.create(bind=op.get_bind(), checkfirst=True)

        # Tenant-owned → tenant-isolation RLS + the app-role grant, exactly like the other tenant tables.
        if not _has_policy(schema, table):
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
    # Intentionally a no-op, same rationale as 0028/0032/0033/0037/0041: teardown of this table and its
    # policy is owned by the migrations that own the metadata-driven baseline (0002 drops the policy,
    # 0001's ``drop_all`` drops the table). Dropping here would race 0002.down's ALTER. Leaving the unused
    # table on a single-step rollback is safe (expand→contract, BR-201).
    pass
