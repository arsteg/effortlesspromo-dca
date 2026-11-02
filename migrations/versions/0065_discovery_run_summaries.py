"""discovery run summaries — the DCA-146 finalized run-summary record (tenant-owned, RLS)

Revision ID: 0065_discovery_run_summaries
Revises: 0064_prospect_suggestions
Create Date: 2026-11-02

Adds ``discovery_run_summaries`` (one tenant-owned row per FINALIZED discovery run: the durable summary
that aggregates the run's recorded DCA-142 task outcomes/counts, the held DCA-113 reservation cost, the
failure/denial reasons, the DCA-144 resolved-batch counts and the DCA-145 suggestion count, and settles
the run into a terminal status). It records the ``run_id`` finalized, the terminal ``final_status``, the
task-outcome counts (``task_count`` / ``done_count`` / ``failed_count`` / ``denied_count``), the
``reserved_cost_minor`` held and the run's ``per_run_budget_minor`` ceiling, the resolved ``subject_count``
/ ``matched_count`` and the ``suggestion_count``, the honest ``partial_failure`` / ``empty`` flags, the
``reason_counts`` breakdown, the durable ``cursor`` checkpoint, the ``schema_version`` and the
``provenance`` invariants (finalized = True; sent / spent = False). It exposes NO raw private data — only
aggregate counts, stable reason codes and opaque, checkpointable row references — and a COMPLETED status
means genuine successful work, so a run with no matches is never fabricated as success.

A ``(business_id, run_id)`` UNIQUE constraint backs the "a run finalizes once" idempotency guarantee:
re-finalizing the same run is a no-op (no new state, no second audit). The table is tenant-owned, so each
row is scoped to one ``business_id`` with NO cross-tenant visibility.

Dual-path safe (same guard style as 0049..0064):

- On a FRESH database the metadata baseline (0001) already created the table and 0002 already built its
  RLS policy (it is a ``TenantOwned`` table, so it is in ``tenant_owned_tables()``), so the table/policy
  steps below are a no-op.
- On a database that reached 0064 BEFORE this table existed, it is created here and its RLS policy /
  grant are added here, so it is protected exactly like the other tenant data tables.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.models  # noqa: F401 — populate Base.metadata
from app.core.config import get_settings
from app.db.models import DiscoveryRunSummary
from app.db.rls import APP_ROLE, POLICY_NAME, RLS_GUC
from app.db.schema_guard import resolve_dca_schema

revision: str = "0065_discovery_run_summaries"
down_revision: str | None = "0064_prospect_suggestions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (("discovery_run_summaries", DiscoveryRunSummary),)


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
    # Intentionally a no-op, same rationale as 0049..0064: teardown of this table and its policy is owned
    # by the migrations that own the metadata-driven baseline (0002 drops the policy, 0001's ``drop_all``
    # drops the table). Dropping here would race 0002.down's ALTER. Leaving the unused table on a
    # single-step rollback is safe (expand→contract, BR-201).
    pass
