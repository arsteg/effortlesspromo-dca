"""profile import provenance — DCA-037 source/source_version/provenance on business_profile_drafts

Revision ID: 0011_profile_import_provenance
Revises: 0010_business_profile_drafts
Create Date: 2026-10-27

Adds the DCA-037 provenance columns to ``business_profile_drafts`` so a draft imported from an
authorized platform profile snapshot retains WHERE its facts came from and the exact source version
they were read at:

- ``source`` — the origin contract (e.g. ``platform_profile_snapshot``); "" for a manually-authored
  draft.
- ``source_version`` — the snapshot/contract version the facts were read at; "" when not imported.
- ``provenance`` — the full self-describing provenance record (source, source version, as_of,
  synthetic); ``{}`` when not imported.

Dual-path safe (same guard style as 0005..0010), additive (expand→migrate→contract, BR-201):

- On a FRESH database the metadata baseline (0001) already created ``business_profile_drafts`` WITH
  these columns, so everything below is a no-op.
- On a database that reached 0010 BEFORE these columns existed, each missing column is added here with
  a backfilling server default so existing draft rows read as "not imported" (empty provenance).

The columns live on an already-RLS-protected table, so no policy change is needed. Downgrade is a
no-op: dropping a column a later app revision may read would violate expand→contract; the columns are
harmless if a single-step rollback leaves them in place.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.models  # noqa: F401 — populate Base.metadata
from app.core.config import get_settings
from app.db.schema_guard import resolve_dca_schema

revision: str = "0011_profile_import_provenance"
down_revision: str | None = "0010_business_profile_drafts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "business_profile_drafts"


def _schema() -> str:
    return resolve_dca_schema(get_settings().db_schema)


def _existing_columns(schema: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(_TABLE, schema=schema):
        return set()
    return {col["name"] for col in inspector.get_columns(_TABLE, schema=schema)}


def upgrade() -> None:
    schema = _schema()
    present = _existing_columns(schema)
    if not present:
        # No table at all (unexpected on a DB at 0010) — nothing to alter; 0010 owns the create.
        return

    if "source" not in present:
        op.add_column(
            _TABLE,
            sa.Column("source", sa.String(length=64), nullable=False, server_default=""),
        )
    if "source_version" not in present:
        op.add_column(
            _TABLE,
            sa.Column("source_version", sa.String(length=64), nullable=False, server_default=""),
        )
    if "provenance" not in present:
        op.add_column(
            _TABLE,
            sa.Column("provenance", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        )


def downgrade() -> None:
    # Intentionally a no-op (expand→contract, BR-201): leaving the additive columns on a single-step
    # rollback is safe, and dropping a column a later app revision may read is not.
    pass
