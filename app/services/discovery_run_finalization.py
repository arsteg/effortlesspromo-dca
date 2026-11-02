"""DCA-146 — finalize a discovery run summary (the terminal, read-and-aggregate pipeline step).

This is the final step of the still-disabled discovery pipeline after DCA-145 suggests prospects from a
run's resolved batch: it AGGREGATES the run's already-recorded state — its task outcomes/counts, the
(held) costs, the failure/denial reasons and a durable checkpoint cursor — into ONE durable
:class:`~app.db.models.DiscoveryRunSummary` row, and SETTLES the run into a terminal
:class:`~app.db.models.DiscoveryRunStatus`. It exposes **no raw private data**: the summary holds only
aggregate counts, stable reason codes and opaque, checkpointable row references — never a contact value.

There is **no network fetch, no model call and no recipient send.** Finalizing reads the pipeline rows
the earlier stages (DCA-142..145) already recorded and concludes — it spends nothing and sends nothing
(``provenance`` pins ``finalized`` = True and ``sent`` / ``spent`` = False).

Every guard is checked fail-closed, in order, so the two acceptance criteria hold:

- **Partial failure is reported honestly; a run with no matches is not fabricated success.** The counts
  mirror the underlying :class:`~app.db.models.DiscoveryTaskExecution` records exactly, and the per-reason
  breakdown names why each non-DONE task stopped. ``final_status`` is
  :attr:`~app.db.models.DiscoveryRunStatus.COMPLETED` ONLY when the run did genuine successful work (at
  least one DONE task); a run whose tasks all failed / were denied, or that executed nothing at all
  (``empty``), settles to :attr:`~app.db.models.DiscoveryRunStatus.FAILED` — never a fabricated success.
  The resolved ``matched_count`` is recorded as-is, so "no matches" reads as zero.

- **Reproduce the result and the relevant denial without unintended state changes.** Authority is checked
  FIRST; then the run must exist in THIS business (an unknown / cross-tenant run reads as not-found). Each
  refusal is raised BEFORE the session is mutated, so it changes nothing. A run finalizes ONCE —
  ``(business_id, run_id)`` is unique, so re-finalizing returns the existing summary and advances no state
  and writes no second audit.

Like the sibling DCA-143/144/145 services, the finalizer stages into the *caller's* session and never
commits — the route/worker owns the unit of work, so the summary, the run's terminal transition and its
atomic DCA-026 audit entry commit together. There is no argument to widen the business scope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.sink import ActorType, AuditActor, AuditSink
from app.db.models import (
    BudgetReservation,
    DiscoveryRunRequest,
    DiscoveryRunStatus,
    DiscoveryRunSummary,
    DiscoveryTaskExecution,
    DiscoveryTaskStatus,
    ObservationBatchResolution,
    ProspectSuggestion,
)
from app.identity.permissions import Scope, scopes_for_business_roles
from app.services.discovery_run import DiscoveryRunStore

# The version of the summary contract rows are written under (expand→migrate→contract).
SUMMARY_SCHEMA_VERSION = 1

# The audited action code for finalizing a discovery run (a controlled code, like the other DCA writes).
AUDIT_ACTION_FINALIZE_RUN = "discovery_run.finalized"

# Where a summary records its origin on the provenance.
FINALIZATION_SOURCE = "discovery_run_finalization"

# Stable refusal codes raised (never recorded) for a malformed / ineligible request — each BEFORE any
# state is mutated, so the attempt changes nothing.
REASON_NOT_AUTHORIZED = "run_finalization_not_authorized"
REASON_RUN_NOT_FOUND = "discovery_run_not_found"


class RunFinalizationError(Exception):
    """A finalize was refused with a stable ``code`` for a malformed / ineligible request (an unknown or
    cross-tenant run). Raised before the session is mutated, so a refusal changes nothing. ``detail``
    names the offending value."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class RunFinalizationAuthorizationError(RunFinalizationError):
    """The caller is not authorized to finalize a discovery run.

    A distinct type so an HTTP boundary can map it to a 403 while the other code maps to a 404. Raised
    FIRST — before any state is read or written. ``code`` is :data:`REASON_NOT_AUTHORIZED`.
    """

    def __init__(self, message: str) -> None:
        super().__init__(REASON_NOT_AUTHORIZED, message)


