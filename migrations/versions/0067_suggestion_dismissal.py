"""prospect suggestion dismissal — DCA-151 private per-business dismissal columns

Revision ID: 0067_suggestion_dismissal
Revises: 0066_search_partition_backoffs
Create Date: 2026-11-02

Adds the DCA-151 private-dismissal columns to ``prospect_suggestions`` so an authorized business user can
dismiss one of THIS business's suggestions privately (relevance feedback) — flipping its ``status`` to
``dismissed`` so it drops out of the DCA-150 VISIBLE candidate list — WITHOUT changing any global fact or
policy and WITHOUT affecting another tenant's independent suggestion for the same subject:

- ``dismissed_at`` — WHEN the suggestion was dismissed (nullable; NULL until dismissed).
- ``dismissal_reason`` — the opaque relevance-feedback note (NOT NULL, server-default ``''`` so
  pre-existing rows backfill to "no reason").
- ``dismissed_by`` / ``dismissed_by_type`` — WHO dismissed it (an opaque business actor id + its kind;
  NOT NULL, server-default ``''`` so pre-existing rows backfill to "not dismissed").

A dismissal only ever makes the agent do LESS (hide one of this business's own candidates); it executes
nothing, sends nothing and spends nothing.

Add-column branch, same guard style as 0011 / 0015 / 0020 / 0060:

- On a FRESH database the metadata baseline (0001) already created ``prospect_suggestions`` WITH these
  columns, so everything below is a no-op.
- On a database that reached 0066 BEFORE these columns existed, they are added here. ``dismissal_reason`` /
  ``dismissed_by`` / ``dismissed_by_type`` carry a server default so pre-existing rows backfill to the
  "not dismissed" empty string; ``dismissed_at`` defaults to NULL.

No RLS change: ``prospect_suggestions`` is a ``TenantOwned`` table already covered by the 0002 / 0064
tenant-isolation policy, so the dismissal stays scoped to one ``business_id`` with no cross-tenant
visibility.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.models  # noqa: F401 — populate Base.metadata
from app.core.config import get_settings
from app.db.schema_guard import resolve_dca_schema

revision: str = "0067_suggestion_dismissal"
down_revision: str | None = "0066_search_partition_backoffs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "prospect_suggestions"


def _schema() -> str:
    return resolve_dca_schema(get_settings().db_schema)


def _columns(schema: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE, schema=schema)}


def upgrade() -> None:
    schema = _schema()
    existing = _columns(schema)
    if "dismissed_at" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
            schema=schema,
        )
    if "dismissal_reason" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("dismissal_reason", sa.Text(), nullable=False, server_default=""),
            schema=schema,
        )
    if "dismissed_by" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("dismissed_by", sa.String(length=64), nullable=False, server_default=""),
            schema=schema,
        )
    if "dismissed_by_type" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("dismissed_by_type", sa.String(length=16), nullable=False, server_default=""),
            schema=schema,
        )


def downgrade() -> None:
    # Intentionally a no-op, same rationale as 0011 / 0015 / 0020 / 0060: teardown of these columns is
    # owned by the metadata-driven baseline (0001's ``drop_all`` drops the table). Leaving the added
    # columns on a single-step rollback is safe (expand→contract, BR-201).
    pass
