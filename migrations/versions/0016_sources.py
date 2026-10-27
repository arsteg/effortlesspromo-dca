"""sources — the DCA-044 platform source-catalog table (control-plane, NOT tenant-scoped)

Revision ID: 0016_sources
Revises: 0015_prospect_score_staleness
Create Date: 2026-10-28

Adds ``sources`` — the platform-owned catalog of prospect data sources (official registries and
licensed commercial datasets). A row records a source's declared shape (kind, capabilities, fields,
access method, India coverage) and its lifecycle: every source is created a DISABLED ``draft`` with no
implicit permissions. Approving/enabling a source (and its rights details) is a later, disabled story.

It is control-plane (platform-sourced) data, like ``tenant_agents`` / ``entitlement_projections`` —
deliberately OUTSIDE tenant RLS, so it is NOT added to the 0002 RLS set.

Dual-path safe (same ``checkfirst`` style as 0003's entitlement projection):

- On a FRESH database the metadata baseline (0001) already created ``sources``, so this is a no-op.
- On a database that reached 0015 BEFORE this table existed, the table is created here.

The ``(key)`` unique constraint travels with the table definition (``UniqueConstraint`` on the model),
so it is created on both paths.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from app.db.models import Source

revision: str = "0016_sources"
down_revision: str | None = "0015_prospect_score_staleness"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Create exactly this table (search_path is pinned to the DCA schema by env.py). The unique catalog
    # key constraint on (key) is part of the table definition. No RLS: it is a platform catalog.
    Source.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Source.__table__.drop(bind=op.get_bind(), checkfirst=True)
