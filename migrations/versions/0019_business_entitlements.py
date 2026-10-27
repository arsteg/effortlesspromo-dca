"""business source entitlements — the DCA-047 per-business source-rights table (tenant-owned, RLS-protected)

Revision ID: 0019_business_entitlements
Revises: 0018_source_term_approvals
Create Date: 2026-10-28

Adds ``business_source_entitlements`` — a tenant-owned table holding, per business and per catalog
source, the VERSIONED field/operation/purpose rights the business grants itself. An entitlement
NARROWS the platform-approved rights (the in-force DCA-046 approval) down to what the business opts
into; it can never widen them. Each save inserts a new version (``(business_id, source_key, version)``
is unique), so a new entitlement preserves every prior version rather than overwriting it — an
immutable, auditable history.

Dual-path safe (same guard style as 0005..0014):

- On a FRESH database the metadata baseline (0001) already created ``business_source_entitlements``
  and 0002 already built its RLS policy (it is a ``TenantOwned`` table, so it is in
  ``tenant_owned_tables()``), so everything below is a no-op.
- On a database that reached 0018 BEFORE this table existed, the table is created here and its RLS
  policy / grants are added here, so it is protected exactly like the other tenant data tables.

The ``(business_id, source_key, version)`` unique constraint travels with the table definition
(``UniqueConstraint`` via ``tenant_unique`` on the model), so it is created on both paths.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.models  # noqa: F401 — populate Base.metadata
from app.core.config import get_settings
from app.db.models import BusinessSourceEntitlement
from app.db.rls import APP_ROLE, POLICY_NAME, RLS_GUC
from app.db.schema_guard import resolve_dca_schema

revision: str = "0019_business_entitlements"
down_revision: str | None = "0018_source_term_approvals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "business_source_entitlements"


def _schema() -> str:
    return resolve_dca_schema(get_settings().db_schema)


def _has_table(schema: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(_TABLE, schema=schema)


def _has_policy(schema: str) -> bool:
    row = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM pg_policies WHERE schemaname = :s "
                "AND tablename = :t AND policyname = :p"
            ),
            {"s": schema, "t": _TABLE, "p": POLICY_NAME},
        )
        .first()
    )
    return row is not None


def upgrade() -> None:
    schema = _schema()
    if not _has_table(schema):
        # Create exactly this table (search_path is pinned to the DCA schema by env.py). The
        # per-tenant (source_key, version) unique constraint is part of the table definition.
        BusinessSourceEntitlement.__table__.create(bind=op.get_bind(), checkfirst=True)

    if not _has_policy(schema):
        op.execute(f'ALTER TABLE "{_TABLE}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{_TABLE}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f"""
            CREATE POLICY {POLICY_NAME} ON "{_TABLE}"
                USING (business_id = current_setting('{RLS_GUC}', true))
                WITH CHECK (business_id = current_setting('{RLS_GUC}', true));
            """
        )
        op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON "{_TABLE}" TO {APP_ROLE}')


def downgrade() -> None:
    # Intentionally a no-op, same rationale as 0005..0014: teardown of ``business_source_entitlements``
    # and its policy is owned by the migrations that own the metadata-driven baseline (0002 drops the
    # policy, 0001's ``drop_all`` drops the table). Dropping here would race 0002.down's ALTER. Leaving
    # the unused table on a single-step rollback is safe (expand→contract, BR-201).
    pass
