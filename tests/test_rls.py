"""DCA-018 — PostgreSQL row-level security on tenant-owned tables.

Behaviour is verified on a **disposable PostgreSQL** (``DCA_TEST_DATABASE_URL``); RLS cannot be
exercised on SQLite, so those tests skip when no Postgres URL is supplied. The unit tests that pin
*which* tables are protected (and which catalog tables are deliberately excluded) always run.

Spin up a throwaway Postgres, e.g.:
    docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55438:5432 postgres:16-alpine
    export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55438/dca"
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.db.rls import APP_ROLE, RLS_GUC, tenant_owned_tables

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SCHEMA = "dca_rls_test"
LOGIN = "dca_app_test_login"
LOGIN_PW = "rls_test_pw"  # disposable, local-only test role password
A = "00000000-0000-4000-8000-00000000000a"
B = "00000000-0000-4000-8000-00000000000b"


# --- always-on: which tables RLS covers (no database) ---------------------------------------------


def test_rls_covers_exactly_the_tenant_owned_tables():
    assert set(tenant_owned_tables()) == {
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
    }


def test_platform_catalog_tables_are_excluded():
    # The platform-owned registration catalog is deliberately outside tenant RLS.
    assert "tenant_agents" not in tenant_owned_tables()
    assert "icp_profiles" not in tenant_owned_tables()


# --- Postgres-backed RLS behaviour ----------------------------------------------------------------


def _dsn(user: str, password: str) -> str:
    u = make_url(PG_URL)
    return f"postgresql://{user}:{password}@{u.host}:{u.port}/{u.database}"


def _su_dsn() -> str:
    u = make_url(PG_URL)
    return f"postgresql://{u.username}:{u.password}@{u.host}:{u.port}/{u.database}"


def _alembic_upgrade_head(schema: str) -> None:
    from alembic import command
    from alembic.config import Config

    os.environ["EFFORTLESSPROMO_DCA_ENVIRONMENT"] = "testing"
    os.environ["EFFORTLESSPROMO_DCA_DATABASE_URL"] = PG_URL  # type: ignore[assignment]
    os.environ["EFFORTLESSPROMO_DCA_DB_SCHEMA"] = schema
    get_settings.cache_clear()
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    command.upgrade(cfg, "head")


_SEED = (
    "INSERT INTO discovery_runs "
    "(id, business_id, status, source_keys, found, screened_out, suggested, started_at) "
    "VALUES (gen_random_uuid()::text, %s, %s, '[]', 0, 0, 0, now())"
)


@pytest.fixture(scope="module")
def rls_db():
    """Fresh schema at head, two businesses seeded (as superuser, bypassing RLS), and a non-super,
    non-bypass LOGIN role that is a member of the app role — i.e. how the service connects."""
    su = psycopg.connect(_su_dsn(), autocommit=True)
    su.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
    su.close()

    _alembic_upgrade_head(SCHEMA)

    su = psycopg.connect(_su_dsn(), autocommit=True)
    su.execute(f'SET search_path TO "{SCHEMA}"')
    for biz, status in [(A, "running"), (A, "completed"), (B, "running")]:
        su.execute(_SEED, (biz, status))
    su.execute(f"DROP ROLE IF EXISTS {LOGIN}")
    su.execute(
        f"CREATE ROLE {LOGIN} LOGIN PASSWORD '{LOGIN_PW}' "
        f"NOSUPERUSER NOBYPASSRLS IN ROLE {APP_ROLE}"
    )
    # Session-default search_path for the role (applied at login, NOT transactional — so a ROLLBACK
    # in a test never strips it, unlike an in-transaction ``SET search_path``).
    su.execute(f'ALTER ROLE {LOGIN} SET search_path TO "{SCHEMA}"')
    su.close()

    yield

    su = psycopg.connect(_su_dsn(), autocommit=True)
    su.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
    su.execute(f"DROP ROLE IF EXISTS {LOGIN}")
    su.close()
    get_settings.cache_clear()


def _app_conn():
    # search_path comes from the role default set in the fixture, so it survives ROLLBACK.
    return psycopg.connect(_dsn(LOGIN, LOGIN_PW))


def _counts(conn) -> dict[str, int]:
    rows = conn.execute(
        "SELECT business_id, count(*) FROM discovery_runs GROUP BY business_id"
    ).fetchall()
    return {bid: n for bid, n in rows}


@requires_pg
def test_app_role_is_non_bypass(rls_db):
    su = psycopg.connect(_su_dsn(), autocommit=True)
    try:
        for role in (APP_ROLE, LOGIN):
            flags = su.execute(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = %s", (role,)
            ).fetchone()
            assert flags == (False, False), f"{role} must be non-super, non-bypass"
    finally:
        su.close()


@requires_pg
def test_scoped_query_sees_only_its_tenant(rls_db):
    conn = _app_conn()
    try:
        conn.execute("SELECT set_config(%s, %s, true)", (RLS_GUC, A))
        assert _counts(conn) == {A: 2}
        conn.rollback()
        conn.execute("SELECT set_config(%s, %s, true)", (RLS_GUC, B))
        assert _counts(conn) == {B: 1}
    finally:
        conn.close()


@requires_pg
def test_unscoped_query_reads_nothing(rls_db):
    # No tenant GUC set => policy predicate is NULL => zero rows (fail-closed), never all tenants.
    conn = _app_conn()
    try:
        assert _counts(conn) == {}
    finally:
        conn.close()


@requires_pg
def test_pooled_connection_does_not_retain_previous_tenant(rls_db):
    # One physical connection, three successive transactions — the tx-local setting never leaks.
    conn = _app_conn()
    try:
        conn.execute("SELECT set_config(%s, %s, true)", (RLS_GUC, A))
        assert _counts(conn) == {A: 2}
        conn.commit()  # ends the transaction; SET LOCAL/set_config(...,true) is discarded here

        assert _counts(conn) == {}  # next tx on the SAME connection carries no scope
        conn.commit()

        conn.execute("SELECT set_config(%s, %s, true)", (RLS_GUC, B))
        assert _counts(conn) == {B: 1}
    finally:
        conn.close()


@requires_pg
def test_write_check_rejects_foreign_owner(rls_db):
    conn = _app_conn()
    try:
        conn.execute("SELECT set_config(%s, %s, true)", (RLS_GUC, A))
        # A row whose business_id is B, written while scoped to A, violates WITH CHECK.
        with pytest.raises(InsufficientPrivilege):
            conn.execute(_SEED, (B, "running"))
        conn.rollback()
    finally:
        conn.close()


@requires_pg
def test_write_with_matching_owner_is_allowed(rls_db):
    conn = _app_conn()
    try:
        conn.execute("SELECT set_config(%s, %s, true)", (RLS_GUC, A))
        conn.execute(_SEED, (A, "running"))
        assert _counts(conn) == {A: 3}  # the new A row is visible within the A scope
        conn.rollback()  # don't pollute the shared seed for other tests
    finally:
        conn.close()


@requires_pg
def test_platform_catalog_is_separate_from_the_app_role(rls_db):
    # The registration catalog is not granted to the app role: it is reached over a different
    # (service-auth / owner) path, not the tenant data plane.
    conn = _app_conn()
    try:
        with pytest.raises(InsufficientPrivilege):
            conn.execute("SELECT count(*) FROM tenant_agents").fetchone()
        conn.rollback()
    finally:
        conn.close()