@dataclass(frozen=True)
class RunFinalizationActor:
    """The business principal finalizing a discovery run.

    ``subject`` is an opaque actor id (never a name/email) recorded on the summary's atomic audit entry.
    ``roles`` are the caller's verified business-plane membership roles (from the tenant context, NEVER a
    bare token claim). :meth:`may_finalize` grants the action only when those roles yield
    :attr:`~app.identity.permissions.Scope.RUN_PLAN_APPROVE` — finalizing a run is part of the same
    chargeable-discovery authority that approved the plan (DCA-141), executed it (DCA-142), converted its
    tasks (DCA-143), resolved them (DCA-144) and suggested prospects (DCA-145). A read-only viewer and a
    platform operator (holding no business scope) cannot perform it.
    """

    subject: str
    roles: tuple[str, ...] = ()
    actor_type: str = "user"

    def may_finalize(self) -> bool:
        return Scope.RUN_PLAN_APPROVE in scopes_for_business_roles(self.roles)

    def as_audit_actor(self) -> AuditActor:
        return AuditActor(id=self.subject, type=ActorType(self.actor_type))


@dataclass
class RunFinalizationResult:
    """The inert, inspectable outcome of one finalize attempt.

    ``summary`` is the staged :class:`~app.db.models.DiscoveryRunSummary` record. ``already_finalized`` is
    ``True`` when this was an idempotent no-op (the run had already been finalized). The remaining fields
    echo the summary's load-bearing aggregates — the terminal ``final_status``, the task-outcome counts,
    the honest ``partial_failure`` / ``empty`` flags, the held ``reserved_cost_minor``, the resolved
    ``matched_count`` and the ``suggestion_count``. Nothing here is ever sent or spent.
    """

    business_id: str
    summary: DiscoveryRunSummary
    final_status: str
    task_count: int
    done_count: int
    failed_count: int
    denied_count: int
    reserved_cost_minor: int
    subject_count: int
    matched_count: int
    suggestion_count: int
    partial_failure: bool
    empty: bool
    reason_counts: dict = field(default_factory=dict)
    already_finalized: bool = False

    @property
    def succeeded(self) -> bool:
        """Whether the run was settled as a genuine success (COMPLETED) rather than FAILED."""
        return self.final_status == DiscoveryRunStatus.COMPLETED.value


