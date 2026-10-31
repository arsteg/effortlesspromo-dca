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
    "sources",
    "source_terms",
    "source_term_approvals",
    "business_source_entitlements",
    "compliance_policy_rulepacks",
    "compliance_policy_approvals",
    "org_observations",
    "org_external_ids",
    "org_observation_imports",
    "person_observations",
    "contact_points",
    "field_observations",
    "evidence_objects",
    "field_corrections",
    "consent_receipts",
    "processing_restrictions",
    "platform_source_restrictions",
    "restriction_propagations",
    "privacy_grievances",
    "data_erasure_journals",
    "external_deletion_propagations",
    "restricted_identity_tombstones",
    "dispatch_recheck_consumptions",
    "durable_job_requests",
    "identity_merges",
    "recurring_schedules",
    "business_emergency_stops",
    "platform_emergency_stops",
    "budget_reservations",
    "provider_call_records",
    "host_crawl_state",
    "website_fact_extractions",
    "licensed_provider_fetches",
    "contact_imports",
    "source_result_cache",
    "discovery_plans",
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
def test_0015_adds_score_staleness_columns_on_a_preexisting_database(clean_schema):
    """DCA-043 — the 0015 add-column branch (for a DB migrated before the score-staleness columns).

    A fresh DB gets ``prospect_candidates`` WITH ``score_status``/``scored_targeting_version`` from the
    metadata baseline (0001), so 0015 is a no-op there. To exercise 0015's real job, upgrade to head,
    DROP the two columns, seed one pre-existing row, stamp history back to 0014, then upgrade to head
    again — 0015 must re-add exactly those columns and backfill the existing row's ``score_status`` to
    ``current`` (no new table, nothing leaked).
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(
            text(
                'ALTER TABLE "prospect_candidates" DROP COLUMN "scored_targeting_version", '
                'DROP COLUMN "score_status"'
            )
        )
        # Seed one pre-existing (pre-staleness) candidate row to prove the backfill. The parent
        # discovery_run is seeded first to satisfy the run_id foreign key.
        conn.execute(
            text(
                "INSERT INTO discovery_runs "
                "(id, business_id, status, source_keys, found, screened_out, suggested, started_at) "
                "VALUES ('run-legacy', 'biz-legacy', 'completed', '[]', 1, 0, 1, now())"
            )
        )
        conn.execute(
            text(
                "INSERT INTO prospect_candidates "
                "(id, business_id, run_id, org_name, org_identifier, contact, source_key, "
                "source_ref, trust_tier, match_score, rationale, status, created_at) VALUES "
                "('cand-legacy', 'biz-legacy', 'run-legacy', 'Legacy Co', 'ID-LEGACY', '{}', "
                "'synthetic.mca', 'synthetic://x', 'official_registry', 1.0, '', 'suggested', now())"
            )
        )
    eng.dispose()

    command.stamp(cfg, "0014_budget_limits")  # pretend we predate the score-staleness columns
    command.upgrade(cfg, "head")  # 0015 runs its add-column branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    cols = {c["name"] for c in insp.get_columns("prospect_candidates", schema=clean_schema)}
    with eng.connect() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        backfilled = conn.execute(
            text(
                "SELECT score_status, scored_targeting_version FROM prospect_candidates "
                "WHERE id = 'cand-legacy'"
            )
        ).first()
    eng.dispose()
    assert {"score_status", "scored_targeting_version"}.issubset(cols)  # columns re-added
    assert backfilled == (
        "current",
        None,
    )  # pre-existing row backfilled to a current, unversioned score


@requires_pg
def test_0016_creates_sources_on_a_preexisting_database(clean_schema):
    """DCA-044 — the 0016 create branch (for a DB migrated before ``sources`` existed).

    ``sources`` is platform control-plane data (NOT tenant-owned), so — unlike 0005..0014 — it has NO
    RLS policy; it is created like the 0003 entitlement projection. A fresh DB gets the table from the
    metadata baseline (0001), so 0016 is a no-op there. To exercise 0016's real job, upgrade to head,
    drop the table, stamp history back to 0015, then upgrade to head again — 0016 must recreate the
    table (with its unique catalog-key constraint) and add NO tenant-isolation policy.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(text('DROP TABLE IF EXISTS "sources" CASCADE'))
    eng.dispose()

    command.stamp(cfg, "0015_prospect_score_staleness")  # pretend we predate the source catalog
    command.upgrade(cfg, "head")  # 0016 runs its create branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    assert "sources" in set(insp.get_table_names(schema=clean_schema))
    uniques = {u["name"] for u in insp.get_unique_constraints("sources", schema=clean_schema)}
    with eng.connect() as conn:
        policy = conn.execute(
            text(
                "SELECT 1 FROM pg_policies WHERE schemaname = :s "
                "AND tablename = 'sources' AND policyname = 'tenant_isolation'"
            ),
            {"s": clean_schema},
        ).first()
    eng.dispose()
    assert "uq_source_key" in uniques  # the globally-unique catalog-key constraint travelled
    assert (
        policy is None
    )  # control-plane catalog: no tenant RLS policy (unlike tenant-owned tables)


@requires_pg
def test_0017_creates_source_terms_on_a_preexisting_database(clean_schema):
    """DCA-045 — the 0017 create branch (for a DB migrated before ``source_terms`` existed).

    ``source_terms`` is platform control-plane data (NOT tenant-owned), like ``sources`` (0016), so it
    has NO RLS policy. A fresh DB gets the table from the metadata baseline (0001), so 0017 is a no-op
    there. To exercise 0017's real job, upgrade to head, drop the table, stamp history back to 0016,
    then upgrade to head again — 0017 must recreate the table (with its unique (source_key, version)
    constraint) and add NO tenant-isolation policy.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(text('DROP TABLE IF EXISTS "source_terms" CASCADE'))
    eng.dispose()

    command.stamp(cfg, "0016_sources")  # pretend we predate the source-terms table
    command.upgrade(cfg, "head")  # 0017 runs its create branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    assert "source_terms" in set(insp.get_table_names(schema=clean_schema))
    uniques = {u["name"] for u in insp.get_unique_constraints("source_terms", schema=clean_schema)}
    with eng.connect() as conn:
        policy = conn.execute(
            text(
                "SELECT 1 FROM pg_policies WHERE schemaname = :s "
                "AND tablename = 'source_terms' AND policyname = 'tenant_isolation'"
            ),
            {"s": clean_schema},
        ).first()
    eng.dispose()
    assert (
        "uq_source_terms_version" in uniques
    )  # one version per source; a version never overwrites
    assert (
        policy is None
    )  # control-plane catalog: no tenant RLS policy (unlike tenant-owned tables)


@requires_pg
def test_0018_creates_source_term_approvals_on_a_preexisting_database(clean_schema):
    """DCA-046 — the 0018 create branch (for a DB migrated before ``source_term_approvals`` existed).

    ``source_term_approvals`` is platform control-plane data (NOT tenant-owned), like ``sources`` (0016)
    and ``source_terms`` (0017), so it has NO RLS policy. A fresh DB gets the table from the metadata
    baseline (0001), so 0018 is a no-op there. To exercise 0018's real job, upgrade to head, drop the
    table, stamp history back to 0017, then upgrade to head again — 0018 must recreate the table (with
    its unique (source_key, terms_version) constraint) and add NO tenant-isolation policy.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(text('DROP TABLE IF EXISTS "source_term_approvals" CASCADE'))
    eng.dispose()

    command.stamp(cfg, "0017_source_terms")  # pretend we predate the source-approval table
    command.upgrade(cfg, "head")  # 0018 runs its create branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    assert "source_term_approvals" in set(insp.get_table_names(schema=clean_schema))
    uniques = {
        u["name"] for u in insp.get_unique_constraints("source_term_approvals", schema=clean_schema)
    }
    with eng.connect() as conn:
        policy = conn.execute(
            text(
                "SELECT 1 FROM pg_policies WHERE schemaname = :s "
                "AND tablename = 'source_term_approvals' AND policyname = 'tenant_isolation'"
            ),
            {"s": clean_schema},
        ).first()
    eng.dispose()
    assert (
        "uq_source_term_approval_version" in uniques
    )  # one approval per terms version; never reused
    assert (
        policy is None
    )  # control-plane catalog: no tenant RLS policy (unlike tenant-owned tables)


