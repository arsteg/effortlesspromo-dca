"""DCA-141 — persist an approved run plan (bind a reviewed plan to a bounded discovery run).

Three layers, matching the DCA-140 / DCA-084 suites:

- **Service unit tests** (in-memory SQLite) pin :func:`~app.services.run_plan_approval.approve_run_plan`:
  an authorized administrator approving a reviewed DCA-135 plan onto a PENDING run pins the exact plan
  version, freezes its bounded tasks onto the run and moves it PENDING → PLANNED, with an atomic audit
  entry; approving executes nothing (no job, no candidate, no send, no spend); the relevant DENIALS — an
  unauthorized actor, an unknown/cross-tenant run or plan, a non-pending run, an ICP-version mismatch,
  and an OVER-BOUNDS plan — each change nothing; and re-approving the SAME plan is idempotent.
- **HTTP tests** (``TestClient`` with the end-user auth contract wired) prove the real API: an admin of
  the business approves (200 → planned); a viewer is denied (403); an over-bounds plan is 422 and changes
  nothing; a replay with the same Idempotency-Key is idempotent.
- **Postgres-backed test** (``DCA_TEST_DATABASE_URL``) runs the approval against a disposable PostgreSQL
  (real transactions, not SQLite): the run transitions to planned with the pinned plan while the audit
  entry survives, and nothing executed.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import (
    AuditEvent,
    BusinessSourceEntitlement,
    DiscoveryPlan,
    DiscoveryRunRequest,
    DiscoveryRunStatus,
    DurableJobRequest,
    IcpConfirmation,
    IcpDraft,
    ProspectCandidate,
    Source,
    SourceAccessMethod,
    SourceApprovalStatus,
    SourceKind,
    SourceStatus,
    SourceTerms,
    SourceTermsApproval,
    SourceTermsStatus,
    TenantAgent,
)
from app.identity.fake import BUSINESS_A, BUSINESS_B, USER_ALICE, USER_CAROL
from app.main import create_app
from app.services.discovery_planner import PLAN_PURPOSE
from app.services.discovery_run import create_discovery_run
from app.services.run_plan_approval import (
    AUDIT_ACTION_APPROVE_RUN_PLAN,
    REASON_ICP_MISMATCH,
    REASON_NOT_AUTHORIZED,
    REASON_PLAN_NOT_FOUND,
    REASON_PLAN_OVER_BOUNDS,
    REASON_RUN_NOT_FOUND,
    REASON_RUN_NOT_PENDING,
    VIOL_QUERY_CAP_EXCEEDED,
    VIOL_SOURCE_NOT_IN_SCOPE,
    RunPlanApprovalAuthorizationError,
    RunPlanApprovalError,
    RunPlanApprover,
    approve_run_plan,
)
from tests.conftest import make_settings

BIZ_A = BUSINESS_A
BIZ_B = BUSINESS_B

ADMIN = RunPlanApprover(subject="00000000-0000-4000-9000-0000000a0141a", roles=("owner",))
VIEWER = RunPlanApprover(subject="00000000-0000-4000-9000-0000000a0141b", roles=("viewer",))
USER = RunPlanApprover(subject="00000000-0000-4000-9000-0000000a0141c", roles=("user",))

ALLOWED_KEY = "mca_master_data"
ALLOWED_FIELDS = ["org_name", "org_identifier"]


# --- seed helpers ---------------------------------------------------------------------------------


def _seed_confirmed_icp(session, business_id, *, version=1) -> None:
    session.add(
        IcpDraft(
            business_id=business_id,
            version=version,
            industries=["information technology"],
            size_bands=["small"],
            roles=["owner"],
            geography=["KA", "MH"],
            status="draft",
        )
    )
    session.add(
        IcpConfirmation(
            business_id=business_id, version=version, actor_id="seed", actor_type="user"
        )
    )


def _seed_entitled_source(session, business_id, key=ALLOWED_KEY, fields=ALLOWED_FIELDS) -> None:
    """Seed the full rights chain so ``key`` is approved+entitled for COLLECT on ``fields`` (DCA-048)."""
    now = datetime.now(UTC)
    session.add(
        Source(
            key=key,
            name=key,
            kind=SourceKind.OFFICIAL.value,
            trust_tier="official_registry",
            capabilities=["org_discovery"],
            fields=list(fields),
            access_method=SourceAccessMethod.API.value,
            india_coverage={"nationwide": True, "states": []},
            status=SourceStatus.ENABLED.value,
            enabled=True,
            permissions=[],
        )
    )
    session.add(
        SourceTerms(
            source_key=key,
            version=1,
            operation_rights={"collect": True},
            field_rights={},
            purposes=[PLAN_PURPOSE],
            contract_evidence={"reference": "lic-1", "kind": "licence", "uri": ""},
            status=SourceTermsStatus.ACTIVE.value,
        )
    )
    session.add(
        SourceTermsApproval(
            source_key=key,
            terms_version=1,
            reviewer_id="op-1",
            reviewer_type="platform_operator",
            review_evidence={"reference": "rev-1", "kind": "review", "uri": ""},
            reviewed_at=now - timedelta(days=1),
            effective_from=now - timedelta(days=1),
            expires_at=now + timedelta(days=365),
            approved_operation_rights={"collect": True},
            approved_field_rights={},
            status=SourceApprovalStatus.ACTIVE.value,
        )
    )
    session.add(
        BusinessSourceEntitlement(
            business_id=business_id,
            source_key=key,
            version=1,
            enabled=True,
            operation_rights={"collect": True},
            field_rights={},
            purposes=[PLAN_PURPOSE],
            approved_terms_version=1,
            actor_id="seed",
            actor_type="user",
        )
    )


def _draft_plan(session, business_id, *, version=1, icp_version=1, tasks=None) -> DiscoveryPlan:
    """Stage a DCA-135-shaped DRAFT plan (the reviewed proposal that DCA-141 approves onto a run)."""
    plan = DiscoveryPlan(
        business_id=business_id,
        version=version,
        icp_version=icp_version,
        status="draft",
        tasks=tasks
        if tasks is not None
        else [
            {
                "sourceKey": ALLOWED_KEY,
                "fields": ["org_name"],
                "filters": {"geography": ["KA"]},
                "maxQueries": 2,
            }
        ],
        excluded=[],
        task_cap=5,
        query_cap=3,
        rationale="reviewed discovery plan",
        overall_confidence=0.8,
        source="discovery_plan",
        source_version="pv-1",
        provenance={},
    )
    session.add(plan)
    return plan


def _seed_run(session, business_id) -> DiscoveryRunRequest:
    """A bounded PENDING run (DCA-140) grounded in the confirmed ICP + entitled source scope."""
    existing = session.scalar(select(TenantAgent).where(TenantAgent.business_id == business_id))
    if existing is None:
        session.add(TenantAgent(business_id=business_id, country="IN", enabled=True))
        session.flush()
    result = create_discovery_run(session, make_settings(), business_id)
    assert result.run is not None
    return result.run


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


# --- an authorized approval pins the plan + moves the run to planned ------------------------------


def test_approve_pins_plan_and_moves_run_to_planned(session_factory):
    with session_factory() as session:
        _seed_confirmed_icp(session, BIZ_A)
        _seed_entitled_source(session, BIZ_A)
        run = _seed_run(session, BIZ_A)
        plan = _draft_plan(session, BIZ_A)
        session.flush()

        result = approve_run_plan(session, BIZ_A, run.id, plan.version, ADMIN)
        session.flush()

        assert result.already_approved is False
        assert result.plan_version == 1
        # The run advanced PENDING → PLANNED and pinned the exact plan version + frozen tasks.
        assert run.status == DiscoveryRunStatus.PLANNED.value
        assert run.plan_version == 1
        assert run.approved_by == ADMIN.subject
        assert run.approved_at is not None
        assert run.plan_snapshot["planId"] == plan.id
        assert run.plan_snapshot["tasks"] == plan.tasks
        assert run.plan_snapshot["taskCount"] == 1
        # Approving freezes a plan — it still executes NOTHING.
        assert run.provenance["executed"] is False
        assert run.provenance["sent"] is False
        assert run.provenance["spent"] is False
        assert run.plan_snapshot["executed"] is False
        assert session.query(ProspectCandidate).count() == 0
        assert session.query(DurableJobRequest).count() == 0
        # One atomic audit entry recorded the transition.
        events = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.business_id == BIZ_A)
            .where(AuditEvent.action == AUDIT_ACTION_APPROVE_RUN_PLAN)
        ).all()
        assert len(events) == 1
        assert events[0].references["planVersion"] == 1
        assert events[0].references["newStatus"] == "planned"


def test_frozen_snapshot_is_immutable_to_later_plan_edits(session_factory):
    with session_factory() as session:
        _seed_confirmed_icp(session, BIZ_A)
        _seed_entitled_source(session, BIZ_A)
        run = _seed_run(session, BIZ_A)
        plan = _draft_plan(session, BIZ_A)
        session.flush()

        approve_run_plan(session, BIZ_A, run.id, plan.version, ADMIN)
        session.flush()

        # Mutating the plan row's tasks afterwards must not change the run's frozen snapshot (it is a copy).
        plan.tasks[0]["maxQueries"] = 99
        assert run.plan_snapshot["tasks"][0]["maxQueries"] == 2


# --- refusals (fail-closed, no state change) ------------------------------------------------------


def test_unauthorized_actor_refused_changes_nothing(session_factory):
    for actor in (VIEWER, USER):
        with session_factory() as session:
            _seed_confirmed_icp(session, BIZ_A)
            _seed_entitled_source(session, BIZ_A)
            run = _seed_run(session, BIZ_A)
            plan = _draft_plan(session, BIZ_A)
            session.flush()

            with pytest.raises(RunPlanApprovalAuthorizationError) as exc:
                approve_run_plan(session, BIZ_A, run.id, plan.version, actor)
            assert exc.value.code == REASON_NOT_AUTHORIZED
            # The relevant denial changed nothing: the run is still PENDING and no audit was written.
            assert run.status == DiscoveryRunStatus.PENDING.value
            assert run.plan_version is None
            assert (
                session.query(AuditEvent).filter_by(action=AUDIT_ACTION_APPROVE_RUN_PLAN).count()
                == 0
            )


def test_over_bounds_plan_refused_changes_nothing(session_factory):
    with session_factory() as session:
        _seed_confirmed_icp(session, BIZ_A)
        _seed_entitled_source(session, BIZ_A)
        run = _seed_run(session, BIZ_A)
        # An over-bounds plan: an un-scoped source AND a per-task query count above the run's query cap.
        plan = _draft_plan(
            session,
            BIZ_A,
            tasks=[
                {"sourceKey": "gst_registry", "fields": ["org_name"], "maxQueries": 1},
                {"sourceKey": ALLOWED_KEY, "fields": ["org_name"], "maxQueries": 99},
            ],
        )
        session.flush()

        with pytest.raises(RunPlanApprovalError) as exc:
            approve_run_plan(session, BIZ_A, run.id, plan.version, ADMIN)
        assert exc.value.code == REASON_PLAN_OVER_BOUNDS
        reasons = {v["reason"] for v in exc.value.violations}
        assert VIOL_SOURCE_NOT_IN_SCOPE in reasons
        assert VIOL_QUERY_CAP_EXCEEDED in reasons
        # No partial approval: the run is untouched (still PENDING, no plan pinned).
        assert run.status == DiscoveryRunStatus.PENDING.value
        assert run.plan_version is None


def test_icp_mismatch_refused(session_factory):
    with session_factory() as session:
        _seed_confirmed_icp(session, BIZ_A)
        _seed_entitled_source(session, BIZ_A)
        run = _seed_run(session, BIZ_A)  # grounded in ICP v1
        plan = _draft_plan(session, BIZ_A, icp_version=2)  # grounded in a DIFFERENT ICP version
        session.flush()

        with pytest.raises(RunPlanApprovalError) as exc:
            approve_run_plan(session, BIZ_A, run.id, plan.version, ADMIN)
        assert exc.value.code == REASON_ICP_MISMATCH
        assert run.status == DiscoveryRunStatus.PENDING.value


def test_non_pending_run_refused(session_factory):
    with session_factory() as session:
        _seed_confirmed_icp(session, BIZ_A)
        _seed_entitled_source(session, BIZ_A)
        run = _seed_run(session, BIZ_A)
        plan = _draft_plan(session, BIZ_A)
        session.flush()
        run.status = DiscoveryRunStatus.COMPLETED.value  # already settled — nothing to plan
        session.flush()

        with pytest.raises(RunPlanApprovalError) as exc:
            approve_run_plan(session, BIZ_A, run.id, plan.version, ADMIN)
        assert exc.value.code == REASON_RUN_NOT_PENDING


def test_unknown_run_and_plan_are_not_found(session_factory):
    with session_factory() as session:
        _seed_confirmed_icp(session, BIZ_A)
        _seed_entitled_source(session, BIZ_A)
        run = _seed_run(session, BIZ_A)
        _draft_plan(session, BIZ_A)
        session.flush()

        with pytest.raises(RunPlanApprovalError) as exc:
            approve_run_plan(session, BIZ_A, "no-such-run", 1, ADMIN)
        assert exc.value.code == REASON_RUN_NOT_FOUND

        with pytest.raises(RunPlanApprovalError) as exc:
            approve_run_plan(session, BIZ_A, run.id, 999, ADMIN)
        assert exc.value.code == REASON_PLAN_NOT_FOUND


# --- idempotency ----------------------------------------------------------------------------------


def test_reapproval_is_idempotent(session_factory):
    with session_factory() as session:
        _seed_confirmed_icp(session, BIZ_A)
        _seed_entitled_source(session, BIZ_A)
        run = _seed_run(session, BIZ_A)
        plan = _draft_plan(session, BIZ_A)
        session.flush()

        first = approve_run_plan(session, BIZ_A, run.id, plan.version, ADMIN)
        session.flush()
        second = approve_run_plan(session, BIZ_A, run.id, plan.version, ADMIN)
        session.flush()

        assert first.already_approved is False
        assert second.already_approved is True
        assert run.status == DiscoveryRunStatus.PLANNED.value
        # Only ONE transition audit entry — the re-approval added nothing.
        assert (
            session.query(AuditEvent).filter_by(action=AUDIT_ACTION_APPROVE_RUN_PLAN).count() == 1
        )


# --- tenant isolation -----------------------------------------------------------------------------


def test_tenant_scoped_run_is_not_found_cross_tenant(session_factory):
    with session_factory() as session:
        _seed_confirmed_icp(session, BIZ_A)
        _seed_entitled_source(session, BIZ_A)
        run_a = _seed_run(session, BIZ_A)
        _draft_plan(session, BIZ_A)
        session.flush()

        # A B-scoped approver cannot reach A's run — it reads exactly like not-found.
        with pytest.raises(RunPlanApprovalError) as exc:
            approve_run_plan(session, BIZ_B, run_a.id, 1, ADMIN)
        assert exc.value.code == REASON_RUN_NOT_FOUND
        assert run_a.status == DiscoveryRunStatus.PENDING.value


# --------------------------------------------------------------------------------------------------
# HTTP tests (TestClient) — the real service API with the end-user auth contract wired
# --------------------------------------------------------------------------------------------------

ISSUER = "https://issuer.dca.test"
AUDIENCE = "dca-api"
SECRET = "dca-test-signing-secret-value"  # synthetic HS256 key, not a real credential
TEST_TOKEN = "test-service-token"


def _auth_settings(**overrides) -> Settings:
    base = {
        "environment": "testing",
        "database_url": "sqlite+pysqlite:///:memory:",
        "service_auth_token": TEST_TOKEN,
        "synthetic_sources_enabled": True,
        "feature_enabled": True,
        "worker_enabled": False,
        "auth_token_issuer": ISSUER,
        "auth_token_audience": AUDIENCE,
        "auth_token_signing_secret": SECRET,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _mint(*, subject: str) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": subject,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    return jwt.encode(claims, SECRET, algorithm="HS256")


def _headers(subject: str, *, idem_key: str | None = None) -> dict[str, str]:
    headers = {
        "X-Service-Auth": TEST_TOKEN,
        "Authorization": f"Bearer {_mint(subject=subject)}",
    }
    if idem_key is not None:
        headers["Idempotency-Key"] = idem_key
    return headers


@pytest.fixture
def client() -> TestClient:
    app = create_app(_auth_settings())
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _seed_run_and_plan(client: TestClient, *, over_bounds: bool = False) -> str:
    with client.app.state.session_factory() as session:
        _seed_confirmed_icp(session, BIZ_A)
        _seed_entitled_source(session, BIZ_A)
        run = _seed_run(session, BIZ_A)
        tasks = (
            [{"sourceKey": "gst_registry", "fields": ["org_name"], "maxQueries": 1}]
            if over_bounds
            else None
        )
        _draft_plan(session, BIZ_A, tasks=tasks)
        session.commit()
        return run.id


def test_http_admin_approves_plan(client):
    run_id = _seed_run_and_plan(client)
    resp = client.post(
        f"/v1/tenants/{BIZ_A}/discovery/runs/{run_id}/plan",
        json={"planVersion": 1},
        headers=_headers(USER_ALICE),  # Alice: owner of A (administrator)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "planned"
    assert body["planVersion"] == 1
    assert body["executed"] is False
    assert body["planSnapshot"]["taskCount"] == 1


def test_http_viewer_denied(client):
    run_id = _seed_run_and_plan(client)
    resp = client.post(
        f"/v1/tenants/{BIZ_A}/discovery/runs/{run_id}/plan",
        json={"planVersion": 1},
        headers=_headers(USER_CAROL),  # Carol: an active viewer in A, without run.plan.approve
    )
    assert resp.status_code == 403
    with client.app.state.session_factory() as session:
        run = session.get(DiscoveryRunRequest, run_id)
        assert run.status == "pending"  # nothing changed


def test_http_over_bounds_is_422_and_changes_nothing(client):
    run_id = _seed_run_and_plan(client, over_bounds=True)
    resp = client.post(
        f"/v1/tenants/{BIZ_A}/discovery/runs/{run_id}/plan",
        json={"planVersion": 1},
        headers=_headers(USER_ALICE),
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["message"] == REASON_PLAN_OVER_BOUNDS
    with client.app.state.session_factory() as session:
        assert session.get(DiscoveryRunRequest, run_id).status == "pending"


def test_http_idempotent_replay(client):
    run_id = _seed_run_and_plan(client)
    headers = _headers(USER_ALICE, idem_key="approve-key-1")
    first = client.post(
        f"/v1/tenants/{BIZ_A}/discovery/runs/{run_id}/plan",
        json={"planVersion": 1},
        headers=headers,
    )
    assert first.status_code == 200
    second = client.post(
        f"/v1/tenants/{BIZ_A}/discovery/runs/{run_id}/plan",
        json={"planVersion": 1},
        headers=headers,
    )
    assert second.status_code == 200
    assert second.json()["status"] == "planned"


# --------------------------------------------------------------------------------------------------
# Postgres-backed test — disposable PostgreSQL (real transactions, not SQLite)
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)
_PG_SCHEMA = "dca_141_test"


@pytest.fixture
def pg_session_factory():
    """An ORM session factory over a fresh schema in the disposable Postgres (dropped on teardown)."""
    engine = make_engine(PG_URL)

    @event.listens_for(engine, "connect")
    def _set_search_path(dbapi_conn, _record):  # pragma: no cover - exercised only with a PG URL
        cur = dbapi_conn.cursor()
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{_PG_SCHEMA}"')
        cur.execute(f'SET search_path TO "{_PG_SCHEMA}"')
        cur.close()

    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        with engine.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{_PG_SCHEMA}" CASCADE')
        engine.dispose()


@requires_pg
def test_pg_approval_transitions_run_and_keeps_audit(pg_session_factory):
    """On real Postgres: approving a plan moves the run PENDING → PLANNED, pins the plan version, and the
    atomic audit entry survives — nothing executed (no jobs, no candidates)."""
    assert make_url(PG_URL).get_backend_name() == "postgresql"
    with pg_session_factory() as session:
        _seed_confirmed_icp(session, BIZ_A)
        _seed_entitled_source(session, BIZ_A)
        run = _seed_run(session, BIZ_A)
        plan = _draft_plan(session, BIZ_A)
        session.commit()
        run_id, plan_version = run.id, plan.version

    with pg_session_factory() as session:
        approve_run_plan(session, BIZ_A, run_id, plan_version, ADMIN)
        session.commit()

    with pg_session_factory() as session:
        run = session.get(DiscoveryRunRequest, run_id)
        assert run.status == "planned"
        assert run.plan_version == plan_version
        assert run.plan_snapshot["taskCount"] == 1
        assert run.provenance["executed"] is False
        assert (
            session.scalars(
                select(AuditEvent)
                .where(AuditEvent.business_id == BIZ_A)
                .where(AuditEvent.action == AUDIT_ACTION_APPROVE_RUN_PLAN)
            ).all()
            != []
        )
        assert session.query(DurableJobRequest).count() == 0
        assert session.query(ProspectCandidate).count() == 0
