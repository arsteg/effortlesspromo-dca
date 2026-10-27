"""compliance_policy_approvals — the DCA-053 policy-version approval table (control-plane, NOT tenant-scoped)

Revision ID: 0022_compliance_policy_approvals
Revises: 0021_compliance_policy_rulepacks
Create Date: 2026-10-28

Adds ``compliance_policy_approvals`` — the platform-owned record of a platform operator's review/
approval of one ``compliance_policy_rulepacks`` version (DCA-052). A row endorses exactly one
``(jurisdiction, policy_version)`` and carries the review essentials: references/evidence, reviewer,
review date and an explicit review expiry, plus a content fingerprint binding the approval to the exact
rules reviewed. Approving promotes the endorsed rulepack version to ``active``; the policy engine that
would CONSULT an in-force pack stays a separate, disabled capability.

Like ``sources`` (0016), ``source_terms`` (0017), ``source_term_approvals`` (0018) and
``compliance_policy_rulepacks`` (0021), this is control-plane (platform-sourced) data — deliberately
OUTSIDE tenant RLS, so it is NOT added to the 0002 RLS set.

Dual-path safe (same ``checkfirst`` style as 0016–0021):

- On a FRESH database the metadata baseline (0001) already created ``compliance_policy_approvals``, so
  this is a no-op.
- On a database that reached 0021 BEFORE this table existed, the table is created here.

The ``(jurisdiction, policy_version)`` unique constraint travels with the table definition
(``UniqueConstraint`` on the model), so it is created on both paths.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from app.db.models import CompliancePolicyApproval

revision: str = "0022_compliance_policy_approvals"
down_revision: str | None = "0021_compliance_policy_rulepacks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Create exactly this table (search_path is pinned to the DCA schema by env.py). The unique
    # (jurisdiction, policy_version) constraint is part of the table definition. No RLS: platform data.
    CompliancePolicyApproval.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    CompliancePolicyApproval.__table__.drop(bind=op.get_bind(), checkfirst=True)