@requires_pg
def test_0019_creates_business_source_entitlements_on_a_preexisting_database(clean_schema):
    """DCA-047 — the 0019 create branch (for a DB migrated before ``business_source_entitlements``).

    ``business_source_entitlements`` is tenant-owned (RLS-protected), like ``budget_limits`` (0014) —
    unlike the control-plane source tables (0016..0018). A fresh DB gets the table from the metadata
    baseline (0001) and its RLS policy from 0002, so 0019 is a no-op there. To exercise 0019's real
    job, upgrade to head, drop the table + policy, stamp history back to 0018, then upgrade to head
    again — 0019 must recreate the table (with its per-tenant (source_key, version) constraint) AND its
    tenant-isolation policy.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(
            text('DROP TABLE IF EXISTS "business_source_entitlements" CASCADE')
        )  # policy drops too
    eng.dispose()

    command.stamp(cfg, "0018_source_term_approvals")  # pretend we predate the entitlement table
    command.upgrade(cfg, "head")  # 0019 runs its create branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    assert "business_source_entitlements" in set(insp.get_table_names(schema=clean_schema))
    uniques = {
        u["name"]
        for u in insp.get_unique_constraints("business_source_entitlements", schema=clean_schema)
    }
    with eng.connect() as conn:
        policy = conn.execute(
            text(
                "SELECT 1 FROM pg_policies WHERE schemaname = :s "
                "AND tablename = 'business_source_entitlements' AND policyname = 'tenant_isolation'"
            ),
            {"s": clean_schema},
        ).first()
    eng.dispose()
    assert (
        "uq_business_source_entitlement_version" in uniques
    )  # one version per (tenant, source); a version never overwrites
    assert policy is not None  # RLS re-established, not just the bare table


@requires_pg
def test_0020_adds_revocation_columns_on_a_preexisting_database(clean_schema):
    """DCA-050 — the 0020 add-column branch (for a DB migrated before the revocation columns existed).

    A fresh DB gets ``source_term_approvals`` WITH ``revoked_at``/``revoked_by``/``revocation_reason``
    from the metadata baseline (0001), so 0020 is a no-op there. To exercise 0020's real job, upgrade to
    head, DROP the three columns, seed one pre-existing approval row, stamp history back to 0019, then
    upgrade to head again — 0020 must re-add exactly those columns (backfilling the existing row to NULL)
    with no new table and no RLS policy (control-plane catalog, like 0016..0018).
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(
            text(
                'ALTER TABLE "source_term_approvals" DROP COLUMN "revocation_reason", '
                'DROP COLUMN "revoked_by", DROP COLUMN "revoked_at"'
            )
        )
        # Seed one pre-existing (pre-revocation) approval row to prove the backfill.
        conn.execute(
            text(
                "INSERT INTO source_term_approvals "
                "(id, source_key, terms_version, reviewer_id, reviewer_type, review_evidence, "
                "reviewed_at, expires_at, approved_operation_rights, approved_field_rights, status, "
                "created_at) VALUES "
                "('appr-legacy', 'legacy_source', 1, 'op-legacy', 'platform_operator', '{}', now(), "
                "now(), '{}', '{}', 'active', now())"
            )
        )
    eng.dispose()

    command.stamp(cfg, "0019_business_entitlements")  # pretend we predate the revocation columns
    command.upgrade(cfg, "head")  # 0020 runs its add-column branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    cols = {c["name"] for c in insp.get_columns("source_term_approvals", schema=clean_schema)}
    with eng.connect() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        backfilled = conn.execute(
            text(
                "SELECT revoked_at, revoked_by, revocation_reason FROM source_term_approvals "
                "WHERE id = 'appr-legacy'"
            )
        ).first()
        policy = conn.execute(
            text(
                "SELECT 1 FROM pg_policies WHERE schemaname = :s "
                "AND tablename = 'source_term_approvals' AND policyname = 'tenant_isolation'"
            ),
            {"s": clean_schema},
        ).first()
    eng.dispose()
    assert {"revoked_at", "revoked_by", "revocation_reason"}.issubset(cols)  # columns re-added
    assert backfilled == (None, None, None)  # pre-existing row backfilled to "not revoked"
    assert policy is None  # control-plane catalog: still no tenant RLS policy


@requires_pg
def test_0022_creates_compliance_policy_approvals_on_a_preexisting_database(clean_schema):
    """DCA-053 — the 0022 create branch (for a DB migrated before ``compliance_policy_approvals``).

    ``compliance_policy_approvals`` is platform control-plane data (NOT tenant-owned), like the source
    registries (0016..0018) and the policy rulepacks (0021), so it has NO RLS policy. A fresh DB gets
    the table from the metadata baseline (0001), so 0022 is a no-op there. To exercise 0022's real job,
    upgrade to head, drop the table, stamp history back to 0021, then upgrade to head again — 0022 must
    recreate the table (with its unique (jurisdiction, policy_version) constraint) and add NO
    tenant-isolation policy.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(text('DROP TABLE IF EXISTS "compliance_policy_approvals" CASCADE'))
    eng.dispose()

    command.stamp(cfg, "0021_compliance_policy_rulepacks")  # pretend we predate the approval table
    command.upgrade(cfg, "head")  # 0022 runs its create branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    assert "compliance_policy_approvals" in set(insp.get_table_names(schema=clean_schema))
    uniques = {
        u["name"]
        for u in insp.get_unique_constraints("compliance_policy_approvals", schema=clean_schema)
    }
    with eng.connect() as conn:
        policy = conn.execute(
            text(
                "SELECT 1 FROM pg_policies WHERE schemaname = :s "
                "AND tablename = 'compliance_policy_approvals' AND policyname = 'tenant_isolation'"
            ),
            {"s": clean_schema},
        ).first()
    eng.dispose()
    assert "uq_policy_approval_version" in uniques  # one approval per policy version; never reused
    assert policy is None  # control-plane registry: no tenant RLS policy


@requires_pg
def test_0024_creates_org_observation_tables_on_a_preexisting_database(clean_schema):
    """DCA-059 — the 0024 create branch (for a DB migrated before the org-observation tables existed).

    ``org_observations`` and ``org_external_ids`` are tenant-owned, so a fresh DB gets them from the
    metadata baseline (0001) with RLS from 0002, and 0024 is a no-op there. To exercise 0024's real
    job, upgrade to head, drop both tables, stamp history back to 0023, then upgrade to head again —
    0024 must recreate both tables (with the per-business unique external-id constraint) and add their
    tenant-isolation RLS policies.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(text('DROP TABLE IF EXISTS "org_external_ids" CASCADE'))
        conn.execute(text('DROP TABLE IF EXISTS "org_observations" CASCADE'))
    eng.dispose()

    command.stamp(cfg, "0023_policy_decision_records")  # pretend we predate the org tables
    command.upgrade(cfg, "head")  # 0024 runs its create branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    tables = set(insp.get_table_names(schema=clean_schema))
    assert {"org_observations", "org_external_ids"}.issubset(tables)
    ext_uniques = {
        u["name"] for u in insp.get_unique_constraints("org_external_ids", schema=clean_schema)
    }
    with eng.connect() as conn:
        policies = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT tablename FROM pg_policies WHERE schemaname = :s "
                    "AND tablename IN ('org_observations', 'org_external_ids') "
                    "AND policyname = 'tenant_isolation'"
                ),
                {"s": clean_schema},
            ).all()
        }
    eng.dispose()
    assert "uq_org_external_id" in ext_uniques  # one (scheme, value) per business; never reused
    assert policies == {"org_observations", "org_external_ids"}  # both tenant-owned: RLS applied


