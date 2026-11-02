"""search partition backoffs — the DCA-149 no-new-result / backoff state (tenant-owned, RLS)

Revision ID: 0066_search_partition_backoffs
Revises: 0065_discovery_run_summaries
Create Date: 2026-11-02

Adds ``search_partition_backoffs`` (one tenant-owned row per repeated-search partition: the durable
no-new-result / marginal-yield STOP-and-backoff state the DCA-149 recorder advances after each bounded
discovery round so a market that stops producing NEW results is visibly paused / backed off rather than
searched — and spent on — forever). It records the ``partition_key`` (an opaque, bounded logical
repeated-search unit within the business), the ``state`` (active | backing_off | exhausted), the
``rounds_recorded`` / ``consecutive_low_yield`` / ``backoff_level`` counters, the last round's yield
(``last_new_count`` / ``last_total_count`` / ``last_low_yield`` / ``last_stop_reason`` / ``last_round_at``),
the ``next_eligible_at`` (when a backing-off partition may search again; NULL while active and NULL when
exhausted), an opaque ``last_run_id`` reference, the ``schema_version`` and the ``provenance`` invariants
(sent / spent = False; invented = False). It holds only aggregate counts, stable reason codes and opaque
row references — never a contact value — and advancing it performs NO fetch, model call, spend or send.

A ``(business_id, partition_key)`` UNIQUE constraint backs the "one backoff row per partition" guarantee:
the state is advanced in place. The table is tenant-owned, so each row is scoped to one ``business_id``
with NO cross-tenant visibility.

Dual-path safe (same guard style as 0049..0065):

- On a FRESH database the metadata baseline (0001) already created the table and 0002 already built its
  RLS policy (it is a ``TenantOwned`` table, so it is in ``tenant_owned_tables()``), so the table/policy
  steps below are a no-op.
- On a database that reached 0065 BEFORE this table existed, it is created here and its RLS policy /
  grant are added here, so it is protected exactly like the other tenant data tables.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.models  # noqa: F401 — populate Base.metadata
from app.core.config import get_settings
from app.db.models import SearchPartitionBackoff
from app.db.rls import APP_ROLE, POLICY_NAME, RLS_GUC
from app.db.schema_guard import resolve_dca_schema

revision: str = "0066_search_partition_backoffs"
down_revision: str | None = "0065_discovery_run_summaries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (("search_partition_backoffs", SearchPartitionBackoff),)


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
    # Intentionally a no-op, same rationale as 0049..0065: teardown of this table and its policy is owned
    # by the migrations that own the metadata-driven baseline (0002 drops the policy, 0001's ``drop_all``
    # drops the table). Dropping here would race 0002.down's ALTER. Leaving the unused table on a
    # single-step rollback is safe (expand→contract, BR-201).
    pass
