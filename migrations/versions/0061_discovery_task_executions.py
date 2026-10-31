"""discovery task executions — the DCA-142 one-task execution record (tenant-owned, RLS)

Revision ID: 0061_discovery_task_executions
Revises: 0060_run_plan_approval
Create Date: 2026-10-31

Adds ``discovery_task_executions`` (one tenant-owned row per EXECUTED planned source task of a discovery
run: the orchestration record that connects, for ONE task, the DCA-048 rights preflight, the DCA-113 cost
reservation, the DCA-115 provider-call ledger and the concrete DCA-123 source adapter — storing only the
permitted staged output / checkpoint). It records the ``run_id`` + ``task_index`` of the approved task
executed (from the run's frozen DCA-141 ``plan_snapshot``), a ``task_fingerprint`` tying it to the exact
task, the ``source_key`` / ``adapter_key`` invoked, the terminal ``status`` (done / failed / denied) with
its ``phase`` + ``reason_code``, the ``reservation_id`` / ``provider_call_id`` it drew on, the permitted
``staged_result`` checkpoint (opaque counts + row refs, no contact data) and the ``provenance``
invariants (sent / spent = False). There is NO real network fetch and NO recipient send: the adapter runs
deterministically against caller-supplied / synthetic fixture inputs.

A ``(business_id, run_id, task_index)`` UNIQUE constraint backs the "a task runs once" idempotency
guarantee: re-executing the same task is a no-op. The table is tenant-owned, so each row is scoped to one
``business_id`` with NO cross-tenant visibility, and holds only the business's own execution metadata.

Dual-path safe (same guard style as 0049/0050/0051/0052/0053/0054/0055/0056/0057/0058/0059):

- On a FRESH database the metadata baseline (0001) already created the table and 0002 already built its
  RLS policy (it is a ``TenantOwned`` table, so it is in ``tenant_owned_tables()``), so the table/policy
  steps below are a no-op.
- On a database that reached 0060 BEFORE this table existed, it is created here and its RLS policy /
  grant are added here, so it is protected exactly like the other tenant data tables.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.models  # noqa: F401 — populate Base.metadata
from app.core.config import get_settings
from app.db.models import DiscoveryTaskExecution
from app.db.rls import APP_ROLE, POLICY_NAME, RLS_GUC
from app.db.schema_guard import resolve_dca_schema

revision: str = "0061_discovery_task_executions"
down_revision: str | None = "0060_run_plan_approval"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (("discovery_task_executions", DiscoveryTaskExecution),)


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
    # Intentionally a no-op, same rationale as 0049..0060: teardown of this table and its policy is owned
    # by the migrations that own the metadata-driven baseline (0002 drops the policy, 0001's ``drop_all``
    # drops the table). Dropping here would race 0002.down's ALTER. Leaving the unused table on a
    # single-step rollback is safe (expand→contract, BR-201).
    pass