@requires_pg
def test_0025_creates_contact_point_tables_on_a_preexisting_database(clean_schema):
    """DCA-060 — the 0025 create branch (for a DB migrated before the contact-point tables existed).

    ``person_observations`` and ``contact_points`` are tenant-owned, so a fresh DB gets them from the
    metadata baseline (0001) with RLS from 0002, and 0025 is a no-op there. To exercise 0025's real
    job, upgrade to head, drop both tables, stamp history back to 0024, then upgrade to head again —
    0025 must recreate both tables (with the per-business unique contact-point constraint) and add
    their tenant-isolation RLS policies.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(text('DROP TABLE IF EXISTS "contact_points" CASCADE'))
        conn.execute(text('DROP TABLE IF EXISTS "person_observations" CASCADE'))
    eng.dispose()

    command.stamp(cfg, "0024_org_observations")  # pretend we predate the contact-point tables
    command.upgrade(cfg, "head")  # 0025 runs its create branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    tables = set(insp.get_table_names(schema=clean_schema))
    assert {"person_observations", "contact_points"}.issubset(tables)
    cp_uniques = {
        u["name"] for u in insp.get_unique_constraints("contact_points", schema=clean_schema)
    }
    with eng.connect() as conn:
        policies = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT tablename FROM pg_policies WHERE schemaname = :s "
                    "AND tablename IN ('person_observations', 'contact_points') "
                    "AND policyname = 'tenant_isolation'"
                ),
                {"s": clean_schema},
            ).all()
        }
    eng.dispose()
    assert "uq_contact_point" in cp_uniques  # one (channel, value) per business; never reused
    assert policies == {"person_observations", "contact_points"}  # both tenant-owned: RLS applied


@requires_pg
def test_0026_creates_field_observation_table_on_a_preexisting_database(clean_schema):
    """DCA-061 — the 0026 create branch (for a DB migrated before the field-observation table existed).

    ``field_observations`` is tenant-owned, so a fresh DB gets it from the metadata baseline (0001)
    with RLS from 0002, and 0026 is a no-op there. To exercise 0026's real job, upgrade to head, drop
    the table, stamp history back to 0025, then upgrade to head again — 0026 must recreate the table
    and add its tenant-isolation RLS policy.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(text('DROP TABLE IF EXISTS "field_observations" CASCADE'))
    eng.dispose()

    command.stamp(cfg, "0025_contact_points")  # pretend we predate the field-observation table
    command.upgrade(cfg, "head")  # 0026 runs its create branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    tables = set(insp.get_table_names(schema=clean_schema))
    assert "field_observations" in tables
    with eng.connect() as conn:
        policies = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT tablename FROM pg_policies WHERE schemaname = :s "
                    "AND tablename = 'field_observations' AND policyname = 'tenant_isolation'"
                ),
                {"s": clean_schema},
            ).all()
        }
    eng.dispose()
    assert policies == {"field_observations"}  # tenant-owned: RLS applied


@requires_pg
def test_0027_creates_evidence_objects_table_on_a_preexisting_database(clean_schema):
    """DCA-062 — the 0027 create branch (for a DB migrated before the evidence-object table existed).

    ``evidence_objects`` is tenant-owned, so a fresh DB gets it from the metadata baseline (0001) with
    RLS from 0002, and 0027 is a no-op there. To exercise 0027's real job, upgrade to head, drop the
    table, stamp history back to 0026, then upgrade to head again — 0027 must recreate the table and
    add its tenant-isolation RLS policy.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(text('DROP TABLE IF EXISTS "evidence_objects" CASCADE'))
    eng.dispose()

    command.stamp(cfg, "0026_field_observations")  # pretend we predate the evidence-object table
    command.upgrade(cfg, "head")  # 0027 runs its create branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    tables = set(insp.get_table_names(schema=clean_schema))
    assert "evidence_objects" in tables
    with eng.connect() as conn:
        policies = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT tablename FROM pg_policies WHERE schemaname = :s "
                    "AND tablename = 'evidence_objects' AND policyname = 'tenant_isolation'"
                ),
                {"s": clean_schema},
            ).all()
        }
    eng.dispose()
    assert policies == {"evidence_objects"}  # tenant-owned: RLS applied


@requires_pg
def test_0028_creates_field_corrections_table_on_a_preexisting_database(clean_schema):
    """DCA-070 — the 0028 create branch (for a DB migrated before the field-correction table existed).

    ``field_corrections`` is tenant-owned, so a fresh DB gets it from the metadata baseline (0001) with
    RLS from 0002, and 0028 is a no-op there. To exercise 0028's real job, upgrade to head, drop the
    table, stamp history back to 0027, then upgrade to head again — 0028 must recreate the table (with
    its per-(business, subject, field) version constraint) and add its tenant-isolation RLS policy.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(text('DROP TABLE IF EXISTS "field_corrections" CASCADE'))
    eng.dispose()

    command.stamp(cfg, "0027_evidence_objects")  # pretend we predate the field-correction table
    command.upgrade(cfg, "head")  # 0028 runs its create branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    tables = set(insp.get_table_names(schema=clean_schema))
    assert "field_corrections" in tables
    uniques = {
        u["name"] for u in insp.get_unique_constraints("field_corrections", schema=clean_schema)
    }
    with eng.connect() as conn:
        policies = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT tablename FROM pg_policies WHERE schemaname = :s "
                    "AND tablename = 'field_corrections' AND policyname = 'tenant_isolation'"
                ),
                {"s": clean_schema},
            ).all()
        }
    eng.dispose()
    assert "uq_field_correction_version" in uniques  # one version per (tenant, subject, field)
    assert policies == {"field_corrections"}  # tenant-owned: RLS applied


