"""acceptance sync reconcile — DCA-156 uncertain-sync reconciliation columns

Revision ID: 0070_acceptance_sync_reconcile
Revises: 0069_acceptance_intent_sync
Create Date: 2026-11-02

Adds the DCA-156 reconciliation-tracking columns to ``acceptance_intents`` so an UNCERTAIN audience sync
(a DCA-155 attempt whose acknowledgment was lost / timed-out / returned PENDING, leaving the intent
in-doubt) can be reconciled by RE-DRIVING the DCA-154 audience-sync port under the SAME request key — in
a duplicate-safe way (the stable key can only ever map to the SAME external record, never a second one),
WITHOUT performing any send and WITHOUT conferring recipient consent (acceptance is NOT consent):

- ``sync_reconcile_state`` — the reconciliation state (``''`` = never reconciled / not in doubt,
  ``resolved`` = a re-drive came back acknowledged so the intent resolved to ``synced`` with the external
  ids, ``needs_review`` = the host's receipt/status lookup did not confirm the write, so the ambiguity is
  marked for operator review rather than assumed successful). NOT NULL, server-default ``''`` so
  pre-existing rows backfill to "never reconciled". Indexed (a reviewer lists what needs attention).
- ``reconcile_attempt_count`` — how many times an uncertain sync has been re-driven for this intent
  (bounded, inspectable evidence the retries are duplicate-safe). NOT NULL, server-default ``0``.
- ``last_reconciled_at`` — WHEN the uncertain sync was last reconciled (nullable; NULL until first
  reconciled).

An unsupported / unavailable status lookup NEVER implies success: a re-drive that does not come back
acknowledged leaves the intent ``pending_sync`` and marks ``sync_reconcile_state='needs_review'`` — no
accepted status and no external ids before the required acknowledgment. Reconciling executes nothing,
sends nothing and spends nothing.

Add-column branch, same guard style as 0011 / 0015 / 0020 / 0060 / 0067 / 0069:

- On a FRESH database the metadata baseline (0001) already created ``acceptance_intents`` WITH these
  columns, so everything below is a no-op.
- On a database that reached 0069 BEFORE these columns existed, they are added here. The string column
  carries a server default so pre-existing rows backfill to "never reconciled"; the integer defaults to
  ``0`` and the timestamp to NULL.

No RLS change: ``acceptance_intents`` is a ``TenantOwned`` table already covered by the 0002 / 0068
tenant-isolation policy, so the reconciliation columns stay scoped to one ``business_id`` with no
cross-tenant visibility.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.models  # noqa: F401 — populate Base.metadata
from app.core.config import get_settings
from app.db.schema_guard import resolve_dca_schema

revision: str = "0070_acceptance_sync_reconcile"
down_revision: str | None = "0069_acceptance_intent_sync"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "acceptance_intents"


def _schema() -> str:
    return resolve_dca_schema(get_settings().db_schema)


def _columns(schema: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE, schema=schema)}


def upgrade() -> None:
    schema = _schema()
    existing = _columns(schema)
    if "sync_reconcile_state" not in existing:
        op.add_column(
            _TABLE,
            sa.Column(
                "sync_reconcile_state", sa.String(length=16), nullable=False, server_default=""
            ),
            schema=schema,
        )
        op.create_index(
            op.f(f"ix_{_TABLE}_sync_reconcile_state"),
            _TABLE,
            ["sync_reconcile_state"],
            schema=schema,
        )
    if "reconcile_attempt_count" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("reconcile_attempt_count", sa.Integer(), nullable=False, server_default="0"),
            schema=schema,
        )
    if "last_reconciled_at" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("last_reconciled_at", sa.DateTime(timezone=True), nullable=True),
            schema=schema,
        )


def downgrade() -> None:
    # Intentionally a no-op, same rationale as 0011 / 0015 / 0020 / 0060 / 0067 / 0069: teardown of these
    # columns is owned by the metadata-driven baseline (0001's ``drop_all`` drops the table). Leaving the
    # added columns on a single-step rollback is safe (expand->contract, BR-201).
    pass
