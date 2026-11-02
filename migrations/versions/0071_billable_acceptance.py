"""billable acceptances — the DCA-157 billable-acceptance ledger (tenant-owned, RLS)

Revision ID: 0071_billable_acceptance
Revises: 0070_acceptance_sync_reconcile
Create Date: 2026-11-02

Adds ``billable_acceptances`` (one tenant-owned row per accepted prospect that became BILLABLE after its
DCA-155/156 audience sync was ACKNOWLEDGED). It records the ``acceptance_intent_id`` it meters, the
suggestion / subject it is about, the STABLE logical ``billing_key`` (deterministic in business + intent),
the platform's OWN ``external_crm_record_id`` (captured from the acknowledged intent — the mapping that
proves the sync was acknowledged), the ``outbox_event_id`` of the staged DCA-027 billing-usage record, the
``status`` (``recorded`` only — the actual delivery to platform billing is the disabled DCA-158 story),
WHO recorded it (``recorded_by`` / ``recorded_by_type``) and the ``provenance`` invariants (billable =
True; delivered / sent / spent / consentConferred / membershipCreated = False). Recording an entry makes
NO live billing call and charges NOTHING; it confers NO recipient consent (acceptance is NOT consent) and
performs NO send — nothing is charged until the disabled DCA-158 delivery story runs.

A ``(business_id, acceptance_intent_id)`` UNIQUE constraint backs the "a prospect is billable once per
business" guarantee: a duplicate acknowledgment (a replay, or a DCA-156 reconcile that re-resolved the
SAME intent) is a no-op (no duplicate row, no second usage record, no second audit) — never double-charged.
The table is tenant-owned, so each row is scoped to one ``business_id`` with NO cross-tenant visibility,
and holds the intent / suggestion / subject ids + the platform's external id + opaque metadata — never a
contact value.

Dual-path safe (same guard style as 0049..0068):

- On a FRESH database the metadata baseline (0001) already created the table and 0002 already built its
  RLS policy (it is a ``TenantOwned`` table, so it is in ``tenant_owned_tables()``), so the table/policy
  steps below are a no-op.
- On a database that reached 0070 BEFORE this table existed, it is created here and its RLS policy /
  grant are added here, so it is protected exactly like the other tenant data tables.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.models  # noqa: F401 — populate Base.metadata
from app.core.config import get_settings
from app.db.models import BillableAcceptance
from app.db.rls import APP_ROLE, POLICY_NAME, RLS_GUC
from app.db.schema_guard import resolve_dca_schema

revision: str = "0071_billable_acceptance"
down_revision: str | None = "0070_acceptance_sync_reconcile"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (("billable_acceptances", BillableAcceptance),)


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
    # Intentionally a no-op, same rationale as 0049..0068: teardown of this table and its policy is owned
    # by the migrations that own the metadata-driven baseline (0002 drops the policy, 0001's ``drop_all``
    # drops the table). Dropping here would race 0002.down's ALTER. Leaving the unused table on a
    # single-step rollback is safe (expand→contract, BR-201).
    pass