@requires_pg
def test_0030_creates_restriction_tables_on_a_preexisting_database(clean_schema):
    """DCA-078 — the 0030 create branch (for a DB migrated before the restriction tables existed).

    0030 adds two tables: ``processing_restrictions`` (tenant-owned → RLS policy + version constraint)
    and ``platform_source_restrictions`` (platform control-plane → NO RLS policy, like ``sources``).
    A fresh DB gets both from the metadata baseline (0001), with RLS for only the tenant-owned one from
    0002, so 0030 is a no-op there. To exercise 0030's real job, upgrade to head, drop both tables,
    stamp history back to 0029, then upgrade to head again — 0030 must recreate both (with their version
    constraints) and add the tenant-isolation RLS policy to ONLY the tenant-owned table.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(text('DROP TABLE IF EXISTS "processing_restrictions" CASCADE'))
        conn.execute(text('DROP TABLE IF EXISTS "platform_source_restrictions" CASCADE'))
    eng.dispose()

    command.stamp(cfg, "0029_consent_receipts")  # pretend we predate the restriction tables
    command.upgrade(cfg, "head")  # 0030 runs its create branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    tables = set(insp.get_table_names(schema=clean_schema))
    assert "processing_restrictions" in tables
    assert "platform_source_restrictions" in tables
    local_uniques = {
        u["name"]
        for u in insp.get_unique_constraints("processing_restrictions", schema=clean_schema)
    }
    platform_uniques = {
        u["name"]
        for u in insp.get_unique_constraints("platform_source_restrictions", schema=clean_schema)
    }
    with eng.connect() as conn:
        policies = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT tablename FROM pg_policies WHERE schemaname = :s "
                    "AND tablename IN ('processing_restrictions', 'platform_source_restrictions') "
                    "AND policyname = 'tenant_isolation'"
                ),
                {"s": clean_schema},
            ).all()
        }
    eng.dispose()
    assert "uq_processing_restriction_version" in local_uniques
    assert "uq_platform_source_restriction_version" in platform_uniques
    # Only the tenant-owned table is RLS-protected; the platform control-plane table is deliberately not.
    assert policies == {"processing_restrictions"}


@requires_pg
def test_0032_creates_privacy_grievances_table_on_a_preexisting_database(clean_schema):
    """DCA-082 — the 0032 create branch (for a DB migrated before the grievance table existed).

    ``privacy_grievances`` is tenant-owned, so a fresh DB gets it from the metadata baseline (0001) with
    RLS from 0002, and 0032 is a no-op there. To exercise 0032's real job, upgrade to head, drop the
    table, stamp history back to 0031, then upgrade to head again — 0032 must recreate the table and add
    its tenant-isolation RLS policy, so a grievance is protected exactly like every other tenant table.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(text('DROP TABLE IF EXISTS "privacy_grievances" CASCADE'))
    eng.dispose()

    command.stamp(cfg, "0031_restriction_propagations")  # pretend we predate the grievance table
    command.upgrade(cfg, "head")  # 0032 runs its create branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    tables = set(insp.get_table_names(schema=clean_schema))
    assert "privacy_grievances" in tables
    with eng.connect() as conn:
        policies = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT tablename FROM pg_policies WHERE schemaname = :s "
                    "AND tablename = 'privacy_grievances' AND policyname = 'tenant_isolation'"
                ),
                {"s": clean_schema},
            ).all()
        }
    eng.dispose()
    assert policies == {"privacy_grievances"}  # tenant-owned: RLS applied


@requires_pg
def test_0033_creates_data_erasure_journals_table_on_a_preexisting_database(clean_schema):
    """DCA-084 — the 0033 create branch (for a DB migrated before the erasure-journal table existed).

    ``data_erasure_journals`` is tenant-owned, so a fresh DB gets it from the metadata baseline (0001)
    with RLS from 0002, and 0033 is a no-op there. To exercise 0033's real job, upgrade to head, drop
    the table, stamp history back to 0032, then upgrade to head again — 0033 must recreate the table and
    add its tenant-isolation RLS policy, so the journal is protected exactly like every other tenant
    table.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(text('DROP TABLE IF EXISTS "data_erasure_journals" CASCADE'))
    eng.dispose()

    command.stamp(cfg, "0032_privacy_grievances")  # pretend we predate the erasure-journal table
    command.upgrade(cfg, "head")  # 0033 runs its create branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    tables = set(insp.get_table_names(schema=clean_schema))
    assert "data_erasure_journals" in tables
    with eng.connect() as conn:
        policies = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT tablename FROM pg_policies WHERE schemaname = :s "
                    "AND tablename = 'data_erasure_journals' AND policyname = 'tenant_isolation'"
                ),
                {"s": clean_schema},
            ).all()
        }
    eng.dispose()
    assert policies == {"data_erasure_journals"}  # tenant-owned: RLS applied


@requires_pg
def test_0037_creates_durable_job_requests_on_a_preexisting_database(clean_schema):
    """DCA-099 — the 0037 create branch (for a DB migrated before ``durable_job_requests`` existed).

    ``durable_job_requests`` is tenant-owned, so a fresh DB gets it from the metadata baseline (0001)
    with RLS from 0002, and 0037 is a no-op there. To exercise 0037's real job, upgrade to head, drop
    the table, stamp history back to 0036, then upgrade to head again — 0037 must recreate the table
    (with its unique dedup constraint) AND add its tenant-isolation RLS policy, so the queue is
    protected exactly like every other tenant table.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(text('DROP TABLE IF EXISTS "durable_job_requests" CASCADE'))
    eng.dispose()

    command.stamp(cfg, "0036_dispatch_recheck")  # pretend we predate the job-request table
    command.upgrade(cfg, "head")  # 0037 runs its create branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    tables = set(insp.get_table_names(schema=clean_schema))
    assert "durable_job_requests" in tables
    uniques = {
        u["name"] for u in insp.get_unique_constraints("durable_job_requests", schema=clean_schema)
    }
    with eng.connect() as conn:
        policies = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT tablename FROM pg_policies WHERE schemaname = :s "
                    "AND tablename = 'durable_job_requests' AND policyname = 'tenant_isolation'"
                ),
                {"s": clean_schema},
            ).all()
        }
    eng.dispose()
    assert "uq_durable_job_request" in uniques  # the per-intent dedup constraint travelled with it
    assert policies == {"durable_job_requests"}  # tenant-owned: RLS applied


