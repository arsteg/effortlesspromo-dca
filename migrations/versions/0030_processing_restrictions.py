"""processing restrictions — the DCA-078 scoped restriction ledger (local + platform-wide)

Revision ID: 0030_processing_restrictions
Revises: 0029_consent_receipts
Create Date: 2026-10-28

Adds the two restriction-ledger tables:

- ``processing_restrictions`` — a LOCAL (tenant) scoped restriction (brand/channel/purpose or the whole
  tenant), VERSIONED. It is tenant-owned, so each row is scoped to one ``business_id`` and carries NO
  cross-tenant visibility — it gets a tenant-isolation RLS policy and the app-role grant, exactly like
  the other tenant data tables.
- ``platform_source_restrictions`` — a justified, platform-wide SOURCE removal, VERSIONED. It is
  platform control-plane data (like ``sources`` / ``source_terms``, DCA-044/045), deliberately OUTSIDE
  tenant RLS: it is created here with NO RLS policy and NO app-role grant (reached over the owner /
  service-auth path, not the tenant data plane).

A restriction only ever makes processing MORE restrictive and authorizes nothing: no host / prospect
record is created by this story and no send is authorized.

Dual-path safe (same guard style as 0005..0029):

- On a FRESH database the metadata baseline (0001) already created both tables; 0002 already built the
  RLS policy for the tenant-owned ``processing_restrictions`` (it is in ``tenant_owned_tables()``) and
  deliberately did NOT for the control-plane ``platform_source_restrictions`` — so everything below is a
  no-op there.
- On a database that reached 0029 BEFORE these tables existed, each is created here; the tenant-owned
  table also gets its RLS policy / grant here, the control-plane table does not.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.models  # noqa: F401 — populate Base.metadata
from app.core.config import get_settings
from app.db.models import PlatformSourceRestriction, ProcessingRestriction
from app.db.rls import APP_ROLE, POLICY_NAME, RLS_GUC
from app.db.schema_guard import resolve_dca_schema

revision: str = "0030_processing_restrictions"
down_revision: str | None = "0029_consent_receipts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, model, is_tenant_owned) — only the tenant-owned table gets an RLS policy + app-role grant.
_TABLES = (
    ("processing_restrictions", ProcessingRestriction, True),
    ("platform_source_restrictions", PlatformSourceRestriction, False),
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
    for table, model, tenant_owned in _TABLES:
        if not _has_table(schema, table):
            # Create exactly this table (search_path is pinned to the DCA schema by env.py).
            model.__table__.create(bind=op.get_bind(), checkfirst=True)

        # Only the tenant-owned table gets tenant-isolation RLS + the app-role grant. The control-plane
        # table is reached over the owner / service-auth path, never the tenant data plane.
        if tenant_owned and not _has_policy(schema, table):
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
    # Intentionally a no-op, same rationale as 0005..0029: teardown of the tenant-owned table and its
    # policy is owned by the migrations that own the metadata-driven baseline (0002 drops the policy,
    # 0001's drop_all drops the table). Dropping here would race 0002.down's ALTER. Leaving the unused
    # tables on a single-step rollback is safe (expand→contract, BR-201).
    pass
