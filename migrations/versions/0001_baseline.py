"""baseline — DCA-owned schema (tenant agents, discovery, candidates, decisions, AI invocations)

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-26

The baseline creates the current DCA model set inside the DCA-owned schema (env.py pins the
connection search_path, so these schema-agnostic tables land in DCA territory, never ``public`` or a
platform schema). Driving the baseline from ``Base.metadata`` keeps it the single source of truth
for the initial shape; subsequent schema changes get their own explicit, hand-written revisions.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

import app.db.models  # noqa: F401 — populate Base.metadata
from app.db.base import Base

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
