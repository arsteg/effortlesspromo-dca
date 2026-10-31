"""provider call resolution — DCA-116 ambiguous (uncertain) outcome resolution columns

Revision ID: 0047_provider_call_resolution
Revises: 0046_provider_call_records
Create Date: 2026-10-31

Adds the DCA-116 resolution columns to ``provider_call_records`` so an AMBIGUOUS (``uncertain``) provider
outcome can be reconciled once the truth is known — recorded as a SEPARATE dimension from the DCA-115
lifecycle ``status`` (the terminal ``uncertain`` status is never reopened). All adjust only the
record/ledger; nothing is performed, retried, charged or sent.

- ``resolution`` — how the ambiguity was reconciled: "" (unresolved) | needs_review (unsupported provider,
  routed to human review) | confirmed (evidence confirmed it happened → tied hold settled) | void (evidence
  confirmed it did not → tied hold released). NOT NULL, default "" (existing rows backfill to "unresolved").
  Indexed for lookup.
- ``resolution_key`` — the receipt/evidence identity (the idempotency token for the ONE resolution; a
  replay under the same key returns the existing outcome). Nullable; NULL while unresolved. Indexed.
- ``resolution_outcome_code`` — an opaque receipt/evidence code (nullable; NULL while unresolved).
- ``resolution_actor_id`` / ``resolution_actor_type`` — WHO reconciled it, opaque (nullable; NULL while
  unresolved).
- ``resolution_at`` — when the ambiguity was reconciled / routed to review (nullable; NULL while
  unresolved).

Add-column branch, same guard style as 0011/0015/0020/0038/0039/0040/0045:

- On a FRESH database the metadata baseline (0001) already created ``provider_call_records`` WITH these
  columns, so this is a no-op.
- On a database that reached 0046 BEFORE these columns existed, they are added here. The nullable columns
  read NULL on existing rows; ``resolution`` carries a server default of "" so existing rows backfill to
  "unresolved" — no further backfill is needed, as resolution only ever writes these on a DCA-116
  reconciliation.

No RLS change: ``provider_call_records`` is a ``TenantOwned`` table already covered by the 0002 policy (and
re-established by 0046's create branch), so the new columns inherit tenant isolation unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.models  # noqa: F401 — populate Base.metadata
from app.core.config import get_settings
from app.db.schema_guard import resolve_dca_schema

revision: str = "0047_provider_call_resolution"
down_revision: str | None = "0046_provider_call_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "provider_call_records"


def _schema() -> str:
    return resolve_dca_schema(get_settings().db_schema)


def _columns(schema: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE, schema=schema)}


def upgrade() -> None:
    schema = _schema()
    existing = _columns(schema)
    if "resolution" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("resolution", sa.String(length=16), nullable=False, server_default=""),
            schema=schema,
        )
        op.create_index(
            "ix_provider_call_records_resolution", _TABLE, ["resolution"], schema=schema
        )
    if "resolution_key" not in existing:
        op.add_column(
            _TABLE, sa.Column("resolution_key", sa.String(length=128), nullable=True), schema=schema
        )
        op.create_index(
            "ix_provider_call_records_resolution_key",
            _TABLE,
            ["resolution_key"],
            schema=schema,
        )
    if "resolution_outcome_code" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("resolution_outcome_code", sa.String(length=64), nullable=True),
            schema=schema,
        )
    if "resolution_actor_id" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("resolution_actor_id", sa.String(length=128), nullable=True),
            schema=schema,
        )
    if "resolution_actor_type" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("resolution_actor_type", sa.String(length=32), nullable=True),
            schema=schema,
        )
    if "resolution_at" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("resolution_at", sa.DateTime(timezone=True), nullable=True),
            schema=schema,
        )


def downgrade() -> None:
    # Intentionally a no-op, same rationale as 0011/0015/0020/0038/0039/0040/0045: teardown of these
    # columns is owned by the metadata-driven baseline (0001's ``drop_all`` drops the table). Leaving the
    # added columns on a single-step rollback is safe (expand→contract, BR-201).
    pass
