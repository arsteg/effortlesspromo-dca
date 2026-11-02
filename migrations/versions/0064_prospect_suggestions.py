"""prospect suggestions — the DCA-145 scoped prospect-suggestion record (tenant-owned, RLS)

Revision ID: 0064_prospect_suggestions
Revises: 0063_observation_batch_resolutions
Create Date: 2026-11-01

Adds ``prospect_suggestions`` (one tenant-owned row per scoped prospect SUGGESTION created from a DCA-144
resolved discovery observation: an eligible organization subject in a run's resolved batch turned into a
business-scoped candidate). It records the ``run_id`` it was drawn from, the ``subject_type`` /
``subject_id`` it is about, the ``visibility`` constraint (``research_only`` vs the disabled
``sendable``), the deterministic ICP ``match_score`` + ``rationale``, the ``scored_icp_version`` targeting
version it was scored against, the ``schema_version``, the ``status`` (suggested-only), the ``expires_at``
expiry and the ``provenance`` invariants (suggested = True; sendable / addedToAudience / autoAccepted /
sent / spent = False). A suggestion is research-only by construction — it carries no sendable contact,
confers NO audience membership, and acceptance is NOT recipient consent — and nothing is fetched, sent,
spent or auto-accepted.

A ``(business_id, run_id, subject_id)`` UNIQUE constraint backs the "a subject is suggested once per run"
idempotency guarantee: re-suggesting the same subject is a no-op (no duplicate row, no second audit). The
table is tenant-owned, so each row is scoped to one ``business_id`` with NO cross-tenant visibility, and
holds the subject id + opaque scoring metadata — never a contact value.

Dual-path safe (same guard style as 0049..0063):

- On a FRESH database the metadata baseline (0001) already created the table and 0002 already built its
  RLS policy (it is a ``TenantOwned`` table, so it is in ``tenant_owned_tables()``), so the table/policy
  steps below are a no-op.
- On a database that reached 0063 BEFORE this table existed, it is created here and its RLS policy /
  grant are added here, so it is protected exactly like the other tenant data tables.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.models  # noqa: F401 — populate Base.metadata
from app.core.config import get_settings
from app.db.models import ProspectSuggestion
from app.db.rls import APP_ROLE, POLICY_NAME, RLS_GUC
from app.db.schema_guard import resolve_dca_schema

revision: str = "0064_prospect_suggestions"
down_revision: str | None = "0063_observation_batch_resolutions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (("prospect_suggestions", ProspectSuggestion),)


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
    # Intentionally a no-op, same rationale as 0049..0063: teardown of this table and its policy is owned
    # by the migrations that own the metadata-driven baseline (0002 drops the policy, 0001's ``drop_all``
    # drops the table). Dropping here would race 0002.down's ALTER. Leaving the unused table on a
    # single-step rollback is safe (expand→contract, BR-201).
    pass
