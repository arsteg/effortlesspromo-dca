"""DCA-147 — advance the existing discovery stages (the resumable, checkpoint-driven orchestrator).

This story adds the ONE piece that was missing from the DCA-140..146 pipeline: an orchestrator that
*advances* a run by composing the already-accepted, single-purpose stages — it invents no new stage,
makes no provider call and calls no model. Each earlier story built one inert step that reads the
durable rows the previous step wrote and stages the next durable row:

    DCA-140 run → DCA-141 approve plan → DCA-142 execute task (the extraction, the only provider access)
                → DCA-143 convert to observations → DCA-144 resolve batch → DCA-145 suggest prospects
                → DCA-146 finalize summary (terminal)

:class:`DiscoveryRunAdvancer` drives the **post-extraction tail** of that chain
(convert → resolve → suggest → finalize) off those durable checkpoints. It is deliberately NOT able to
run the two input-driven stages — approving a plan (DCA-141) and executing a task (DCA-142, the only
stage that makes a provider call and needs caller-supplied / synthetic fixture inputs). A run that still
needs either is reported as *blocked* (``awaiting_plan`` / ``awaiting_execution``) and advanced no
further — the orchestrator never fabricates extraction it cannot perform.

The two acceptance criteria hold by construction:

- **Restart resumes the next incomplete stage, not all provider calls; no endless model loop.** The
  orchestrator holds NO state of its own — it recomputes the next-incomplete stage purely from the
  durable rows the stages already wrote (each unique on ``(business_id, run_id[, task_index|subject_id])``
  and idempotent). So a worker that crashes after extraction and restarts picks up at exactly the next
  unfinished stage: an already-converted task is skipped (no duplicate, no re-import), an already-resolved
  batch is not re-resolved, an already-suggested subject is not re-suggested. None of the tail stages
  touches the AI gateway or dispatches a provider call, so advancing a run adds **zero** provider calls
  (the extraction's count is unchanged) and there is no model loop. Every step makes monotonic progress
  (it stages one more durable row), and the loop stops at the first terminal / blocked / cancelled /
  cap-reached state, so it always terminates.

- **Reproduce the result and the relevant denial without unintended state changes.** Authority is checked
  FIRST (the same ``RUN_PLAN_APPROVE`` administrator scope the whole chargeable-discovery chain requires);
  then the run must exist in THIS business (an unknown / cross-tenant run reads as not-found). Each refusal
  is raised BEFORE any stage runs, so it changes nothing. A **cancelled** run and a run **blocked** on an
  input-driven stage are advanced no further and leave the run exactly as it was.

The advancer stages into the *caller's* session and never commits — the route/worker owns the unit of
work, so every stage row it drives plus their atomic DCA-026 audit entries commit together. There is no
argument to widen the business scope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    DiscoveryRunRequest,
    DiscoveryRunStatus,
    DiscoveryRunSummary,
    DiscoveryTaskConversion,
    DiscoveryTaskExecution,
    DiscoveryTaskStatus,
    FieldObservation,
    FieldSubjectType,
    ObservationBatchResolution,
    ProspectSuggestion,
)
from app.identity.permissions import Scope, scopes_for_business_roles
from app.services.discovery_observation import DiscoveryObservationConverter, TaskConversionActor
from app.services.discovery_run import DiscoveryRunStore
from app.services.discovery_run_finalization import DiscoveryRunFinalizer, RunFinalizationActor
from app.services.observation_resolution import BatchResolutionActor, ObservationBatchResolver
from app.services.prospect_suggestion import ProspectSuggestionActor, ProspectSuggestionCreator

# The subject kind the discovery tail works over — organization identities (the converter records
# organization-level facts only; there is no contact in a batch, which is why every suggestion is
# research-only).
SUBJECT_TYPE = FieldSubjectType.ORGANIZATION.value


def _run_provenance_prefix(run_id: str) -> str:
    """How DCA-143 stamps a converted field's provenance: ``discovery_run:{run_id}/task:``. The run's
    batch is exactly the distinct organization subjects whose field observations carry this prefix —
    identical to the DCA-144 / DCA-145 batch definition, so the tail works over precisely what the run
    converted, nothing wider."""
    return f"discovery_run:{run_id}/task:"


# Where an advancement records its origin (kept for parity with the sibling stages' provenance source).
ADVANCEMENT_SOURCE = "discovery_run_advancement"

# The stage labels the orchestrator reasons over. The ADVANCEABLE stages are the inert, no-provider-call
# tail the advancer can drive off durable checkpoints; the BLOCKED stages need an input-driven step the
# advancer deliberately cannot perform; the SETTLED labels are where the loop stops.
STAGE_AWAITING_PLAN = "awaiting_plan"  # PENDING / no frozen plan — needs DCA-141 approval (blocked)
STAGE_AWAITING_EXECUTION = (
    "awaiting_execution"  # planned, extraction incomplete — needs DCA-142 (blocked)
)
STAGE_CONVERT = "convert"  # a DONE task has not been converted (DCA-143)
STAGE_RESOLVE = "resolve"  # the converted batch has not been resolved (DCA-144)
STAGE_SUGGEST = (
    "suggest"  # a resolved, eligible subject has not been suggested (DCA-145), cap permitting
)
STAGE_FINALIZE = "finalize"  # nothing left to advance but the run is not yet settled (DCA-146)
STAGE_DONE = "done"  # the run is finalized / terminal — nothing to do
STAGE_CANCELLED = "cancelled"  # the run was cancelled — advance nothing

# The stages the advancer can actually drive (each inert, no provider call, idempotent).
ADVANCEABLE_STAGES = frozenset({STAGE_CONVERT, STAGE_RESOLVE, STAGE_SUGGEST, STAGE_FINALIZE})
# The stages where the loop stops because it cannot (or must not) advance further.
STOP_STAGES = frozenset(
    {STAGE_AWAITING_PLAN, STAGE_AWAITING_EXECUTION, STAGE_DONE, STAGE_CANCELLED}
)

# A defensive ceiling on advance steps. Each step stages one more durable row, so the loop terminates on
# its own; this only guards against a logic error ever re-choosing a stage without making progress.
MAX_ADVANCE_STEPS = 10_000

# Stable refusal codes raised (never recorded) for a malformed / unauthorized request — each BEFORE any
# stage runs, so the attempt changes nothing.
REASON_NOT_AUTHORIZED = "run_advancement_not_authorized"
REASON_RUN_NOT_FOUND = "discovery_run_not_found"


class RunAdvancementError(Exception):
    """A run advancement was refused with a stable ``code`` for a malformed / ineligible request (an
    unknown or cross-tenant run). Raised before any stage runs, so a refusal changes nothing.
    ``detail`` names the offending value."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class RunAdvancementAuthorizationError(RunAdvancementError):
    """The caller is not authorized to advance a discovery run.

    A distinct type so an HTTP boundary can map it to a 403 while the other code maps to a 404. Raised
    FIRST — before any state is read or written. ``code`` is :data:`REASON_NOT_AUTHORIZED`.
    """

    def __init__(self, message: str) -> None:
        super().__init__(REASON_NOT_AUTHORIZED, message)


