"""run plan approval — DCA-141 columns on discovery_run_requests (pinned/frozen approved plan)

Revision ID: 0060_run_plan_approval
Revises: 0059_discovery_run_requests
Create Date: 2026-10-31

Adds the DCA-141 plan-binding columns to ``discovery_run_requests`` so an authorized business actor can
APPROVE + persist a reviewed DCA-135 discovery plan onto a bounded run — freezing the exact bounded tasks
the run would execute and moving the run ``pending`` → ``planned``:

- ``plan_version`` — the exact :class:`~app.db.models.DiscoveryPlan` version pinned onto the run
  (nullable; NULL until a plan is approved). The run froze THIS version; a later plan edit (a new draft
  version) never changes what the run committed to.
- ``plan_snapshot`` — the immutable, frozen copy of that plan's bounded tasks + caps + grounding at the
  moment of approval (nullable JSON; NULL until approved). It is the exact set the later, still-disabled
  pipeline would execute, and it carries the load-bearing invariants (executed / sent / spent = False).
- ``approved_by`` / ``approved_by_type`` — WHO approved it (an opaque business actor id + its kind; NOT
  NULL, server-default ``''`` so pre-existing PENDING rows backfill to "not yet approved").
- ``approved_at`` — WHEN it was approved (nullable; NULL until approved).

Approving a plan **executes nothing**: the run only advances PENDING → PLANNED and records the pinned
plan snapshot (no task is run, no job enqueued, no message sent and nothing is spent).

Add-column branch, same guard style as 0011 / 0015 / 0020:

- On a FRESH database the metadata baseline (0001) already created ``discovery_run_requests`` WITH these
  columns, so everything below is a no-op.
- On a database that reached 0059 BEFORE these columns existed, they are added here. ``approved_by`` /
  ``approved_by_type`` carry a server default so pre-existing rows backfill to the "not yet approved"
  empty string; the nullable columns default to NULL.

No RLS change: ``discovery_run_requests`` is a ``TenantOwned`` table already covered by the 0002 / 0059
tenant-isolation policy.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.models  # noqa: F401 — populate Base.metadata
from app.core.config import get_settings
from app.db.schema_guard import resolve_dca_schema

revision: str = "0060_run_plan_approval"
down_revision: str | None = "0059_discovery_run_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "discovery_run_requests"


def _schema() -> str:
    return resolve_dca_schema(get_settings().db_schema)


def _columns(schema: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE, schema=schema)}


def upgrade() -> None:
    schema = _schema()
    existing = _columns(schema)
    if "plan_version" not in existing:
        op.add_column(_TABLE, sa.Column("plan_version", sa.Integer(), nullable=True), schema=schema)
    if "plan_snapshot" not in existing:
        op.add_column(_TABLE, sa.Column("plan_snapshot", sa.JSON(), nullable=True), schema=schema)
    if "approved_by" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("approved_by", sa.String(length=64), nullable=False, server_default=""),
            schema=schema,
        )
    if "approved_by_type" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("approved_by_type", sa.String(length=16), nullable=False, server_default=""),
            schema=schema,
        )
    if "approved_at" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
            schema=schema,
        )


def downgrade() -> None:
    # Intentionally a no-op, same rationale as 0011 / 0015 / 0020: teardown of these columns is owned by
    # the metadata-driven baseline (0001's ``drop_all`` drops the table). Leaving the added columns on a
    # single-step rollback is safe (expand→contract, BR-201).
    pass
