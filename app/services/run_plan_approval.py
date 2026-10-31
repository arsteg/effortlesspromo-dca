"""DCA-141 — persist an approved run plan (bind a reviewed plan to a bounded discovery run).

This story is the first step of the still-disabled discovery pipeline after the DCA-140 envelope: it
connects the already-accepted DCA-135 planner to a single run stage. An AUTHORIZED business actor
approves a reviewed :class:`~app.db.models.DiscoveryPlan` version onto one of the business's
:class:`~app.db.models.DiscoveryRunRequest` rows, which **freezes** the exact set of bounded tasks the
run would execute and moves the run :attr:`~app.db.models.DiscoveryRunStatus.PENDING` →
:attr:`~app.db.models.DiscoveryRunStatus.PLANNED`. The frozen plan is an immutable checkpoint pinned to
a specific plan version, so a subsequent edit of the plan (a NEW draft version, DCA-135 is insert-only)
never changes what the run committed to.

Approving a plan **executes nothing** (the DCA-141 acceptance): the run only advances PENDING → PLANNED
and records the pinned plan snapshot — no task is run, no job enqueued, no prospect suggested, no message
sent and nothing is spent. A plan cannot execute directly; executing a frozen plan's tasks is the later,
still-disabled DCA-142+ pipeline.

Every guard is checked fail-closed, BEFORE any row is mutated, so a denial leaves the run and plan
exactly as they were (the reproducible-denial acceptance), and rejected steps carry an auditable reason:

- **Authority FIRST (DCA-019).** The actor must hold the business-plane
  :attr:`~app.identity.permissions.Scope.RUN_PLAN_APPROVE` administrator scope for THIS business. An
  ordinary acting user, a read-only viewer and a platform operator (holding no membership) are each
  refused before any state is read or written.
- **Both exist, within the tenant.** The run and the plan version must both exist for this business (an
  unknown / cross-tenant id reads as not-found); either missing is refused and changes nothing.
- **The run is still open.** Only a PENDING run can have a plan persisted onto it; a run already PLANNED
  with a DIFFERENT plan, or already running/terminal, is refused (re-approving the SAME plan is
  idempotent — see below).
- **The plan grounds the same scope.** The plan's confirmed ``icp_version`` must match the run's — a plan
  grounded in different targeting cannot be bound to the run.
- **Bounded by the run's envelope.** Every task in the plan must stay inside the run's own bounds — its
  ``source_scope`` approved+entitled allowlist (source AND permitted fields), its per-task ``query_cap``
  and its ``task_cap``. An over-bounds plan is refused WHOLE (never silently trimmed — the run would
  otherwise freeze a set the actor did not review), with an auditable per-violation record. Over-scope
  can never widen a run.

**Idempotent, atomic, no I/O.** Re-approving the SAME plan version onto an already-PLANNED run returns the
existing binding unchanged (no second transition, no duplicate audit). The store stages into the caller's
session and never commits — the run transition and its atomic DCA-026 audit entry commit together, in the
caller's unit of work. There is no argument to widen the business scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.sink import ActorType, AuditActor, AuditSink
from app.db.models import DiscoveryPlan, DiscoveryRunRequest, DiscoveryRunStatus
from app.identity.permissions import Scope, scopes_for_business_roles
from app.services.discovery_run import DiscoveryRunStore

# The audited action code for approving/persisting a run plan (a controlled code, like the other writes).
AUDIT_ACTION_APPROVE_RUN_PLAN = "discovery_run.plan.approved"

# Where an approved run records its origin on the frozen snapshot provenance.
PLAN_APPROVAL_SOURCE = "run_plan_approval"

# Stable refusal codes — each raised/returned BEFORE the session is mutated, so a refusal changes
# nothing (the run and plan stay exactly as they were).
REASON_NOT_AUTHORIZED = "run_plan_not_authorized"
REASON_RUN_NOT_FOUND = "discovery_run_not_found"
REASON_PLAN_NOT_FOUND = "discovery_plan_version_not_found"
REASON_RUN_NOT_PENDING = "discovery_run_not_pending"
REASON_ICP_MISMATCH = "plan_icp_version_mismatch"
REASON_PLAN_OVER_BOUNDS = "plan_over_run_bounds"

# Stable per-task violation codes — why a plan task breaches the run's bounded envelope (recorded on the
# refusal's ``violations`` list, never silently trimmed). Each is the run re-enforcing a bound.
VIOL_SOURCE_NOT_IN_SCOPE = "source_not_in_run_scope"
VIOL_FIELD_NOT_PERMITTED = "field_not_permitted_by_run_scope"
VIOL_QUERY_CAP_EXCEEDED = "query_cap_exceeded"
VIOL_TASK_CAP_EXCEEDED = "task_cap_exceeded"


class RunPlanApprovalError(Exception):
    """A run-plan approval was refused with a stable ``code``. Raised before the session is mutated, so a
    refusal leaves the run and plan unchanged. ``detail`` names the offending value when useful;
    ``violations`` carries the per-task bound breaches for an over-bounds plan."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        detail: str | None = None,
        violations: list[dict] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail
        self.violations = violations or []


