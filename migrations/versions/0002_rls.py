"""rls — row-level security on tenant-owned tables (non-bypass app role, tx-local tenant GUC)

Revision ID: 0002_rls
Revises: 0001_baseline
Create Date: 2026-10-26

Applies RLS to exactly the DCA-017 tenant-owned tables (``app.db.rls.tenant_owned_tables``): each
gets ``ENABLE``/``FORCE ROW LEVEL SECURITY`` and a single policy that filters rows by the
transaction-local tenant GUC. A non-login, ``NOBYPASSRLS`` application role is (idempotently) created
and granted DML on those tables plus ``USAGE`` on the DCA schema — the runtime login user is a member
of it, so direct app queries are always subject to RLS.

The platform-owned registration catalog (``tenant_agents``, ``icp_profiles``) is intentionally left
OUT of tenant RLS and is NOT granted to the app role: it is control-plane data accessed over the
separate service-auth path, not tenant operational data.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from app.core.config import get_settings
from app.db.rls import APP_ROLE, POLICY_NAME, RLS_GUC, tenant_owned_tables
from app.db.schema_guard import resolve_dca_schema

revision: str = "0002_rls"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _schema() -> str:
    # Same resolution env.py uses; the connection's search_path is already pinned to it, so table
    # names below stay unqualified and still land on the DCA-owned tables.
    return resolve_dca_schema(get_settings().db_schema)


def upgrade() -> None:
    schema = _schema()

    # Non-login, non-superuser, NON-BYPASS application role. Idempotent: roles are cluster-global and
    # may already exist from another DCA database on the same cluster.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                CREATE ROLE {APP_ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS INHERIT;
            END IF;
        END
        $$;
        """
    )
    op.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO {APP_ROLE}')

    for table in tenant_owned_tables():
        # FORCE so even a (hypothetical non-superuser) table owner is subject to the policy.
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        # One policy, same predicate for read (USING) and write (WITH CHECK): the row's business_id
        # must equal the transaction's tenant GUC. Unset GUC => current_setting(...,true) is NULL =>
        # predicate is NULL => no rows (fail-closed), and writes with a mismatched owner are rejected.
        op.execute(
            f"""
            CREATE POLICY {POLICY_NAME} ON "{table}"
                USING (business_id = current_setting('{RLS_GUC}', true))
                WITH CHECK (business_id = current_setting('{RLS_GUC}', true));
            """
        )
        op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON "{table}" TO {APP_ROLE}')


def downgrade() -> None:
    # Reverse order is irrelevant here (independent tables); undo per table so a re-upgrade is clean.
    for table in tenant_owned_tables():
        op.execute(f'REVOKE SELECT, INSERT, UPDATE, DELETE ON "{table}" FROM {APP_ROLE}')
        op.execute(f'DROP POLICY IF EXISTS {POLICY_NAME} ON "{table}"')
        op.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
    # The app role is left in place (idempotent create on re-upgrade); dropping a cluster-global role
    # that other databases may share is deliberately not done in a per-database downgrade.
