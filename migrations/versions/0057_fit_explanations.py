"""fit explanations — the DCA-138 grounded business-fit explanation table (tenant-owned, RLS)

Revision ID: 0057_fit_explanations
Revises: 0056_identity_match_suggestions
Create Date: 2026-10-31

Adds ``fit_explanations`` (one tenant-owned row per prospect: the advisory ``fit_band`` + ``rationale``,
the GROUNDED, citation-validated ``assessments`` snapshot, an honest ``dropped`` record of ungrounded
claims the DCA re-grounding gate rejected, the SEPARATE ``eligibility`` / ``eligibility_reason`` carried
through verbatim — never derived from the fit — the model's advisory ``overall_confidence``, and the AI
``source`` / ``prompt_version`` / ``synthetic`` provenance). The table is the business-state write for
DCA-138, which EXPLAINS why a prospect fits the business's confirmed ICP through the one DCA AI
chokepoint. Fit is strictly separate from eligibility — a high fit can never change a channel
restriction — and the explanation is advisory and never auto-acted. It is tenant-owned, so each row is
scoped to one ``business_id`` with NO cross-tenant visibility, and holds only the business's own
ICP-fit reasoning (no personal contact data, no consent/restriction state).
``(business_id, subject_type, subject_ref)`` is unique — one current explanation per prospect per
business, the idempotency key that makes a re-run return the existing row rather than a duplicate.

Dual-path safe (same guard style as 0028/0032/0033/0037/0041/0042/0044/0048/0049/0050/0051/0052/0053/0054/0055/0056):

- On a FRESH database the metadata baseline (0001) already created the table and 0002 already built its
  RLS policy (it is a ``TenantOwned`` table, so it is in ``tenant_owned_tables()``), so everything below
  is a no-op.
- On a database that reached 0056 BEFORE this table existed, it is created here and its RLS policy /
  grant is added here, so it is protected exactly like the other tenant data tables.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.models  # noqa: F401 — populate Base.metadata
from app.core.config import get_settings
from app.db.models import FitExplanation
from app.db.rls import APP_ROLE, POLICY_NAME, RLS_GUC
from app.db.schema_guard import resolve_dca_schema

revision: str = "0057_fit_explanations"
down_revision: str | None = "0056_identity_match_suggestions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (("fit_explanations", FitExplanation),)


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
    # Intentionally a no-op, same rationale as 0028..0056: teardown of this table and its policy is owned
    # by the migrations that own the metadata-driven baseline (0002 drops the policy, 0001's ``drop_all``
    # drops the table). Dropping here would race 0002.down's ALTER. Leaving the unused table on a
    # single-step rollback is safe (expand→contract, BR-201).
    pass
