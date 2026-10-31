"""budget reservation reconciliation — DCA-114 settle/release/uncertain columns

Revision ID: 0045_reservation_reconcile
Revises: 0044_budget_reservations
Create Date: 2026-10-31

Adds the DCA-114 reconciliation columns to ``budget_reservations`` so a DCA-113 hold can be reconciled —
exactly once — against the ACTUAL cost once it is known: settled at the clamped actual (unused difference
released), released in full, or preserved as uncertain. All adjust only the ledger; nothing is charged or
sent.

- ``reconciliation_key`` — the provider-call identity that reconciled the hold (the idempotency token for
  the ONE reconciliation; a duplicate settlement under the same identity is a replay). Nullable; NULL
  while ACTIVE. Indexed for lookup. NOT globally unique (one provider call → one hold).
- ``actual_minor`` — the reported actual cost in INR minor units (nullable; NULL while ACTIVE and when
  the result is UNCERTAIN — an unknown result is never coerced to a number).
- ``settled_amount_minor`` — the amount the hold counts toward the caps after settlement: min(actual,
  reserved) for a settle, 0 for a release (nullable; NULL while ACTIVE and when UNCERTAIN).
- ``overspend_minor`` — the flagged, clamped over-run max(0, actual - reserved) (NOT NULL, default 0;
  existing rows backfill to 0).
- ``reconciled_at`` — when the reconciliation was applied (nullable; NULL while ACTIVE).
- ``reconciled_by_actor_id`` / ``reconciled_by_actor_type`` — WHO reconciled it, opaque (nullable; NULL
  while ACTIVE).

Add-column branch, same guard style as 0011/0015/0020/0038/0039/0040:

- On a FRESH database the metadata baseline (0001) already created ``budget_reservations`` WITH these
  columns, so this is a no-op.
- On a database that reached 0044 BEFORE these columns existed, they are added here. The nullable columns
  simply read NULL on existing (ACTIVE) rows; ``overspend_minor`` carries a server default of 0 so
  existing rows backfill to "no over-run" — no further backfill is needed, as reconciliation only ever
  writes these on a terminal transition.

No RLS change: ``budget_reservations`` is a ``TenantOwned`` table already covered by the 0002 policy (and
re-established by 0044's create branch), so the new columns inherit tenant isolation unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.models  # noqa: F401 — populate Base.metadata
from app.core.config import get_settings
from app.db.schema_guard import resolve_dca_schema

revision: str = "0045_reservation_reconcile"
down_revision: str | None = "0044_budget_reservations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "budget_reservations"


def _schema() -> str:
    return resolve_dca_schema(get_settings().db_schema)


def _columns(schema: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE, schema=schema)}


def upgrade() -> None:
    schema = _schema()
    existing = _columns(schema)
    if "reconciliation_key" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("reconciliation_key", sa.String(length=128), nullable=True),
            schema=schema,
        )
        op.create_index(
            "ix_budget_reservations_reconciliation_key",
            _TABLE,
            ["reconciliation_key"],
            schema=schema,
        )
    if "actual_minor" not in existing:
        op.add_column(
            _TABLE, sa.Column("actual_minor", sa.BigInteger(), nullable=True), schema=schema
        )
    if "settled_amount_minor" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("settled_amount_minor", sa.BigInteger(), nullable=True),
            schema=schema,
        )
    if "overspend_minor" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("overspend_minor", sa.BigInteger(), nullable=False, server_default="0"),
            schema=schema,
        )
    if "reconciled_at" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
            schema=schema,
        )
    if "reconciled_by_actor_id" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("reconciled_by_actor_id", sa.String(length=128), nullable=True),
            schema=schema,
        )
    if "reconciled_by_actor_type" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("reconciled_by_actor_type", sa.String(length=32), nullable=True),
            schema=schema,
        )


def downgrade() -> None:
    # Intentionally a no-op, same rationale as 0011/0015/0020/0038/0039/0040: teardown of these columns is
    # owned by the metadata-driven baseline (0001's ``drop_all`` drops the table). Leaving the added
    # columns on a single-step rollback is safe (expand→contract, BR-201).
    pass
