"""discovery task conversions — the DCA-143 staged-task-to-observations record (tenant-owned, RLS)

Revision ID: 0062_discovery_task_conversions
Revises: 0061_discovery_task_executions
Create Date: 2026-10-31

Adds ``discovery_task_conversions`` (one tenant-owned row per CONVERTED DCA-142 task: the record that
normalizes one DONE task's staged checkpoint into provenanced observations — reading the organization
identities the task imported and recording each PERMITTED organization-level field as a DCA-061 field
observation that cites a DCA-062 evidence object capturing the checkpoint). It records the ``run_id`` +
``task_index`` converted and the ``execution_id`` of the DCA-142 record, the ``source_key`` /
``source_version`` the observations + evidence are grounded in, the captured ``evidence_id``, the counts
(``org_observation_count`` / ``observations_recorded`` / ``fields_dropped``) and the ``provenance``
invariants (converted = True; sent / spent = False). There is NO network fetch, NO model call and NO
recipient send: the conversion reads already-collected, already-permitted data.

A ``(business_id, run_id, task_index)`` UNIQUE constraint backs the "a task converts once" idempotency
guarantee: re-converting the same task is a no-op (no duplicate observations). The table is tenant-owned,
so each row is scoped to one ``business_id`` with NO cross-tenant visibility, and holds only opaque
counts + references to the rows it produced.

Dual-path safe (same guard style as 0049..0061):

- On a FRESH database the metadata baseline (0001) already created the table and 0002 already built its
  RLS policy (it is a ``TenantOwned`` table, so it is in ``tenant_owned_tables()``), so the table/policy
  steps below are a no-op.
- On a database that reached 0061 BEFORE this table existed, it is created here and its RLS policy /
  grant are added here, so it is protected exactly like the other tenant data tables.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.models  # noqa: F401 — populate Base.metadata
from app.core.config import get_settings
from app.db.models import DiscoveryTaskConversion
from app.db.rls import APP_ROLE, POLICY_NAME, RLS_GUC
from app.db.schema_guard import resolve_dca_schema

revision: str = "0062_discovery_task_conversions"
down_revision: str | None = "0061_discovery_task_executions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (("discovery_task_conversions", DiscoveryTaskConversion),)


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
    bind = op.get_bind()
    for table, model in _TABLES:
        if not _has_table(schema, table):
            # Create exactly this table (search_path is pinned to the DCA schema by env.py).
            model.__table__.create(bind=bind, checkfirst=True)

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
    # Intentionally a no-op, same rationale as 0049..0061: teardown of this table and its policy is owned
    # by the migrations that own the metadata-driven baseline (0002 drops the policy, 0001's ``drop_all``
    # drops the table). Dropping here would race 0002.down's ALTER. Leaving the unused table on a
    # single-step rollback is safe (expand→contract, BR-201).
    pass
