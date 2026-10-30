"""durable job request lease deadline — DCA-102 bounded-extension ceiling for lease renewal

Revision ID: 0039_job_request_lease_deadline
Revises: 0038_durable_job_request_lease
Create Date: 2026-10-28

Adds ONE column to ``durable_job_requests`` so a running job's lease can be renewed (heartbeated) within
hard bounds (DCA-101 claim → DCA-102 renew):

- ``lease_deadline`` — the absolute latest a heartbeat may push ``lease_expires_at`` out to for the
  current claim (nullable; stamped at claim time as now + MAX_LEASE_LIFETIME_SECONDS, reset on reclaim).
  A renewal never extends past it, so a stuck/looping worker cannot hold a claim forever and starve a
  reclaim. NULL on an unclaimed row and on any row claimed before this column existed (treated by
  :meth:`~app.jobs.claim.JobClaimStore.renew` as "no DCA-102 ceiling recorded").

Add-column branch, same guard style as 0011/0015/0020/0038:

- On a FRESH database the metadata baseline (0001) already created ``durable_job_requests`` WITH
  ``lease_deadline``, so this is a no-op.
- On a database that reached 0038 BEFORE this column existed, it is added here. The column is nullable,
  so existing rows (queued or already-running) simply read NULL — renewal falls back to the per-call
  bound and the absolute ceiling only applies to claims taken after the migration. No backfill needed.

No RLS change: ``durable_job_requests`` is a ``TenantOwned`` table already covered by the 0002 policy
(and re-established by 0037's create branch), so the new column inherits tenant isolation unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.models  # noqa: F401 — populate Base.metadata
from app.core.config import get_settings
from app.db.schema_guard import resolve_dca_schema

revision: str = "0039_job_request_lease_deadline"
down_revision: str | None = "0038_durable_job_request_lease"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "durable_job_requests"


def _schema() -> str:
    return resolve_dca_schema(get_settings().db_schema)


def _columns(schema: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE, schema=schema)}


def upgrade() -> None:
    schema = _schema()
    if "lease_deadline" not in _columns(schema):
        op.add_column(
            _TABLE,
            sa.Column("lease_deadline", sa.DateTime(timezone=True), nullable=True),
            schema=schema,
        )


def downgrade() -> None:
    # Intentionally a no-op, same rationale as 0011/0015/0020/0038: teardown of this column is owned by
    # the metadata-driven baseline (0001's ``drop_all`` drops the table). Leaving the added column on a
    # single-step rollback is safe (expand→contract, BR-201).
    pass
