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
    "idempotency_records",
    "business_profile_drafts",
    "icp_drafts",
    "icp_confirmations",
    "budget_limits",
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
def test_0009_creates_idempotency_records_on_a_preexisting_database(clean_schema):
    """DCA-035 — the 0009 create branch (for a DB migrated before ``idempotency_records`` existed).

    Same shape as the 0005/0006/0007/0008 tests: a fresh DB gets the table from the metadata baseline
    (0001) and its RLS policy from 0002, so 0009 is a no-op there. To exercise 0009's real job, upgrade
    to head, drop the table + policy, stamp history back to 0008, then upgrade to head again — 0009
    must recreate the table (with its unique key constraint) AND its tenant-isolation policy.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(text('DROP TABLE IF EXISTS "idempotency_records" CASCADE'))  # policy drops too
    eng.dispose()

    command.stamp(cfg, "0008_revision_projections")  # pretend we predate the idempotency table
    command.upgrade(cfg, "head")  # 0009 runs its create branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    assert "idempotency_records" in set(insp.get_table_names(schema=clean_schema))
    uniques = {
        u["name"] for u in insp.get_unique_constraints("idempotency_records", schema=clean_schema)
    }
    with eng.connect() as conn:
        policy = conn.execute(
            text(
                "SELECT 1 FROM pg_policies WHERE schemaname = :s "
                "AND tablename = 'idempotency_records' AND policyname = 'tenant_isolation'"
            ),
            {"s": clean_schema},
        ).first()
    eng.dispose()
    assert "uq_idempotency_key" in uniques  # the per-tenant/route key-reuse constraint travelled
    assert policy is not None  # RLS re-established, not just the bare table


@requires_pg
def test_0010_creates_business_profile_drafts_on_a_preexisting_database(clean_schema):
    """DCA-036 — the 0010 create branch (for a DB migrated before ``business_profile_drafts`` existed).

    Same shape as the 0005..0009 tests: a fresh DB gets the table from the metadata baseline (0001) and
    its RLS policy from 0002, so 0010 is a no-op there. To exercise 0010's real job, upgrade to head,
    drop the table + policy, stamp history back to 0009, then upgrade to head again — 0010 must recreate
    the table (with its per-tenant version constraint) AND its tenant-isolation policy.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(
            text('DROP TABLE IF EXISTS "business_profile_drafts" CASCADE')
        )  # policy drops too
    eng.dispose()

    command.stamp(cfg, "0009_idempotency_records")  # pretend we predate the profile table
    command.upgrade(cfg, "head")  # 0010 runs its create branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    assert "business_profile_drafts" in set(insp.get_table_names(schema=clean_schema))
    uniques = {
        u["name"]
        for u in insp.get_unique_constraints("business_profile_drafts", schema=clean_schema)
    }
    with eng.connect() as conn:
        policy = conn.execute(
            text(
                "SELECT 1 FROM pg_policies WHERE schemaname = :s "
                "AND tablename = 'business_profile_drafts' AND policyname = 'tenant_isolation'"
            ),
            {"s": clean_schema},
        ).first()
    eng.dispose()
    assert (
        "uq_profile_draft_version" in uniques
    )  # one version per tenant; a version never overwrites
    assert policy is not None  # RLS re-established, not just the bare table


