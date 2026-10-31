"""DCA-142 — execute ONE planned source task of a discovery run.

This story is the next step of the still-disabled discovery pipeline after the DCA-141 plan is frozen
onto a run: it EXECUTES exactly ONE of that run's approved, frozen tasks. It is the orchestration glue
that connects, for one task, the four mechanisms earlier stories already built — the DCA-048 source
rights preflight, the DCA-113 cost reservation, the DCA-115 provider-call ledger, and the concrete
DCA-123 source adapter — storing only the PERMITTED staged output / checkpoint.

There is **no real network fetch and no recipient send.** The adapter runs deterministically against
caller-supplied / synthetic fixture inputs (exactly as the DCA-123 importer takes an already-approved
dataset): "execute" means run the adapter mechanics, bounded to ONE task. Acceptance of a prospect is
not recipient consent, and no send path exists here.

Every guard is checked fail-closed, in order, so the two acceptance criteria hold:

- **A revoked source or exhausted budget stops the task BEFORE external access.** Authority is checked
  FIRST; then the run must be executable (``planned`` / ``running``) and the task must come from the
  run's FROZEN DCA-141 ``plan_snapshot`` (only approved tasks run); then the DCA-048 rights preflight is
  evaluated — a denied / revoked source records a ``DENIED`` outcome and the adapter is NEVER invoked
  (no provider call is dispatched); then the DCA-113 reservation is taken — an over-budget / no-budget
  refusal likewise records a ``DENIED`` outcome and the adapter is never invoked. Only once BOTH guards
  permit is the provider call dispatched and the adapter run.

- **Reproduce the result and the relevant denial without unintended state changes.** ``(business_id,
  run_id, task_index)`` is unique, so a task runs ONCE: re-executing it is a deterministic no-op that
  returns the existing record (whether ``DONE`` / ``FAILED`` / ``DENIED``) and stages nothing new. A
  denial leaves the run's state unchanged; a successful / failed invocation advances the run
  ``planned`` → ``running`` exactly once. The executor stages into the caller's session and never
  commits — the execution record and its atomic DCA-026 audit entry commit together, in the caller's
  unit of work.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.sink import ActorType, AuditActor, AuditSink
from app.cost.provider_call import (
    ProviderCallActor,
    ProviderCallIntent,
    ProviderCallStore,
    ProviderCallTransition,
)
from app.cost.reserve import (
    BudgetReservationStore,
    ReservationActor,
    ReservationError,
    ReservationRequest,
)
from app.db.models import (
    DiscoveryRunRequest,
    DiscoveryRunStatus,
    DiscoveryTaskExecution,
    DiscoveryTaskStatus,
    ProviderCallStatus,
)
from app.identity.permissions import Scope, scopes_for_business_roles
from app.services.discovery_run import DiscoveryRunStore
from app.services.source_preflight import PreflightRequest, SourcePreflight
from app.sources.org_dataset_import import (
    ADAPTER_KEY as ORG_DATASET_ADAPTER_KEY,
)
from app.sources.org_dataset_import import (
    DatasetManifest,
    DatasetRow,
    OrgDatasetImporter,
)

# The AI/source capability label the provider-call ledger attributes a source task's (synthetic) access
# to. It is NOT a model capability — a source adapter run — but it rides the same DCA-115 ledger so cost
# accounting has an authoritative lifecycle record of the bounded, no-network access.
SOURCE_FETCH_CAPABILITY = "source_fetch"

# The source operation a discovery task executes under (collection), checked by the rights preflight.
TASK_OPERATION = "collect"

# A conservative per-query cost estimate (INR minor units / paise) used to RESERVE the task's spend
# before it runs. The reservation is a HOLD against the business's DCA-042 budget caps, never a charge.
PER_QUERY_COST_MINOR = 500

# The audited action code for executing a planned task (a controlled code, like the other DCA writes).
AUDIT_ACTION_EXECUTE_TASK = "discovery_run.task.executed"

# Where an execution records its origin on the staged-result / provenance.
EXECUTION_SOURCE = "discovery_task_execution"

# Stable refusal codes raised (never recorded) for a malformed request — each BEFORE any state is
# mutated, so the attempt changes nothing.
REASON_NOT_AUTHORIZED = "task_execution_not_authorized"
REASON_RUN_NOT_FOUND = "discovery_run_not_found"
REASON_RUN_NOT_EXECUTABLE = "discovery_run_not_executable"
REASON_TASK_NOT_FOUND = "discovery_task_not_found"
REASON_SOURCE_MISMATCH = "adapter_input_source_mismatch"

# Stable DENIED-outcome reasons that identify the guard phase (the reason_code carries the specific
# underlying code, e.g. the preflight reason or the reservation refusal code).
PHASE_RIGHTS = "rights"
PHASE_BUDGET = "budget"
PHASE_ADAPTER = "adapter"

# The runnable adapters are those a discovery task's frozen source maps to. Only the DCA-123
# organization-dataset importer is wired here (the canonical official-registry discovery adapter);
# the DCA-124 website / DCA-125 licensed-provider / DCA-126 private-CSV adapters stay reachable through
# the same seam but are each their own wiring (a disabled future capability until then).
RUNNABLE_ADAPTER_KEYS = frozenset({ORG_DATASET_ADAPTER_KEY})


class TaskExecutionError(Exception):
    """A task execution was refused with a stable ``code`` for a malformed request (unknown run / task,
    non-executable run, or an adapter input that does not target the task's approved source). Raised
    before the session is mutated, so a refusal changes nothing. ``detail`` names the offending value."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class TaskExecutionAuthorizationError(TaskExecutionError):
    """The caller is not authorized to execute a discovery task.

    A distinct type so an HTTP boundary can map it to a 403 while the other codes map to a 404/409/422.
    Raised FIRST — before any state is read or written. ``code`` is :data:`REASON_NOT_AUTHORIZED`.
    """

    def __init__(self, message: str) -> None:
        super().__init__(REASON_NOT_AUTHORIZED, message)


@dataclass(frozen=True)
class TaskExecutionActor:
    """The business principal executing a planned task.

    ``subject`` is an opaque actor id (never a name/email) recorded on the execution record and its
    atomic audit entry. ``roles`` are the caller's verified business-plane membership roles (from the
    :class:`~app.identity.tenant_context.TenantContext`, NEVER a bare token claim). :meth:`may_execute`
    grants the action only when those roles yield :attr:`~app.identity.permissions.Scope.RUN_PLAN_APPROVE`
    — executing a run's approved task governs chargeable discovery work, the same administrator authority
    that approved the plan (DCA-141). An ordinary acting user and a read-only viewer do not hold it, and
    a platform operator holds no business scope for the tenant. The same administrator roles also grant
    :attr:`~app.identity.permissions.Scope.BUDGET_MANAGE`, which the reservation / provider-call sub-stores
    require.
    """

    subject: str
    roles: tuple[str, ...] = ()
    actor_type: str = "user"

    def may_execute(self) -> bool:
        return Scope.RUN_PLAN_APPROVE in scopes_for_business_roles(self.roles)

    def as_audit_actor(self) -> AuditActor:
        return AuditActor(id=self.subject, type=ActorType(self.actor_type))

    def as_reservation_actor(self) -> ReservationActor:
        return ReservationActor(subject=self.subject, roles=self.roles, actor_type=self.actor_type)

    def as_provider_call_actor(self) -> ProviderCallActor:
        return ProviderCallActor(subject=self.subject, roles=self.roles, actor_type=self.actor_type)


@dataclass(frozen=True)
class OrgDatasetTaskInput:
    """Caller-supplied / synthetic fixture inputs for a task whose source maps to the DCA-123 importer.

    ``manifest`` + ``rows`` are the already-approved organization dataset (exactly what
    :meth:`~app.sources.org_dataset_import.OrgDatasetImporter.import_dataset` takes) — NOT a live fetch.
    The manifest's ``source_key`` MUST equal the executed task's ``sourceKey`` (the adapter runs against
    the task's APPROVED+entitled source, never some other source).
    """

    manifest: DatasetManifest
    rows: tuple[DatasetRow, ...] = ()

    @property
    def adapter_key(self) -> str:
        return ORG_DATASET_ADAPTER_KEY


@dataclass
class TaskExecutionResult:
    """The inert, inspectable outcome of one execute attempt.

    ``execution`` is the staged :class:`~app.db.models.DiscoveryTaskExecution` record. ``status`` echoes
    its terminal :class:`~app.db.models.DiscoveryTaskStatus`. ``already_executed`` is ``True`` when this
    was an idempotent no-op (the task had already run). ``ran_adapter`` is ``True`` only when the adapter
    was actually invoked (never on a DENIED guard stop). Nothing here is ever sent or charged.
    """

    business_id: str
    execution: DiscoveryTaskExecution
    status: str
    already_executed: bool = False
    ran_adapter: bool = False

    @property
    def denied(self) -> bool:
        return self.status == DiscoveryTaskStatus.DENIED.value


def _task_fingerprint(task: dict) -> str:
    """A non-reversible SHA-256 (hex) of the frozen task — ties an execution to the exact approved task."""
    return hashlib.sha256(json.dumps(task, sort_keys=True, default=str).encode("utf-8")).hexdigest()


class DiscoveryTaskExecutor:
    """Execute ONE planned source task of a discovery run, for ONE business (tenant-scoped).

    Construct with the active session and the verified business scope (a route/worker resolves it from
    the membership-verified tenant context). Composes the DCA-048 rights preflight (checked before any
    external access), the DCA-113 reservation, the DCA-115 provider-call ledger and the concrete DCA-123
    adapter. Every query is confined to this business — there is no argument to widen the scope. The
    executor stages into the caller's session and never commits.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def execution_for(self, run_id: str, task_index: int) -> DiscoveryTaskExecution | None:
        """The execution record for one (run, task) within this business, or ``None`` (the idempotency
        read; a cross-tenant / unknown run reads as not-found)."""
        if not run_id:
            return None
        stmt = (
            select(DiscoveryTaskExecution)
            .where(DiscoveryTaskExecution.business_id == self._business_id)
            .where(DiscoveryTaskExecution.run_id == run_id)
            .where(DiscoveryTaskExecution.task_index == task_index)
        )
        return self._session.scalars(stmt).one_or_none()

    def executions_for(self, run_id: str) -> list[DiscoveryTaskExecution]:
        """Every task execution recorded for one run in this business, by task index."""
        stmt = (
            select(DiscoveryTaskExecution)
            .where(DiscoveryTaskExecution.business_id == self._business_id)
            .where(DiscoveryTaskExecution.run_id == run_id)
            .order_by(DiscoveryTaskExecution.task_index)
        )
        return list(self._session.scalars(stmt).all())

    # -- the one execute path -----------------------------------------------------------------------

    def execute_task(
        self,
        run_id: str,
        task_index: int,
        actor: TaskExecutionActor,
        *,
        adapter_input: OrgDatasetTaskInput,
        now: datetime | None = None,
    ) -> TaskExecutionResult:
        """Execute task ``task_index`` of run ``run_id`` for this business; return the result.

        Order (each gate leaves the run / prior state unchanged when it refuses or denies):

        1. **Authority FIRST.** A caller lacking :attr:`~app.identity.permissions.Scope.RUN_PLAN_APPROVE`
           is refused (:class:`TaskExecutionAuthorizationError`) before any state is read.
        2. **The run is executable.** The run must exist WITHIN this business and be ``planned`` /
           ``running``; otherwise refused (:data:`REASON_RUN_NOT_FOUND` / :data:`REASON_RUN_NOT_EXECUTABLE`).
        3. **The task is approved.** ``task_index`` must address a task in the run's FROZEN
           ``plan_snapshot`` (:data:`REASON_TASK_NOT_FOUND`); the supplied adapter input must target that
           task's ``sourceKey`` (:data:`REASON_SOURCE_MISMATCH`). Only approved tasks run.
        4. **Idempotent replay.** An existing execution for (run, task_index) returns it unchanged (no
           second run, no new audit) — a task runs once.
        5. **Rights (DCA-048) BEFORE external access.** The task's source must still be approved+entitled
           to ``collect`` for the manifest's purpose. A revoked / denied source records a ``DENIED``
           outcome (phase ``rights``) and the adapter is NEVER invoked; the run is unchanged.
        6. **Budget (DCA-113) BEFORE external access.** The task's estimated cost is RESERVED against the
           business's caps. An over-budget / no-budget refusal records a ``DENIED`` outcome (phase
           ``budget``) and the adapter is never invoked; the run is unchanged.
        7. **Provider call + adapter.** Only once both guards permit: a DCA-115 provider-call record is
           dispatched (``pending`` → ``in_flight``), the DCA-123 adapter is invoked deterministically on
           the caller-supplied fixture, the call is resolved (``succeeded`` / ``failed``), the raw result
           is staged, and the run is advanced ``planned`` → ``running`` exactly once.

        On any outcome an atomic DCA-026 audit entry is staged in the caller's unit of work. Executing a
        task performs no real network fetch and sends no message.
        """
        moment = now or datetime.now(UTC)

        # 1) Authority FIRST — an unauthorized caller is refused with no state touched.
        if not actor.may_execute():
            raise TaskExecutionAuthorizationError(
                "executing a discovery run task requires a business administrator holding "
                f"'{Scope.RUN_PLAN_APPROVE.value}'; an ordinary acting user, a read-only viewer or a "
                "platform operator (holding no business scope) cannot perform it"
            )

        # 2) The run must exist WITHIN this business and be executable (planned / running).
        run = DiscoveryRunStore(self._session, self._business_id).get(run_id)
        if run is None:
            raise TaskExecutionError(
                REASON_RUN_NOT_FOUND,
                "no such discovery run in this business (an unknown or cross-tenant id is not-found)",
                detail=run_id,
            )
        if run.status not in (DiscoveryRunStatus.PLANNED.value, DiscoveryRunStatus.RUNNING.value):
            raise TaskExecutionError(
                REASON_RUN_NOT_EXECUTABLE,
                f"discovery run '{run.id}' is '{run.status}', not planned/running — only a planned or "
                "running run can execute a task",
                detail=run.status,
            )

        # 3) The task must come from the run's FROZEN plan_snapshot (only approved tasks run).
        tasks = list((run.plan_snapshot or {}).get("tasks") or [])
        if task_index < 0 or task_index >= len(tasks):
            raise TaskExecutionError(
                REASON_TASK_NOT_FOUND,
                f"task index {task_index} is outside the run's approved plan ({len(tasks)} task(s))",
                detail=str(task_index),
            )
        task = tasks[task_index]
        source_key = str(task.get("sourceKey") or "").strip()
        manifest_source = str(adapter_input.manifest.source_key or "").strip()
        if manifest_source != source_key:
            raise TaskExecutionError(
                REASON_SOURCE_MISMATCH,
                f"adapter input targets source '{manifest_source}' but the approved task's source is "
                f"'{source_key}' — the adapter must run against the task's approved source",
                detail=manifest_source,
            )

        # 4) Idempotent replay — a task runs ONCE; return the existing record unchanged.
        existing = self.execution_for(run_id, task_index)
        if existing is not None:
            return TaskExecutionResult(
                business_id=self._business_id,
                execution=existing,
                status=existing.status,
                already_executed=True,
                ran_adapter=existing.provider_call_id is not None,
            )

        fingerprint = _task_fingerprint(task)
        purpose = str(adapter_input.manifest.purpose or "").strip()

        # 5) Rights FIRST (DCA-048) — a revoked / denied source stops the task before any external access.
        rights = SourcePreflight(self._session, self._business_id).authorize(
            PreflightRequest(
                source_key=source_key, operation=TASK_OPERATION, purpose=purpose, field=""
            ),
            now=moment,
        )
        if not rights.allowed:
            return self._record_denied(
                run=run,
                task_index=task_index,
                fingerprint=fingerprint,
                source_key=source_key,
                adapter_key=adapter_input.adapter_key,
                phase=PHASE_RIGHTS,
                reason_code=rights.reason or "rights_denied",
                actor=actor,
                moment=moment,
            )

        # 6) Budget (DCA-113) — reserve the task's estimated cost. An over-budget / no-budget refusal
        #    stages NOTHING (the reservation store fails closed) and the adapter is never invoked.
        estimate = max(1, int(task.get("maxQueries") or 1)) * PER_QUERY_COST_MINOR
        reservation_key = f"{EXECUTION_SOURCE}:{run_id}:{task_index}"
        try:
            reservation = BudgetReservationStore(self._session, self._business_id).reserve(
                ReservationRequest(
                    idempotency_key=reservation_key,
                    amount_minor=estimate,
                    provider=source_key or "platform",
                ),
                actor.as_reservation_actor(),
                now=moment,
            )
            # Flush so the hold's id is populated before it is referenced on the provider call / record.
            self._session.flush()
        except ReservationError as exc:
            return self._record_denied(
                run=run,
                task_index=task_index,
                fingerprint=fingerprint,
                source_key=source_key,
                adapter_key=adapter_input.adapter_key,
                phase=PHASE_BUDGET,
                reason_code=exc.code,
                actor=actor,
                moment=moment,
            )

        # 7) Both guards permit — dispatch the provider call and invoke the adapter (external access).
        return self._run_adapter(
            run=run,
            task=task,
            task_index=task_index,
            fingerprint=fingerprint,
            source_key=source_key,
            reservation_id=reservation.id,
            adapter_input=adapter_input,
            actor=actor,
            moment=moment,
        )

    # -- internals ----------------------------------------------------------------------------------

    def _run_adapter(
        self,
        *,
        run: DiscoveryRunRequest,
        task: dict,
        task_index: int,
        fingerprint: str,
        source_key: str,
        reservation_id: str,
        adapter_input: OrgDatasetTaskInput,
        actor: TaskExecutionActor,
        moment: datetime,
    ) -> TaskExecutionResult:
        """Dispatch the provider-call lifecycle around the one adapter invocation, stage the result, and
        advance the run. Guards have already permitted; this is the only place external access happens."""
        call_store = ProviderCallStore(self._session, self._business_id)
        call_actor = actor.as_provider_call_actor()
        call_key = f"{EXECUTION_SOURCE}:{run.id}:{task_index}"
        call = call_store.record(
            ProviderCallIntent(
                call_key=call_key,
                capability=SOURCE_FETCH_CAPABILITY,
                intent=adapter_input.adapter_key,
                provider=source_key or "platform",
                source_version=str(adapter_input.manifest.source_version or ""),
                reservation_id=reservation_id,
            ),
            call_actor,
            now=moment,
        )
        # Flush so the provider-call record's id is populated before it is referenced on the execution.
        self._session.flush()
        # Mark the (synthetic) external access as dispatched before invoking the adapter.
        call_store.transition(
            ProviderCallTransition(call_key, ProviderCallStatus.IN_FLIGHT), call_actor, now=moment
        )

        importer = OrgDatasetImporter(self._session, self._business_id)
        import_result = importer.import_dataset(
            adapter_input.manifest,
            list(adapter_input.rows),
            actor.as_audit_actor(),
            now=moment,
        )

        if import_result.authorized:
            status = DiscoveryTaskStatus.DONE
            reason_code = ""
            call_store.transition(
                ProviderCallTransition(
                    call_key, ProviderCallStatus.SUCCEEDED, outcome_code="imported"
                ),
                call_actor,
                now=moment,
            )
            staged_result = {
                "source": EXECUTION_SOURCE,
                "adapterKey": adapter_input.adapter_key,
                "sourceKey": source_key,
                "datasetId": import_result.dataset_id,
                "sourceVersion": import_result.source_version,
                "importedCount": import_result.imported_count,
                "skippedCount": import_result.skipped_count,
                "refusedCount": len(import_result.refused_rows),
                # Opaque, checkpointable row references only — never prospect contact data.
                "importedRowRefs": [r.row_ref for r in import_result.imported],
                "skippedRowRefs": [r.row_ref for r in import_result.skipped],
            }
        else:
            # The adapter's OWN rights gate refused (external access was reached, nothing staged).
            status = DiscoveryTaskStatus.FAILED
            reason_code = import_result.reason or "adapter_refused"
            call_store.transition(
                ProviderCallTransition(
                    call_key, ProviderCallStatus.FAILED, outcome_code=reason_code
                ),
                call_actor,
                now=moment,
            )
            staged_result = {}

        execution = self._stage_execution(
            run=run,
            task_index=task_index,
            fingerprint=fingerprint,
            source_key=source_key,
            adapter_key=adapter_input.adapter_key,
            status=status,
            phase=PHASE_ADAPTER,
            reason_code=reason_code,
            reservation_id=reservation_id,
            provider_call_id=call.id,
            staged_result=staged_result,
            executed=True,
            actor=actor,
            moment=moment,
        )
        # The adapter was invoked — advance the run planned → running exactly once.
        self._advance_run_to_running(run, moment)
        return TaskExecutionResult(
            business_id=self._business_id,
            execution=execution,
            status=status.value,
            ran_adapter=True,
        )

    def _record_denied(
        self,
        *,
        run: DiscoveryRunRequest,
        task_index: int,
        fingerprint: str,
        source_key: str,
        adapter_key: str,
        phase: str,
        reason_code: str,
        actor: TaskExecutionActor,
        moment: datetime,
    ) -> TaskExecutionResult:
        """Record a ``DENIED`` task execution: a guard stopped the task before any adapter was invoked.

        Nothing ran — no provider call was dispatched and the run's state is UNCHANGED. Only this denial
        record and its atomic audit entry are staged, so the denial is reproducible and inspectable."""
        execution = self._stage_execution(
            run=run,
            task_index=task_index,
            fingerprint=fingerprint,
            source_key=source_key,
            adapter_key=adapter_key,
            status=DiscoveryTaskStatus.DENIED,
            phase=phase,
            reason_code=reason_code,
            reservation_id=None,
            provider_call_id=None,
            staged_result={},
            executed=False,
            actor=actor,
            moment=moment,
        )
        return TaskExecutionResult(
            business_id=self._business_id,
            execution=execution,
            status=DiscoveryTaskStatus.DENIED.value,
            ran_adapter=False,
        )

    def _stage_execution(
        self,
        *,
        run: DiscoveryRunRequest,
        task_index: int,
        fingerprint: str,
        source_key: str,
        adapter_key: str,
        status: DiscoveryTaskStatus,
        phase: str,
        reason_code: str,
        reservation_id: str | None,
        provider_call_id: str | None,
        staged_result: dict,
        executed: bool,
        actor: TaskExecutionActor,
        moment: datetime,
    ) -> DiscoveryTaskExecution:
        """Stage the execution record + its atomic DCA-026 audit entry in the caller's unit of work."""
        provenance = {
            "source": EXECUTION_SOURCE,
            "runId": run.id,
            "taskIndex": task_index,
            "phase": phase,
            # Load-bearing invariants: a task execution never sends and never charges (a reservation is a
            # hold, not a spend). ``executed`` reflects whether the adapter actually ran.
            "executed": executed,
            "sent": False,
            "spent": False,
            "autoApproved": False,
        }
        execution = DiscoveryTaskExecution(
            business_id=self._business_id,
            run_id=run.id,
            task_index=task_index,
            task_fingerprint=fingerprint,
            source_key=source_key,
            adapter_key=adapter_key,
            status=status.value,
            phase=phase,
            reason_code=reason_code,
            reservation_id=reservation_id,
            provider_call_id=provider_call_id,
            staged_result=staged_result,
            provenance=provenance,
            created_at=moment,
        )
        self._session.add(execution)

        # Atomic audit (SEC-001/BR-122): staged into the caller's session so it commits with the record.
        # References stay OPAQUE — run/task ids, the source key, the outcome and guard phase — never a
        # contact value. Executing a task runs an adapter but sends nothing and charges nothing.
        AuditSink(
            self._session, business_id=self._business_id, actor=actor.as_audit_actor()
        ).record(
            action=AUDIT_ACTION_EXECUTE_TASK,
            reason=(
                f"executed planned task #{task_index} of run '{run.id}' against source "
                f"'{source_key}' → {status.value}"
            ),
            references={
                "runId": run.id,
                "taskIndex": task_index,
                "sourceKey": source_key,
                "adapterKey": adapter_key,
                "status": status.value,
                "phase": phase,
                "reasonCode": reason_code,
                "reservationId": reservation_id,
                "providerCallId": provider_call_id,
                "executed": executed,
            },
        )
        return execution

    def _advance_run_to_running(self, run: DiscoveryRunRequest, moment: datetime) -> None:
        """Advance the run ``planned`` → ``running`` on the first executed task; record the invariant.

        Idempotent across tasks: a run already ``running`` stays ``running``. Executing a task sends
        nothing and charges nothing, so ``sent`` / ``spent`` stay ``False`` on the run provenance."""
        if run.status == DiscoveryRunStatus.PLANNED.value:
            run.status = DiscoveryRunStatus.RUNNING.value
        provenance = dict(run.provenance or {})
        provenance.update({"executed": True, "sent": False, "spent": False})
        run.provenance = provenance


__all__ = [
    "SOURCE_FETCH_CAPABILITY",
    "TASK_OPERATION",
    "PER_QUERY_COST_MINOR",
    "AUDIT_ACTION_EXECUTE_TASK",
    "EXECUTION_SOURCE",
    "REASON_NOT_AUTHORIZED",
    "REASON_RUN_NOT_FOUND",
    "REASON_RUN_NOT_EXECUTABLE",
    "REASON_TASK_NOT_FOUND",
    "REASON_SOURCE_MISMATCH",
    "PHASE_RIGHTS",
    "PHASE_BUDGET",
    "PHASE_ADAPTER",
    "RUNNABLE_ADAPTER_KEYS",
    "TaskExecutionError",
    "TaskExecutionAuthorizationError",
    "TaskExecutionActor",
    "OrgDatasetTaskInput",
    "TaskExecutionResult",
    "DiscoveryTaskExecutor",
]
