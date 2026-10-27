"""source_term_approvals — the DCA-046 source-configuration approval table (control-plane, NOT tenant-scoped)

Revision ID: 0018_source_term_approvals
Revises: 0017_source_terms
Create Date: 2026-10-28

Adds ``source_term_approvals`` — the platform-owned record of a platform operator's review/approval
of one ``source_terms`` version (DCA-045). A row endorses exactly one ``(source_key, terms_version)``
and carries the review essentials: evidence, reviewer, review date and an explicit expiry, plus a
snapshot of the field/use rights it endorsed. Approving promotes the endorsed terms version to
``active``; enabling the source for acquisition stays a separate, disabled capability (DCA-044).

Like ``sources`` (0016) and ``source_terms`` (0017), this is control-plane (platform-sourced) data —
deliberately OUTSIDE tenant RLS, so it is NOT added to the 0002 RLS set.

Dual-path safe (same ``checkfirst`` style as 0016 / 0017):

- On a FRESH database the metadata baseline (0001) already created ``source_term_approvals``, so this
  is a no-op.
- On a database that reached 0017 BEFORE this table existed, the table is created here.

The ``(source_key, terms_version)`` unique constraint travels with the table definition
(``UniqueConstraint`` on the model), so it is created on both paths.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from app.db.models import SourceTermsApproval

revision: str = "0018_source_term_approvals"
down_revision: str | None = "0017_source_terms"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Create exactly this table (search_path is pinned to the DCA schema by env.py). The unique
    # (source_key, terms_version) constraint is part of the table definition. No RLS: platform catalog.
    SourceTermsApproval.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    SourceTermsApproval.__table__.drop(bind=op.get_bind(), checkfirst=True)
