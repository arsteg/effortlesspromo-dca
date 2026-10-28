"""privacy grievances — the DCA-082 India privacy-grievance intake/status record (tenant-owned, RLS)

Revision ID: 0032_privacy_grievances
Revises: 0031_restriction_propagations
Create Date: 2026-10-28

Adds ``privacy_grievances`` (one scoped record per India privacy grievance a data principal raised with
a business: its category, its PROPORTIONATE verification requirement and state, its CONFIGURABLE,
REVIEWED response deadline, the one-way digest of the requester's contact — never the raw value — and
the hash of the requester's claim token). The table is tenant-owned, so each row is scoped to one
``business_id`` and carries NO cross-tenant visibility. No erasure/host/prospect record is created by
this story.

Dual-path safe (same guard style as 0005..0031):

- On a FRESH database the metadata baseline (0001) already created the table and 0002 already built its
  RLS policy (it is a ``TenantOwned`` table, so it is in ``tenant_owned_tables()``), so everything below
  is a no-op.
- On a database that reached 0031 BEFORE this table existed, it is created here and its RLS policy /
  grant is added here, so it is protected exactly like the other tenant data tables.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.models  # noqa: F401 — populate Base.metadata
from app.core.config import get_settings
from app.db.models import PrivacyGrievance
from app.db.rls import APP_ROLE, POLICY_NAME, RLS_GUC
from app.db.schema_guard import resolve_dca_schema

revision: str = "0032_privacy_grievances"
down_revision: str | None = "0031_restriction_propagations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (("privacy_grievances", PrivacyGrievance),)


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
    # Intentionally a no-op, same rationale as 0005..0031: teardown of the tenant-owned table and its
    # policy is owned by the migrations that own the metadata-driven baseline (0002 drops the policy,
    # 0001's drop_all drops the table). Dropping here would race 0002.down's ALTER. Leaving the unused
    # table on a single-step rollback is safe (expand→contract, BR-201).
    pass
