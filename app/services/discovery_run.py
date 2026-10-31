"""DCA-140 — create a bounded discovery run (the orchestration envelope).

The single job of this story: persist a business's intent to run ONE bounded discovery cycle — the
envelope the later DCA-141..149 pipeline steps progressively fill (persist a plan, execute a task,
convert to observations, resolve, suggest, finalise) — grounded in the business's CONFIRMED India ICP
and bounded, at creation, to the sources it is ALREADY approved+entitled to and to explicit scope /
budget caps. Creating a run **executes nothing**: it stages a :class:`~app.db.models.DiscoveryRunRequest`
in :attr:`~app.db.models.DiscoveryRunStatus.PENDING` and runs no task, enqueues no job, suggests no
prospect, sends no message and spends nothing.

Every guard is checked fail-closed, BEFORE any row is staged, so a denial leaves no state (the
reproducible-denial acceptance):

- **Feature gate (DCA-021).** With the platform feature off, or the business not enabled / out of
  country, no run can be created.
- **Entitlement freshness (DCA-023).** When enforcement is on, an absent / expired / stale subscription
  refuses the run before anything is staged (a run is the start of chargeable work).
- **Confirmed targeting (DCA-040).** The run is grounded in the ONE confirmed ICP version; an
  unconfirmed profile cannot start a run.
- **No overlapping logical runs.** Only ONE active run may exist per ``(business_id, icp_version)``; a
  second one is refused so duplicate active scope can never start redundant work. A Postgres partial
  unique index backs this app-level guard for concurrency.
- **Bounded by construction.** The source scope is the approved+entitled allowlist resolved now, and the
  scope / budget caps are clamped to their absolute ceilings — a requested value over a ceiling is
  clamped down (recorded honestly in ``clamp_notes``), never widened. Over-scope / over-budget can never
  inflate a run.

The creator stages into the *caller's* session and never commits — the route owns the unit of work (so
the run + its idempotency record commit together), exactly like the sibling DCA-135 / DCA-139 planners.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.feature import dca_feature_enabled
from app.db.models import DiscoveryRunRequest, DiscoveryRunStatus, TenantAgent
from app.entitlements import EntitlementClient
from app.services.budget_store import BudgetLimitStore
from app.services.discovery_planner import (
    MAX_QUERY_CAP,
    MAX_TASK_CAP,
    clamp_query_cap,
    clamp_task_cap,
    resolve_allowed_sources,
)
from app.services.entitlement_gate import (
    EntitlementGateError,
    default_entitlement_client,
    ensure_entitled,
)
from app.services.icp_confirmation import IcpConfirmationStore

# Where a created run records its origin (the provenance ``source``).
RUN_SOURCE = "discovery_run"

# The entitlement capability a discovery run is chargeable work under (same key the legacy cycle uses).
ENTITLEMENT_CAPABILITY = "discovery"

# Hard caps the run is bounded by. The caller may request a smaller cap; it can NEVER exceed the
# absolute ceiling — so "exceed the scope" is impossible regardless of what a caller asks for. The task
# / query ceilings are shared with the DCA-135 planner so a run and its later plan are bounded alike.
DEFAULT_TARGET_CAP = 50
MAX_TARGET_CAP = 500

# Stable refusal codes — each returned BEFORE any run is staged, so a denial changes nothing.
REASON_FEATURE_DISABLED = "dca_feature_disabled"
REASON_TENANT_NOT_ENABLED = "tenant_not_enabled"
REASON_COUNTRY_NOT_ALLOWED = "country_not_allowed"
REASON_NO_CONFIRMED_ICP = "no_confirmed_icp"
REASON_DUPLICATE_ACTIVE_RUN = "duplicate_active_run"
# Entitlement refusals are surfaced as ``entitlement_<reason>`` (e.g. ``entitlement_no_entitlement``).
ENTITLEMENT_REASON_PREFIX = "entitlement_"

# Stable clamp reason codes — why a requested limit was clamped down (recorded on ``clamp_notes``, never
# silently). Each is the creator re-enforcing a bound the caller tried to exceed.
CLAMP_TARGET_CAP = "target_cap_clamped"
CLAMP_TASK_CAP = "task_cap_clamped"
CLAMP_QUERY_CAP = "query_cap_clamped"
CLAMP_BUDGET = "budget_clamped_to_per_run_cap"


def clamp_target_cap(requested: int | None) -> int:
    """The effective target cap: the caller's request (or the default), never above :data:`MAX_TARGET_CAP`."""
    value = DEFAULT_TARGET_CAP if requested is None else int(requested)
    return max(0, min(value, MAX_TARGET_CAP))


