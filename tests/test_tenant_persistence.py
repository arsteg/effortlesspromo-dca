"""DCA-017 — tenant ownership enforced in persistence.

Through the ordinary :class:`~app.db.tenancy.ScopedRepository` methods a business can neither read
nor write another business's rows: reads filter to the bound context, a by-id lookup of a foreign
row is ``None``, writes are stamped from the verified context, and a body-supplied ``business_id``
that conflicts is rejected before any row is staged. Composite tenant uniqueness lets two businesses
hold the same external identifier without colliding. SQLite is sufficient here — the enforcement is
app-level scoping over the session, independent of the backend; the Postgres DDL path is covered by
``tests/test_migrations.py``.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.base import Base, make_engine, make_session_factory
from app.db.models import DiscoveryRun, ProspectCandidate
from app.db.tenancy import ScopedRepository, TenantOwnershipError
from app.identity.tenant_context import TenantContext

BIZ_A = "00000000-0000-4000-8000-00000000000a"
BIZ_B = "00000000-0000-4000-8000-00000000000b"


def _ctx(business_id: str) -> TenantContext:
    return TenantContext(
        subject="00000000-0000-4000-9000-0000000a11ce",
        business_id=business_id,
        roles=("owner",),
        membership_version="fake-identity-v1",
    )


@pytest.fixture
def session():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = make_session_factory(engine)()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _repo(session, business_id: str) -> ScopedRepository[DiscoveryRun]:
    return ScopedRepository(session, _ctx(business_id), DiscoveryRun)


# --- writes are bound to the verified context -----------------------------------------------------


def test_create_stamps_business_id_from_context(session):
    repo = _repo(session, BIZ_A)
    row = repo.create(status="running")
    session.commit()
    assert row.business_id == BIZ_A


def test_create_rejects_conflicting_body_supplied_owner(session):
    repo = _repo(session, BIZ_A)
    with pytest.raises(TenantOwnershipError):
        repo.create(business_id=BIZ_B, status="running")
    session.commit()
    # Nothing was staged: the rejection happened before any add.
    assert session.query(DiscoveryRun).count() == 0


def test_create_allows_matching_business_id(session):
    repo = _repo(session, BIZ_A)
    row = repo.create(business_id=BIZ_A, status="running")  # explicit but matching is fine
    session.commit()
    assert row.business_id == BIZ_A


def test_add_rejects_entity_carrying_foreign_owner(session):
    repo = _repo(session, BIZ_A)
    # An entity deserialized/constructed with B's id must not be re-homed into A silently.
    with pytest.raises(TenantOwnershipError):
        repo.add(DiscoveryRun(business_id=BIZ_B, status="running"))
    session.commit()
    assert session.query(DiscoveryRun).count() == 0


# --- reads cannot cross a tenant boundary ---------------------------------------------------------


def test_get_by_id_is_tenant_scoped(session):
    row_a = _repo(session, BIZ_A).create(status="running")
    row_b = _repo(session, BIZ_B).create(status="running")
    session.commit()

    repo_a = _repo(session, BIZ_A)
    assert repo_a.get(row_a.id) is not None
    # B's row, fetched by its known id through A's repo, is reported as not-found — never returned.
    assert repo_a.get(row_b.id) is None


def test_list_returns_only_the_bound_tenant(session):
    _repo(session, BIZ_A).create(status="running")
    _repo(session, BIZ_A).create(status="completed")
    _repo(session, BIZ_B).create(status="running")
    session.commit()

    rows_a = _repo(session, BIZ_A).list()
    assert len(rows_a) == 2
    assert all(r.business_id == BIZ_A for r in rows_a)
    assert len(_repo(session, BIZ_B).list()) == 1


def test_cross_business_update_by_known_id_is_impossible(session):
    row_b = _repo(session, BIZ_B).create(status="running")
    session.commit()
    before = row_b.status

    # A operator holds B's id but can only reach rows through its own scoped repo, which returns
    # None — so there is no row to mutate and B's row is untouched.
    repo_a = _repo(session, BIZ_A)
    target = repo_a.get(row_b.id)
    assert target is None

    session.expire_all()
    assert session.get(DiscoveryRun, row_b.id).status == before  # unchanged


# --- composite tenant uniqueness ------------------------------------------------------------------


def _candidate(repo: ScopedRepository[ProspectCandidate], run_id: str, org_id: str):
    return repo.create(
        run_id=run_id,
        org_name="Synthetic Pvt Ltd",
        org_identifier=org_id,
        source_key="synthetic_in",
        source_ref="synthetic://fixture",
        trust_tier="official_registry",
    )


def test_org_identifier_unique_per_business_not_globally(session):
    run_a = _repo(session, BIZ_A).create(status="running")
    run_b = _repo(session, BIZ_B).create(status="running")
    session.commit()

    cand_a = ScopedRepository(session, _ctx(BIZ_A), ProspectCandidate)
    cand_b = ScopedRepository(session, _ctx(BIZ_B), ProspectCandidate)

    # Same external identifier in two different businesses: allowed (composite constraint includes
    # business_id), neither constrains the other.
    _candidate(cand_a, run_a.id, "U74999KA2020PTC000001")
    _candidate(cand_b, run_b.id, "U74999KA2020PTC000001")
    session.commit()
    assert len(cand_a.list()) == 1
    assert len(cand_b.list()) == 1


def test_duplicate_org_identifier_within_a_business_is_rejected(session):
    run_a = _repo(session, BIZ_A).create(status="running")
    session.commit()
    cand_a = ScopedRepository(session, _ctx(BIZ_A), ProspectCandidate)
    _candidate(cand_a, run_a.id, "U74999KA2020PTC000001")
    session.commit()
    _candidate(cand_a, run_a.id, "U74999KA2020PTC000001")
    with pytest.raises(IntegrityError):
        session.commit()