@requires_pg
def test_0011_adds_provenance_columns_on_a_preexisting_database(clean_schema):
    """DCA-037 — the 0011 add-column branch (for a DB migrated before the provenance columns existed).

    A fresh DB gets ``business_profile_drafts`` WITH ``source``/``source_version``/``provenance`` from
    the metadata baseline (0001), so 0011 is a no-op there. To exercise 0011's real job, upgrade to
    head, DROP the three columns, stamp history back to 0010, then upgrade to head again — 0011 must
    re-add exactly those columns (backfilling existing rows), with no new table and nothing leaked.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        # Seed one pre-existing (pre-provenance) draft row to prove the backfill.
        conn.execute(
            text(
                'ALTER TABLE "business_profile_drafts" DROP COLUMN "provenance", '
                'DROP COLUMN "source_version", DROP COLUMN "source"'
            )
        )
        conn.execute(
            text(
                "INSERT INTO business_profile_drafts "
                "(id, business_id, version, products, business_type, value_proposition, "
                "languages, confirmed_facts, unknowns, status, created_at) VALUES "
                "('row-1', 'biz-legacy', 1, '[]', '', '', '[]', '[]', '[]', 'draft', now())"
            )
        )
    eng.dispose()

    command.stamp(cfg, "0010_business_profile_drafts")  # pretend we predate the provenance columns
    command.upgrade(cfg, "head")  # 0011 runs its add-column branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    cols = {c["name"] for c in insp.get_columns("business_profile_drafts", schema=clean_schema)}
    with eng.connect() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        backfilled = conn.execute(
            text("SELECT source, source_version FROM business_profile_drafts WHERE id = 'row-1'")
        ).first()
    eng.dispose()
    assert {"source", "source_version", "provenance"}.issubset(cols)  # columns re-added
    assert backfilled == ("", "")  # the pre-existing row backfilled to "not imported"


@requires_pg
def test_0012_creates_icp_drafts_on_a_preexisting_database(clean_schema):
    """DCA-039 — the 0012 create branch (for a DB migrated before ``icp_drafts`` existed).

    Same shape as the 0005..0010 tests: a fresh DB gets the table from the metadata baseline (0001) and
    its RLS policy from 0002, so 0012 is a no-op there. To exercise 0012's real job, upgrade to head,
    drop the table + policy, stamp history back to 0011, then upgrade to head again — 0012 must recreate
    the table (with its per-tenant version constraint) AND its tenant-isolation policy.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(text('DROP TABLE IF EXISTS "icp_drafts" CASCADE'))  # policy drops too
    eng.dispose()

    command.stamp(cfg, "0011_profile_import_provenance")  # pretend we predate the ICP draft table
    command.upgrade(cfg, "head")  # 0012 runs its create branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    assert "icp_drafts" in set(insp.get_table_names(schema=clean_schema))
    uniques = {u["name"] for u in insp.get_unique_constraints("icp_drafts", schema=clean_schema)}
    with eng.connect() as conn:
        policy = conn.execute(
            text(
                "SELECT 1 FROM pg_policies WHERE schemaname = :s "
                "AND tablename = 'icp_drafts' AND policyname = 'tenant_isolation'"
            ),
            {"s": clean_schema},
        ).first()
    eng.dispose()
    assert "uq_icp_draft_version" in uniques  # one version per tenant; a version never overwrites
    assert policy is not None  # RLS re-established, not just the bare table


@requires_pg
def test_0013_creates_icp_confirmations_on_a_preexisting_database(clean_schema):
    """DCA-040 — the 0013 create branch (for a DB migrated before ``icp_confirmations`` existed).

    Same shape as the 0005..0012 tests: a fresh DB gets the table from the metadata baseline (0001) and
    its RLS policy from 0002, so 0013 is a no-op there. To exercise 0013's real job, upgrade to head,
    drop the table + policy, stamp history back to 0012, then upgrade to head again — 0013 must recreate
    the table (with its one-active-per-business constraint) AND its tenant-isolation policy.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(text('DROP TABLE IF EXISTS "icp_confirmations" CASCADE'))  # policy drops too
    eng.dispose()

    command.stamp(cfg, "0012_icp_drafts")  # pretend we predate the ICP confirmation table
    command.upgrade(cfg, "head")  # 0013 runs its create branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    assert "icp_confirmations" in set(insp.get_table_names(schema=clean_schema))
    uniques = {
        u["name"] for u in insp.get_unique_constraints("icp_confirmations", schema=clean_schema)
    }
    with eng.connect() as conn:
        policy = conn.execute(
            text(
                "SELECT 1 FROM pg_policies WHERE schemaname = :s "
                "AND tablename = 'icp_confirmations' AND policyname = 'tenant_isolation'"
            ),
            {"s": clean_schema},
        ).first()
    eng.dispose()
    assert "uq_icp_confirmation_business" in uniques  # one active ICP version per tenant
    assert policy is not None  # RLS re-established, not just the bare table


@requires_pg
def test_0014_creates_budget_limits_on_a_preexisting_database(clean_schema):
    """DCA-042 — the 0014 create branch (for a DB migrated before ``budget_limits`` existed).

    Same shape as the 0005..0013 tests: a fresh DB gets the table from the metadata baseline (0001) and
    its RLS policy from 0002, so 0014 is a no-op there. To exercise 0014's real job, upgrade to head,
    drop the table + policy, stamp history back to 0013, then upgrade to head again — 0014 must recreate
    the table (with its per-tenant version constraint) AND its tenant-isolation policy.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(text('DROP TABLE IF EXISTS "budget_limits" CASCADE'))  # policy drops too
    eng.dispose()

    command.stamp(cfg, "0013_icp_confirmations")  # pretend we predate the budget-limit table
    command.upgrade(cfg, "head")  # 0014 runs its create branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    assert "budget_limits" in set(insp.get_table_names(schema=clean_schema))
    uniques = {u["name"] for u in insp.get_unique_constraints("budget_limits", schema=clean_schema)}
    with eng.connect() as conn:
        policy = conn.execute(
            text(
                "SELECT 1 FROM pg_policies WHERE schemaname = :s "
                "AND tablename = 'budget_limits' AND policyname = 'tenant_isolation'"
            ),
            {"s": clean_schema},
        ).first()
    eng.dispose()
    assert (
        "uq_budget_limit_version" in uniques
    )  # one version per tenant; a version never overwrites
    assert policy is not None  # RLS re-established, not just the bare table


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
