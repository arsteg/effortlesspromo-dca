"""DCA-025 — tenant-isolation regression matrix.

One reusable authorization matrix over the currently implemented tenant-owned DCA resources,
asserting known-ID read, update and list behaviour never crosses a business boundary. It is
sensitive by construction: it detects the deliberate removal of a tenant filter (a "leaky"
repository fails the same assertions), and it verifies an unresolved scope reads nothing (fail
closed). The HTTP section covers the candidate service resource's known-ID update isolation.

No product routes are added — this is a regression suite only.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.db.models  # noqa: F401 — register tables on Base.metadata
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import (
    AcquisitionDecision,
    AiInvocation,
    DiscoveryRun,
    ProspectCandidate,
)
from app.db.tenancy import ScopedRepository
from app.identity.fake import BUSINESS_A, BUSINESS_B
from app.identity.tenant_context import TenantContext
from app.main import create_app
from tests.conftest import AUTH, make_settings


def _ctx(business_id: str) -> TenantContext:
    return TenantContext(
        subject="00000000-0000-4000-9000-0000000a11ce",
        business_id=business_id,
        roles=("owner",),
        membership_version="fake-identity-v1",
    )


# --- seeds: one valid row per resource, in the given business scope -------------------------------


def _seed_run(session, business_id):
    return ScopedRepository(session, _ctx(business_id), DiscoveryRun).create(status="running")


def _seed_candidate(session, business_id):
    run = ScopedRepository(session, _ctx(business_id), DiscoveryRun).create(status="running")
    session.flush()
    return ScopedRepository(session, _ctx(business_id), ProspectCandidate).create(
        run_id=run.id,
        org_name="Synthetic Pvt Ltd",
        org_identifier=f"CAND-{uuid.uuid4().hex[:10]}",
        source_key="synthetic_in",
        source_ref="synthetic://fixture",
        trust_tier="official_registry",
    )


def _seed_decision(session, business_id):
    run = ScopedRepository(session, _ctx(business_id), DiscoveryRun).create(status="running")
    session.flush()
    return ScopedRepository(session, _ctx(business_id), AcquisitionDecision).create(
        run_id=run.id,
        org_identifier=f"ORG-{uuid.uuid4().hex[:8]}",
        decision="suggested",
        reason_code="ok",
    )


def _seed_ai(session, business_id):
    return ScopedRepository(session, _ctx(business_id), AiInvocation).create(
        job_id=f"dca:{uuid.uuid4().hex[:8]}",
        request_id=f"req-{uuid.uuid4().hex}",
        capability="scoring",
    )


# The matrix: every tenant-owned resource + a seed that creates one valid row.
MATRIX: list[tuple[str, type, Callable]] = [
    ("discovery_runs", DiscoveryRun, _seed_run),
    ("prospect_candidates", ProspectCandidate, _seed_candidate),
    ("acquisition_decisions", AcquisitionDecision, _seed_decision),
    ("ai_invocations", AiInvocation, _seed_ai),
]
_IDS = [row[0] for row in MATRIX]


def assert_resource_isolated(session, model, seed, *, repo_cls=ScopedRepository) -> None:
    """The reusable assertion: A and B each own one row; neither can read/update/list the other's.

    ``repo_cls`` is injectable so the same assertions can be run against a deliberately-leaky
    repository — exactly how the matrix proves it would catch a removed tenant filter.
    """
    a = seed(session, BUSINESS_A)
    b = seed(session, BUSINESS_B)
    session.commit()

    repo_a = repo_cls(session, _ctx(BUSINESS_A), model)
    repo_b = repo_cls(session, _ctx(BUSINESS_B), model)

    # read by known id
    assert repo_a.get(a.id) is not None, "A must read its own row"
    assert repo_a.get(b.id) is None, "A must NOT read B's row by id"
    assert repo_b.get(a.id) is None, "B must NOT read A's row by id"

    # list (exact-set equality is what detects a dropped tenant filter)
    assert {r.id for r in repo_a.list()} == {a.id}, "A's list must contain exactly A's row"
    assert {r.id for r in repo_b.list()} == {b.id}, "B's list must contain exactly B's row"

    # update by known id: A has no handle on B's row, and B's row is unchanged
    assert repo_a.get(b.id) is None, "A must NOT obtain B's row to update it"
    raw_b = session.get(model, b.id)
    assert raw_b is not None and raw_b.business_id == BUSINESS_B


class _LeakyRepository(ScopedRepository):
    """A repository with the tenant filter REMOVED — the regression the matrix must catch."""

    def select(self):  # type: ignore[override]
        return select(self._model)  # BUG: no business_id filter

    def get(self, entity_id):  # type: ignore[override]
        return self._session.get(self._model, entity_id)  # BUG: no ownership check


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


# --- the matrix runs green for the real repository -----------------------------------------------


@pytest.mark.parametrize("name,model,seed", MATRIX, ids=_IDS)
def test_resource_is_tenant_isolated(session, name, model, seed):
    assert_resource_isolated(session, model, seed)


# --- ...and the same matrix FAILS against a repository with the filter removed --------------------


@pytest.mark.parametrize("name,model,seed", MATRIX, ids=_IDS)
def test_matrix_detects_removed_tenant_filter(session, name, model, seed):
    with pytest.raises(AssertionError):
        assert_resource_isolated(session, model, seed, repo_cls=_LeakyRepository)


# --- unresolved scope fails closed ----------------------------------------------------------------


def test_unresolved_scope_reads_nothing(session):
    a = _seed_run(session, BUSINESS_A)
    b = _seed_run(session, BUSINESS_B)
    session.commit()
    # A scope that resolves to a business owning nothing yields nothing — never another tenant's
    # rows. (At the DB layer, DCA-018 RLS additionally returns zero rows when the tenant GUC is
    # unset — see tests/test_rls.py.)
    unknown = ScopedRepository(session, _ctx("unresolved-scope-00000000"), DiscoveryRun)
    assert unknown.list() == []
    assert unknown.get(a.id) is None
    assert unknown.get(b.id) is None


# --- HTTP service resource: candidate known-ID update isolation -----------------------------------

HA = "11111111-1111-1111-1111-111111111111"
HB = "22222222-2222-2222-2222-222222222222"


def _setup_with_candidates(client: TestClient, business_id: str) -> None:
    client.post("/v1/tenants", json={"businessId": business_id}, headers=AUTH)
    client.post(f"/v1/tenants/{business_id}/enable", headers=AUTH)
    client.put(
        f"/v1/tenants/{business_id}/icp",
        json={"industries": [], "regions": [], "roles": []},
        headers=AUTH,
    )
    client.post(f"/v1/tenants/{business_id}/discovery/run", headers=AUTH)


def test_http_candidate_known_id_isolation():
    # Feature on via conftest; entitlement enforcement off by default, so any business may run.
    with TestClient(create_app(make_settings())) as client:
        _setup_with_candidates(client, HA)
        _setup_with_candidates(client, HB)
        a_cand = client.get(f"/v1/tenants/{HA}/candidates", headers=AUTH).json()[0]

        # known-ID update across tenants is a 404 (never a cross-tenant mutation)
        assert (
            client.post(
                f"/v1/tenants/{HB}/candidates/{a_cand['id']}/accept", headers=AUTH
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"/v1/tenants/{HB}/candidates/{a_cand['id']}/dismiss", headers=AUTH
            ).status_code
            == 404
        )
        # list isolation: B never sees A's candidate
        b_list = client.get(f"/v1/tenants/{HB}/candidates", headers=AUTH).json()
        assert all(c["id"] != a_cand["id"] for c in b_list)
        # A's candidate is unchanged by the denied cross-tenant calls
        a_list = client.get(f"/v1/tenants/{HA}/candidates", headers=AUTH).json()
        assert next(c for c in a_list if c["id"] == a_cand["id"])["status"] == "suggested"
