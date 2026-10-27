"""compliance_policy_rulepacks — the DCA-052 versioned India policy rulepack table (control-plane)

Revision ID: 0021_compliance_policy_rulepacks
Revises: 0020_source_approval_revocation
Create Date: 2026-10-28

Adds ``compliance_policy_rulepacks`` — the platform-owned, versioned record of an India compliance
policy rulepack (DCA-052). A row is ONE version of the pack: its validated ``rules`` (each with an
effect, action, channel, authority = law/provider/product, structured conditions, evidence and an
explicit per-rule validity window), the pack-level evidence and validity window, and a status. Every
row is created ``draft`` — defining rules puts nothing in force (activation is a separate, disabled
capability). ``(jurisdiction, version)`` is unique, so a version never overwrites a prior one.

Like ``sources`` (0016), ``source_terms`` (0017) and ``source_term_approvals`` (0018), this is
control-plane (platform-sourced) data — deliberately OUTSIDE tenant RLS, so it is NOT added to the
0002 RLS set.

Dual-path safe (same ``checkfirst`` style as 0016–0020):

- On a FRESH database the metadata baseline (0001) already created ``compliance_policy_rulepacks``,
  so this is a no-op.
- On a database that reached 0020 BEFORE this table existed, the table is created here.

The ``(jurisdiction, version)`` unique constraint travels with the table definition
(``UniqueConstraint`` on the model), so it is created on both paths.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from app.db.models import CompliancePolicyRulepack

revision: str = "0021_compliance_policy_rulepacks"
down_revision: str | None = "0020_source_approval_revocation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Create exactly this table (search_path is pinned to the DCA schema by env.py). The unique
    # (jurisdiction, version) constraint is part of the table definition. No RLS: platform catalog.
    CompliancePolicyRulepack.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    CompliancePolicyRulepack.__table__.drop(bind=op.get_bind(), checkfirst=True)
