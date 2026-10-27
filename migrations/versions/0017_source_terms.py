"""source_terms — the DCA-045 source field-and-operation rights table (control-plane, NOT tenant-scoped)

Revision ID: 0017_source_terms
Revises: 0016_sources
Create Date: 2026-10-28

Adds ``source_terms`` — the platform-owned, versioned record of what a catalogued ``sources`` entry
(DCA-044) is permitted to do with each field it returns. A row is one *version* of a source's terms,
expressing rights per field and per operation (collect/store/derive/display/export/share/
model_processing), its purposes, attribution, retention, contract evidence and an explicit validity
window. Every version is created a ``draft``; approving/activating a version is a later, disabled story.

Like ``sources`` (0016) and the 0003 entitlement projection, this is control-plane (platform-sourced)
data — deliberately OUTSIDE tenant RLS, so it is NOT added to the 0002 RLS set.

Dual-path safe (same ``checkfirst`` style as 0016 / 0003):

- On a FRESH database the metadata baseline (0001) already created ``source_terms``, so this is a no-op.
- On a database that reached 0016 BEFORE this table existed, the table is created here.

The ``(source_key, version)`` unique constraint travels with the table definition (``UniqueConstraint``
on the model), so it is created on both paths.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from app.db.models import SourceTerms

revision: str = "0017_source_terms"
down_revision: str | None = "0016_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Create exactly this table (search_path is pinned to the DCA schema by env.py). The unique
    # (source_key, version) constraint is part of the table definition. No RLS: it is a platform catalog.
    SourceTerms.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    SourceTerms.__table__.drop(bind=op.get_bind(), checkfirst=True)
