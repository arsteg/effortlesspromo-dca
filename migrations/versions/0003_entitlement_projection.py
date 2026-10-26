"""entitlement projection — the DCA-023 cached-entitlement table

Revision ID: 0003_entitlement_projection
Revises: 0002_rls
Create Date: 2026-10-26

Creates the ``entitlement_projections`` table: the permitted, bounded cache of a business's last
active entitlement snapshot. It is control-plane (platform-sourced) data, like ``tenant_agents`` —
deliberately OUTSIDE tenant RLS, so it is not added to the 0002 RLS set.

Created from the model's own ``Table`` with ``checkfirst=True`` so it coexists with the
metadata-driven baseline (which also builds current models): on a fresh database the baseline already
created it and this is a no-op; on a database already at 0002 it is created here.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from app.db.models import EntitlementProjection

revision: str = "0003_entitlement_projection"
down_revision: str | None = "0002_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    EntitlementProjection.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    EntitlementProjection.__table__.drop(bind=op.get_bind(), checkfirst=True)
