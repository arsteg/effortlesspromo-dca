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
        "business_profile_drafts",
        "icp_drafts",
        "icp_confirmations",
        "budget_limits",
        "business_source_entitlements",
        "policy_decision_records",
        "org_observations",
        "org_external_ids",
        "person_observations",
        "contact_points",
        "field_observations",
        "evidence_objects",
        "field_corrections",
        "consent_receipts",
        "processing_restrictions",
        "restriction_propagations",
        "privacy_grievances",
        "data_erasure_journals",
        "external_deletion_propagations",
    }


def test_platform_catalog_tables_are_excluded():
    # The platform-owned registration catalog is deliberately outside tenant RLS.
    assert "tenant_agents" not in tenant_owned_tables()
    assert "icp_profiles" not in tenant_owned_tables()
    # The DCA-044 source catalog is platform control-plane data, also outside tenant RLS.
    assert "sources" not in tenant_owned_tables()
    # The DCA-045 source terms registry is platform control-plane data, also outside tenant RLS.
    assert "source_terms" not in tenant_owned_tables()
    # The DCA-046 source-approval registry is platform control-plane data, also outside tenant RLS.
    assert "source_term_approvals" not in tenant_owned_tables()
    # The DCA-052 compliance policy rulepacks are platform control-plane data, also outside tenant RLS.
    assert "compliance_policy_rulepacks" not in tenant_owned_tables()
    # The DCA-053 compliance policy approvals are platform control-plane data, also outside tenant RLS.
    assert "compliance_policy_approvals" not in tenant_owned_tables()
    # The DCA-078 platform-wide source removals are platform control-plane data, also outside tenant RLS.
    assert "platform_source_restrictions" not in tenant_owned_tables()


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


_SEED_PROPAGATION = (
    "INSERT INTO restriction_propagations "
    "(id, business_id, restriction_id, restriction_version, authority_plane, source_key, "
    "scope_brand, scope_channel, scope_purpose, revoked_count, revoked_candidate_ids, "
    "acknowledged_by, acknowledged_by_type, created_at) "
    "VALUES (gen_random_uuid()::text, %s, 'rstr-1', 1, 'platform', 'src_x', '', '', '', "
    "1, '[]', 'op-1', 'user', now())"
)


@requires_pg
def test_restriction_propagation_is_tenant_isolated(rls_db):
    """DCA-081 — a propagation acknowledgment is tenant-owned: a platform-wide fanout writes one row per
    tenant, and each tenant can read only its OWN acknowledgments (RLS, exercised on real Postgres)."""
    conn = _app_conn()
    try:
        # A platform-wide source removal acknowledged for BOTH tenants (one row each), written under each
        # tenant's own scope — exactly how the fanout stages a per-tenant acknowledgment.
        conn.execute("SELECT set_config(%s, %s, true)", (RLS_GUC, A))
        conn.execute(_SEED_PROPAGATION, (A,))
        conn.commit()
        conn.execute("SELECT set_config(%s, %s, true)", (RLS_GUC, B))
        conn.execute(_SEED_PROPAGATION, (B,))
        conn.commit()

        # Each tenant sees ONLY its own acknowledgment row.
        conn.execute("SELECT set_config(%s, %s, true)", (RLS_GUC, A))
        a_rows = conn.execute("SELECT business_id FROM restriction_propagations").fetchall()
        assert a_rows == [(A,)]
        conn.commit()
        conn.execute("SELECT set_config(%s, %s, true)", (RLS_GUC, B))
        b_rows = conn.execute("SELECT business_id FROM restriction_propagations").fetchall()
        assert b_rows == [(B,)]
        conn.commit()
    finally:
        conn.close()


@requires_pg
def test_restriction_propagation_write_check_rejects_foreign_owner(rls_db):
    """An acknowledgment can never be stamped for another tenant: writing a B-owned row while scoped to
    A violates the WITH CHECK clause (defence-in-depth beneath the fanout's own per-row business_id)."""
    conn = _app_conn()
    try:
        conn.execute("SELECT set_config(%s, %s, true)", (RLS_GUC, A))
        with pytest.raises(InsufficientPrivilege):
            conn.execute(_SEED_PROPAGATION, (B,))
        conn.rollback()
    finally:
        conn.close()


_SEED_GRIEVANCE = (
    "INSERT INTO privacy_grievances "
    "(id, business_id, category, channel, requester_contact_digest, detail, "
    "verification_level_required, verification_state, status, response_deadline_days, "
    "respond_by, access_token_hash, created_at) "
    "VALUES (gen_random_uuid()::text, %s, 'access', 'email', 'digest-x', '', "
    "'elevated', 'unverified', 'received', 30, now() + interval '30 days', 'hash-x', now())"
)


@requires_pg
def test_privacy_grievance_is_tenant_isolated(rls_db):
    """DCA-082 — a grievance is tenant-owned: each tenant can read only its OWN grievances (RLS on
    real Postgres), so one business can never enumerate another's privacy grievances."""
    conn = _app_conn()
    try:
        conn.execute("SELECT set_config(%s, %s, true)", (RLS_GUC, A))
        conn.execute(_SEED_GRIEVANCE, (A,))
        conn.commit()
        conn.execute("SELECT set_config(%s, %s, true)", (RLS_GUC, B))
        conn.execute(_SEED_GRIEVANCE, (B,))
        conn.commit()

        conn.execute("SELECT set_config(%s, %s, true)", (RLS_GUC, A))
        a_rows = conn.execute("SELECT business_id FROM privacy_grievances").fetchall()
        assert a_rows == [(A,)]
        conn.commit()
        conn.execute("SELECT set_config(%s, %s, true)", (RLS_GUC, B))
        b_rows = conn.execute("SELECT business_id FROM privacy_grievances").fetchall()
        assert b_rows == [(B,)]
        conn.commit()
    finally:
        conn.close()


@requires_pg
def test_privacy_grievance_write_check_rejects_foreign_owner(rls_db):
    """A grievance can never be stamped for another tenant: writing a B-owned row while scoped to A
    violates the WITH CHECK clause (defence-in-depth beneath the store's own per-row business_id)."""
    conn = _app_conn()
    try:
        conn.execute("SELECT set_config(%s, %s, true)", (RLS_GUC, A))
        with pytest.raises(InsufficientPrivilege):
            conn.execute(_SEED_GRIEVANCE, (B,))
        conn.rollback()
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
