"""durable job request lease — DCA-101 lease/fencing columns for safe cross-worker claims

Revision ID: 0038_durable_job_request_lease
Revises: 0037_durable_job_requests
Create Date: 2026-10-28

Adds three columns to ``durable_job_requests`` so a worker can CLAIM a queued job safely across workers
(DCA-099/100 → DCA-101). A claim is a short ``SELECT ... FOR UPDATE SKIP LOCKED`` transaction that flips
one row to ``running`` with a lease, then commits — releasing the row lock before any handler I/O:

- ``lease_owner`` — the worker id currently holding the claim (nullable; NULL on an unclaimed row).
- ``lease_expires_at`` — when the current claim lapses so a crashed/stalled worker's job can be reclaimed
  (nullable; NULL on an unclaimed row).
- ``lease_version`` — a monotonic FENCING TOKEN (NOT NULL, defaults to 0) bumped on every (re)claim, so
  a worker whose lease was superseded holds a stale token and is refused. Pre-existing queued rows
  backfill to 0 (never claimed).

Add-column branch, same guard style as 0011/0015/0020:

- On a FRESH database the metadata baseline (0001) already created ``durable_job_requests`` WITH all
  three columns, so everything below is a no-op.
- On a database that reached 0037 BEFORE these columns existed, they are added here, with
  ``lease_version`` carrying a server default so existing rows backfill to 0.

No RLS change: ``durable_job_requests`` is a ``TenantOwned`` table already covered by the 0002 policy
(and re-established by 0037's create branch), so the lease columns inherit tenant isolation unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.models  # noqa: F401 — populate Base.metadata
from app.core.config import get_settings
from app.db.schema_guard import resolve_dca_schema

revision: str = "0038_durable_job_request_lease"
down_revision: str | None = "0037_durable_job_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "durable_job_requests"


def _schema() -> str:
    return resolve_dca_schema(get_settings().db_schema)


def _columns(schema: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE, schema=schema)}


def upgrade() -> None:
    schema = _schema()
    existing = _columns(schema)
    if "lease_owner" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("lease_owner", sa.String(length=128), nullable=True),
            schema=schema,
        )
    if "lease_expires_at" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
            schema=schema,
        )
    if "lease_version" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("lease_version", sa.Integer(), nullable=False, server_default="0"),
            schema=schema,
        )


def downgrade() -> None:
    # Intentionally a no-op, same rationale as 0011/0015/0020: teardown of these columns is owned by the
    # metadata-driven baseline (0001's ``drop_all`` drops the table). Leaving the added columns on a
    # single-step rollback is safe (expand→contract, BR-201).
    pass
