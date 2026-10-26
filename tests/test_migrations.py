"""DCA-007 — Alembic initialized against the DCA-owned schema only, with a host-schema guard.

Schema/DDL behaviour is verified on a **disposable PostgreSQL** (`DCA_TEST_DATABASE_URL`), never
SQLite. Those tests skip when no Postgres URL is supplied; the guard unit tests always run.

Spin up a throwaway Postgres, e.g.:
    docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55432:5432 postgres:16-alpine
    export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55432/dca"
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from app.core.config import get_settings
from app.db.schema_guard import HostSchemaError, resolve_dca_schema

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DCA_TABLES = {
    "tenant_agents",
    "icp_profiles",
    "discovery_runs",
    "prospect_candidates",
    "acquisition_decisions",
    "ai_invocations",
    "audit_events",
    "outbox_messages",
    "inbox_receipts",
    "revision_projections",
    "revision_pending_events",
}


# --- always-on guard unit tests (no database) ---


def test_resolve_dca_schema_accepts_owned_name():
    assert resolve_dca_schema("dca") == "dca"
    assert resolve_dca_schema("  dca_acq  ") == "dca_acq"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "public",
        "platform",
        "effortlesspromo",
        "information_schema",
        "pg_catalog",
        "pg_temp_3",
    ],
)
def test_resolve_dca_schema_rejects_host_owned(bad):
    with pytest.raises(HostSchemaError):
        resolve_dca_schema(bad)


# --- Postgres-backed migration behaviour ---


def _alembic_config(schema: str):
    from alembic.config import Config

    os.environ["EFFORTLESSPROMO_DCA_ENVIRONMENT"] = "testing"
    os.environ["EFFORTLESSPROMO_DCA_DATABASE_URL"] = PG_URL
    os.environ["EFFORTLESSPROMO_DCA_DB_SCHEMA"] = schema
    get_settings.cache_clear()  # env.py reads settings fresh each run
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    return cfg


@pytest.fixture
def clean_schema():
    # Drop the DCA schema before and after so history is proven reproducible on an empty database.
    def _drop(name: str):
        eng = create_engine(PG_URL)
        with eng.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
        eng.dispose()

    schema = "dca_test"
    _drop(schema)
    yield schema
    _drop(schema)
    get_settings.cache_clear()


def _public_tables() -> set[str]:
    eng = create_engine(PG_URL)
    try:
        return set(inspect(eng).get_table_names(schema="public"))
    finally:
        eng.dispose()


@requires_pg
def test_upgrade_creates_tables_only_in_dca_schema(clean_schema):
    from alembic import command

    # Delta on `public` (other tests may already have dirtied it): the migration must add nothing.
    public_before = _public_tables()
    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    in_dca = set(insp.get_table_names(schema=clean_schema))
    public_after = set(insp.get_table_names(schema="public"))
    eng.dispose()

    assert DCA_TABLES.issubset(in_dca)  # all model tables created inside the DCA schema
    assert "alembic_version" in in_dca  # history table also inside DCA ownership
    assert public_after == public_before  # the migration leaked nothing into public/host


@requires_pg
def test_history_is_reproducible_on_empty_database(clean_schema):
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")  # back to empty

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    assert DCA_TABLES.isdisjoint(set(insp.get_table_names(schema=clean_schema)))
    eng.dispose()

    command.upgrade(cfg, "head")  # reproducible: upgrade from empty again succeeds
    eng = create_engine(PG_URL)
    insp = inspect(eng)
    assert DCA_TABLES.issubset(set(insp.get_table_names(schema=clean_schema)))
    eng.dispose()


@requires_pg
def test_0005_creates_audit_events_on_a_preexisting_database(clean_schema):
    """DCA-026 — the 0005 create branch (for a DB migrated before ``audit_events`` existed).

    A fresh DB gets ``audit_events`` from the metadata baseline (0001) and its RLS policy from 0002,
    so 0005 is a no-op there. To exercise 0005's real job, simulate an older DB: upgrade to head,
    drop the table + policy, stamp history back to 0004, then upgrade to head again — 0005 must
    recreate the table AND its tenant-isolation policy.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(text('DROP TABLE IF EXISTS "audit_events" CASCADE'))  # policy drops with it
    eng.dispose()

    command.stamp(cfg, "0004_tenant_data_mode")  # pretend we predate the audit table
    command.upgrade(cfg, "head")  # 0005 runs its create branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    assert "audit_events" in set(insp.get_table_names(schema=clean_schema))
    with eng.connect() as conn:
        policy = conn.execute(
            text(
                "SELECT 1 FROM pg_policies WHERE schemaname = :s "
                "AND tablename = 'audit_events' AND policyname = 'tenant_isolation'"
            ),
            {"s": clean_schema},
        ).first()
    eng.dispose()
    assert policy is not None  # RLS re-established, not just the bare table


