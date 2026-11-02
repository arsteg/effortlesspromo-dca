"""DCA-146 — finalize a discovery run summary.

Two layers, matching the DCA-142..145 suites:

- **Service unit tests** (in-memory SQLite) pin
  :meth:`~app.services.discovery_run_finalization.DiscoveryRunFinalizer.finalize_run`: an authorized
  administrator finalizing a run AGGREGATES its recorded task outcomes/counts, the held reservation cost,
  the failure/denial reasons, the resolved-batch counts, the suggestion count and a durable checkpoint
  cursor into ONE :class:`~app.db.models.DiscoveryRunSummary`, and settles the run into a terminal status
  with an atomic audit entry. The two acceptance invariants are pinned: partial failure is reported
  honestly (a COMPLETED run that still had denied tasks), and a run with no successful work — all-denied
  or empty — is FAILED, never a fabricated success. Re-finalizing is an idempotent no-op; an unauthorized
  caller is refused before any state is read; a cross-tenant run reads as not-found. Finalizing performs
  no fetch, model call, spend or send.
- **Postgres-backed test** (``DCA_TEST_DATABASE_URL``) runs one finalize against a disposable PostgreSQL
  (real transactions, not SQLite): the summary + its atomic audit entry survive, the run is settled
  terminal, and a re-finalize stays a no-op.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import make_url

from app.db.base import Base, make_engine, make_session_factory
from app.db.models import (
    AuditEvent,
    BusinessSourceEntitlement,
    DiscoveryPlan,
    DiscoveryRunRequest,
    DiscoveryRunStatus,
    DiscoveryRunSummary,
    DiscoveryTaskStatus,
    TenantAgent,
)
from app.services.discovery_run import create_discovery_run
from app.services.discovery_run_finalization import (
    AUDIT_ACTION_FINALIZE_RUN,
    REASON_RUN_NOT_FOUND,
    DiscoveryRunFinalizer,
    RunFinalizationActor,
    RunFinalizationAuthorizationError,
    RunFinalizationError,
)
from app.services.discovery_task_execution import (
    PER_QUERY_COST_MINOR,
    DiscoveryTaskExecutor,
    TaskExecutionActor,
)
from app.services.prospect_suggestion import ProspectSuggestionActor, ProspectSuggestionCreator
from app.services.run_plan_approval import RunPlanApprover, approve_run_plan

# Reuse the accepted DCA-142/143/144/145 scaffolding verbatim, so the finalized run is exactly what the
# accepted pipeline produces.
from tests.conftest import make_settings
from tests.test_discovery_observation import (
    ALLOWED_KEY,
    BIZ_A,
    BIZ_B,
    _adapter_input,
    _executed_run,
    _planned_run,
    _seed_budget,
    _seed_confirmed_icp,
    _seed_entitled_source,
)
from tests.test_prospect_suggestion import _resolved_run

ADMIN = RunFinalizationActor(subject="00000000-0000-4000-9000-0000000a0146a", roles=("owner",))
VIEWER = RunFinalizationActor(subject="00000000-0000-4000-9000-0000000a0146b", roles=("viewer",))
EXEC_ADMIN = TaskExecutionActor(subject="00000000-0000-4000-9000-0000000a0146a", roles=("owner",))
SUGGEST_ADMIN = ProspectSuggestionActor(
    subject="00000000-0000-4000-9000-0000000a0146a", roles=("owner",)
)

# The per-task estimate the executor reserves (maxQueries * PER_QUERY_COST_MINOR); the DCA-143 fixture
# task has maxQueries=2, so one DONE task holds exactly this much.
_HELD_PER_TASK = 2 * PER_QUERY_COST_MINOR


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def _planned_run_two_tasks(session, business_id) -> DiscoveryRunRequest:
    """A run advanced DCA-140 → DCA-141 PLANNED with TWO approved, frozen tasks (both on ``ALLOWED_KEY``)
    — the fixture for the partial-failure case (task 0 succeeds, task 1 is denied)."""
    existing = session.scalar(select(TenantAgent).where(TenantAgent.business_id == business_id))
    if existing is None:
        session.add(TenantAgent(business_id=business_id, country="IN", enabled=True))
        session.flush()
    run = create_discovery_run(session, make_settings(), business_id).run
    assert run is not None
    task = {
        "sourceKey": ALLOWED_KEY,
        "fields": ["org_name"],
        "filters": {"geography": ["KA"]},
        "maxQueries": 2,
    }
    session.add(
        DiscoveryPlan(
            business_id=business_id,
            version=1,
            icp_version=run.icp_version,
            status="draft",
            tasks=[dict(task), dict(task)],
            excluded=[],
            task_cap=5,
            query_cap=3,
            rationale="reviewed discovery plan",
            overall_confidence=0.8,
            source="discovery_plan",
            source_version="pv-1",
            provenance={},
        )
    )
    session.flush()
    approve_run_plan(
        session, business_id, run.id, 1, RunPlanApprover(subject="appr", roles=("owner",))
    )
    session.flush()
    return run


# --- a completed run aggregates its outcomes into a durable summary -------------------------------


def test_finalize_completed_run_aggregates_outcomes(session_factory):
    with session_factory() as session:
        run = _executed_run(session, BIZ_A, with_duns=True)  # one DONE task
        session.flush()

        result = DiscoveryRunFinalizer(session, BIZ_A).finalize_run(run.id, ADMIN)
        session.flush()

        # Settled as a genuine success: 1 DONE task, nothing failed/denied, not partial, not empty.
        assert result.already_finalized is False
        assert result.final_status == DiscoveryRunStatus.COMPLETED.value
        assert result.succeeded is True
        assert (result.task_count, result.done_count, result.failed_count, result.denied_count) == (
            1,
            1,
            0,
            0,
        )
        assert result.partial_failure is False
        assert result.empty is False
        # The held cost mirrors the one task's reservation (a HOLD, never a spend).
        assert result.reserved_cost_minor == _HELD_PER_TASK

        summary = result.summary
        assert summary.final_status == DiscoveryRunStatus.COMPLETED.value
        assert summary.per_run_budget_minor == run.per_run_budget_minor
        assert summary.reason_counts == {}
        # The cursor is a durable checkpoint of opaque row refs + counts — never contact data.
        assert summary.cursor["executedTaskIndices"] == [0]
        assert summary.cursor["lastTaskIndex"] == 0
        assert summary.cursor["importedCount"] == 1
        assert summary.cursor["importedRowRefs"] == ["le-1"]
        assert summary.cursor["resolved"] is False  # this run was never resolved
        # Invariants recorded and the run settled terminal.
        assert summary.provenance["finalized"] is True
        assert summary.provenance["sent"] is False
        assert summary.provenance["spent"] is False
        assert summary.provenance["fromStatus"] == DiscoveryRunStatus.RUNNING.value
        assert run.status == DiscoveryRunStatus.COMPLETED.value
        assert run.provenance["finalized"] is True

        # Exactly one atomic finalize audit entry, references echo the honest counts.
        events = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.business_id == BIZ_A)
            .where(AuditEvent.action == AUDIT_ACTION_FINALIZE_RUN)
        ).all()
        assert len(events) == 1
        assert events[0].references["finalStatus"] == "completed"
        assert events[0].references["doneCount"] == 1


# --- a resolved run reports its matches/suggestions honestly (no matches != fabricated success) ---


def test_finalize_resolved_run_reports_matches_and_suggestions(session_factory):
    with session_factory() as session:
        run, subject = _resolved_run(session, BIZ_A)
        ProspectSuggestionCreator(session, BIZ_A).suggest(run.id, subject.id, SUGGEST_ADMIN)
        session.flush()

        result = DiscoveryRunFinalizer(session, BIZ_A).finalize_run(run.id, ADMIN)
        session.flush()

        # The run did successful work (DONE task), so it COMPLETED — but it matched NOTHING existing
        # (distinct), recorded honestly as matched_count 0 rather than fabricated as a "match".
        assert result.final_status == DiscoveryRunStatus.COMPLETED.value
        assert result.subject_count == 1
        assert result.matched_count == 0
        assert result.suggestion_count == 1
        summary = result.summary
        assert summary.cursor["resolved"] is True
        assert summary.cursor["resolution"]["subjectCount"] == 1
        assert summary.cursor["resolution"]["matchedCount"] == 0
        assert summary.cursor["resolution"]["distinctCount"] == 1


# --- a run whose tasks were ALL denied is FAILED, never a fabricated success ----------------------


def test_finalize_all_denied_run_is_failed(session_factory):
    with session_factory() as session:
        _seed_confirmed_icp(session, BIZ_A)
        _seed_entitled_source(session, BIZ_A)
        _seed_budget(session, BIZ_A)
        run = _planned_run(session, BIZ_A)
        # Revoke the source so the one task is DENIED at the rights phase (nothing imported, run unchanged).
        session.query(BusinessSourceEntitlement).filter_by(business_id=BIZ_A).delete()
        session.flush()
        denied = DiscoveryTaskExecutor(session, BIZ_A).execute_task(
            run.id, 0, EXEC_ADMIN, adapter_input=_adapter_input()
        )
        session.flush()
        assert denied.status == DiscoveryTaskStatus.DENIED.value

        result = DiscoveryRunFinalizer(session, BIZ_A).finalize_run(run.id, ADMIN)
        session.flush()

        # No DONE task → FAILED (honest: a run with no successful work is not a success).
        assert result.final_status == DiscoveryRunStatus.FAILED.value
        assert result.succeeded is False
        assert (result.done_count, result.denied_count, result.task_count) == (0, 1, 1)
        assert result.empty is False
        assert result.partial_failure is False
        # The denial reason is named in the breakdown (one non-DONE task → one reason counted).
        assert sum(result.reason_counts.values()) == 1
        assert run.status == DiscoveryRunStatus.FAILED.value


# --- an EMPTY run (nothing executed) is FAILED, never a fabricated success ------------------------


def test_finalize_empty_run_is_failed_not_fabricated_success(session_factory):
    with session_factory() as session:
        _seed_confirmed_icp(session, BIZ_A)
        _seed_entitled_source(session, BIZ_A)
        _seed_budget(session, BIZ_A)
        run = _planned_run(session, BIZ_A)  # planned, but NO task was ever executed
        session.flush()

        result = DiscoveryRunFinalizer(session, BIZ_A).finalize_run(run.id, ADMIN)
        session.flush()

        # A run with no matches / no work is NOT fabricated success.
        assert result.final_status == DiscoveryRunStatus.FAILED.value
        assert result.empty is True
        assert result.task_count == 0
        assert result.reason_counts == {}
        assert result.summary.cursor["executedTaskIndices"] == []
        assert result.summary.cursor["lastTaskIndex"] is None
        assert run.status == DiscoveryRunStatus.FAILED.value


# --- partial failure is reported honestly ---------------------------------------------------------


def test_finalize_partial_failure_reported_honestly(session_factory):
    with session_factory() as session:
        _seed_confirmed_icp(session, BIZ_A)
        _seed_entitled_source(session, BIZ_A)
        _seed_budget(session, BIZ_A)
        run = _planned_run_two_tasks(session, BIZ_A)
        session.flush()

        executor = DiscoveryTaskExecutor(session, BIZ_A)
        done = executor.execute_task(run.id, 0, EXEC_ADMIN, adapter_input=_adapter_input())
        session.flush()
        assert done.status == DiscoveryTaskStatus.DONE.value
        # Revoke the source, then run the SECOND task — it is DENIED at the rights phase.
        session.query(BusinessSourceEntitlement).filter_by(business_id=BIZ_A).delete()
        session.flush()
        denied = executor.execute_task(run.id, 1, EXEC_ADMIN, adapter_input=_adapter_input())
        session.flush()
        assert denied.status == DiscoveryTaskStatus.DENIED.value

        result = DiscoveryRunFinalizer(session, BIZ_A).finalize_run(run.id, ADMIN)
        session.flush()

        # COMPLETED (it did real work) but the partial failure is reported honestly, not hidden.
        assert result.final_status == DiscoveryRunStatus.COMPLETED.value
        assert result.partial_failure is True
        assert (result.task_count, result.done_count, result.denied_count) == (2, 1, 1)
        assert sum(result.reason_counts.values()) == 1
        assert result.summary.provenance["partialFailure"] is True
        assert result.summary.cursor["executedTaskIndices"] == [0, 1]


# --- idempotency: a run finalizes once ------------------------------------------------------------


def test_refinalize_is_idempotent_noop(session_factory):
    with session_factory() as session:
        run = _executed_run(session, BIZ_A, with_duns=True)
        session.flush()

        finalizer = DiscoveryRunFinalizer(session, BIZ_A)
        first = finalizer.finalize_run(run.id, ADMIN)
        session.flush()
        second = finalizer.finalize_run(run.id, ADMIN)
        session.flush()

        assert first.already_finalized is False
        assert second.already_finalized is True
        assert second.summary.id == first.summary.id
        # A run finalizes ONCE: exactly one summary row and one finalize audit entry.
        assert session.query(DiscoveryRunSummary).filter_by(business_id=BIZ_A).count() == 1
        assert session.query(AuditEvent).filter_by(action=AUDIT_ACTION_FINALIZE_RUN).count() == 1


# --- refusals (fail-closed, no state change) ------------------------------------------------------


def test_unauthorized_actor_refused(session_factory):
    with session_factory() as session:
        run = _executed_run(session, BIZ_A, with_duns=True)
        session.flush()

        with pytest.raises(RunFinalizationAuthorizationError):
            DiscoveryRunFinalizer(session, BIZ_A).finalize_run(run.id, VIEWER)
        # Nothing staged and the run is unchanged (still running).
        assert session.query(DiscoveryRunSummary).count() == 0
        assert run.status == DiscoveryRunStatus.RUNNING.value


def test_cross_tenant_run_is_not_found(session_factory):
    with session_factory() as session:
        run_a = _executed_run(session, BIZ_A, with_duns=True)
        session.flush()

        # A B-scoped finalizer cannot reach A's run — it reads exactly like not-found.
        with pytest.raises(RunFinalizationError) as exc:
            DiscoveryRunFinalizer(session, BIZ_B).finalize_run(run_a.id, ADMIN)
        assert exc.value.code == REASON_RUN_NOT_FOUND
        assert session.query(DiscoveryRunSummary).count() == 0
        assert run_a.status == DiscoveryRunStatus.RUNNING.value


# --------------------------------------------------------------------------------------------------
# Postgres-backed test — disposable PostgreSQL (real transactions, not SQLite)
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)
_PG_SCHEMA = "dca_146_test"


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
def test_pg_finalize_settles_run_and_keeps_audit(pg_session_factory):
    """On real Postgres: finalizing one completed run stages a durable summary, settles the run terminal,
    the atomic audit entry survives, and a re-finalize is a no-op — nothing sent or spent."""
    assert make_url(PG_URL).get_backend_name() == "postgresql"
    with pg_session_factory() as session:
        run = _executed_run(session, BIZ_A, with_duns=True)
        session.commit()
        run_id = run.id

    with pg_session_factory() as session:
        result = DiscoveryRunFinalizer(session, BIZ_A).finalize_run(run_id, ADMIN)
        session.commit()
        assert result.final_status == DiscoveryRunStatus.COMPLETED.value
        assert result.done_count == 1

    with pg_session_factory() as session:
        run = session.get(DiscoveryRunRequest, run_id)
        assert run.status == "completed"
        summary = session.scalars(
            select(DiscoveryRunSummary).where(DiscoveryRunSummary.run_id == run_id)
        ).one()
        assert summary.done_count == 1
        assert summary.reserved_cost_minor == _HELD_PER_TASK
        assert (
            session.scalars(
                select(AuditEvent)
                .where(AuditEvent.business_id == BIZ_A)
                .where(AuditEvent.action == AUDIT_ACTION_FINALIZE_RUN)
            ).all()
            != []
        )
        # Re-finalize is an idempotent no-op — still exactly one summary.
        again = DiscoveryRunFinalizer(session, BIZ_A).finalize_run(run_id, ADMIN)
        session.commit()
        assert again.already_finalized is True
        assert (
            len(
                session.scalars(
                    select(DiscoveryRunSummary).where(DiscoveryRunSummary.run_id == run_id)
                ).all()
            )
            == 1
        )
