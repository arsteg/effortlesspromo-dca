"""discovery run requests — the DCA-140 bounded discovery run envelope (tenant-owned, RLS)

Revision ID: 0059_discovery_run_requests
Revises: 0058_acquisition_plans
Create Date: 2026-10-31

Adds ``discovery_run_requests`` (one tenant-owned row per created bounded discovery run: the
orchestration envelope the later DCA-141..149 pipeline steps fill — the confirmed ``icp_version`` it is
grounded in, the PENDING ``status``, the approved+entitled ``source_scope`` snapshot, the clamped scope
caps (``target_cap`` / ``task_cap`` / ``query_cap``), the INR ``per_run_budget_minor`` ceiling +
``budget_limit_version`` it is bounded by, the ``requested`` values + ``clamped`` / ``clamp_notes`` honest
over-scope record, and the ``provenance`` invariants). The table is the business-state write for DCA-140,
which CREATES a bounded discovery run in a not-yet-executing state: creating a run executes nothing (no
task, no job, no suggestion, no send, no spend). It is tenant-owned, so each row is scoped to one
``business_id`` with NO cross-tenant visibility, and holds only the business's own targeting/scope/limit
metadata (no prospect contact data).

A Postgres **partial unique index** on ``(business_id, icp_version)`` WHERE the run is active
(pending/planned/running) backs the app-level overlap guard for concurrency: only ONE active run may
exist per logical scope, so duplicate active scope can never start redundant work even under a race. It
is created only on PostgreSQL (SQLite dev/test relies on the service-level guard); a terminal run frees
the scope for a fresh run, so the partial predicate is deliberately not a plain unique constraint.

Dual-path safe (same guard style as 0028/0032/0033/0037/0041/0042/0044/0048/0049/0050/0051/0052/0053/0054/0055/0056/0057/0058):

- On a FRESH database the metadata baseline (0001) already created the table and 0002 already built its
  RLS policy (it is a ``TenantOwned`` table, so it is in ``tenant_owned_tables()``), so the table/policy
  steps below are a no-op; the partial index (not in metadata) is created here.
- On a database that reached 0058 BEFORE this table existed, it is created here and its RLS policy /
  grant / partial index are added here, so it is protected exactly like the other tenant data tables.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.models  # noqa: F401 — populate Base.metadata
from app.core.config import get_settings
from app.db.models import DiscoveryRunRequest
from app.db.rls import APP_ROLE, POLICY_NAME, RLS_GUC
from app.db.schema_guard import resolve_dca_schema

revision: str = "0059_discovery_run_requests"
down_revision: str | None = "0058_acquisition_plans"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (("discovery_run_requests", DiscoveryRunRequest),)

# The Postgres partial unique index backing the active-run overlap guard.
_ACTIVE_INDEX = "uq_discovery_run_active_scope"
_ACTIVE_STATES = ("pending", "planned", "running")


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


def _has_index(schema: str, index: str) -> bool:
    row = (
        op.get_bind()
        .execute(
            sa.text("SELECT 1 FROM pg_indexes WHERE schemaname = :s AND indexname = :i"),
            {"s": schema, "i": index},
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

    # Postgres-only partial unique index: at most one ACTIVE run per (business_id, icp_version), so a
    # concurrent duplicate loses the unique-constraint race rather than double-starting a logical scope.
    # SQLite dev/test does not get it and relies on the service-level overlap guard instead.
    if bind.dialect.name == "postgresql" and not _has_index(schema, _ACTIVE_INDEX):
        states = ", ".join(f"'{s}'" for s in _ACTIVE_STATES)
        op.execute(
            f'CREATE UNIQUE INDEX {_ACTIVE_INDEX} ON "discovery_run_requests" '
            f"(business_id, icp_version) WHERE status IN ({states})"
        )


def downgrade() -> None:
    # Intentionally a no-op, same rationale as 0028..0058: teardown of this table and its policy is owned
    # by the migrations that own the metadata-driven baseline (0002 drops the policy, 0001's ``drop_all``
    # drops the table, which drops the dependent index). Dropping here would race 0002.down's ALTER.
    # Leaving the unused table on a single-step rollback is safe (expand→contract, BR-201).
    pass
