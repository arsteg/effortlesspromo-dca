"""contact points — the DCA-060 tenant-private person + contact-point tables (tenant-owned, RLS)

Revision ID: 0025_contact_points
Revises: 0024_org_observations
Create Date: 2026-10-28

Adds ``person_observations`` (a tenant-private person identity, optionally linked to one of this
business's organization observations) and ``contact_points`` (the SEPARATE reachable-value table:
channel + value + identity evidence -> person, unique per business). Both are tenant-owned, so each
row is scoped to one ``business_id`` and carries NO cross-tenant visibility — one business's contact
points are never reachable from another, even via a shared organization mapping. No host / prospect
record is created by this story, and no value is promoted to any shared/global table.

Dual-path safe (same guard style as 0005..0024):

- On a FRESH database the metadata baseline (0001) already created both tables and 0002 already built
  their RLS policies (they are ``TenantOwned`` tables, so they are in ``tenant_owned_tables()``), so
  everything below is a no-op.
- On a database that reached 0024 BEFORE these tables existed, they are created here and their RLS
  policies / grants are added here, so they are protected exactly like the other tenant data tables.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.models  # noqa: F401 — populate Base.metadata
from app.core.config import get_settings
from app.db.models import ContactPoint, PersonObservation
from app.db.rls import APP_ROLE, POLICY_NAME, RLS_GUC
from app.db.schema_guard import resolve_dca_schema

revision: str = "0025_contact_points"
down_revision: str | None = "0024_org_observations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    ("person_observations", PersonObservation),
    ("contact_points", ContactPoint),
)


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
    # Intentionally a no-op, same rationale as 0005..0024: teardown of these tables and their policies
    # is owned by the migrations that own the metadata-driven baseline (0002 drops the policy, 0001's
    # ``drop_all`` drops the tables). Dropping here would race 0002.down's ALTER. Leaving the unused
    # tables on a single-step rollback is safe (expand→contract, BR-201).
    pass
