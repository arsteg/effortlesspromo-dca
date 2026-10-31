"""org observation imports — the DCA-123 approved-dataset import provenance table (tenant-owned, RLS)

Revision ID: 0049_org_observation_imports
Revises: 0048_host_crawl_state
Create Date: 2026-10-31

Adds ``org_observation_imports`` (one tenant-owned row per imported dataset row: the observation it
produced, the catalog ``source_key`` the dataset was collected under, the opaque ``dataset_id``, its
``source_version`` / ``dataset_license`` attribution, and the checkpointable ``source_row_ref``). The
table is the SOURCE ATTRIBUTION for the first concrete source adapter (DCA-123), which imports an
APPROVED + entitled organization dataset into ``org_observations``. It is tenant-owned, so each row is
scoped to one ``business_id`` and carries NO cross-tenant visibility — one business can never read
another's import provenance. It holds only organization provenance (source/dataset/version/license +
an opaque row reference): NO personal contact data — the importer is organization-only (DCA-122).
``(business_id, source_key, dataset_id, source_row_ref)`` is unique — one provenance row per imported
dataset row within a business, the idempotency key that makes a re-import a no-op rather than a
duplicate.

Dual-path safe (same guard style as 0028/0032/0033/0037/0041/0042/0044/0048):

- On a FRESH database the metadata baseline (0001) already created the table and 0002 already built its
  RLS policy (it is a ``TenantOwned`` table, so it is in ``tenant_owned_tables()``), so everything below
  is a no-op.
- On a database that reached 0048 BEFORE this table existed, it is created here and its RLS policy /
  grant is added here, so it is protected exactly like the other tenant data tables.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.models  # noqa: F401 — populate Base.metadata
from app.core.config import get_settings
from app.db.models import OrgObservationImport
from app.db.rls import APP_ROLE, POLICY_NAME, RLS_GUC
from app.db.schema_guard import resolve_dca_schema

revision: str = "0049_org_observation_imports"
down_revision: str | None = "0048_host_crawl_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (("org_observation_imports", OrgObservationImport),)


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
    # Intentionally a no-op, same rationale as 0028/0032/0033/0037/0041/0042/0044/0048: teardown of this
    # table and its policy is owned by the migrations that own the metadata-driven baseline (0002 drops
    # the policy, 0001's ``drop_all`` drops the table). Dropping here would race 0002.down's ALTER.
    # Leaving the unused table on a single-step rollback is safe (expand→contract, BR-201).
    pass
