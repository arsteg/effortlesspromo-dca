"""durable job request result — DCA-103 fenced-completion result columns

Revision ID: 0040_durable_job_request_result
Revises: 0039_job_request_lease_deadline
Create Date: 2026-10-28

Adds three columns to ``durable_job_requests`` so a worker can COMMIT a fenced job result (DCA-101 claim
→ DCA-102 renew → DCA-103 complete). Completion flips a ``running`` row to ``succeeded`` and clears its
lease, guarded by the current lease fence AND a re-check of the authorization revisions the worker relied
on — so a stale/replaced worker (reclaimed job) or one whose source rights changed cannot commit:

- ``result_ref`` — an OPAQUE pointer to the committed result (nullable; NULL until a result is committed),
  never raw PII (screened with the same rule as ``payload_ref``).
- ``result_digest`` — a one-way sha256 fingerprint of the committed result (nullable; NULL until commit).
- ``completed_at`` — when the fenced result was committed (nullable; NULL on a queued/running row).

Add-column branch, same guard style as 0011/0015/0020/0038/0039:

- On a FRESH database the metadata baseline (0001) already created ``durable_job_requests`` WITH these
  columns, so this is a no-op.
- On a database that reached 0039 BEFORE these columns existed, they are added here. All three are
  nullable, so existing rows (queued or running) simply read NULL — no backfill needed; completion only
  ever writes them on a successful fenced commit.

No RLS change: ``durable_job_requests`` is a ``TenantOwned`` table already covered by the 0002 policy
(and re-established by 0037's create branch), so the new columns inherit tenant isolation unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.models  # noqa: F401 — populate Base.metadata
from app.core.config import get_settings
from app.db.schema_guard import resolve_dca_schema

revision: str = "0040_durable_job_request_result"
down_revision: str | None = "0039_job_request_lease_deadline"
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
    if "result_ref" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("result_ref", sa.String(length=300), nullable=True),
            schema=schema,
        )
    if "result_digest" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("result_digest", sa.String(length=64), nullable=True),
            schema=schema,
        )
    if "completed_at" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            schema=schema,
        )


def downgrade() -> None:
    # Intentionally a no-op, same rationale as 0011/0015/0020/0038/0039: teardown of these columns is owned
    # by the metadata-driven baseline (0001's ``drop_all`` drops the table). Leaving the added columns on a
    # single-step rollback is safe (expand→contract, BR-201).
    pass