@dataclass(frozen=True)
class RunAdvanceActor:
    """The business principal advancing a discovery run.

    ``subject`` is an opaque actor id (never a name/email) recorded on each stage's atomic audit entry.
    ``roles`` are the caller's verified business-plane membership roles (from the tenant context, NEVER a
    bare token claim). :meth:`may_advance` grants the action only when those roles yield
    :attr:`~app.identity.permissions.Scope.RUN_PLAN_APPROVE` — advancing a run drives the same
    chargeable-discovery stages (convert / resolve / suggest / finalize) the whole DCA-141..146 chain
    governs, so it demands the same administrator authority. A read-only viewer and a platform operator
    (holding no business scope) cannot perform it. The one scope is carried through unchanged to every
    sub-stage actor, so no stage can run under weaker authority than the advancer itself was granted.
    """

    subject: str
    roles: tuple[str, ...] = ()
    actor_type: str = "user"

    def may_advance(self) -> bool:
        return Scope.RUN_PLAN_APPROVE in scopes_for_business_roles(self.roles)

    def as_conversion_actor(self) -> TaskConversionActor:
        return TaskConversionActor(
            subject=self.subject, roles=self.roles, actor_type=self.actor_type
        )

    def as_resolution_actor(self) -> BatchResolutionActor:
        return BatchResolutionActor(
            subject=self.subject, roles=self.roles, actor_type=self.actor_type
        )

    def as_suggestion_actor(self) -> ProspectSuggestionActor:
        return ProspectSuggestionActor(
            subject=self.subject, roles=self.roles, actor_type=self.actor_type
        )

    def as_finalization_actor(self) -> RunFinalizationActor:
        return RunFinalizationActor(
            subject=self.subject, roles=self.roles, actor_type=self.actor_type
        )