@requires_pg
def test_0038_adds_lease_columns_on_a_preexisting_database(clean_schema):
    """DCA-101 — the 0038 add-column branch (for a DB migrated before the lease/fencing columns).

    A fresh DB gets ``durable_job_requests`` WITH ``lease_owner``/``lease_expires_at``/``lease_version``
    from the metadata baseline (0001), so 0038 is a no-op there. To exercise 0038's real job, upgrade to
    head, DROP the three columns, seed one pre-existing queued row, stamp history back to 0037, then
    upgrade to head again — 0038 must re-add exactly those columns and backfill the existing row's
    ``lease_version`` to 0 (never claimed), with no new table and the tenant RLS policy intact.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(
            text(
                'ALTER TABLE "durable_job_requests" DROP COLUMN "lease_version", '
                'DROP COLUMN "lease_expires_at", DROP COLUMN "lease_owner"'
            )
        )
        # Seed one pre-existing (pre-lease) queued row to prove the backfill.
        conn.execute(
            text(
                "INSERT INTO durable_job_requests "
                "(id, business_id, schema_version, job_type, idempotency_key, payload_ref, "
                "payload_digest, payload_metadata, status, attempt_count, max_attempts, "
                "available_at, created_at, updated_at) VALUES "
                "('job-legacy', 'biz-legacy', 1, 'prospect.research', 'legacy-key', 'ref://x', "
                "'digest-x', '{}', 'queued', 0, 5, now(), now(), now())"
            )
        )
    eng.dispose()

    command.stamp(cfg, "0037_durable_job_requests")  # pretend we predate the lease columns
    command.upgrade(cfg, "head")  # 0038 runs its add-column branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    cols = {c["name"] for c in insp.get_columns("durable_job_requests", schema=clean_schema)}
    with eng.connect() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        backfilled = conn.execute(
            text(
                "SELECT lease_owner, lease_expires_at, lease_version FROM durable_job_requests "
                "WHERE id = 'job-legacy'"
            )
        ).first()
        policy = conn.execute(
            text(
                "SELECT 1 FROM pg_policies WHERE schemaname = :s "
                "AND tablename = 'durable_job_requests' AND policyname = 'tenant_isolation'"
            ),
            {"s": clean_schema},
        ).first()
    eng.dispose()
    assert {"lease_owner", "lease_expires_at", "lease_version"}.issubset(cols)  # columns re-added
    assert backfilled == (None, None, 0)  # pre-existing row backfilled to "never claimed"
    assert policy is not None  # tenant RLS policy intact (unchanged by the add-column migration)


@requires_pg
def test_0039_adds_lease_deadline_on_a_preexisting_database(clean_schema):
    """DCA-102 — the 0039 add-column branch (for a DB migrated before the ``lease_deadline`` ceiling).

    A fresh DB gets ``durable_job_requests`` WITH ``lease_deadline`` from the metadata baseline (0001),
    so 0039 is a no-op there. To exercise 0039's real job, upgrade to head, DROP ``lease_deadline``, seed
    one pre-existing running row (claimed before the ceiling existed), stamp history back to 0038, then
    upgrade to head again — 0039 must re-add exactly that column as NULLABLE, leave the pre-existing row's
    ``lease_deadline`` NULL (no backfill — renewal falls back to the per-call bound), add no new table, and
    leave the tenant RLS policy intact.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(text('ALTER TABLE "durable_job_requests" DROP COLUMN "lease_deadline"'))
        # Seed one pre-existing (pre-ceiling) running row to prove it simply reads NULL after the add.
        conn.execute(
            text(
                "INSERT INTO durable_job_requests "
                "(id, business_id, schema_version, job_type, idempotency_key, payload_ref, "
                "payload_digest, payload_metadata, status, attempt_count, max_attempts, "
                "lease_owner, lease_expires_at, lease_version, "
                "available_at, created_at, updated_at) VALUES "
                "('job-preceiling', 'biz-legacy', 1, 'prospect.research', 'preceiling-key', 'ref://x', "
                "'digest-x', '{}', 'running', 1, 5, 'worker-1', now(), 1, now(), now(), now())"
            )
        )
    eng.dispose()

    command.stamp(cfg, "0038_durable_job_request_lease")  # pretend we predate the ceiling column
    command.upgrade(cfg, "head")  # 0039 runs its add-column branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    cols = {c["name"] for c in insp.get_columns("durable_job_requests", schema=clean_schema)}
    deadline_col = next(
        c
        for c in insp.get_columns("durable_job_requests", schema=clean_schema)
        if c["name"] == "lease_deadline"
    )
    with eng.connect() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        existing = conn.execute(
            text("SELECT lease_deadline FROM durable_job_requests WHERE id = 'job-preceiling'")
        ).first()
        policy = conn.execute(
            text(
                "SELECT 1 FROM pg_policies WHERE schemaname = :s "
                "AND tablename = 'durable_job_requests' AND policyname = 'tenant_isolation'"
            ),
            {"s": clean_schema},
        ).first()
    eng.dispose()
    assert "lease_deadline" in cols  # column re-added
    assert deadline_col["nullable"] is True  # nullable: no backfill, pre-existing rows read NULL
    assert existing == (None,)  # pre-existing row has no DCA-102 ceiling recorded
    assert policy is not None  # tenant RLS policy intact (unchanged by the add-column migration)


@requires_pg
def test_0040_adds_result_columns_on_a_preexisting_database(clean_schema):
    """DCA-103 — the 0040 add-column branch (for a DB migrated before the fenced-result columns).

    A fresh DB gets ``durable_job_requests`` WITH ``result_ref``/``result_digest``/``completed_at`` from
    the metadata baseline (0001), so 0040 is a no-op there. To exercise 0040's real job, upgrade to head,
    DROP the three columns, seed one pre-existing running row (claimed before the result columns existed),
    stamp history back to 0039, then upgrade to head again — 0040 must re-add exactly those columns as
    NULLABLE, leave the pre-existing row's values NULL (no backfill — a commit only ever writes them on a
    successful fenced completion), add no new table, and leave the tenant RLS policy intact.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(
            text(
                'ALTER TABLE "durable_job_requests" DROP COLUMN "result_ref", '
                'DROP COLUMN "result_digest", DROP COLUMN "completed_at"'
            )
        )
        # Seed one pre-existing (pre-result) running row to prove it simply reads NULL after the add.
        conn.execute(
            text(
                "INSERT INTO durable_job_requests "
                "(id, business_id, schema_version, job_type, idempotency_key, payload_ref, "
                "payload_digest, payload_metadata, status, attempt_count, max_attempts, "
                "lease_owner, lease_expires_at, lease_version, lease_deadline, "
                "available_at, created_at, updated_at) VALUES "
                "('job-preresult', 'biz-legacy', 1, 'prospect.research', 'preresult-key', 'ref://x', "
                "'digest-x', '{}', 'running', 1, 5, 'worker-1', now(), 1, now(), now(), now(), now())"
            )
        )
    eng.dispose()

    command.stamp(cfg, "0039_job_request_lease_deadline")  # pretend we predate the result columns
    command.upgrade(cfg, "head")  # 0040 runs its add-column branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    columns = {c["name"]: c for c in insp.get_columns("durable_job_requests", schema=clean_schema)}
    with eng.connect() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        existing = conn.execute(
            text(
                "SELECT result_ref, result_digest, completed_at FROM durable_job_requests "
                "WHERE id = 'job-preresult'"
            )
        ).first()
        policy = conn.execute(
            text(
                "SELECT 1 FROM pg_policies WHERE schemaname = :s "
                "AND tablename = 'durable_job_requests' AND policyname = 'tenant_isolation'"
            ),
            {"s": clean_schema},
        ).first()
    eng.dispose()
    assert {"result_ref", "result_digest", "completed_at"}.issubset(columns)  # columns re-added
    assert all(
        columns[c]["nullable"] is True for c in ("result_ref", "result_digest", "completed_at")
    )
    assert existing == (None, None, None)  # pre-existing row has no committed result
    assert policy is not None  # tenant RLS policy intact (unchanged by the add-column migration)


@requires_pg
def test_0041_creates_identity_merges_table_on_a_preexisting_database(clean_schema):
    """DCA-071 — the 0041 create branch (for a DB migrated before the identity-merge table existed).

    ``identity_merges`` is tenant-owned, so a fresh DB gets it from the metadata baseline (0001) with
    RLS from 0002, and 0041 is a no-op there. To exercise 0041's real job, upgrade to head, drop the
    table, stamp history back to 0040, then upgrade to head again — 0041 must recreate the table (with
    its per-(business, subject, pair) uniqueness constraint) and add its tenant-isolation RLS policy, so
    a merge is protected exactly like every other tenant table.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(text('DROP TABLE IF EXISTS "identity_merges" CASCADE'))
    eng.dispose()

    command.stamp(cfg, "0040_durable_job_request_result")  # pretend we predate the merge table
    command.upgrade(cfg, "head")  # 0041 runs its create branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    tables = set(insp.get_table_names(schema=clean_schema))
    assert "identity_merges" in tables
    uniques = {
        u["name"] for u in insp.get_unique_constraints("identity_merges", schema=clean_schema)
    }
    with eng.connect() as conn:
        policies = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT tablename FROM pg_policies WHERE schemaname = :s "
                    "AND tablename = 'identity_merges' AND policyname = 'tenant_isolation'"
                ),
                {"s": clean_schema},
            ).all()
        }
    eng.dispose()
    assert "uq_identity_merge_pair" in uniques  # one merge per (business, subject, directed pair)
    assert policies == {"identity_merges"}  # tenant-owned: RLS applied