@requires_pg
def test_0006_creates_outbox_messages_on_a_preexisting_database(clean_schema):
    """DCA-027 — the 0006 create branch (for a DB migrated before ``outbox_messages`` existed).

    Same shape as the 0005 test: a fresh DB gets the table from the metadata baseline (0001) and its
    RLS policy from 0002, so 0006 is a no-op there. To exercise 0006's real job, upgrade to head, drop
    the table + policy, stamp history back to 0005, then upgrade to head again — 0006 must recreate
    the table (with its unique intent constraint) AND its tenant-isolation policy.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(text('DROP TABLE IF EXISTS "outbox_messages" CASCADE'))  # policy drops with it
    eng.dispose()

    command.stamp(cfg, "0005_audit_events")  # pretend we predate the outbox table
    command.upgrade(cfg, "head")  # 0006 runs its create branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    assert "outbox_messages" in set(insp.get_table_names(schema=clean_schema))
    uniques = {
        u["name"] for u in insp.get_unique_constraints("outbox_messages", schema=clean_schema)
    }
    with eng.connect() as conn:
        policy = conn.execute(
            text(
                "SELECT 1 FROM pg_policies WHERE schemaname = :s "
                "AND tablename = 'outbox_messages' AND policyname = 'tenant_isolation'"
            ),
            {"s": clean_schema},
        ).first()
    eng.dispose()
    assert "uq_outbox_event" in uniques  # the duplicate-intent constraint travelled with the table
    assert policy is not None  # RLS re-established, not just the bare table


@requires_pg
def test_0007_creates_inbox_receipts_on_a_preexisting_database(clean_schema):
    """DCA-028 — the 0007 create branch (for a DB migrated before ``inbox_receipts`` existed).

    Same shape as the 0005/0006 tests: a fresh DB gets the table from the metadata baseline (0001) and
    its RLS policy from 0002, so 0007 is a no-op there. To exercise 0007's real job, upgrade to head,
    drop the table + policy, stamp history back to 0006, then upgrade to head again — 0007 must
    recreate the table (with its unique receipt constraint) AND its tenant-isolation policy.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(text('DROP TABLE IF EXISTS "inbox_receipts" CASCADE'))  # policy drops with it
    eng.dispose()

    command.stamp(cfg, "0006_outbox_messages")  # pretend we predate the inbox table
    command.upgrade(cfg, "head")  # 0007 runs its create branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    assert "inbox_receipts" in set(insp.get_table_names(schema=clean_schema))
    uniques = {
        u["name"] for u in insp.get_unique_constraints("inbox_receipts", schema=clean_schema)
    }
    with eng.connect() as conn:
        policy = conn.execute(
            text(
                "SELECT 1 FROM pg_policies WHERE schemaname = :s "
                "AND tablename = 'inbox_receipts' AND policyname = 'tenant_isolation'"
            ),
            {"s": clean_schema},
        ).first()
    eng.dispose()
    assert "uq_inbox_event" in uniques  # the replay-dedup constraint travelled with the table
    assert policy is not None  # RLS re-established, not just the bare table


@requires_pg
def test_0008_creates_revision_tables_on_a_preexisting_database(clean_schema):
    """DCA-031 — the 0008 create branch (for a DB migrated before the revision tables existed).

    Same shape as the 0005/0006/0007 tests: a fresh DB gets the tables from the metadata baseline
    (0001) and their RLS policies from 0002, so 0008 is a no-op there. To exercise 0008's real job,
    upgrade to head, drop both tables + policies, stamp history back to 0007, then upgrade to head
    again — 0008 must recreate both tables (with their unique constraints) AND their RLS policies.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(text('DROP TABLE IF EXISTS "revision_pending_events" CASCADE'))
        conn.execute(text('DROP TABLE IF EXISTS "revision_projections" CASCADE'))
    eng.dispose()

    command.stamp(cfg, "0007_inbox_receipts")  # pretend we predate the revision tables
    command.upgrade(cfg, "head")  # 0008 runs its create branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    tables = set(insp.get_table_names(schema=clean_schema))
    assert {"revision_projections", "revision_pending_events"}.issubset(tables)
    proj_uniques = {
        u["name"] for u in insp.get_unique_constraints("revision_projections", schema=clean_schema)
    }
    pending_uniques = {
        u["name"]
        for u in insp.get_unique_constraints("revision_pending_events", schema=clean_schema)
    }
    with eng.connect() as conn:
        policies = conn.execute(
            text(
                "SELECT tablename FROM pg_policies WHERE schemaname = :s "
                "AND tablename IN ('revision_projections', 'revision_pending_events') "
                "AND policyname = 'tenant_isolation'"
            ),
            {"s": clean_schema},
        ).fetchall()
    eng.dispose()
    assert "uq_revision_projection" in proj_uniques  # one projection per aggregate per tenant
    assert "uq_revision_pending" in pending_uniques  # a revision is buffered at most once
    assert {row[0] for row in policies} == {
        "revision_projections",
        "revision_pending_events",
    }  # RLS re-established on both, not just the bare tables


@requires_pg
def test_host_schema_setting_is_rejected_and_changes_nothing(clean_schema):
    from alembic import command

    public_before = _public_tables()
    cfg = _alembic_config("information_schema")  # a host-owned schema
    with pytest.raises(HostSchemaError):
        command.upgrade(cfg, "head")

    # Denial changed nothing: no new public tables, and the DCA schema was never created.
    eng = create_engine(PG_URL)
    insp = inspect(eng)
    assert set(insp.get_table_names(schema="public")) == public_before
    assert DCA_TABLES.isdisjoint(set(insp.get_table_names(schema=clean_schema)))
    eng.dispose()
