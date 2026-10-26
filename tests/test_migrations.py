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
