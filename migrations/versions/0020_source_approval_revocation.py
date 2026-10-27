"""source approval revocation — DCA-050 columns on source_term_approvals (control-plane, NOT tenant-scoped)

Revision ID: 0020_source_approval_revocation
Revises: 0019_business_entitlements
Create Date: 2026-10-28

Adds three columns to ``source_term_approvals`` so a platform operator's revocation of an approved
source-terms version (DCA-050) records its own who/when/why on the approval row (mirroring how the
DCA-046 review essentials are captured there):

- ``revoked_at`` — when the approval was revoked (nullable; NULL until revoked).
- ``revoked_by`` — the opaque operator subject id that revoked it (nullable).
- ``revocation_reason`` — a short opaque reason code/explanation (nullable).

The revocation itself flips the existing ``status`` column ACTIVE → ``revoked`` (``status`` is already
``String(16)``, so the new enum string needs no DDL change) and demotes the promoted ``source_terms``
version's ``status`` ACTIVE → ``revoked`` (same, no DDL). Only these three record columns are new.

Add-column branch, same guard style as 0011 / 0015:

- On a FRESH database the metadata baseline (0001) already created ``source_term_approvals`` WITH all
  three columns, so everything below is a no-op.
- On a database that reached 0019 BEFORE these columns existed, they are added here. All three are
  nullable (an un-revoked approval simply has them NULL), so pre-existing rows backfill to NULL.

No RLS change: ``source_term_approvals`` is platform control-plane data (like ``sources`` 0016 /
``source_terms`` 0017), deliberately OUTSIDE tenant RLS, so it is NOT in the 0002 RLS set.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.models  # noqa: F401 — populate Base.metadata
from app.core.config import get_settings
from app.db.schema_guard import resolve_dca_schema

revision: str = "0020_source_approval_revocation"
down_revision: str | None = "0019_business_entitlements"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "source_term_approvals"


def _schema() -> str:
    return resolve_dca_schema(get_settings().db_schema)


def _columns(schema: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE, schema=schema)}


def upgrade() -> None:
    schema = _schema()
    existing = _columns(schema)
    if "revoked_at" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            schema=schema,
        )
    if "revoked_by" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("revoked_by", sa.String(length=128), nullable=True),
            schema=schema,
        )
    if "revocation_reason" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("revocation_reason", sa.String(length=300), nullable=True),
            schema=schema,
        )


def downgrade() -> None:
    # Intentionally a no-op, same rationale as 0011 / 0015: teardown of these columns is owned by the
    # metadata-driven baseline (0001's ``drop_all`` drops the table). Leaving the added columns on a
    # single-step rollback is safe (expand→contract, BR-201).
    pass
