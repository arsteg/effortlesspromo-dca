"""DCA-142 — execute ONE planned source task of a discovery run.

Two layers, matching the DCA-140 / DCA-141 suites:

- **Service unit tests** (in-memory SQLite) pin
  :meth:`~app.services.discovery_task_execution.DiscoveryTaskExecutor.execute_task`: an authorized
  administrator executing an APPROVED, frozen task runs the DCA-123 adapter deterministically against a
  synthetic fixture, stages the raw result (the checkpoint) and advances the run ``planned`` →
  ``running`` with an atomic audit entry; executing performs no real fetch and sends nothing. The
  relevant DENIALS — a revoked source (rights) and an exhausted / absent budget — each run NOTHING
  (the adapter is never invoked, no provider call is dispatched) and the run is unchanged; re-executing
  a task is an idempotent no-op; only approved tasks run; a cross-tenant run reads as not-found.
- **Postgres-backed test** (``DCA_TEST_DATABASE_URL``) runs one execution against a disposable
  PostgreSQL (real transactions, not SQLite): the task stages a result, advances the run, and the audit
  entry survives.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import make_url

from app.db.base import Base, make_engine, make_session_factory
from app.db.models import (
    AuditEvent,
    BudgetLimit,
    BudgetReservation,
    BusinessSourceEntitlement,
    DiscoveryRunRequest,
    DiscoveryRunStatus,
    DiscoveryTaskExecution,
    DiscoveryTaskStatus,
    IcpConfirmation,
    IcpDraft,
    OrgObservation,
    ProviderCallRecord,
    ReservationStatus,
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
from app.identity.fake import BUSINESS_A, BUSINESS_B
from app.services.discovery_planner import PLAN_PURPOSE
from app.services.discovery_run import create_discovery_run
from app.services.discovery_task_execution import (
    AUDIT_ACTION_EXECUTE_TASK,
    PER_QUERY_COST_MINOR,
    PHASE_BUDGET,
    PHASE_RIGHTS,
    REASON_RUN_NOT_FOUND,
    REASON_SOURCE_MISMATCH,
    REASON_TASK_NOT_FOUND,
    DiscoveryTaskExecutor,
    OrgDatasetTaskInput,
    TaskExecutionActor,
    TaskExecutionAuthorizationError,
    TaskExecutionError,
)
from app.services.run_plan_approval import RunPlanApprover, approve_run_plan
from app.sources.org_dataset_import import ADAPTER_KEY as ORG_DATASET_ADAPTER_KEY
from app.sources.org_dataset_import import DatasetManifest, DatasetRow
from app.services.org_observation import ExternalIdInput
from tests.conftest import make_settings

BIZ_A = BUSINESS_A
BIZ_B = BUSINESS_B

ADMIN = TaskExecutionActor(subject="00000000-0000-4000-9000-0000000a0142a", roles=("owner",))
VIEWER = TaskExecutionActor(subject="00000000-0000-4000-9000-0000000a0142b", roles=("viewer",))

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
            access_method=SourceAccessMethod.BULK_DOWNLOAD.value,
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


def _seed_budget(session, business_id, *, per_run=1_000_000) -> None:
    """A generous (or, with a tiny ``per_run``, deliberately exhausted) in-force budget cap set."""
    session.add(
        BudgetLimit(
            business_id=business_id,
            version=1,
            currency="INR",
            daily_cap_minor=10_000_000,
            monthly_cap_minor=100_000_000,
            per_run_cap_minor=per_run,
            timezone="Asia/Kolkata",
            actor_id="seed",
            actor_type="user",
        )
    )


def _planned_run(session, business_id) -> DiscoveryRunRequest:
    """A run advanced DCA-140 → DCA-141: PLANNED with one approved, frozen task on ``ALLOWED_KEY``."""
    existing = session.scalar(select(TenantAgent).where(TenantAgent.business_id == business_id))
    if existing is None:
        session.add(TenantAgent(business_id=business_id, country="IN", enabled=True))
        session.flush()
    result = create_discovery_run(session, make_settings(), business_id)
    assert result.run is not None
    run = result.run
    from app.db.models import DiscoveryPlan

    plan = DiscoveryPlan(
        business_id=business_id,
        version=1,
        icp_version=run.icp_version,
        status="draft",
        tasks=[
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
    session.flush()
    approve_run_plan(
        session, business_id, run.id, 1, RunPlanApprover(subject="appr", roles=("owner",))
    )
    session.flush()
    assert run.status == DiscoveryRunStatus.PLANNED.value
    return run


def _manifest(**overrides) -> DatasetManifest:
    base = {
        "dataset_id": "mca-2026-q3",
        "source_key": ALLOWED_KEY,
        "source_version": "2026-09-30",
        "license": "data.gov.in-GODL-1.0",
        "purpose": PLAN_PURPOSE,
    }
    base.update(overrides)
    return DatasetManifest(**base)


def _adapter_input(**overrides) -> OrgDatasetTaskInput:
    rows = (
        DatasetRow(
            row_ref="le-1",
            entity_type="legal_entity",
            display_name="Synthetic Textiles Pvt Ltd",
            external_ids=(ExternalIdInput("cin", "U17120MH2026PTC900099"),),
        ),
    )
    return OrgDatasetTaskInput(manifest=_manifest(**overrides), rows=rows)


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


# --- executing one approved task stages a result + advances state ---------------------------------


def test_execute_task_stages_result_and_advances_run(session_factory):
    with session_factory() as session:
        _seed_confirmed_icp(session, BIZ_A)
        _seed_entitled_source(session, BIZ_A)
        _seed_budget(session, BIZ_A)
        run = _planned_run(session, BIZ_A)
        session.flush()

        result = DiscoveryTaskExecutor(session, BIZ_A).execute_task(
            run.id, 0, ADMIN, adapter_input=_adapter_input()
        )
        session.flush()

        # The task ran: a DONE record staged the raw result (checkpoint), nothing sent/charged.
        assert result.status == DiscoveryTaskStatus.DONE.value
        assert result.ran_adapter is True
        assert result.already_executed is False
        ex = result.execution
        assert ex.source_key == ALLOWED_KEY
        assert ex.adapter_key == ORG_DATASET_ADAPTER_KEY
        assert ex.reservation_id is not None
        assert ex.provider_call_id is not None
        assert ex.staged_result["importedCount"] == 1
        assert ex.staged_result["importedRowRefs"] == ["le-1"]
        assert ex.provenance["executed"] is True
        assert ex.provenance["sent"] is False
        assert ex.provenance["spent"] is False
        # The run advanced PLANNED → RUNNING exactly once.
        assert run.status == DiscoveryRunStatus.RUNNING.value
        assert run.provenance["executed"] is True
        # The adapter actually imported the organization observation (the real DCA-123 mechanics ran).
        assert session.query(OrgObservation).count() == 1
        # A budget hold and a provider-call record were created, then resolved.
        assert session.query(BudgetReservation).count() == 1
        call = session.query(ProviderCallRecord).one()
        assert call.status == "succeeded"
        assert call.dispatched is True
        # One atomic execute-task audit entry was recorded.
        events = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.business_id == BIZ_A)
            .where(AuditEvent.action == AUDIT_ACTION_EXECUTE_TASK)
        ).all()
        assert len(events) == 1
        assert events[0].references["status"] == "done"
        assert events[0].references["taskIndex"] == 0


# --- a revoked source stops the task before external access ---------------------------------------


def test_revoked_source_denies_and_runs_nothing(session_factory):
    with session_factory() as session:
        _seed_confirmed_icp(session, BIZ_A)
        _seed_entitled_source(session, BIZ_A)
        _seed_budget(session, BIZ_A)
        run = _planned_run(session, BIZ_A)
        # Revoke the source AFTER the plan was approved: delete the business entitlement so the rights
        # preflight denies at execution time.
        session.query(BusinessSourceEntitlement).filter_by(business_id=BIZ_A).delete()
        session.flush()

        result = DiscoveryTaskExecutor(session, BIZ_A).execute_task(
            run.id, 0, ADMIN, adapter_input=_adapter_input()
        )
        session.flush()

        # DENIED at the rights phase: nothing ran — no adapter, no provider call, no budget hold.
        assert result.status == DiscoveryTaskStatus.DENIED.value
        assert result.ran_adapter is False
        assert result.execution.phase == PHASE_RIGHTS
        assert result.execution.provider_call_id is None
        assert result.execution.reservation_id is None
        assert result.execution.staged_result == {}
        assert result.execution.provenance["executed"] is False
        # The run is unchanged (still PLANNED) and no external access occurred.
        assert run.status == DiscoveryRunStatus.PLANNED.value
        assert session.query(OrgObservation).count() == 0
        assert session.query(ProviderCallRecord).count() == 0
        assert session.query(BudgetReservation).count() == 0


# --- an exhausted budget stops the task before external access ------------------------------------


def test_exhausted_budget_denies_and_runs_nothing(session_factory):
    with session_factory() as session:
        _seed_confirmed_icp(session, BIZ_A)
        _seed_entitled_source(session, BIZ_A)
        # A per-run cap smaller than the task's estimate (2 queries * PER_QUERY_COST_MINOR) → the
        # reservation is refused, so the adapter is never invoked.
        _seed_budget(session, BIZ_A, per_run=PER_QUERY_COST_MINOR - 1)
        run = _planned_run(session, BIZ_A)
        session.flush()

        result = DiscoveryTaskExecutor(session, BIZ_A).execute_task(
            run.id, 0, ADMIN, adapter_input=_adapter_input()
        )
        session.flush()

        assert result.status == DiscoveryTaskStatus.DENIED.value
        assert result.ran_adapter is False
        assert result.execution.phase == PHASE_BUDGET
        assert result.execution.provider_call_id is None
        # The over-budget refusal staged NO hold (the reservation store fails closed).
        assert (
            session.query(BudgetReservation)
            .filter_by(status=ReservationStatus.ACTIVE.value)
            .count()
            == 0
        )
        assert run.status == DiscoveryRunStatus.PLANNED.value
        assert session.query(OrgObservation).count() == 0
        assert session.query(ProviderCallRecord).count() == 0


# --- idempotency: a task runs once ----------------------------------------------------------------


def test_reexecute_is_idempotent_noop(session_factory):
    with session_factory() as session:
        _seed_confirmed_icp(session, BIZ_A)
        _seed_entitled_source(session, BIZ_A)
        _seed_budget(session, BIZ_A)
        run = _planned_run(session, BIZ_A)
        session.flush()

        executor = DiscoveryTaskExecutor(session, BIZ_A)
        first = executor.execute_task(run.id, 0, ADMIN, adapter_input=_adapter_input())
        session.flush()
        second = executor.execute_task(run.id, 0, ADMIN, adapter_input=_adapter_input())
        session.flush()

        assert first.already_executed is False
        assert second.already_executed is True
        assert second.execution.id == first.execution.id
        # A task runs ONCE: exactly one execution record, one org observation, one provider call.
        assert session.query(DiscoveryTaskExecution).count() == 1
        assert session.query(OrgObservation).count() == 1
        assert session.query(ProviderCallRecord).count() == 1
        # Only one execute-task audit entry — the replay added nothing.
        assert session.query(AuditEvent).filter_by(action=AUDIT_ACTION_EXECUTE_TASK).count() == 1


# --- refusals (malformed request, fail-closed) ----------------------------------------------------


def test_unauthorized_actor_refused(session_factory):
    with session_factory() as session:
        _seed_confirmed_icp(session, BIZ_A)
        _seed_entitled_source(session, BIZ_A)
        _seed_budget(session, BIZ_A)
        run = _planned_run(session, BIZ_A)
        session.flush()

        with pytest.raises(TaskExecutionAuthorizationError):
            DiscoveryTaskExecutor(session, BIZ_A).execute_task(
                run.id, 0, VIEWER, adapter_input=_adapter_input()
            )
        assert session.query(DiscoveryTaskExecution).count() == 0
        assert run.status == DiscoveryRunStatus.PLANNED.value


def test_unknown_task_index_is_not_found(session_factory):
    with session_factory() as session:
        _seed_confirmed_icp(session, BIZ_A)
        _seed_entitled_source(session, BIZ_A)
        _seed_budget(session, BIZ_A)
        run = _planned_run(session, BIZ_A)
        session.flush()

        with pytest.raises(TaskExecutionError) as exc:
            DiscoveryTaskExecutor(session, BIZ_A).execute_task(
                run.id, 5, ADMIN, adapter_input=_adapter_input()
            )
        assert exc.value.code == REASON_TASK_NOT_FOUND


def test_adapter_input_must_target_the_tasks_source(session_factory):
    with session_factory() as session:
        _seed_confirmed_icp(session, BIZ_A)
        _seed_entitled_source(session, BIZ_A)
        _seed_budget(session, BIZ_A)
        run = _planned_run(session, BIZ_A)
        session.flush()

        with pytest.raises(TaskExecutionError) as exc:
            DiscoveryTaskExecutor(session, BIZ_A).execute_task(
                run.id, 0, ADMIN, adapter_input=_adapter_input(source_key="other_source")
            )
        assert exc.value.code == REASON_SOURCE_MISMATCH
        assert session.query(DiscoveryTaskExecution).count() == 0


# --- tenant isolation -----------------------------------------------------------------------------


def test_cross_tenant_run_is_not_found(session_factory):
    with session_factory() as session:
        _seed_confirmed_icp(session, BIZ_A)
        _seed_entitled_source(session, BIZ_A)
        _seed_budget(session, BIZ_A)
        run_a = _planned_run(session, BIZ_A)
        session.flush()

        # A B-scoped executor cannot reach A's run — it reads exactly like not-found.
        with pytest.raises(TaskExecutionError) as exc:
            DiscoveryTaskExecutor(session, BIZ_B).execute_task(
                run_a.id, 0, ADMIN, adapter_input=_adapter_input()
            )
        assert exc.value.code == REASON_RUN_NOT_FOUND
        assert run_a.status == DiscoveryRunStatus.PLANNED.value


# --------------------------------------------------------------------------------------------------
# Postgres-backed test — disposable PostgreSQL (real transactions, not SQLite)
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)
_PG_SCHEMA = "dca_142_test"


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
def test_pg_execute_task_stages_result_and_keeps_audit(pg_session_factory):
    """On real Postgres: executing one planned task stages a DONE result, advances the run to running,
    and the atomic audit entry survives — nothing sent."""
    assert make_url(PG_URL).get_backend_name() == "postgresql"
    with pg_session_factory() as session:
        _seed_confirmed_icp(session, BIZ_A)
        _seed_entitled_source(session, BIZ_A)
        _seed_budget(session, BIZ_A)
        run = _planned_run(session, BIZ_A)
        session.commit()
        run_id = run.id

    with pg_session_factory() as session:
        result = DiscoveryTaskExecutor(session, BIZ_A).execute_task(
            run_id, 0, ADMIN, adapter_input=_adapter_input()
        )
        session.commit()
        assert result.status == DiscoveryTaskStatus.DONE.value

    with pg_session_factory() as session:
        run = session.get(DiscoveryRunRequest, run_id)
        assert run.status == "running"
        ex = session.scalars(
            select(DiscoveryTaskExecution).where(DiscoveryTaskExecution.run_id == run_id)
        ).one()
        assert ex.status == "done"
        assert ex.staged_result["importedCount"] == 1
        assert (
            session.scalars(
                select(AuditEvent)
                .where(AuditEvent.business_id == BIZ_A)
                .where(AuditEvent.action == AUDIT_ACTION_EXECUTE_TASK)
            ).all()
            != []
        )