@dataclass
class DiscoveryRunResult:
    """The inert, inspectable outcome of one create attempt.

    ``run`` is the staged :class:`~app.db.models.DiscoveryRunRequest` (``None`` on a refusal — a denial
    stages nothing). ``refused_reason`` names the stable refusal code when no run was created. The
    remaining fields echo the bounded scope / limits actually applied and the honest ``clamp_notes``.
    Nothing here is ever executed, sent or spent.
    """

    business_id: str
    run: DiscoveryRunRequest | None = None
    refused_reason: str | None = None
    icp_version: int | None = None
    source_scope: list[dict] = field(default_factory=list)
    target_cap: int = 0
    task_cap: int = 0
    query_cap: int = 0
    per_run_budget_minor: int | None = None
    budget_limit_version: int | None = None
    clamped: bool = False
    clamp_notes: list[dict] = field(default_factory=list)

    @property
    def produced(self) -> bool:
        return self.run is not None


class DiscoveryRunStore:
    """Tenant-scoped reader for discovery runs, bound to one business.

    Construct with the active session and the verified business scope. Every query is confined to that
    business — there is no argument to widen the scope.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    def get(self, run_id: str) -> DiscoveryRunRequest | None:
        """Fetch one run by id WITHIN this business (a cross-tenant / unknown id → ``None``)."""
        if not run_id:
            return None
        stmt = (
            select(DiscoveryRunRequest)
            .where(DiscoveryRunRequest.business_id == self._business_id)
            .where(DiscoveryRunRequest.id == run_id)
        )
        return self._session.scalars(stmt).one_or_none()

    def runs(self) -> list[DiscoveryRunRequest]:
        """Every run recorded in this business, newest first."""
        stmt = (
            select(DiscoveryRunRequest)
            .where(DiscoveryRunRequest.business_id == self._business_id)
            .order_by(DiscoveryRunRequest.created_at.desc(), DiscoveryRunRequest.id)
        )
        return list(self._session.scalars(stmt).all())

    def active_run_for_icp(self, icp_version: int) -> DiscoveryRunRequest | None:
        """The current ACTIVE run for a confirmed ICP version in this business, or ``None``.

        An active run (pending / planned / running) occupies the logical scope; while one exists a second
        run for the same ``(business_id, icp_version)`` cannot be created (the overlap guard). A terminal
        run (completed / failed / cancelled) frees the scope.
        """
        stmt = (
            select(DiscoveryRunRequest)
            .where(DiscoveryRunRequest.business_id == self._business_id)
            .where(DiscoveryRunRequest.icp_version == icp_version)
            .where(DiscoveryRunRequest.status.in_(DiscoveryRunStatus.active_values()))
            .order_by(DiscoveryRunRequest.created_at)
        )
        return self._session.scalars(stmt).first()


def _resolve_source_scope(
    session: Session, business_id: str, *, now: datetime | None
) -> list[dict]:
    """The bounded source scope for a run: the approved+entitled source/field allowlist, right now.

    Reuses the sanctioned DCA-048 resolver (:func:`~app.services.discovery_planner.resolve_allowed_sources`)
    so a run can reference no source the rights chain does not already grant for discovery."""
    allowed = resolve_allowed_sources(session, business_id, now=now)
    return [
        {"sourceKey": key, "permittedFields": list(fields)}
        for key, fields in sorted(allowed.items())
    ]


def create_discovery_run(
    session: Session,
    settings: Settings,
    business_id: str,
    *,
    entitlement_client: EntitlementClient | None = None,
    target_cap: int | None = None,
    task_cap: int | None = None,
    query_cap: int | None = None,
    requested_budget_minor: int | None = None,
    now: datetime | None = None,
) -> DiscoveryRunResult:
    """Create a bounded discovery run for ``business_id`` in the not-yet-executing PENDING state.

    Stages a new :class:`~app.db.models.DiscoveryRunRequest` (uncommitted) grounded in the confirmed ICP,
    bounded to the approved+entitled source scope and the (clamped) scope / budget caps. Returns a
    :class:`DiscoveryRunResult`; raises nothing for the ordinary denials — a disabled feature, a
    not-enabled / out-of-country tenant, a failed entitlement check, no confirmed ICP, or an existing
    active run for the same scope each returns a refusal that stages NO run (fail-closed, no state
    change). Creating a run executes nothing: it runs no task, enqueues no job, suggests no prospect,
    sends no message and spends nothing.
    """
    effective_target = clamp_target_cap(target_cap)
    effective_task = clamp_task_cap(task_cap)
    effective_query = clamp_query_cap(query_cap)

    def _refuse(reason: str) -> DiscoveryRunResult:
        return DiscoveryRunResult(
            business_id=business_id,
            refused_reason=reason,
            target_cap=effective_target,
            task_cap=effective_task,
            query_cap=effective_query,
        )

    # 1) Platform feature gate (DCA-021), checked before any lookup or write so a denial leaves no state.
    if not dca_feature_enabled(settings):
        return _refuse(REASON_FEATURE_DISABLED)

    # 2) Per-business narrowing: the feature being on does not enable any individual business.
    tenant = session.scalar(select(TenantAgent).where(TenantAgent.business_id == business_id))
    if tenant is None or not tenant.enabled:
        return _refuse(REASON_TENANT_NOT_ENABLED)
    if tenant.country not in settings.allowed_countries:
        return _refuse(REASON_COUNTRY_NOT_ALLOWED)

    # 3) Entitlement freshness gate (DCA-023): a chargeable run needs a fresh, active subscription. Off
    #    by default; when on, an absent/expired/stale entitlement fails closed before any run is staged.
    if settings.entitlement_enforcement_enabled:
        client = (
            entitlement_client
            if entitlement_client is not None
            else default_entitlement_client(settings)
        )
        try:
            ensure_entitled(session, settings, client, business_id, ENTITLEMENT_CAPABILITY, now=now)
        except EntitlementGateError as exc:
            return _refuse(f"{ENTITLEMENT_REASON_PREFIX}{exc.reason}")

    # 4) Ground in the confirmed ICP (DCA-040). No confirmed targeting → an unconfirmed profile cannot
    #    start a run; stage nothing.
    icp_version = IcpConfirmationStore(session, business_id).active_version()
    if icp_version is None:
        return _refuse(REASON_NO_CONFIRMED_ICP)

    # 5) Overlap guard: only ONE active run per (business_id, icp_version). A second one would be
    #    redundant work on the same logical scope — refuse before staging anything.
    store = DiscoveryRunStore(session, business_id)
    if store.active_run_for_icp(icp_version) is not None:
        return DiscoveryRunResult(
            business_id=business_id,
            refused_reason=REASON_DUPLICATE_ACTIVE_RUN,
            icp_version=icp_version,
            target_cap=effective_target,
            task_cap=effective_task,
            query_cap=effective_query,
        )

    # 6) Bound the run: the approved+entitled source scope + the clamped scope / budget caps. Record every
    #    clamp honestly — a requested value over a ceiling is clamped down, never widened.
    source_scope = _resolve_source_scope(session, business_id, now=now)
    clamp_notes: list[dict] = []
    if target_cap is not None and int(target_cap) > MAX_TARGET_CAP:
        clamp_notes.append(
            {
                "field": "targetCap",
                "requested": int(target_cap),
                "effective": effective_target,
                "reason": CLAMP_TARGET_CAP,
            }
        )
    if task_cap is not None and int(task_cap) > MAX_TASK_CAP:
        clamp_notes.append(
            {
                "field": "taskCap",
                "requested": int(task_cap),
                "effective": effective_task,
                "reason": CLAMP_TASK_CAP,
            }
        )
    if query_cap is not None and int(query_cap) > MAX_QUERY_CAP:
        clamp_notes.append(
            {
                "field": "queryCap",
                "requested": int(query_cap),
                "effective": effective_query,
                "reason": CLAMP_QUERY_CAP,
            }
        )

    # Budget ceiling: the business's per-run spend cap (BudgetLimit). The run SPENDS NOTHING; this only
    # records the ceiling a future disabled spend path would honour. A requested run budget over the
    # per-run cap is clamped down to it (over-budget → clamped, never widened).
    latest_budget = BudgetLimitStore(session, business_id).latest()
    per_run_cap = latest_budget.per_run_cap_minor if latest_budget is not None else None
    budget_version = latest_budget.version if latest_budget is not None else None
    if requested_budget_minor is None:
        effective_budget = per_run_cap
    elif per_run_cap is not None and int(requested_budget_minor) > per_run_cap:
        effective_budget = per_run_cap
        clamp_notes.append(
            {
                "field": "budgetMinor",
                "requested": int(requested_budget_minor),
                "effective": per_run_cap,
                "reason": CLAMP_BUDGET,
            }
        )
    else:
        effective_budget = int(requested_budget_minor)

    clamped = bool(clamp_notes)
    provenance = {
        "source": RUN_SOURCE,
        "icp_version": icp_version,
        "created": True,
        "executed": False,  # a run is CREATED only — no task is ever run here
        "sent": False,  # no message is ever sent
        "spent": False,  # and nothing is ever spent
        "autoApproved": False,  # and it is never auto-approved (promotion is a disabled future capability)
        "feature_enabled": True,
        "entitlement_enforced": bool(settings.entitlement_enforcement_enabled),
        "target_cap": effective_target,
        "task_cap": effective_task,
        "query_cap": effective_query,
        "per_run_budget_minor": effective_budget,
        "budget_limit_version": budget_version,
        "source_keys": [s["sourceKey"] for s in source_scope],
        "clamped": clamped,
        "clamp_notes": list(clamp_notes),
    }

    # 7) Stage the run in the PENDING (not-yet-executing) state. Insert-only; nothing advances it here.
    run = DiscoveryRunRequest(
        business_id=business_id,
        icp_version=icp_version,
        status=DiscoveryRunStatus.PENDING.value,
        source_scope=source_scope,
        target_cap=effective_target,
        task_cap=effective_task,
        query_cap=effective_query,
        per_run_budget_minor=effective_budget,
        budget_limit_version=budget_version,
        requested={
            "targetCap": target_cap,
            "taskCap": task_cap,
            "queryCap": query_cap,
            "budgetMinor": requested_budget_minor,
        },
        clamped=clamped,
        clamp_notes=clamp_notes,
        provenance=provenance,
    )
    session.add(run)
    return DiscoveryRunResult(
        business_id=business_id,
        run=run,
        icp_version=icp_version,
        source_scope=source_scope,
        target_cap=effective_target,
        task_cap=effective_task,
        query_cap=effective_query,
        per_run_budget_minor=effective_budget,
        budget_limit_version=budget_version,
        clamped=clamped,
        clamp_notes=clamp_notes,
    )


__all__ = [
    "RUN_SOURCE",
    "ENTITLEMENT_CAPABILITY",
    "DEFAULT_TARGET_CAP",
    "MAX_TARGET_CAP",
    "REASON_FEATURE_DISABLED",
    "REASON_TENANT_NOT_ENABLED",
    "REASON_COUNTRY_NOT_ALLOWED",
    "REASON_NO_CONFIRMED_ICP",
    "REASON_DUPLICATE_ACTIVE_RUN",
    "ENTITLEMENT_REASON_PREFIX",
    "CLAMP_TARGET_CAP",
    "CLAMP_TASK_CAP",
    "CLAMP_QUERY_CAP",
    "CLAMP_BUDGET",
    "DiscoveryRunResult",
    "DiscoveryRunStore",
    "clamp_target_cap",
    "create_discovery_run",
]