class RunPlanApprovalAuthorizationError(RunPlanApprovalError):
    """The caller is not authorized to approve a run plan.

    A distinct type so an HTTP boundary can map it to a 403 while the other codes map to a 404/409/422.
    Raised FIRST — before any state is read or written. ``code`` is :data:`REASON_NOT_AUTHORIZED`.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


@dataclass(frozen=True)
class RunPlanApprover:
    """The business principal approving a run plan.

    ``subject`` is an opaque actor id (never a name/email) recorded on the run and its atomic audit entry.
    ``roles`` are the caller's business-plane membership roles (from the verified
    :class:`~app.identity.tenant_context.TenantContext`, NEVER a bare token claim). :meth:`may_approve`
    grants the action only when those roles yield
    :attr:`~app.identity.permissions.Scope.RUN_PLAN_APPROVE` — an ordinary acting user and a read-only
    viewer do not hold it (approving a run plan is an administrator action), and a platform operator holds
    no business scope for the tenant.
    """

    subject: str
    roles: tuple[str, ...] = ()
    actor_type: str = "user"

    def may_approve(self) -> bool:
        return Scope.RUN_PLAN_APPROVE in scopes_for_business_roles(self.roles)

    def as_actor(self) -> AuditActor:
        return AuditActor(id=self.subject, type=ActorType(self.actor_type))


@dataclass
class RunPlanApprovalResult:
    """The inert, inspectable outcome of one approval.

    ``run`` is the PLANNED :class:`~app.db.models.DiscoveryRunRequest` with its pinned plan snapshot;
    ``plan_version`` is the exact plan version frozen; ``already_approved`` is ``True`` when this was an
    idempotent re-approval of the same plan (no transition happened). Nothing here is ever executed.
    """

    business_id: str
    run: DiscoveryRunRequest
    plan_version: int
    task_count: int = 0
    already_approved: bool = False


def _run_allowlist(run: DiscoveryRunRequest) -> dict[str, set[str]]:
    """The run's approved+entitled source → permitted-fields allowlist, as a lookup for bounds checks."""
    allowlist: dict[str, set[str]] = {}
    for entry in run.source_scope or []:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("sourceKey") or "").strip()
        if not key:
            continue
        allowlist[key] = {str(f) for f in (entry.get("permittedFields") or [])}
    return allowlist


def _check_bounds(plan: DiscoveryPlan, run: DiscoveryRunRequest) -> list[dict]:
    """Every way the plan's tasks breach the run's bounded envelope, as stable per-item violation records.

    Re-enforces, run-side: each task's source must be in the run's ``source_scope`` allowlist; each
    requested field must be permitted for that source; the per-task query count must not exceed the run's
    ``query_cap``; and the task count must not exceed the run's ``task_cap``. An empty list means the plan
    fits entirely inside the run's envelope. Pure — it mutates nothing.
    """
    violations: list[dict] = []
    allowlist = _run_allowlist(run)
    tasks = [t for t in (plan.tasks or []) if isinstance(t, dict)]

    for index, task in enumerate(tasks):
        source_key = str(task.get("sourceKey") or "").strip()
        permitted = allowlist.get(source_key)
        if permitted is None:
            violations.append(
                {"index": index, "sourceKey": source_key, "reason": VIOL_SOURCE_NOT_IN_SCOPE}
            )
            continue  # no permitted field set to check against for an out-of-scope source
        for f in task.get("fields") or []:
            if str(f) not in permitted:
                violations.append(
                    {
                        "index": index,
                        "sourceKey": source_key,
                        "field": str(f),
                        "reason": VIOL_FIELD_NOT_PERMITTED,
                    }
                )
        requested_queries = int(task.get("maxQueries") or 0)
        if requested_queries > run.query_cap:
            violations.append(
                {
                    "index": index,
                    "sourceKey": source_key,
                    "reason": VIOL_QUERY_CAP_EXCEEDED,
                    "detail": f"{requested_queries}>{run.query_cap}",
                }
            )

    if len(tasks) > run.task_cap:
        violations.append(
            {"reason": VIOL_TASK_CAP_EXCEEDED, "detail": f"{len(tasks)}>{run.task_cap}"}
        )
    return violations