@requires_pg
def test_0042_creates_recurring_schedules_on_a_preexisting_database(clean_schema):
    """DCA-107 — the 0042 create branch (for a DB migrated before ``recurring_schedules`` existed).

    ``recurring_schedules`` is tenant-owned, so a fresh DB gets it from the metadata baseline (0001) with
    RLS from 0002, and 0042 is a no-op there. To exercise 0042's real job, upgrade to head, drop the
    table, stamp history back to 0041, then upgrade to head again — 0042 must recreate the table (with
    its per-(business, schedule_key) uniqueness constraint) and add its tenant-isolation RLS policy, so a
    schedule is protected exactly like every other tenant table.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(text('DROP TABLE IF EXISTS "recurring_schedules" CASCADE'))
    eng.dispose()

    command.stamp(cfg, "0041_identity_merges")  # pretend we predate the recurring-schedule table
    command.upgrade(cfg, "head")  # 0042 runs its create branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    tables = set(insp.get_table_names(schema=clean_schema))
    assert "recurring_schedules" in tables
    uniques = {
        u["name"] for u in insp.get_unique_constraints("recurring_schedules", schema=clean_schema)
    }
    with eng.connect() as conn:
        policies = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT tablename FROM pg_policies WHERE schemaname = :s "
                    "AND tablename = 'recurring_schedules' AND policyname = 'tenant_isolation'"
                ),
                {"s": clean_schema},
            ).all()
        }
    eng.dispose()
    assert "uq_recurring_schedule_key" in uniques  # one schedule per (business, schedule_key)
    assert policies == {"recurring_schedules"}  # tenant-owned: RLS applied


@requires_pg
def test_0043_creates_emergency_stops_on_a_preexisting_database(clean_schema):
    """DCA-111 — the 0043 create branch (for a DB migrated before the emergency-stop tables existed).

    0043 adds two tables: ``business_emergency_stops`` (tenant-owned → RLS policy + per-business version
    constraint) and ``platform_emergency_stops`` (platform control-plane → NO RLS policy, like
    ``platform_source_restrictions``). A fresh DB gets both from the metadata baseline (0001), with RLS
    for only the tenant-owned one from 0002, so 0043 is a no-op there. To exercise 0043's real job,
    upgrade to head, drop both tables, stamp history back to 0042, then upgrade to head again — 0043 must
    recreate both (with their version constraints) and add the tenant-isolation RLS policy to ONLY the
    tenant-owned table.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(text('DROP TABLE IF EXISTS "business_emergency_stops" CASCADE'))
        conn.execute(text('DROP TABLE IF EXISTS "platform_emergency_stops" CASCADE'))
    eng.dispose()

    command.stamp(cfg, "0042_recurring_schedules")  # pretend we predate the emergency-stop tables
    command.upgrade(cfg, "head")  # 0043 runs its create branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    tables = set(insp.get_table_names(schema=clean_schema))
    assert "business_emergency_stops" in tables
    assert "platform_emergency_stops" in tables
    biz_uniques = {
        u["name"]
        for u in insp.get_unique_constraints("business_emergency_stops", schema=clean_schema)
    }
    plat_uniques = {
        u["name"]
        for u in insp.get_unique_constraints("platform_emergency_stops", schema=clean_schema)
    }
    with eng.connect() as conn:
        policies = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT tablename FROM pg_policies WHERE schemaname = :s "
                    "AND tablename IN ('business_emergency_stops', 'platform_emergency_stops') "
                    "AND policyname = 'tenant_isolation'"
                ),
                {"s": clean_schema},
            ).all()
        }
    eng.dispose()
    assert "uq_business_emergency_stop_version" in biz_uniques  # one version per business
    assert "uq_platform_emergency_stop_version" in plat_uniques  # one version per platform scope
    # RLS applied to ONLY the tenant-owned table; the control-plane platform table has none.
    assert policies == {"business_emergency_stops"}


@requires_pg
def test_0044_creates_budget_reservations_on_a_preexisting_database(clean_schema):
    """DCA-113 — the 0044 create branch (for a DB migrated before ``budget_reservations`` existed).

    ``budget_reservations`` is tenant-owned, so a fresh DB gets it from the metadata baseline (0001) with
    RLS from 0002, and 0044 is a no-op there. To exercise 0044's real job, upgrade to head, drop the
    table, stamp history back to 0043, then upgrade to head again — 0044 must recreate the table (with
    its per-(business, idempotency_key) uniqueness constraint) and add its tenant-isolation RLS policy, so
    a reservation is protected (and summed) exactly like every other tenant table.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(text('DROP TABLE IF EXISTS "budget_reservations" CASCADE'))
    eng.dispose()

    command.stamp(cfg, "0043_emergency_stops")  # pretend we predate the budget-reservation table
    command.upgrade(cfg, "head")  # 0044 runs its create branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    tables = set(insp.get_table_names(schema=clean_schema))
    assert "budget_reservations" in tables
    uniques = {
        u["name"] for u in insp.get_unique_constraints("budget_reservations", schema=clean_schema)
    }
    with eng.connect() as conn:
        policies = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT tablename FROM pg_policies WHERE schemaname = :s "
                    "AND tablename = 'budget_reservations' AND policyname = 'tenant_isolation'"
                ),
                {"s": clean_schema},
            ).all()
        }
    eng.dispose()
    assert "uq_budget_reservation_key" in uniques  # one hold per (business, idempotency_key)
    assert policies == {"budget_reservations"}  # tenant-owned: RLS applied


@requires_pg
def test_0045_adds_reconciliation_columns_on_a_preexisting_database(clean_schema):
    """DCA-114 — the 0045 add-column branch (for a DB migrated before the reconciliation columns existed).

    A fresh DB gets ``budget_reservations`` WITH the DCA-114 reconciliation columns from the metadata
    baseline (0001), so 0045 is a no-op there. To exercise 0045's real job, upgrade to head, DROP the
    columns, seed one pre-existing ACTIVE hold, stamp history back to 0044, then upgrade to head again —
    0045 must re-add exactly those columns and backfill the existing row to "not reconciled" (NULLs with
    ``overspend_minor`` 0), with no new table and no RLS change (the 0044 policy still covers the table).
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(
            text(
                'ALTER TABLE "budget_reservations" '
                'DROP COLUMN "reconciliation_key", DROP COLUMN "actual_minor", '
                'DROP COLUMN "settled_amount_minor", DROP COLUMN "overspend_minor", '
                'DROP COLUMN "reconciled_at", DROP COLUMN "reconciled_by_actor_id", '
                'DROP COLUMN "reconciled_by_actor_type"'
            )
        )
        # Seed one pre-existing (pre-reconciliation) ACTIVE hold to prove the backfill.
        conn.execute(
            text(
                "INSERT INTO budget_reservations "
                "(id, business_id, schema_version, idempotency_key, amount_minor, currency, provider, "
                "budget_version, day_key, month_key, timezone, status, actor_id, actor_type, "
                "created_at) VALUES "
                "('resv-legacy', 'biz-legacy', 1, 'legacy-key', 4000, 'INR', 'platform', 1, "
                "'2026-10-31', '2026-10', 'Asia/Kolkata', 'active', 'admin-legacy', 'user', now())"
            )
        )
    eng.dispose()

    command.stamp(cfg, "0044_budget_reservations")  # pretend we predate the reconciliation columns
    command.upgrade(cfg, "head")  # 0045 runs its add-column branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    cols = {c["name"] for c in insp.get_columns("budget_reservations", schema=clean_schema)}
    with eng.connect() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        backfilled = conn.execute(
            text(
                "SELECT reconciliation_key, actual_minor, settled_amount_minor, overspend_minor, "
                "reconciled_at FROM budget_reservations WHERE id = 'resv-legacy'"
            )
        ).first()
        policies = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT tablename FROM pg_policies WHERE schemaname = :s "
                    "AND tablename = 'budget_reservations' AND policyname = 'tenant_isolation'"
                ),
                {"s": clean_schema},
            ).all()
        }
    eng.dispose()
    assert {
        "reconciliation_key",
        "actual_minor",
        "settled_amount_minor",
        "overspend_minor",
        "reconciled_at",
        "reconciled_by_actor_id",
        "reconciled_by_actor_type",
    }.issubset(cols)  # columns re-added
    assert backfilled == (
        None,
        None,
        None,
        0,
        None,
    )  # pre-existing hold backfilled to "not reconciled"
    assert policies == {
        "budget_reservations"
    }  # tenant-owned RLS unchanged by the add-column migration