@dataclass
class RunProgress:
    """A read-only snapshot of a run's position in the pipeline, derived purely from durable checkpoints.

    Every field is recomputed from the stage rows the earlier stories wrote — the advancer keeps no state
    of its own, which is exactly why a restart resumes at :attr:`next_stage` rather than from the top.
    ``next_stage`` is the single next action the advancer would take (one of the stage labels); it is a
    :data:`STOP_STAGES` label when the run is terminal, cancelled or blocked on an input-driven stage.
    """

    run_id: str
    status: str
    plan_task_count: int
    execution_count: int
    done_task_indices: list[int]
    converted_task_indices: list[int]
    batch_subject_ids: list[str]
    resolved: bool
    suggested_subject_ids: list[str]
    finalized: bool
    target_cap: int
    next_stage: str

    @property
    def extraction_complete(self) -> bool:
        """Whether every task frozen into the run's plan has an execution record (DONE/FAILED/DENIED).

        The tail is driven only once extraction is complete, so the advancer never prematurely finalizes a
        run that is still waiting for a DCA-142 task to run."""
        return self.plan_task_count > 0 and self.execution_count >= self.plan_task_count

    @property
    def is_blocked(self) -> bool:
        return self.next_stage in (STAGE_AWAITING_PLAN, STAGE_AWAITING_EXECUTION)

    @property
    def is_terminal(self) -> bool:
        return self.next_stage in (STAGE_DONE, STAGE_CANCELLED)


@dataclass
class RunAdvanceResult:
    """The inert, inspectable outcome of one advance attempt.

    ``started_stage`` is the next-incomplete stage at entry (what a restart resumed at); ``final_stage``
    the :data:`STOP_STAGES` label the loop stopped at. ``stages_advanced`` lists every stage-step actually
    performed, in order (empty when the run was already terminal / blocked / cancelled). ``status`` is the
    run's status after advancing. ``summary`` is the finalized :class:`~app.db.models.DiscoveryRunSummary`
    when the run was settled this call (else ``None``). The remaining fields echo the work done. Advancing
    makes NO provider call — ``provider_calls_made`` is always 0 (the invariant the restart criterion
    pins). Nothing here is ever fetched, sent, spent or auto-accepted.
    """

    business_id: str
    run: DiscoveryRunRequest
    started_stage: str
    final_stage: str
    status: str
    stages_advanced: list[str] = field(default_factory=list)
    conversions_made: int = 0
    suggestions_created: int = 0
    resolved: bool = False
    finalized: bool = False
    cap_reached: bool = False
    stopped_reason: str | None = None
    summary: DiscoveryRunSummary | None = None
    provider_calls_made: int = 0

    @property
    def advanced(self) -> bool:
        """Whether any stage was actually advanced this call (a terminal / blocked run advances nothing)."""
        return bool(self.stages_advanced)


