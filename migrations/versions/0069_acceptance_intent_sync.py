"""acceptance intent sync — DCA-155 external-mapping + sync-status columns

Revision ID: 0069_acceptance_intent_sync
Revises: 0068_acceptance_intents
Create Date: 2026-11-02

Adds the DCA-155 sync-reconciliation columns to ``acceptance_intents`` so one accepted record can be
synchronized THROUGH EffortlessPromo and the platform's acknowledgment applied back onto the intent —
WITHOUT performing any send and WITHOUT conferring recipient consent (acceptance is NOT consent):

- ``sync_status`` — the last AudienceSyncStatus observed (``''`` before any attempt, ``pending`` for a
  queued hand-off awaiting reconciliation, ``acknowledged`` once the platform wrote the record). NOT
  NULL, server-default ``''`` so pre-existing rows backfill to "not yet synced".
- ``external_crm_record_id`` / ``external_audience_id`` / ``external_version`` — the platform's OWN
  external record mapping, persisted ONLY on an acknowledged sync (opaque handles, never contact data).
  NOT NULL, server-default ``''`` so pre-existing rows backfill to "no external mapping".
- ``synced_at`` — WHEN the sync was acknowledged (nullable; NULL until acknowledged).
- ``last_sync_attempt_at`` — WHEN a sync was last attempted (nullable; NULL until first attempted), so a
  pending hand-off awaiting reconciliation is inspectable.

A timeout / lost acknowledgment leaves ``sync_status='pending'`` and the intent ``pending_sync`` — no
accepted status and no external ids before the required acknowledgment. Syncing executes nothing, sends
nothing and spends nothing.

Add-column branch, same guard style as 0011 / 0015 / 0020 / 0060 / 0067:

- On a FRESH database the metadata baseline (0001) already created ``acceptance_intents`` WITH these
  columns, so everything below is a no-op.
- On a database that reached 0068 BEFORE these columns existed, they are added here. The string columns
  carry a server default so pre-existing rows backfill to the "not yet synced" empty string; the
  timestamps default to NULL.

No RLS change: ``acceptance_intents`` is a ``TenantOwned`` table already covered by the 0002 / 0068
tenant-isolation policy, so the sync columns stay scoped to one ``business_id`` with no cross-tenant
visibility.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.db.models  # noqa: F401 — populate Base.metadata
from app.core.config import get_settings
from app.db.schema_guard import resolve_dca_schema

revision: str = "0069_acceptance_intent_sync"
down_revision: str | None = "0068_acceptance_intents"
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
    if "sync_status" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("sync_status", sa.String(length=16), nullable=False, server_default=""),
            schema=schema,
        )
        op.create_index(op.f(f"ix_{_TABLE}_sync_status"), _TABLE, ["sync_status"], schema=schema)
    if "external_crm_record_id" not in existing:
        op.add_column(
            _TABLE,
            sa.Column(
                "external_crm_record_id",
                sa.String(length=128),
                nullable=False,
                server_default="",
            ),
            schema=schema,
        )
    if "external_audience_id" not in existing:
        op.add_column(
            _TABLE,
            sa.Column(
                "external_audience_id", sa.String(length=128), nullable=False, server_default=""
            ),
            schema=schema,
        )
    if "external_version" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("external_version", sa.String(length=64), nullable=False, server_default=""),
            schema=schema,
        )
    if "synced_at" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
            schema=schema,
        )
    if "last_sync_attempt_at" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("last_sync_attempt_at", sa.DateTime(timezone=True), nullable=True),
            schema=schema,
        )


def downgrade() -> None:
    # Intentionally a no-op, same rationale as 0011 / 0015 / 0020 / 0060 / 0067: teardown of these columns
    # is owned by the metadata-driven baseline (0001's ``drop_all`` drops the table). Leaving the added
    # columns on a single-step rollback is safe (expand→contract, BR-201).
    pass