@requires_pg
def test_0046_creates_provider_call_records_on_a_preexisting_database(clean_schema):
    """DCA-115 — the 0046 create branch (for a DB migrated before ``provider_call_records`` existed).

    ``provider_call_records`` is tenant-owned, so a fresh DB gets it from the metadata baseline (0001) with
    RLS from 0002, and 0046 is a no-op there. To exercise 0046's real job, upgrade to head, drop the
    table, stamp history back to 0045, then upgrade to head again — 0046 must recreate the table (with its
    per-(business, call_key) uniqueness constraint) and add its tenant-isolation RLS policy, so a
    provider-call lifecycle record is protected exactly like every other tenant table.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(text('DROP TABLE IF EXISTS "provider_call_records" CASCADE'))
    eng.dispose()

    command.stamp(cfg, "0045_reservation_reconcile")  # pretend we predate the provider-call table
    command.upgrade(cfg, "head")  # 0046 runs its create branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    tables = set(insp.get_table_names(schema=clean_schema))
    assert "provider_call_records" in tables
    uniques = {
        u["name"] for u in insp.get_unique_constraints("provider_call_records", schema=clean_schema)
    }
    with eng.connect() as conn:
        policies = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT tablename FROM pg_policies WHERE schemaname = :s "
                    "AND tablename = 'provider_call_records' AND policyname = 'tenant_isolation'"
                ),
                {"s": clean_schema},
            ).all()
        }
    eng.dispose()
    assert "uq_provider_call_key" in uniques  # one record per (business, call_key); a retry replays
    assert policies == {"provider_call_records"}  # tenant-owned: RLS applied


@requires_pg
def test_0047_adds_resolution_columns_on_a_preexisting_database(clean_schema):
    """DCA-116 — the 0047 add-column branch (for a DB migrated before the resolution columns existed).

    A fresh DB gets ``provider_call_records`` WITH the DCA-116 resolution columns from the metadata
    baseline (0001), so 0047 is a no-op there. To exercise 0047's real job, upgrade to head, DROP the
    columns, seed one pre-existing UNCERTAIN record, stamp history back to 0046, then upgrade to head
    again — 0047 must re-add exactly those columns and backfill the existing row to "unresolved" ("" with
    NULLs), with no new table and no RLS change (the 0046 policy still covers the table).
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(
            text(
                'ALTER TABLE "provider_call_records" '
                'DROP COLUMN "resolution", DROP COLUMN "resolution_key", '
                'DROP COLUMN "resolution_outcome_code", DROP COLUMN "resolution_actor_id", '
                'DROP COLUMN "resolution_actor_type", DROP COLUMN "resolution_at"'
            )
        )
        # Seed one pre-existing (pre-resolution) UNCERTAIN record to prove the backfill.
        conn.execute(
            text(
                "INSERT INTO provider_call_records "
                "(id, business_id, schema_version, call_key, request_id, capability, intent, provider, "
                "model, source_version, status, dispatched, actor_id, actor_type, created_at, "
                "updated_at) VALUES "
                "('pcr-legacy', 'biz-legacy', 1, 'legacy-call', 'req-legacy', 'contact_research', '', "
                "'platform', '', '', 'uncertain', true, 'admin-legacy', 'user', now(), now())"
            )
        )
    eng.dispose()

    command.stamp(cfg, "0046_provider_call_records")  # pretend we predate the resolution columns
    command.upgrade(cfg, "head")  # 0047 runs its add-column branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    cols = {c["name"] for c in insp.get_columns("provider_call_records", schema=clean_schema)}
    with eng.connect() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        backfilled = conn.execute(
            text(
                "SELECT resolution, resolution_key, resolution_outcome_code, resolution_at "
                "FROM provider_call_records WHERE id = 'pcr-legacy'"
            )
        ).first()
        policies = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT tablename FROM pg_policies WHERE schemaname = :s "
                    "AND tablename = 'provider_call_records' AND policyname = 'tenant_isolation'"
                ),
                {"s": clean_schema},
            ).all()
        }
    eng.dispose()
    assert {
        "resolution",
        "resolution_key",
        "resolution_outcome_code",
        "resolution_actor_id",
        "resolution_actor_type",
        "resolution_at",
    }.issubset(cols)  # columns re-added
    assert backfilled == ("", None, None, None)  # pre-existing record backfilled to "unresolved"
    assert policies == {
        "provider_call_records"
    }  # tenant-owned RLS unchanged by the add-column migration


@requires_pg
def test_0048_creates_host_crawl_state_on_a_preexisting_database(clean_schema):
    """DCA-121 — the 0048 create branch (for a DB migrated before ``host_crawl_state`` existed).

    ``host_crawl_state`` is tenant-owned (RLS-protected), like ``budget_reservations`` (0044). A fresh
    DB gets the table from the metadata baseline (0001) and its RLS policy from 0002, so 0048 is a
    no-op there. To exercise 0048's real job, upgrade to head, drop the table + policy, stamp history
    back to 0047, then upgrade to head again — 0048 must recreate the table (with its per-tenant
    (source_key, host) constraint) AND its tenant-isolation RLS policy.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(text('DROP TABLE IF EXISTS "host_crawl_state" CASCADE'))  # policy drops too
    eng.dispose()

    command.stamp(cfg, "0047_provider_call_resolution")  # pretend we predate the crawl-state table
    command.upgrade(cfg, "head")  # 0048 runs its create branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    assert "host_crawl_state" in set(insp.get_table_names(schema=clean_schema))
    uniques = {
        u["name"] for u in insp.get_unique_constraints("host_crawl_state", schema=clean_schema)
    }
    with eng.connect() as conn:
        policy = conn.execute(
            text(
                "SELECT 1 FROM pg_policies WHERE schemaname = :s "
                "AND tablename = 'host_crawl_state' AND policyname = 'tenant_isolation'"
            ),
            {"s": clean_schema},
        ).first()
    eng.dispose()
    assert "uq_host_crawl_state" in uniques  # one crawl-state row per (tenant, source, host)
    assert policy is not None  # RLS re-established, not just the bare table


@requires_pg
def test_0049_creates_org_observation_imports_on_a_preexisting_database(clean_schema):
    """DCA-123 — the 0049 create branch (for a DB migrated before ``org_observation_imports`` existed).

    ``org_observation_imports`` is tenant-owned (RLS-protected), like ``host_crawl_state`` (0048). A
    fresh DB gets the table from the metadata baseline (0001) and its RLS policy from 0002, so 0049 is a
    no-op there. To exercise 0049's real job, upgrade to head, drop the table + policy, stamp history
    back to 0048, then upgrade to head again — 0049 must recreate the table (with its per-tenant
    (source_key, dataset_id, source_row_ref) constraint) AND its tenant-isolation RLS policy.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(text('DROP TABLE IF EXISTS "org_observation_imports" CASCADE'))  # policy too
    eng.dispose()

    command.stamp(cfg, "0048_host_crawl_state")  # pretend we predate the import-provenance table
    command.upgrade(cfg, "head")  # 0049 runs its create branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    assert "org_observation_imports" in set(insp.get_table_names(schema=clean_schema))
    uniques = {
        u["name"]
        for u in insp.get_unique_constraints("org_observation_imports", schema=clean_schema)
    }
    with eng.connect() as conn:
        policy = conn.execute(
            text(
                "SELECT 1 FROM pg_policies WHERE schemaname = :s "
                "AND tablename = 'org_observation_imports' AND policyname = 'tenant_isolation'"
            ),
            {"s": clean_schema},
        ).first()
    eng.dispose()
    assert (
        "uq_org_observation_import_row" in uniques
    )  # one provenance row per (tenant, source, dataset, row)
    assert policy is not None  # RLS re-established, not just the bare table