class DiscoveryRunAdvancer:
    """Advance ONE discovery run through its inert, no-provider-call tail, for ONE business (tenant-scoped).

    Construct with the active session and the verified business scope (a route/worker resolves it from the
    membership-verified tenant context). Composes the accepted DCA-143 converter, DCA-144 resolver, DCA-145
    suggestion creator and DCA-146 finalizer over the run's durable checkpoints. Every query is confined to
    this business — there is no argument to widen the scope. The advancer stages into the caller's session
    and never commits; it drives only inert stages and so fetches / sends / spends nothing and makes no
    provider call.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def _run(self, run_id: str) -> DiscoveryRunRequest | None:
        return DiscoveryRunStore(self._session, self._business_id).get(run_id)

    def _executions(self, run_id: str) -> list[DiscoveryTaskExecution]:
        stmt = (
            select(DiscoveryTaskExecution)
            .where(DiscoveryTaskExecution.business_id == self._business_id)
            .where(DiscoveryTaskExecution.run_id == run_id)
            .order_by(DiscoveryTaskExecution.task_index)
        )
        return list(self._session.scalars(stmt).all())

    def _converted_task_indices(self, run_id: str) -> set[int]:
        stmt = (
            select(DiscoveryTaskConversion.task_index)
            .where(DiscoveryTaskConversion.business_id == self._business_id)
            .where(DiscoveryTaskConversion.run_id == run_id)
        )
        return set(self._session.scalars(stmt).all())

    def _batch_subject_ids(self, run_id: str) -> list[str]:
        """The distinct organization subjects of this run's converted field observations, stable order —
        exactly the DCA-144 / DCA-145 batch (provenance-prefixed), tenant-scoped (never cross-tenant)."""
        stmt = (
            select(FieldObservation.subject_id)
            .where(FieldObservation.business_id == self._business_id)
            .where(FieldObservation.subject_type == SUBJECT_TYPE)
            .where(FieldObservation.provenance_ref.like(f"{_run_provenance_prefix(run_id)}%"))
            .distinct()
        )
        ids = list(self._session.scalars(stmt).all())
        ids.sort()
        return ids

    def _resolution(self, run_id: str) -> ObservationBatchResolution | None:
        stmt = (
            select(ObservationBatchResolution)
            .where(ObservationBatchResolution.business_id == self._business_id)
            .where(ObservationBatchResolution.run_id == run_id)
        )
        return self._session.scalars(stmt).one_or_none()

    def _suggested_subject_ids(self, run_id: str) -> set[str]:
        stmt = (
            select(ProspectSuggestion.subject_id)
            .where(ProspectSuggestion.business_id == self._business_id)
            .where(ProspectSuggestion.run_id == run_id)
        )
        return set(self._session.scalars(stmt).all())

    def _summary(self, run_id: str) -> DiscoveryRunSummary | None:
        stmt = (
            select(DiscoveryRunSummary)
            .where(DiscoveryRunSummary.business_id == self._business_id)
            .where(DiscoveryRunSummary.run_id == run_id)
        )
        return self._session.scalars(stmt).one_or_none()

    # -- progress / next-stage (pure, derived from durable checkpoints) -----------------------------

    def progress(self, run_id: str) -> RunProgress:
        """A read-only :class:`RunProgress` snapshot for a run WITHIN this business (an unknown /
        cross-tenant run is refused as not-found). This is the inspection surface a restart uses to
        recompute the next-incomplete stage — it mutates nothing."""
        run = self._run(run_id)
        if run is None:
            raise RunAdvancementError(
                REASON_RUN_NOT_FOUND,
                "no such discovery run in this business (an unknown or cross-tenant id is not-found)",
                detail=run_id,
            )
        return self._snapshot(run)

    def _snapshot(self, run: DiscoveryRunRequest) -> RunProgress:
        run_id = run.id
        plan_tasks = list((run.plan_snapshot or {}).get("tasks") or [])
        executions = self._executions(run_id)
        done_indices = sorted(
            e.task_index for e in executions if e.status == DiscoveryTaskStatus.DONE.value
        )
        converted = self._converted_task_indices(run_id)
        batch = self._batch_subject_ids(run_id)
        resolution = self._resolution(run_id)
        suggested = self._suggested_subject_ids(run_id)
        summary = self._summary(run_id)
        target_cap = max(0, int(run.target_cap or 0))

        next_stage = self._compute_next_stage(
            run_status=run.status,
            plan_task_count=len(plan_tasks),
            execution_count=len(executions),
            done_indices=done_indices,
            converted=converted,
            batch=batch,
            resolved=resolution is not None,
            suggested=suggested,
            finalized=summary is not None,
            target_cap=target_cap,
        )
        return RunProgress(
            run_id=run_id,
            status=run.status,
            plan_task_count=len(plan_tasks),
            execution_count=len(executions),
            done_task_indices=done_indices,
            converted_task_indices=sorted(converted),
            batch_subject_ids=batch,
            resolved=resolution is not None,
            suggested_subject_ids=sorted(suggested),
            finalized=summary is not None,
            target_cap=target_cap,
            next_stage=next_stage,
        )

    @staticmethod
    def _compute_next_stage(
        *,
        run_status: str,
        plan_task_count: int,
        execution_count: int,
        done_indices: list[int],
        converted: set[int],
        batch: list[str],
        resolved: bool,
        suggested: set[str],
        finalized: bool,
        target_cap: int,
    ) -> str:
        """The single next action, derived purely from durable checkpoints (no I/O, no mutation).

        Order mirrors the pipeline: a cancelled or already-terminal run has nothing to do; a run still
        needing an input-driven step (plan approval / task execution) is BLOCKED; otherwise the inert tail
        runs convert → resolve → suggest → finalize, each gated on the rows the previous stage wrote. A run
        whose extraction produced no observations (empty batch) skips resolve/suggest straight to finalize,
        and suggesting stops once the run's ``target_cap`` is reached (the cap bound)."""
        if run_status == DiscoveryRunStatus.CANCELLED.value:
            return STAGE_CANCELLED
        if finalized or run_status in DiscoveryRunStatus.terminal_values():
            return STAGE_DONE
        # Input-driven stages the advancer cannot perform → blocked, advance nothing.
        if run_status == DiscoveryRunStatus.PENDING.value or plan_task_count == 0:
            return STAGE_AWAITING_PLAN
        if execution_count < plan_task_count:
            return STAGE_AWAITING_EXECUTION
        # Extraction complete — drive the inert tail off the durable checkpoints.
        if any(i not in converted for i in done_indices):
            return STAGE_CONVERT
        if batch and not resolved:
            return STAGE_RESOLVE
        if batch and resolved:
            remaining = [s for s in batch if s not in suggested]
            if remaining and len(suggested) < target_cap:
                return STAGE_SUGGEST
        return STAGE_FINALIZE

    # -- the one advance path -----------------------------------------------------------------------

    def advance_run(
        self,
        run_id: str,
        actor: RunAdvanceActor,
        *,
        now: datetime | None = None,
    ) -> RunAdvanceResult:
        """Advance run ``run_id`` through its inert tail until it settles, blocks or is cancelled.

        Order (each gate leaves prior state unchanged when it refuses or stops):

        1. **Authority FIRST.** A caller lacking :attr:`~app.identity.permissions.Scope.RUN_PLAN_APPROVE`
           is refused (:class:`RunAdvancementAuthorizationError`) before any state is read.
        2. **The run exists in THIS business.** Otherwise refused (:data:`REASON_RUN_NOT_FOUND`) — an
           unknown or cross-tenant run reads exactly like not-found.
        3. **Resume + advance.** The next-incomplete stage is recomputed from durable checkpoints and the
           inert tail is driven one stage-step at a time — convert each DONE task, resolve the batch,
           suggest each eligible subject (up to the run's ``target_cap``), finalize — re-deriving the next
           stage after each step so an already-done stage is skipped (no duplicate, no re-import). The loop
           stops at the first terminal / blocked / cancelled / cap-reached state.

        Advancing drives only inert stages: it makes NO provider call and no model call, so the run's
        provider-call count is unchanged and there is no model loop. Each stage stages its own atomic
        DCA-026 audit entry in the caller's unit of work.
        """
        moment = now or datetime.now(UTC)

        # 1) Authority FIRST — an unauthorized caller is refused with no state touched.
        if not actor.may_advance():
            raise RunAdvancementAuthorizationError(
                "advancing a discovery run requires a business administrator holding "
                f"'{Scope.RUN_PLAN_APPROVE.value}'; a read-only viewer or a platform operator "
                "(holding no business scope) cannot perform it"
            )

        # 2) The run must exist WITHIN this business (an unknown / cross-tenant run is not-found).
        run = self._run(run_id)
        if run is None:
            raise RunAdvancementError(
                REASON_RUN_NOT_FOUND,
                "no such discovery run in this business (an unknown or cross-tenant id is not-found)",
                detail=run_id,
            )

        started_stage = self._snapshot(run).next_stage

        converter = DiscoveryObservationConverter(self._session, self._business_id)
        resolver = ObservationBatchResolver(self._session, self._business_id)
        suggester = ProspectSuggestionCreator(self._session, self._business_id)
        finalizer = DiscoveryRunFinalizer(self._session, self._business_id)

        stages_advanced: list[str] = []
        conversions_made = 0
        suggestions_created = 0

        steps = 0
        while True:
            steps += 1
            if steps > MAX_ADVANCE_STEPS:  # pragma: no cover - defensive, never reached in practice
                raise RunAdvancementError(
                    "run_advancement_step_overflow",
                    f"advancing run '{run_id}' exceeded {MAX_ADVANCE_STEPS} steps without settling",
                    detail=run_id,
                )
            snapshot = self._snapshot(run)
            stage = snapshot.next_stage

            if stage == STAGE_CONVERT:
                task_index = next(
                    i
                    for i in snapshot.done_task_indices
                    if i not in set(snapshot.converted_task_indices)
                )
                converter.convert_task(run_id, task_index, actor.as_conversion_actor(), now=moment)
                self._session.flush()
                conversions_made += 1
                stages_advanced.append(STAGE_CONVERT)
                continue

            if stage == STAGE_RESOLVE:
                resolver.resolve_run(run_id, actor.as_resolution_actor(), now=moment)
                self._session.flush()
                stages_advanced.append(STAGE_RESOLVE)
                continue

            if stage == STAGE_SUGGEST:
                suggested = set(snapshot.suggested_subject_ids)
                subject_id = next(s for s in snapshot.batch_subject_ids if s not in suggested)
                suggester.suggest(run_id, subject_id, actor.as_suggestion_actor(), now=moment)
                self._session.flush()
                suggestions_created += 1
                stages_advanced.append(STAGE_SUGGEST)
                continue

            if stage == STAGE_FINALIZE:
                finalizer.finalize_run(run_id, actor.as_finalization_actor(), now=moment)
                self._session.flush()
                stages_advanced.append(STAGE_FINALIZE)
                continue

            # A STOP stage (done / cancelled / awaiting_plan / awaiting_execution) — advance nothing more.
            final_stage = stage
            break

        final = self._snapshot(run)
        remaining_uncapped = [
            s for s in final.batch_subject_ids if s not in set(final.suggested_subject_ids)
        ]
        cap_reached = (
            bool(remaining_uncapped) and len(final.suggested_subject_ids) >= final.target_cap
        )
        stopped_reason = final_stage if final_stage != STAGE_DONE else None

        return RunAdvanceResult(
            business_id=self._business_id,
            run=run,
            started_stage=started_stage,
            final_stage=final_stage,
            status=run.status,
            stages_advanced=stages_advanced,
            conversions_made=conversions_made,
            suggestions_created=suggestions_created,
            resolved=final.resolved,
            finalized=final.finalized,
            cap_reached=cap_reached,
            stopped_reason=stopped_reason,
            summary=self._summary(run_id),
            provider_calls_made=0,
        )


__all__ = [
    "SUBJECT_TYPE",
    "ADVANCEMENT_SOURCE",
    "STAGE_AWAITING_PLAN",
    "STAGE_AWAITING_EXECUTION",
    "STAGE_CONVERT",
    "STAGE_RESOLVE",
    "STAGE_SUGGEST",
    "STAGE_FINALIZE",
    "STAGE_DONE",
    "STAGE_CANCELLED",
    "ADVANCEABLE_STAGES",
    "STOP_STAGES",
    "MAX_ADVANCE_STEPS",
    "REASON_NOT_AUTHORIZED",
    "REASON_RUN_NOT_FOUND",
    "RunAdvancementError",
    "RunAdvancementAuthorizationError",
    "RunAdvanceActor",
    "RunProgress",
    "RunAdvanceResult",
    "DiscoveryRunAdvancer",
]
