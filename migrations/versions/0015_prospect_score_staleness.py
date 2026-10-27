"""prospect score staleness — DCA-043 columns on prospect_candidates (score_status, scored version)

Revision ID: 0015_prospect_score_staleness
Revises: 0014_budget_limits
Create Date: 2026-10-28

Adds two columns to ``prospect_candidates`` so a recommendation's match score can be flagged for
recomputation after a confirmed targeting change (DCA-040 → DCA-043):

- ``score_status`` — ``current`` | ``stale`` (NOT NULL, defaults to ``current``). Flipped to ``stale``
  when a confirmed ICP revision change invalidates the score. It is kept strictly separate from the
  existing ``status`` column (the recommendation's acceptance/approval), so invalidating a score never
  alters approval or grants any sending right.
- ``scored_targeting_version`` — the confirmed ICP version the score was computed against (nullable;
  NULL for a placeholder score produced before any targeting was confirmed).

Add-column branch, same guard style as 0011 (the provenance columns):

- On a FRESH database the metadata baseline (0001) already created ``prospect_candidates`` WITH both
  columns, so everything below is a no-op.
- On a database that reached 0014 BEFORE these columns existed, they are added here. ``score_status``
  carries a server default so pre-existing rows backfill to ``current`` (their scores reflect whatever
  targeting produced them until a confirmed change invalidates them).

No RLS change: ``prospect_candidates`` is a ``TenantOwned`` table already covered by the 0002 policy.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.models  # noqa: F401 — populate Base.metadata
from app.core.config import get_settings
from app.db.schema_guard import resolve_dca_schema

revision: str = "0015_prospect_score_staleness"
down_revision: str | None = "0014_budget_limits"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "prospect_candidates"


def _schema() -> str:
    return resolve_dca_schema(get_settings().db_schema)


def _columns(schema: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE, schema=schema)}


def upgrade() -> None:
    schema = _schema()
    existing = _columns(schema)
    if "score_status" not in existing:
        op.add_column(
            _TABLE,
            sa.Column(
                "score_status",
                sa.String(length=16),
                nullable=False,
                server_default="current",
            ),
            schema=schema,
        )
        op.create_index(op.f(f"ix_{_TABLE}_score_status"), _TABLE, ["score_status"], schema=schema)
    if "scored_targeting_version" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("scored_targeting_version", sa.Integer(), nullable=True),
            schema=schema,
        )


def downgrade() -> None:
    # Intentionally a no-op, same rationale as 0011: teardown of these columns is owned by the
    # metadata-driven baseline (0001's ``drop_all`` drops the table). Leaving the added columns on a
    # single-step rollback is safe (expand→contract, BR-201).
    pass