class DiscoveryRunFinalizer:
    """Finalize ONE discovery run's durable summary, for ONE business (tenant-scoped).

    Construct with the active session and the verified business scope (a route/worker resolves it from the
    membership-verified tenant context). Aggregates the run's already-recorded DCA-142 executions, DCA-144
    resolution and DCA-145 suggestions. Every query is confined to this business — there is no argument to
    widen the scope. The finalizer stages into the caller's session and never commits; it aggregates only
    and sends / spends nothing.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def summary_for(self, run_id: str) -> DiscoveryRunSummary | None:
        """The summary for one run within this business, or ``None`` (the idempotency read; a
        cross-tenant / unknown run reads as not-found)."""
        if not run_id:
            return None
        stmt = (
            select(DiscoveryRunSummary)
            .where(DiscoveryRunSummary.business_id == self._business_id)
            .where(DiscoveryRunSummary.run_id == run_id)
        )
        return self._session.scalars(stmt).one_or_none()

    def _executions(self, run_id: str) -> list[DiscoveryTaskExecution]:
        stmt = (
            select(DiscoveryTaskExecution)
            .where(DiscoveryTaskExecution.business_id == self._business_id)
            .where(DiscoveryTaskExecution.run_id == run_id)
            .order_by(DiscoveryTaskExecution.task_index)
        )
        return list(self._session.scalars(stmt).all())

    def _resolution(self, run_id: str) -> ObservationBatchResolution | None:
        stmt = (
            select(ObservationBatchResolution)
            .where(ObservationBatchResolution.business_id == self._business_id)
            .where(ObservationBatchResolution.run_id == run_id)
        )
        return self._session.scalars(stmt).one_or_none()

    def _suggestion_count(self, run_id: str) -> int:
        stmt = (
            select(ProspectSuggestion.id)
            .where(ProspectSuggestion.business_id == self._business_id)
            .where(ProspectSuggestion.run_id == run_id)
        )
        return len(list(self._session.scalars(stmt).all()))

    def _reserved_cost_minor(self, executions: list[DiscoveryTaskExecution]) -> int:
        """The total HELD cost (sum of the tasks' DCA-113 reservation holds), tenant-scoped. A hold is
        NEVER a spend — the DCA has no spend path — so this is the budget that WOULD have been consumed."""
        reservation_ids = [e.reservation_id for e in executions if e.reservation_id]
        if not reservation_ids:
            return 0
        stmt = (
            select(BudgetReservation.amount_minor)
            .where(BudgetReservation.business_id == self._business_id)
            .where(BudgetReservation.id.in_(reservation_ids))
        )
        return int(sum(self._session.scalars(stmt).all()))

    # -- the one finalize path ----------------------------------------------------------------------

    def finalize_run(
        self,
        run_id: str,
        actor: RunFinalizationActor,
        *,
        now: datetime | None = None,
    ) -> RunFinalizationResult:
        """Finalize run ``run_id`` for this business: aggregate its outcomes into a durable summary and
        settle it into a terminal status.

        Order (each gate leaves prior state unchanged when it refuses):

        1. **Authority FIRST.** A caller lacking :attr:`~app.identity.permissions.Scope.RUN_PLAN_APPROVE`
           is refused (:class:`RunFinalizationAuthorizationError`) before any state is read.
        2. **The run exists in THIS business.** Otherwise refused (:data:`REASON_RUN_NOT_FOUND`) — an
           unknown or cross-tenant run reads exactly like not-found.
        3. **Idempotent replay.** An existing summary for the run is returned unchanged (no new state, no
           new audit) — a run finalizes once.
        4. **Aggregate + settle.** The run's DCA-142 executions are counted by outcome (done / failed /
           denied) with a per-reason breakdown; the held reservation cost, the DCA-144 resolved-batch
           counts and the DCA-145 suggestion count are aggregated; a durable checkpoint cursor is built;
           and the run is settled to COMPLETED (>=1 DONE task) or FAILED (no successful work — honest for
           an all-failed / all-denied / empty run).

        On success an atomic DCA-026 audit entry is staged in the caller's unit of work. Finalizing reads
        already-recorded pipeline rows and performs no fetch, model call, spend or send.
        """
        moment = now or datetime.now(UTC)

        # 1) Authority FIRST — an unauthorized caller is refused with no state touched.
        if not actor.may_finalize():
            raise RunFinalizationAuthorizationError(
                "finalizing a discovery run requires a business administrator holding "
                f"'{Scope.RUN_PLAN_APPROVE.value}'; a read-only viewer or a platform operator "
                "(holding no business scope) cannot perform it"
            )

        # 2) The run must exist WITHIN this business (an unknown / cross-tenant run is not-found).
        run = DiscoveryRunStore(self._session, self._business_id).get(run_id)
        if run is None:
            raise RunFinalizationError(
                REASON_RUN_NOT_FOUND,
                "no such discovery run in this business (an unknown or cross-tenant id is not-found)",
                detail=run_id,
            )

        # 3) Idempotent replay — a run finalizes ONCE; return the existing summary unchanged.
        existing = self.summary_for(run_id)
        if existing is not None:
            return self._result_from(existing, already_finalized=True)

        # 4) Aggregate the run's recorded outcomes honestly (counts mirror the rows, reasons named).
        executions = self._executions(run_id)
        task_count = len(executions)
        done_count = sum(1 for e in executions if e.status == DiscoveryTaskStatus.DONE.value)
        failed_count = sum(1 for e in executions if e.status == DiscoveryTaskStatus.FAILED.value)
        denied_count = sum(1 for e in executions if e.status == DiscoveryTaskStatus.DENIED.value)

        reason_counts: dict[str, int] = {}
        for e in executions:
            if e.status == DiscoveryTaskStatus.DONE.value:
                continue
            code = e.reason_code or "unspecified"
            reason_counts[code] = reason_counts.get(code, 0) + 1

        reserved_cost_minor = self._reserved_cost_minor(executions)
        resolution = self._resolution(run_id)
        subject_count = resolution.subject_count if resolution is not None else 0
        matched_count = resolution.matched_count if resolution is not None else 0
        suggestion_count = self._suggestion_count(run_id)

        # Honest terminal status: COMPLETED only on genuine successful work (>=1 DONE task); otherwise
        # FAILED — a run whose tasks all failed / were denied, or that executed nothing (empty), is NEVER
        # fabricated as success.
        empty = task_count == 0
        if done_count > 0:
            final_status = DiscoveryRunStatus.COMPLETED.value
        else:
            final_status = DiscoveryRunStatus.FAILED.value
        partial_failure = done_count > 0 and (failed_count + denied_count) > 0

        cursor = self._build_cursor(executions, resolution)

        summary = self._stage_summary(
            run=run,
            final_status=final_status,
            task_count=task_count,
            done_count=done_count,
            failed_count=failed_count,
            denied_count=denied_count,
            reserved_cost_minor=reserved_cost_minor,
            subject_count=subject_count,
            matched_count=matched_count,
            suggestion_count=suggestion_count,
            partial_failure=partial_failure,
            empty=empty,
            reason_counts=reason_counts,
            cursor=cursor,
            actor=actor,
            moment=moment,
        )
        return self._result_from(summary, already_finalized=False)

    # -- internals ----------------------------------------------------------------------------------

    def _build_cursor(
        self,
        executions: list[DiscoveryTaskExecution],
        resolution: ObservationBatchResolution | None,
    ) -> dict:
        """The durable checkpoint cursor: which task indices executed, the aggregate import counts and the
        opaque, checkpointable row references, plus the resolved-batch breakdown. Holds NO contact value —
        only counts, task indices and opaque row refs."""
        executed_indices = [e.task_index for e in executions]
        imported = skipped = refused = 0
        imported_row_refs: list[str] = []
        for e in executions:
            staged = e.staged_result or {}
            imported += int(staged.get("importedCount") or 0)
            skipped += int(staged.get("skippedCount") or 0)
            refused += int(staged.get("refusedCount") or 0)
            imported_row_refs.extend(staged.get("importedRowRefs") or [])
        cursor: dict = {
            "executedTaskIndices": executed_indices,
            "lastTaskIndex": executed_indices[-1] if executed_indices else None,
            "importedCount": imported,
            "skippedCount": skipped,
            "refusedCount": refused,
            # Opaque, checkpointable row references only — never prospect contact data.
            "importedRowRefs": imported_row_refs,
            "resolved": resolution is not None,
        }
        if resolution is not None:
            cursor["resolution"] = {
                "subjectCount": resolution.subject_count,
                "matchedCount": resolution.matched_count,
                "ambiguousCount": resolution.ambiguous_count,
                "distinctCount": resolution.distinct_count,
                "reviewSuggestionCount": resolution.review_suggestion_count,
                "fieldsCanonicalized": resolution.fields_canonicalized,
                "correctionsPreserved": resolution.corrections_preserved,
            }
        return cursor

    def _stage_summary(
        self,
        *,
        run: DiscoveryRunRequest,
        final_status: str,
        task_count: int,
        done_count: int,
        failed_count: int,
        denied_count: int,
        reserved_cost_minor: int,
        subject_count: int,
        matched_count: int,
        suggestion_count: int,
        partial_failure: bool,
        empty: bool,
        reason_counts: dict,
        cursor: dict,
        actor: RunFinalizationActor,
        moment: datetime,
    ) -> DiscoveryRunSummary:
        """Stage the summary row, settle the run's terminal status, and record the atomic DCA-026 audit
        entry — all in the caller's unit of work."""
        provenance = {
            "source": FINALIZATION_SOURCE,
            "runId": run.id,
            "fromStatus": run.status,
            "finalStatus": final_status,
            # Load-bearing invariants: finalizing reads already-recorded rows and never sends or charges.
            # ``finalized`` records that the run was settled into a durable terminal summary.
            "finalized": True,
            "sent": False,
            "spent": False,
            "autoApproved": False,
            "partialFailure": partial_failure,
            "empty": empty,
        }
        summary = DiscoveryRunSummary(
            business_id=self._business_id,
            run_id=run.id,
            final_status=final_status,
            task_count=task_count,
            done_count=done_count,
            failed_count=failed_count,
            denied_count=denied_count,
            reserved_cost_minor=reserved_cost_minor,
            per_run_budget_minor=run.per_run_budget_minor,
            subject_count=subject_count,
            matched_count=matched_count,
            suggestion_count=suggestion_count,
            partial_failure=partial_failure,
            empty=empty,
            reason_counts=reason_counts,
            cursor=cursor,
            schema_version=SUMMARY_SCHEMA_VERSION,
            provenance=provenance,
            created_at=moment,
        )
        self._session.add(summary)

        # Settle the run into its terminal status (freeing its logical scope for a fresh run) and record
        # the finalization invariant on the run provenance. Finalizing sends / spends nothing.
        run.status = final_status
        run_provenance = dict(run.provenance or {})
        run_provenance.update({"finalized": True, "sent": False, "spent": False})
        run.provenance = run_provenance

        # Atomic audit (SEC-001/BR-122): staged into the caller's session so it commits with the row.
        # References stay OPAQUE — the run id, the terminal status and the aggregate counts — never a
        # contact value. Finalizing settles a run but sends / charges nothing.
        AuditSink(
            self._session, business_id=self._business_id, actor=actor.as_audit_actor()
        ).record(
            action=AUDIT_ACTION_FINALIZE_RUN,
            reason=(
                f"finalized run '{run.id}' → {final_status}: {done_count} done, {failed_count} failed, "
                f"{denied_count} denied of {task_count} task(s)"
                + (" (partial failure)" if partial_failure else "")
                + (" (empty run)" if empty else "")
            ),
            references={
                "runId": run.id,
                "finalStatus": final_status,
                "taskCount": task_count,
                "doneCount": done_count,
                "failedCount": failed_count,
                "deniedCount": denied_count,
                "reservedCostMinor": reserved_cost_minor,
                "subjectCount": subject_count,
                "matchedCount": matched_count,
                "suggestionCount": suggestion_count,
                "partialFailure": partial_failure,
                "empty": empty,
                "reasonCounts": reason_counts,
            },
        )
        return summary

    def _result_from(
        self, summary: DiscoveryRunSummary, *, already_finalized: bool
    ) -> RunFinalizationResult:
        return RunFinalizationResult(
            business_id=self._business_id,
            summary=summary,
            final_status=summary.final_status,
            task_count=summary.task_count,
            done_count=summary.done_count,
            failed_count=summary.failed_count,
            denied_count=summary.denied_count,
            reserved_cost_minor=summary.reserved_cost_minor,
            subject_count=summary.subject_count,
            matched_count=summary.matched_count,
            suggestion_count=summary.suggestion_count,
            partial_failure=summary.partial_failure,
            empty=summary.empty,
            reason_counts=dict(summary.reason_counts or {}),
            already_finalized=already_finalized,
        )


__all__ = [
    "SUMMARY_SCHEMA_VERSION",
    "AUDIT_ACTION_FINALIZE_RUN",
    "FINALIZATION_SOURCE",
    "REASON_NOT_AUTHORIZED",
    "REASON_RUN_NOT_FOUND",
    "RunFinalizationError",
    "RunFinalizationAuthorizationError",
    "RunFinalizationActor",
    "RunFinalizationResult",
    "DiscoveryRunFinalizer",
]
