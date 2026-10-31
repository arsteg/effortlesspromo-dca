"""source result cache — the DCA-127 entitled+fresh source-result cache table (tenant-owned, RLS)

Revision ID: 0053_source_result_cache
Revises: 0052_contact_imports
Create Date: 2026-10-31

Adds ``source_result_cache`` (one tenant-owned row per cached source-fetch result: the catalog
``source_key`` / terms ``source_version`` the result was fetched under, the ``operation`` / ``field_key``
/ ``purpose`` rights dimensions rechecked on READ, the opaque ``cache_key`` request fingerprint, the
``negative`` no-result flag, the ``result_kind`` / ``result_ref`` / non-reversible ``result_digest`` kept
in BOTH retention modes, the ``result_payload`` kept ONLY when the licence permits raw retention, the
DCA-062-style ``retention_mode`` / ``retention_reason``, the ``rights_basis``, the ``fetched_at`` the
freshness is measured from, and the bounded ``fresh_until`` deadline after which the entry is a MISS).
The table backs the DCA-127 cache: a hit can only ever serve a result the requesting business is still
ENTITLED to (rights rechecked on read), that matches the CURRENT in-force terms version (the
``source_version`` pin), and that is still FRESH (``fresh_until``) — a stale/expired or negative-TTL
entry is a miss, never served. It is tenant-owned, so each row is scoped to one ``business_id`` with NO
cross-tenant visibility — one business can never read another's cached result.
``(business_id, source_key, source_version, cache_key)`` is unique — one entry per logical request within
a business and terms version, the idempotency key that makes a re-put a no-op rather than a duplicate. The
cache stores/serves PROVIDED results only; it performs no fetch, so paid/live acquisition stays disabled.

Dual-path safe (same guard style as 0028/0032/0033/0037/0041/0042/0044/0048/0049/0050/0051/0052):

- On a FRESH database the metadata baseline (0001) already created the table and 0002 already built its
  RLS policy (it is a ``TenantOwned`` table, so it is in ``tenant_owned_tables()``), so everything below
  is a no-op.
- On a database that reached 0052 BEFORE this table existed, it is created here and its RLS policy /
  grant is added here, so it is protected exactly like the other tenant data tables.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.models  # noqa: F401 — populate Base.metadata
from app.core.config import get_settings
from app.db.models import SourceResultCacheEntry
from app.db.rls import APP_ROLE, POLICY_NAME, RLS_GUC
from app.db.schema_guard import resolve_dca_schema

revision: str = "0053_source_result_cache"
down_revision: str | None = "0052_contact_imports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (("source_result_cache", SourceResultCacheEntry),)


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
    # Intentionally a no-op, same rationale as 0028/0032/0033/0037/0041/0042/0044/0048/0049/0050/0051/0052:
    # teardown of this table and its policy is owned by the migrations that own the metadata-driven
    # baseline (0002 drops the policy, 0001's ``drop_all`` drops the table). Dropping here would race
    # 0002.down's ALTER. Leaving the unused table on a single-step rollback is safe (expand→contract,
    # BR-201).
    pass