def _frozen_snapshot(
    plan: DiscoveryPlan, run: DiscoveryRunRequest, approver: RunPlanApprover, moment: datetime
) -> dict:
    """The immutable checkpoint pinned onto the run: the exact bounded tasks + caps + grounding approved.

    A deep-ish copy of the plan's bounded tasks (so a later plan edit cannot mutate the run's frozen set)
    plus the grounding/provenance a reviewer sees. Carries only targeting/scope metadata — no prospect
    contact data — and the load-bearing invariants (``executed`` / ``sent`` / ``spent`` all ``False``).
    """
    frozen_tasks = [dict(t) for t in (plan.tasks or []) if isinstance(t, dict)]
    return {
        "source": PLAN_APPROVAL_SOURCE,
        "planId": plan.id,
        "planVersion": plan.version,
        "icpVersion": plan.icp_version,
        "taskCap": run.task_cap,
        "queryCap": run.query_cap,
        "overallConfidence": plan.overall_confidence,
        "planSource": plan.source,
        "planSourceVersion": plan.source_version,
        "tasks": frozen_tasks,
        "taskCount": len(frozen_tasks),
        "sourceKeys": sorted({str(t.get("sourceKey") or "") for t in frozen_tasks} - {""}),
        "approvedBy": approver.subject,
        "approvedByType": approver.actor_type,
        "approvedAt": moment.isoformat(),
        # Load-bearing invariants: approving FREEZES a plan — it executes nothing.
        "executed": False,
        "sent": False,
        "spent": False,
        "autoApproved": False,
    }


