"""DCA-009 — seed two isolated test businesses.

Behaviour: an explicit dev-only seed creates Business A and Business B (synthetic tenants + ICPs),
is idempotent on repeat, and is refused under production configuration leaving no rows behind.

Idempotency is a persistence/transaction property, so the authoritative idempotency assertion runs
against a **disposable PostgreSQL** (`DCA_TEST_DATABASE_URL`), never SQLite — it skips when no URL is
supplied so the default suite stays network-free. The logic/denial tests run on in-memory SQLite.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import func, select

from app.db.base import Base, make_engine, make_session_factory
from app.db.models import IcpProfile, TenantAgent
from app.db.seed import TEST_BUSINESSES, SeedRefused, seed_test_businesses
from tests.conftest import make_settings

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)


def _sqlite_session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


@pytest.fixture
def session():
    factory = _sqlite_session_factory()
    with factory() as s:
        yield s


def _counts(session) -> tuple[int, int]:
    tenants = session.scalar(select(func.count()).select_from(TenantAgent))
    icps = session.scalar(select(func.count()).select_from(IcpProfile))
    return tenants, icps


def test_seed_creates_two_isolated_businesses_with_icps(session):
    result = seed_test_businesses(session, make_settings())
    assert len(result["businesses"]) == 2
    assert result["created"] == [b.business_id for b in TEST_BUSINESSES]
    assert result["updated"] == []
    assert _counts(session) == (2, 2)

    # Two distinct tenants, each with its own ICP — the isolation baseline for later stories.
    a, b = TEST_BUSINESSES
    assert a.business_id != b.business_id
    for biz in TEST_BUSINESSES:
        tenant = session.scalar(
            select(TenantAgent).where(TenantAgent.business_id == biz.business_id)
        )
        assert tenant is not None and tenant.country == "IN" and tenant.enabled is False
        icp = session.scalar(select(IcpProfile).where(IcpProfile.business_id == biz.business_id))
        assert icp is not None and icp.industries == biz.industries


def test_seed_is_idempotent_sqlite(session):
    first = seed_test_businesses(session, make_settings())
    tenant_ids = {t.business_id: t.id for t in session.scalars(select(TenantAgent)).all()}

    second = seed_test_businesses(session, make_settings())
    assert second["created"] == []  # nothing new on the second pass
    assert second["updated"] == first["businesses"]
    assert _counts(session) == (2, 2)  # row counts stable

    # Primary-key ids are stable across re-seeds (upsert, not insert).
    again = {t.business_id: t.id for t in session.scalars(select(TenantAgent)).all()}
    assert again == tenant_ids
    # ICP version is not bumped by a repeat seed (not mistaken for a real revision).
    assert all(icp.version == 1 for icp in session.scalars(select(IcpProfile)).all())


def test_seed_refused_in_production_writes_nothing(session):
    prod = make_settings(environment="production")
    with pytest.raises(SeedRefused):
        seed_test_businesses(session, prod)
    assert _counts(session) == (0, 0)  # denial left no state behind


@requires_pg
def test_seed_is_idempotent_on_postgres():
    engine = make_engine(PG_URL)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    try:
        with factory() as s:
            seed_test_businesses(s, make_settings(database_url=PG_URL))
            seed_test_businesses(s, make_settings(database_url=PG_URL))
        with factory() as s:
            ids = [b.business_id for b in TEST_BUSINESSES]
            tenants = s.scalar(
                select(func.count())
                .select_from(TenantAgent)
                .where(TenantAgent.business_id.in_(ids))
            )
            icps = s.scalar(
                select(func.count()).select_from(IcpProfile).where(IcpProfile.business_id.in_(ids))
            )
            assert (tenants, icps) == (2, 2)
    finally:
        with factory() as s:
            for biz in TEST_BUSINESSES:
                s.query(TenantAgent).filter(TenantAgent.business_id == biz.business_id).delete()
                s.query(IcpProfile).filter(IcpProfile.business_id == biz.business_id).delete()
            s.commit()
        engine.dispose()
