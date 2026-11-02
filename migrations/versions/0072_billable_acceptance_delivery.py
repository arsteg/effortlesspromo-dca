"""billable acceptance delivery — DCA-158 usage-delivery-to-platform-billing columns

Revision ID: 0072_billable_delivery
Revises: 0071_billable_acceptance
Create Date: 2026-11-02

Adds the DCA-158 delivery-tracking columns to ``billable_acceptances`` so a DCA-157 billable-acceptance
ledger entry (status ``recorded``) can be DELIVERED to the EffortlessPromo platform's billing capability and
its status advanced ``recorded → delivered`` ONLY on an acknowledgment — in a duplicate-safe way (the stable
``billing_key`` can only ever map to the SAME platform usage-record handle, never a second meter), WITHOUT
the DCA charging anything (pricing is the platform's):

- ``delivered_at`` — WHEN the usage was acknowledged/delivered (nullable; NULL until delivered).
- ``delivery_receipt_id`` — the platform's OWN opaque usage-record handle (deterministic in the billing/
  request key). NOT NULL, server-default ``''`` so pre-existing rows backfill to "not delivered". Only an
  ACKNOWLEDGED delivery fills it; a PENDING / refused delivery leaves it empty.
- ``delivery_attempt_count`` — how many times delivery has been attempted for this entry (bounded,
  inspectable evidence the retries are duplicate-safe). NOT NULL, server-default ``0``.
- ``last_delivery_attempt_at`` — WHEN delivery was last attempted (nullable; NULL until first attempted).

An unacknowledged delivery NEVER implies success: a PENDING hand-off (a lost / timed-out response) or a
DISABLED refusal (an unpriced meter / unavailable host) leaves the entry ``recorded`` with these columns
empty — no entry is marked delivered, and a missing configured price invents NO charge. Delivering performs
no send and the DCA charges nothing.

Add-column branch, same guard style as 0011 / 0015 / 0020 / 0060 / 0067 / 0069 / 0070:

- On a FRESH database the metadata baseline (0001) already created ``billable_acceptances`` WITH these
  columns, so everything below is a no-op.
- On a database that reached 0071 BEFORE these columns existed, they are added here. The string column
  carries a server default so pre-existing rows backfill to "not delivered"; the integer defaults to ``0``
  and the timestamps to NULL.

No RLS change: ``billable_acceptances`` is a ``TenantOwned`` table already covered by the 0002 / 0071
tenant-isolation policy, so the delivery columns stay scoped to one ``business_id`` with no cross-tenant
visibility.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.models  # noqa: F401 — populate Base.metadata
from app.core.config import get_settings
from app.db.schema_guard import resolve_dca_schema

revision: str = "0072_billable_delivery"
down_revision: str | None = "0071_billable_acceptance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "billable_acceptances"


def _schema() -> str:
    return resolve_dca_schema(get_settings().db_schema)


def _columns(schema: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE, schema=schema)}


def upgrade() -> None:
    schema = _schema()
    existing = _columns(schema)
    if "delivered_at" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
            schema=schema,
        )
    if "delivery_receipt_id" not in existing:
        op.add_column(
            _TABLE,
            sa.Column(
                "delivery_receipt_id", sa.String(length=128), nullable=False, server_default=""
            ),
            schema=schema,
        )
    if "delivery_attempt_count" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("delivery_attempt_count", sa.Integer(), nullable=False, server_default="0"),
            schema=schema,
        )
    if "last_delivery_attempt_at" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("last_delivery_attempt_at", sa.DateTime(timezone=True), nullable=True),
            schema=schema,
        )


def downgrade() -> None:
    # Intentionally a no-op, same rationale as 0011 / 0015 / 0020 / 0060 / 0067 / 0069 / 0070: teardown of
    # these columns is owned by the metadata-driven baseline (0001's ``drop_all`` drops the table). Leaving
    # the added columns on a single-step rollback is safe (expand->contract, BR-201).
    pass
