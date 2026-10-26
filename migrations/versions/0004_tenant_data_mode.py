"""tenant data mode — the DCA-024 agent-configuration column

Revision ID: 0004_tenant_data_mode
Revises: 0003_entitlement_projection
Create Date: 2026-10-26

Adds ``tenant_agents.data_mode`` (default ``my_data_only``). Guarded with an inspector check so it
coexists with the metadata-driven baseline: on a fresh database the baseline already created the
column and this is a no-op; on a database already at 0003 the column is added here (with a
server_default so existing rows are backfilled to the conservative default).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.config import get_settings
from app.db.models import DataMode
from app.db.schema_guard import resolve_dca_schema

revision: str = "0004_tenant_data_mode"
down_revision: str | None = "0003_entitlement_projection"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMN = "data_mode"
_TABLE = "tenant_agents"


def _schema() -> str:
    return resolve_dca_schema(get_settings().db_schema)


def _has_column(schema: str) -> bool:
    insp = sa.inspect(op.get_bind())
    return _COLUMN in {c["name"] for c in insp.get_columns(_TABLE, schema=schema)}


def upgrade() -> None:
    schema = _schema()
    if not _has_column(schema):
        op.add_column(
            _TABLE,
            sa.Column(
                _COLUMN,
                sa.String(length=32),
                nullable=False,
                server_default=DataMode.MY_DATA_ONLY.value,
            ),
            schema=schema,
        )


def downgrade() -> None:
    schema = _schema()
    if _has_column(schema):
        op.drop_column(_TABLE, _COLUMN, schema=schema)