@requires_pg
def test_0050_creates_website_fact_extractions_on_a_preexisting_database(clean_schema):
    """DCA-124 — the 0050 create branch (for a DB migrated before ``website_fact_extractions`` existed).

    ``website_fact_extractions`` is tenant-owned (RLS-protected), like ``org_observation_imports``
    (0049). A fresh DB gets the table from the metadata baseline (0001) and its RLS policy from 0002, so
    0050 is a no-op there. To exercise 0050's real job, upgrade to head, drop the table + policy, stamp
    history back to 0049, then upgrade to head again — 0050 must recreate the table (with its per-tenant
    (source_key, subject_id, page_ref) constraint) AND its tenant-isolation RLS policy.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(text('DROP TABLE IF EXISTS "website_fact_extractions" CASCADE'))  # policy too
    eng.dispose()

    command.stamp(cfg, "0049_org_observation_imports")  # pretend we predate the extraction table
    command.upgrade(cfg, "head")  # 0050 runs its create branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    assert "website_fact_extractions" in set(insp.get_table_names(schema=clean_schema))
    uniques = {
        u["name"]
        for u in insp.get_unique_constraints("website_fact_extractions", schema=clean_schema)
    }
    with eng.connect() as conn:
        policy = conn.execute(
            text(
                "SELECT 1 FROM pg_policies WHERE schemaname = :s "
                "AND tablename = 'website_fact_extractions' AND policyname = 'tenant_isolation'"
            ),
            {"s": clean_schema},
        ).first()
    eng.dispose()
    assert (
        "uq_website_fact_extraction_page" in uniques
    )  # one provenance row per (tenant, source, subject, page)
    assert policy is not None  # RLS re-established, not just the bare table


@requires_pg
def test_0051_creates_licensed_provider_fetches_on_a_preexisting_database(clean_schema):
    """DCA-125 — the 0051 create branch (for a DB migrated before ``licensed_provider_fetches`` existed).

    ``licensed_provider_fetches`` is tenant-owned (RLS-protected), like ``website_fact_extractions``
    (0050). A fresh DB gets the table from the metadata baseline (0001) and its RLS policy from 0002, so
    0051 is a no-op there. To exercise 0051's real job, upgrade to head, drop the table + policy, stamp
    history back to 0050, then upgrade to head again — 0051 must recreate the table (with its per-tenant
    (source_key, subject_id, fetch_key) constraint) AND its tenant-isolation RLS policy.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(text('DROP TABLE IF EXISTS "licensed_provider_fetches" CASCADE'))  # policy too
    eng.dispose()

    command.stamp(cfg, "0050_website_fact_extractions")  # pretend we predate the fetch table
    command.upgrade(cfg, "head")  # 0051 runs its create branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    assert "licensed_provider_fetches" in set(insp.get_table_names(schema=clean_schema))
    uniques = {
        u["name"]
        for u in insp.get_unique_constraints("licensed_provider_fetches", schema=clean_schema)
    }
    with eng.connect() as conn:
        policy = conn.execute(
            text(
                "SELECT 1 FROM pg_policies WHERE schemaname = :s "
                "AND tablename = 'licensed_provider_fetches' AND policyname = 'tenant_isolation'"
            ),
            {"s": clean_schema},
        ).first()
    eng.dispose()
    assert (
        "uq_licensed_provider_fetch" in uniques
    )  # one provenance row per (tenant, source, subject, fetch)
    assert policy is not None  # RLS re-established, not just the bare table


@requires_pg
def test_0052_creates_contact_imports_on_a_preexisting_database(clean_schema):
    """DCA-126 — the 0052 create branch (for a DB migrated before ``contact_imports`` existed).

    ``contact_imports`` is tenant-owned (RLS-protected), like ``licensed_provider_fetches`` (0051). A
    fresh DB gets the table from the metadata baseline (0001) and its RLS policy from 0002, so 0052 is a
    no-op there. To exercise 0052's real job, upgrade to head, drop the table + policy, stamp history back
    to 0051, then upgrade to head again — 0052 must recreate the table (with its per-tenant
    (dataset_id, source_row_ref) constraint) AND its tenant-isolation RLS policy.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(text('DROP TABLE IF EXISTS "contact_imports" CASCADE'))  # policy drops with it
    eng.dispose()

    command.stamp(cfg, "0051_licensed_provider_fetches")  # pretend we predate the import table
    command.upgrade(cfg, "head")  # 0052 runs its create branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    assert "contact_imports" in set(insp.get_table_names(schema=clean_schema))
    uniques = {
        u["name"] for u in insp.get_unique_constraints("contact_imports", schema=clean_schema)
    }
    with eng.connect() as conn:
        policy = conn.execute(
            text(
                "SELECT 1 FROM pg_policies WHERE schemaname = :s "
                "AND tablename = 'contact_imports' AND policyname = 'tenant_isolation'"
            ),
            {"s": clean_schema},
        ).first()
    eng.dispose()
    assert "uq_contact_import_row" in uniques  # one provenance row per (tenant, dataset, row)
    assert policy is not None  # RLS re-established, not just the bare table


@requires_pg
def test_0053_creates_source_result_cache_on_a_preexisting_database(clean_schema):
    """DCA-127 — the 0053 create branch (for a DB migrated before ``source_result_cache`` existed).

    ``source_result_cache`` is tenant-owned (RLS-protected), like ``contact_imports`` (0052). A fresh DB
    gets the table from the metadata baseline (0001) and its RLS policy from 0002, so 0053 is a no-op
    there. To exercise 0053's real job, upgrade to head, drop the table + policy, stamp history back to
    0052, then upgrade to head again — 0053 must recreate the table (with its per-tenant
    (source_key, source_version, cache_key) constraint) AND its tenant-isolation RLS policy.
    """
    from alembic import command

    cfg = _alembic_config(clean_schema)
    command.upgrade(cfg, "head")

    eng = create_engine(PG_URL)
    with eng.begin() as conn:
        conn.execute(text(f'SET search_path TO "{clean_schema}"'))
        conn.execute(text('DROP TABLE IF EXISTS "source_result_cache" CASCADE'))  # policy drops too
    eng.dispose()

    command.stamp(cfg, "0052_contact_imports")  # pretend we predate the cache table
    command.upgrade(cfg, "head")  # 0053 runs its create branch

    eng = create_engine(PG_URL)
    insp = inspect(eng)
    assert "source_result_cache" in set(insp.get_table_names(schema=clean_schema))
    uniques = {
        u["name"] for u in insp.get_unique_constraints("source_result_cache", schema=clean_schema)
    }
    with eng.connect() as conn:
        policy = conn.execute(
            text(
                "SELECT 1 FROM pg_policies WHERE schemaname = :s "
                "AND tablename = 'source_result_cache' AND policyname = 'tenant_isolation'"
            ),
            {"s": clean_schema},
        ).first()
    eng.dispose()
    assert (
        "uq_source_result_cache_entry" in uniques
    )  # one entry per (tenant, source, version, cache_key)
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