def approve_run_plan(
    session: Session,
    business_id: str,
    run_id: str,
    plan_version: int,
    approver: RunPlanApprover,
    *,
    now: datetime | None = None,
) -> RunPlanApprovalResult:
    """Approve + persist plan ``plan_version`` onto run ``run_id`` for ``business_id``; return the result.

    Authorizes the actor FIRST (a caller lacking :attr:`~app.identity.permissions.Scope.RUN_PLAN_APPROVE`
    is refused with :class:`RunPlanApprovalAuthorizationError` before any state is read), then validates —
    the run and the plan version both exist for this business, the run is still PENDING, the plan grounds
    the SAME confirmed ICP version, and every task stays inside the run's bounded envelope — each refusal
    raised with a stable code BEFORE the session is mutated (so a denial changes nothing). On success the
    run moves PENDING → PLANNED, the exact plan version is pinned and its bounded tasks frozen onto the
    run, and an atomic DCA-026 audit entry is staged in the caller's unit of work. Re-approving the SAME
    plan onto an already-PLANNED run is idempotent (returns the existing binding, no second transition).
    Approving executes NOTHING — no task is run, no job enqueued, no message sent and nothing is spent.
    """
    moment = now or datetime.now(UTC)

    # 1) Authority FIRST — an unauthorized caller is refused with no state touched.
    if not approver.may_approve():
        raise RunPlanApprovalAuthorizationError(
            REASON_NOT_AUTHORIZED,
            "approving a discovery run plan requires a business administrator holding "
            f"'{Scope.RUN_PLAN_APPROVE.value}'; an ordinary user or a read-only viewer cannot perform "
            "it, and a platform operator holds no business scope for this tenant",
        )

    # 2) The run must exist WITHIN this business (an unknown / cross-tenant id reads as not-found).
    run = DiscoveryRunStore(session, business_id).get(run_id)
    if run is None:
        raise RunPlanApprovalError(
            REASON_RUN_NOT_FOUND,
            "no such discovery run in this business (an unknown or cross-tenant id is not-found)",
            detail=run_id,
        )

    # 3) The plan version must exist WITHIN this business (likewise not-found for a cross-tenant id).
    plan = session.scalars(
        select(DiscoveryPlan)
        .where(DiscoveryPlan.business_id == business_id)
        .where(DiscoveryPlan.version == plan_version)
    ).one_or_none()
    if plan is None:
        raise RunPlanApprovalError(
            REASON_PLAN_NOT_FOUND,
            f"no discovery plan version {plan_version} exists for this business",
            detail=str(plan_version),
        )

    # 4) Idempotency — re-approving the SAME plan onto an already-PLANNED run returns the existing
    #    binding unchanged (no second transition, no duplicate audit).
    if run.status == DiscoveryRunStatus.PLANNED.value and run.plan_version == plan_version:
        return RunPlanApprovalResult(
            business_id=business_id,
            run=run,
            plan_version=plan_version,
            task_count=len(plan.tasks or []),
            already_approved=True,
        )

    # 5) Only a still-open (PENDING) run can have a plan persisted onto it. A run already PLANNED with a
    #    DIFFERENT plan, or already running/terminal, is refused — the run has moved on.
    if run.status != DiscoveryRunStatus.PENDING.value:
        raise RunPlanApprovalError(
            REASON_RUN_NOT_PENDING,
            f"discovery run '{run.id}' is '{run.status}', not pending — a plan can only be persisted "
            "onto a pending run",
            detail=run.status,
        )

    # 6) The plan must ground the SAME confirmed targeting as the run — a plan grounded in a different
    #    ICP version cannot be bound to this run's scope.
    if plan.icp_version != run.icp_version:
        raise RunPlanApprovalError(
            REASON_ICP_MISMATCH,
            f"plan is grounded in ICP version {plan.icp_version} but the run is scoped to "
            f"{run.icp_version} — a differently-grounded plan cannot be bound to this run",
            detail=f"plan={plan.icp_version}!=run={run.icp_version}",
        )

    # 7) The plan must fit ENTIRELY inside the run's bounded envelope. An over-bounds plan is refused
    #    whole (never silently trimmed) with an auditable per-violation record — over-scope cannot widen.
    violations = _check_bounds(plan, run)
    if violations:
        raise RunPlanApprovalError(
            REASON_PLAN_OVER_BOUNDS,
            f"plan version {plan_version} exceeds the run's bounded envelope and cannot be approved",
            detail=str(len(violations)),
            violations=violations,
        )

    # All checks passed — freeze the plan onto the run and move PENDING → PLANNED. Insert-only on the
    # plan side (its draft row is never mutated); the run advances exactly one state. Nothing executes.
    snapshot = _frozen_snapshot(plan, run, approver, moment)
    run.status = DiscoveryRunStatus.PLANNED.value
    run.plan_version = plan_version
    run.plan_snapshot = snapshot
    run.approved_by = approver.subject
    run.approved_by_type = approver.actor_type
    run.approved_at = moment
    provenance = dict(run.provenance or {})
    provenance.update(
        {
            "planned": True,
            "plan_version": plan_version,
            "plan_id": plan.id,
            "approved_by": approver.subject,
            "approved_at": moment.isoformat(),
            # Reaffirm the run invariants — approving freezes a plan but still runs/sends/spends nothing.
            "executed": False,
            "sent": False,
            "spent": False,
            "autoApproved": False,
        }
    )
    run.provenance = provenance

    # Atomic audit (SEC-001/BR-122): staged into the caller's session so it commits with the transition.
    # References stay OPAQUE — run/plan ids, versions, task count, the scope key and the authority — never
    # a contact value or any raw targeting detail.
    AuditSink(session, business_id=business_id, actor=approver.as_actor()).record(
        action=AUDIT_ACTION_APPROVE_RUN_PLAN,
        reason=(
            f"persisted discovery plan v{plan_version} onto run '{run.id}' "
            f"(pending → planned); {snapshot['taskCount']} task(s) frozen"
        ),
        references={
            "runId": run.id,
            "planId": plan.id,
            "planVersion": plan_version,
            "icpVersion": run.icp_version,
            "taskCount": snapshot["taskCount"],
            "previousStatus": DiscoveryRunStatus.PENDING.value,
            "newStatus": run.status,
            "authorityPlane": "business",
            "authorityScope": Scope.RUN_PLAN_APPROVE.value,
        },
    )

    return RunPlanApprovalResult(
        business_id=business_id,
        run=run,
        plan_version=plan_version,
        task_count=snapshot["taskCount"],
        already_approved=False,
    )


__all__ = [
    "AUDIT_ACTION_APPROVE_RUN_PLAN",
    "PLAN_APPROVAL_SOURCE",
    "REASON_NOT_AUTHORIZED",
    "REASON_RUN_NOT_FOUND",
    "REASON_PLAN_NOT_FOUND",
    "REASON_RUN_NOT_PENDING",
    "REASON_ICP_MISMATCH",
    "REASON_PLAN_OVER_BOUNDS",
    "VIOL_SOURCE_NOT_IN_SCOPE",
    "VIOL_FIELD_NOT_PERMITTED",
    "VIOL_QUERY_CAP_EXCEEDED",
    "VIOL_TASK_CAP_EXCEEDED",
    "RunPlanApprovalError",
    "RunPlanApprovalAuthorizationError",
    "RunPlanApprover",
    "RunPlanApprovalResult",
    "approve_run_plan",
]
