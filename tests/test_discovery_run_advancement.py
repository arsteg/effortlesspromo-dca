"""DCA-147 — advance the existing discovery stages (the resumable, checkpoint-driven orchestrator).

Two layers, matching the DCA-142..146 suites:

- **Service unit tests** (in-memory SQLite) pin
  :meth:`~app.services.discovery_run_advancement.DiscoveryRunAdvancer.advance_run`: an authorized
  administrator advancing a run drives ONLY the inert post-extraction tail (convert → resolve → suggest →
  finalize) off the durable checkpoints the earlier stages wrote, settling the run into a terminal status.
  The two acceptance invariants are pinned: a restart **resumes the next incomplete stage** (an already
  converted / resolved / suggested stage is skipped, no duplicate, no re-import) and advancing makes
  **zero provider calls** (the extraction's count is unchanged) with no endless loop; and the relevant
  denials / stops (unauthorized caller, cross-tenant run, a blocked run awaiting plan/execution, a
  cancelled run, the ``target_cap`` suggestion cap) reproduce with no unintended state change.
- **Postgres-backed test** (``DCA_TEST_DATABASE_URL``) runs a stop-after-extraction → restart → advance
  against a disposable PostgreSQL (real transactions, not SQLite): the run settles terminal, the resumed
  advance skips the finished stages and the provider-call count never grows.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import event
from sqlalchemy.engine import make_url

from app.db.base import Base, make_engine, make_session_factory
from app.db.models import (
    BusinessSourceEntitlement,
    DiscoveryRunRequest,
    DiscoveryRunStatus,
    DiscoveryRunSummary,
    DiscoveryTaskConversion,
    DiscoveryTaskStatus,
    ProspectSuggestion,
    ProviderCallRecord,
    TenantAgent,
)
from app.services.discovery_observation import DiscoveryObservationConverter, TaskConversionActor
from app.services.discovery_run import create_discovery_run
from app.services.discovery_run_advancement import (
    REASON_RUN_NOT_FOUND,
    STAGE_AWAITING_EXECUTION,
    STAGE_AWAITING_PLAN,
    STAGE_CANCELLED,
    STAGE_CONVERT,
    STAGE_DONE,
    STAGE_FINALIZE,
    STAGE_RESOLVE,
    STAGE_SUGGEST,
    DiscoveryRunAdvancer,
    RunAdvanceActor,
    RunAdvancementAuthorizationError,
    RunAdvancementError,
)
from app.services.discovery_task_execution import DiscoveryTaskExecutor, TaskExecutionActor

# Reuse the accepted DCA-142/143 scaffolding verbatim, so the run under test is exactly what the accepted
# pipeline produces.
from tests.conftest import make_settings
from tests.test_discovery_observation import (
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

ADMIN = RunAdvanceActor(subject="00000000-0000-4000-9000-0000000a0147a", roles=("owner",))
VIEWER = RunAdvanceActor(subject="00000000-0000-4000-9000-0000000a0147b", roles=("viewer",))
CONVERT_ADMIN = TaskConversionActor(
    subject="00000000-0000-4000-9000-0000000a0147a", roles=("owner",)
)
EXEC_ADMIN = TaskExecutionActor(subject="00000000-0000-4000-9000-0000000a0147a", roles=("owner",))


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def _provider_calls(session, business_id) -> int:
    return session.query(ProviderCallRecord).filter_by(business_id=business_id).count()


# --- a full advance drives the inert tail to a terminal, successful run ---------------------------


def test_advance_drives_full_tail_to_completed_without_provider_calls(session_factory):
    with session_factory() as session:
        run = _executed_run(session, BIZ_A, with_duns=True)  # one DONE task; extraction complete
        session.flush()
        calls_before = _provider_calls(session, BIZ_A)
        assert calls_before == 1  # the one DCA-142 extraction call

        result = DiscoveryRunAdvancer(session, BIZ_A).advance_run(run.id, ADMIN)
        session.flush()

        # It resumed at CONVERT and drove the whole inert tail, settling the run COMPLETED.
        assert result.started_stage == STAGE_CONVERT
        assert result.stages_advanced == [
            STAGE_CONVERT,
            STAGE_RESOLVE,
            STAGE_SUGGEST,
            STAGE_FINALIZE,
        ]
        assert result.final_stage == STAGE_DONE
        assert result.stopped_reason is None
        assert run.status == DiscoveryRunStatus.COMPLETED.value
        assert result.conversions_made == 1
        assert result.suggestions_created == 1
        assert result.resolved is True
        assert result.finalized is True
        assert result.summary is not None
        assert result.summary.final_status == DiscoveryRunStatus.COMPLETED.value
        assert result.summary.suggestion_count == 1

        # The invariant that pins the restart criterion: advancing added NO provider call, no model loop.
        assert result.provider_calls_made == 0
        assert _provider_calls(session, BIZ_A) == calls_before

        # Exactly one of each tail row (idempotent, no duplicate).
        assert session.query(DiscoveryTaskConversion).filter_by(business_id=BIZ_A).count() == 1
        assert session.query(ProspectSuggestion).filter_by(business_id=BIZ_A).count() == 1
        assert session.query(DiscoveryRunSummary).filter_by(business_id=BIZ_A).count() == 1


# --- a restart resumes the NEXT incomplete stage, not the whole chain -----------------------------


def test_restart_resumes_next_incomplete_stage_only(session_factory):
    with session_factory() as session:
        run = _executed_run(session, BIZ_A, with_duns=True)
        session.flush()
        # Simulate a worker that converted the one DONE task and then crashed BEFORE resolving.
        DiscoveryObservationConverter(session, BIZ_A).convert_task(run.id, 0, CONVERT_ADMIN)
        session.flush()
        calls_before = _provider_calls(session, BIZ_A)

        # Restart: advance picks up at RESOLVE — it does NOT re-convert (no duplicate, no re-import).
        result = DiscoveryRunAdvancer(session, BIZ_A).advance_run(run.id, ADMIN)
        session.flush()

        assert result.started_stage == STAGE_RESOLVE
        assert result.stages_advanced == [STAGE_RESOLVE, STAGE_SUGGEST, STAGE_FINALIZE]
        assert result.conversions_made == 0  # the already-done conversion was skipped
        assert run.status == DiscoveryRunStatus.COMPLETED.value
        # Still exactly ONE conversion row — the finished stage was not repeated.
        assert session.query(DiscoveryTaskConversion).filter_by(business_id=BIZ_A).count() == 1
        # And no new provider call was made resuming the tail.
        assert _provider_calls(session, BIZ_A) == calls_before


def test_re_advance_terminal_run_is_idempotent_noop(session_factory):
    with session_factory() as session:
        run = _executed_run(session, BIZ_A, with_duns=True)
        session.flush()

        advancer = DiscoveryRunAdvancer(session, BIZ_A)
        first = advancer.advance_run(run.id, ADMIN)
        session.flush()
        second = advancer.advance_run(run.id, ADMIN)
        session.flush()

        assert first.final_stage == STAGE_DONE
        # A settled run has nothing to advance: the second call does no work at all.
        assert second.started_stage == STAGE_DONE
        assert second.stages_advanced == []
        assert second.suggestions_created == 0
        assert session.query(DiscoveryRunSummary).filter_by(business_id=BIZ_A).count() == 1
        assert session.query(ProspectSuggestion).filter_by(business_id=BIZ_A).count() == 1


# --- a run whose only task was DENIED advances straight to a FAILED finalize ----------------------


def test_advance_all_denied_run_finalizes_failed_without_convert_or_resolve(session_factory):
    with session_factory() as session:
        _seed_confirmed_icp(session, BIZ_A)
        _seed_entitled_source(session, BIZ_A)
        _seed_budget(session, BIZ_A)
        run = _planned_run(session, BIZ_A)
        # Revoke the source so the one task is DENIED at the rights phase (no provider call, nothing staged).
        session.query(BusinessSourceEntitlement).filter_by(business_id=BIZ_A).delete()
        session.flush()
        denied = DiscoveryTaskExecutor(session, BIZ_A).execute_task(
            run.id, 0, EXEC_ADMIN, adapter_input=_adapter_input()
        )
        session.flush()
        assert denied.status == DiscoveryTaskStatus.DENIED.value
        assert _provider_calls(session, BIZ_A) == 0  # a denial dispatches no provider call

        result = DiscoveryRunAdvancer(session, BIZ_A).advance_run(run.id, ADMIN)
        session.flush()

        # No DONE task → nothing to convert / resolve / suggest; it advances straight to FINALIZE → FAILED.
        assert result.started_stage == STAGE_FINALIZE
        assert result.stages_advanced == [STAGE_FINALIZE]
        assert result.conversions_made == 0
        assert result.suggestions_created == 0
        assert run.status == DiscoveryRunStatus.FAILED.value
        assert result.summary is not None and result.summary.empty is False
        assert _provider_calls(session, BIZ_A) == 0


# --- blocked: a run still needing an input-driven stage is advanced no further --------------------


def test_advance_run_awaiting_plan_is_blocked_no_state_change(session_factory):
    with session_factory() as session:
        _seed_confirmed_icp(session, BIZ_A)
        session.add(TenantAgent(business_id=BIZ_A, country="IN", enabled=True))
        session.flush()
        run = create_discovery_run(session, make_settings(), BIZ_A).run  # PENDING, no plan
        assert run is not None
        session.flush()

        result = DiscoveryRunAdvancer(session, BIZ_A).advance_run(run.id, ADMIN)
        session.flush()

        # The advancer cannot approve a plan — it reports the block and changes nothing.
        assert result.started_stage == STAGE_AWAITING_PLAN
        assert result.final_stage == STAGE_AWAITING_PLAN
        assert result.stopped_reason == STAGE_AWAITING_PLAN
        assert result.stages_advanced == []
        assert run.status == DiscoveryRunStatus.PENDING.value
        assert session.query(DiscoveryRunSummary).count() == 0


def test_advance_run_awaiting_execution_is_blocked_no_state_change(session_factory):
    with session_factory() as session:
        _seed_confirmed_icp(session, BIZ_A)
        _seed_entitled_source(session, BIZ_A)
        _seed_budget(session, BIZ_A)
        run = _planned_run(session, BIZ_A)  # PLANNED, but NO task executed (extraction not started)
        session.flush()

        result = DiscoveryRunAdvancer(session, BIZ_A).advance_run(run.id, ADMIN)
        session.flush()

        # The advancer cannot execute a task (no provider access, no fixture input) — it blocks.
        assert result.started_stage == STAGE_AWAITING_EXECUTION
        assert result.stopped_reason == STAGE_AWAITING_EXECUTION
        assert result.stages_advanced == []
        assert run.status == DiscoveryRunStatus.PLANNED.value
        assert session.query(DiscoveryRunSummary).count() == 0
        assert _provider_calls(session, BIZ_A) == 0


# --- cancellation stops the orchestrator cold -----------------------------------------------------


def test_advance_cancelled_run_stops_and_does_not_finalize(session_factory):
    with session_factory() as session:
        run = _executed_run(session, BIZ_A, with_duns=True)
        session.flush()
        # The run is cancelled (a withdrawal) before the tail is driven.
        run.status = DiscoveryRunStatus.CANCELLED.value
        session.flush()

        result = DiscoveryRunAdvancer(session, BIZ_A).advance_run(run.id, ADMIN)
        session.flush()

        assert result.started_stage == STAGE_CANCELLED
        assert result.final_stage == STAGE_CANCELLED
        assert result.stopped_reason == STAGE_CANCELLED
        assert result.stages_advanced == []
        # A cancelled run is NOT finalized and nothing is converted / resolved / suggested.
        assert run.status == DiscoveryRunStatus.CANCELLED.value
        assert session.query(DiscoveryRunSummary).count() == 0
        assert session.query(DiscoveryTaskConversion).count() == 0


# --- the target_cap bounds suggesting; the run still finalizes ------------------------------------


def test_advance_stops_suggesting_at_target_cap(session_factory):
    with session_factory() as session:
        run, _subject = _resolved_run(
            session, BIZ_A
        )  # convert + resolve already done; 1 batch subject
        # Clamp the run's suggestion ceiling to 0 so the cap is reached with a non-empty eligible batch.
        run.target_cap = 0
        session.flush()

        result = DiscoveryRunAdvancer(session, BIZ_A).advance_run(run.id, ADMIN)
        session.flush()

        # Suggesting is capped out (0 created) but the run still finalizes honestly.
        assert result.started_stage == STAGE_FINALIZE
        assert result.suggestions_created == 0
        assert result.cap_reached is True
        assert run.status == DiscoveryRunStatus.COMPLETED.value
        assert result.summary is not None and result.summary.suggestion_count == 0
        assert session.query(ProspectSuggestion).filter_by(business_id=BIZ_A).count() == 0


# --- progress() is a pure, read-only snapshot of the next-incomplete stage ------------------------


def test_progress_reports_next_incomplete_stage(session_factory):
    with session_factory() as session:
        run = _executed_run(session, BIZ_A, with_duns=True)
        session.flush()

        advancer = DiscoveryRunAdvancer(session, BIZ_A)
        progress = advancer.progress(run.id)

        assert progress.next_stage == STAGE_CONVERT
        assert progress.extraction_complete is True
        assert progress.done_task_indices == [0]
        assert progress.converted_task_indices == []
        assert progress.resolved is False
        assert progress.finalized is False
        # A read-only snapshot mutates nothing.
        assert session.query(DiscoveryTaskConversion).count() == 0


# --- refusals (fail-closed, no state change) ------------------------------------------------------


def test_unauthorized_actor_refused(session_factory):
    with session_factory() as session:
        run = _executed_run(session, BIZ_A, with_duns=True)
        session.flush()

        with pytest.raises(RunAdvancementAuthorizationError):
            DiscoveryRunAdvancer(session, BIZ_A).advance_run(run.id, VIEWER)
        # Nothing advanced and the run is unchanged (still running).
        assert session.query(DiscoveryRunSummary).count() == 0
        assert session.query(DiscoveryTaskConversion).count() == 0
        assert run.status == DiscoveryRunStatus.RUNNING.value


def test_cross_tenant_run_is_not_found(session_factory):
    with session_factory() as session:
        run_a = _executed_run(session, BIZ_A, with_duns=True)
        session.flush()

        # A B-scoped advancer cannot reach A's run — it reads exactly like not-found.
        with pytest.raises(RunAdvancementError) as exc:
            DiscoveryRunAdvancer(session, BIZ_B).advance_run(run_a.id, ADMIN)
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
_PG_SCHEMA = "dca_147_test"


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
def test_pg_stop_after_extraction_then_restart_advances_without_new_provider_calls(
    pg_session_factory,
):
    """On real Postgres: a worker stops after extraction; a restart advances the inert tail to a terminal
    run, resumes at the next-incomplete stage, and the provider-call count never grows."""
    assert make_url(PG_URL).get_backend_name() == "postgresql"
    with pg_session_factory() as session:
        run = _executed_run(session, BIZ_A, with_duns=True)
        session.commit()
        run_id = run.id
        calls_after_extraction = _provider_calls(session, BIZ_A)
        assert calls_after_extraction == 1

    # Restart (a fresh session/worker) — advance the tail off the durable checkpoints.
    with pg_session_factory() as session:
        result = DiscoveryRunAdvancer(session, BIZ_A).advance_run(run_id, ADMIN)
        session.commit()
        assert result.started_stage == STAGE_CONVERT
        assert result.stages_advanced == [
            STAGE_CONVERT,
            STAGE_RESOLVE,
            STAGE_SUGGEST,
            STAGE_FINALIZE,
        ]
        assert result.provider_calls_made == 0

    with pg_session_factory() as session:
        run = session.get(DiscoveryRunRequest, run_id)
        assert run.status == DiscoveryRunStatus.COMPLETED.value
        assert session.query(DiscoveryRunSummary).filter_by(run_id=run_id).count() == 1
        # No provider call was added advancing the tail.
        assert _provider_calls(session, BIZ_A) == 1
        # A second restart is a no-op — the run is already settled.
        again = DiscoveryRunAdvancer(session, BIZ_A).advance_run(run_id, ADMIN)
        session.commit()
        assert again.started_stage == STAGE_DONE
        assert again.stages_advanced == []
