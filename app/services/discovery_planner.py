"""DCA-135 — generate a bounded discovery plan (the one AI-capability integration).

The single job of this story: turn the business's CONFIRMED India ICP + the sources it is ALREADY
approved+entitled to into a reviewable, BOUNDED discovery **plan** — a proposal of source tasks a later
story could execute — through the accepted DCA AI chokepoint, with every bound re-enforced on the DCA
side before anything is stored. It composes the already-accepted seams end to end; it builds no second
pipeline and performs no provider/model/paid work of its own:

- **DCA-040 confirmed ICP.** The plan is grounded in the ONE confirmed ICP version
  (:class:`~app.services.icp_confirmation.IcpConfirmationStore`): its industries / size bands / roles /
  geography are the only targeting the plan may use. No confirmed ICP → a refusal that stages nothing.
- **DCA-048 approved+entitled sources.** The sources the plan may use (and the exact fields each may
  request) are resolved ONLY through the sanctioned :class:`~app.services.source_preflight.SourcePreflight`
  gate — a source/field pair is available iff it is catalogued+enabled (DCA-044), platform-approved
  (DCA-045/046) AND the business's entitlement grants ``COLLECT`` on it for the plan's purpose (DCA-047).
- **DCA-128 facade.** The request runs through the ONE facade for the ``discovery_plan`` capability (the
  dev/test fixture transport today — no network, no spend; the live gateway only when configured). The
  trusted context is ONLY the ICP targeting + the allowed-source allowlist + the caps (DCA-131 minimises
  it); no prospect PII and no external content is ever sent.
- **The bound is enforced twice.** The gateway capability's own ``rule_check`` rejects a model output
  that references an un-allowed source, an un-permitted field, a geography outside the ICP, or exceeds a
  cap (a rejected output stages NOTHING here). On top of that, the DCA re-checks and **clamps/excludes**
  every task defensively before storing — so even a permissive transport can never widen the plan.

**Acceptance guarantees (DCA-135):** the stored plan is a DRAFT proposal (``status='draft'``) carrying
its uncertainty (overall confidence) and an HONEST record of everything clamped/dropped and why. It is
**never executed** (no task is run) and **never auto-approved** (promotion is a disabled future
capability). The model cannot add a source, expand India scope, request an un-permitted field, or exceed
the task/query caps — any attempt is excluded, recorded and changes nothing downstream. Everything is
confined to the one ``business_id`` the planner runs for (tenant isolation).

The planner stages into the *caller's* session and never commits — the route owns the unit of work (so
the plan + its idempotency record commit together).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai import (
    AiCapabilityDisabled,
    AiCapabilityFacade,
    AiCapabilityRequest,
    AiContractViolation,
    dca_job_binding,
)
from app.db.models import DiscoveryPlan, SourceOperation
from app.geography import india as india_ref
from app.services.icp_confirmation import IcpConfirmationStore
from app.services.icp_store import IcpStore
from app.services.source_catalog import SourceCatalogStore
from app.services.source_preflight import PreflightRequest, SourcePreflight

# The capability key this story integrates (must be in the DCA-128 allowlist + the gateway registry).
CAPABILITY = "discovery_plan"

# Where a generated plan records its origin (the DCA-037-style provenance ``source``).
PLAN_SOURCE = "discovery_plan"

# The purpose a discovery plan's source access is bound to (rights are purpose-limited, never blanket).
PLAN_PURPOSE = "prospect_discovery"

# Hard caps the plan is bounded by. The caller may request smaller caps; it can NEVER exceed the absolute
# ceilings — so "exceed task limits" is impossible regardless of what a caller or the model asks for.
DEFAULT_TASK_CAP = 5
MAX_TASK_CAP = 10
DEFAULT_QUERY_CAP = 3
MAX_QUERY_CAP = 10

# Stable refusal codes — each returned BEFORE any plan is staged, so a denial changes nothing.
REASON_CAPABILITY_DISABLED = "discovery_plan_capability_disabled"
REASON_OUTPUT_REJECTED = "discovery_plan_output_rejected"
REASON_NO_CONFIRMED_ICP = "no_confirmed_icp"

# Stable exclusion reason codes — why a model-proposed task/field was clamped or dropped (recorded on the
# plan's ``excluded`` list, never silently). Each is the DCA re-enforcing a bound the model tried to cross.
EXCL_SOURCE_NOT_ALLOWED = "source_not_approved_or_entitled"
EXCL_FIELD_NOT_PERMITTED = "field_not_permitted"
EXCL_NO_PERMITTED_FIELDS = "no_permitted_fields"
EXCL_GEOGRAPHY_SCOPE_EXPANSION = "geography_outside_confirmed_icp"
EXCL_TASK_CAP_EXCEEDED = "task_cap_exceeded"
EXCL_QUERY_CAP_CLAMPED = "query_cap_clamped"


def clamp_task_cap(requested: int | None) -> int:
    """The effective task cap: the caller's request (or the default), never above :data:`MAX_TASK_CAP`."""
    value = DEFAULT_TASK_CAP if requested is None else int(requested)
    return max(0, min(value, MAX_TASK_CAP))


def clamp_query_cap(requested: int | None) -> int:
    """The effective per-task query cap: the request (or default), clamped to [1, :data:`MAX_QUERY_CAP`]."""
    value = DEFAULT_QUERY_CAP if requested is None else int(requested)
    return max(1, min(value, MAX_QUERY_CAP))


@dataclass
class PlanningResult:
    """The inert, inspectable outcome of one planning attempt.

    ``plan`` is the staged :class:`~app.db.models.DiscoveryPlan` (``None`` on a refusal — a denial stages
    nothing). ``refused_reason`` names the stable refusal code when no plan was produced. ``tasks`` are
    the bounded tasks actually kept; ``excluded`` is the per-item record of everything clamped/dropped to
    keep the plan in bounds. ``overall_confidence`` is the model's self-reported plan confidence.
    """

    business_id: str
    plan: DiscoveryPlan | None = None
    refused_reason: str | None = None
    tasks: list[dict] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)
    icp_version: int | None = None
    task_cap: int = 0
    query_cap: int = 0
    overall_confidence: float | None = None
    rationale: str = ""
    model_provider: str | None = None
    model_id: str | None = None
    prompt_version: str | None = None
    synthetic: bool = False

    @property
    def produced(self) -> bool:
        return self.plan is not None


def resolve_allowed_sources(
    session: Session, business_id: str, *, now: datetime | None = None
) -> dict[str, list[str]]:
    """The sources (and the exact fields) the business may use for a discovery plan, right now.

    A pure, side-effect-free read: for every catalogued source, a field is "available" iff the sanctioned
    DCA-048 :class:`~app.services.source_preflight.SourcePreflight` gate authorizes ``COLLECT`` on it for
    :data:`PLAN_PURPOSE` — i.e. the source is approved+ENABLED (DCA-044), platform-approved (DCA-045/046)
    AND the business entitlement grants it (DCA-047). A source with no available field is omitted — the
    plan can reference no source, and no field, the rights chain withholds.
    """
    preflight = SourcePreflight(session, business_id)
    allowed: dict[str, list[str]] = {}
    for source in SourceCatalogStore(session).all():
        permitted: list[str] = []
        for field_name in source.fields or []:
            decision = preflight.authorize(
                PreflightRequest(
                    source_key=source.key,
                    operation=SourceOperation.COLLECT.value,
                    field=str(field_name),
                    purpose=PLAN_PURPOSE,
                ),
                now=now,
            )
            if decision.allowed:
                permitted.append(str(field_name))
        if permitted:
            allowed[source.key] = permitted
    return allowed


def _confirmed_icp(session: Session, business_id: str):
    """The confirmed ICP version + its targeting draft for ``business_id``, or ``(None, None)``."""
    version = IcpConfirmationStore(session, business_id).active_version()
    if version is None:
        return None, None
    draft = IcpStore(session, business_id).get_version(version)
    return version, draft


def _model_tasks(structured_output: dict | None) -> list[dict]:
    """The raw task list the gateway returned (no mutation; each item sanity-checked to be a dict)."""
    tasks = (structured_output or {}).get("tasks") or []
    return [t for t in tasks if isinstance(t, dict)]


def _clamp_task(
    task: dict,
    *,
    allowed: dict[str, list[str]],
    icp_geography: set[str],
    query_cap: int,
    excluded: list[dict],
) -> dict | None:
    """Clamp ONE model-proposed task to the DCA bounds, or drop it (recording why). Returns the kept task.

    Enforces, DCA-side: the source must be approved+entitled; only its permitted fields survive; any
    geography outside the confirmed ICP is a scope expansion (the whole task is dropped, India-only); the
    per-task query count is clamped to ``query_cap``. Every clamp/drop appends a stable record to
    ``excluded`` — nothing is removed silently.
    """
    source_key = str(task.get("sourceKey") or "").strip()
    permitted = allowed.get(source_key)
    if permitted is None:
        # The model referenced a source the business is NOT approved+entitled to — never plan against it.
        excluded.append({"sourceKey": source_key, "reason": EXCL_SOURCE_NOT_ALLOWED})
        return None

    permitted_set = set(permitted)
    requested_fields = [str(f) for f in (task.get("fields") or [])]
    if requested_fields:
        kept_fields = [f for f in requested_fields if f in permitted_set]
        for f in requested_fields:
            if f not in permitted_set:
                excluded.append(
                    {"sourceKey": source_key, "field": f, "reason": EXCL_FIELD_NOT_PERMITTED}
                )
    else:
        # No explicit field request → the source's full permitted set (already an allowlist).
        kept_fields = list(permitted)
    if not kept_fields:
        excluded.append({"sourceKey": source_key, "reason": EXCL_NO_PERMITTED_FIELDS})
        return None

    # Geography is the hard India scope bound: ANY code outside the confirmed ICP drops the whole task.
    raw_filters = task.get("filters")
    filters: dict = raw_filters if isinstance(raw_filters, dict) else {}
    requested_geo = [str(g) for g in (filters.get("geography") or [])]
    for geo in requested_geo:
        code = india_ref.normalize_state_ut(geo)
        if code is None or (icp_geography and code not in icp_geography):
            excluded.append(
                {"sourceKey": source_key, "reason": EXCL_GEOGRAPHY_SCOPE_EXPANSION, "detail": geo}
            )
            return None

    requested_queries = int(task.get("maxQueries") or 0)
    max_queries = min(requested_queries, query_cap) if requested_queries > 0 else query_cap
    if requested_queries > query_cap:
        excluded.append(
            {
                "sourceKey": source_key,
                "reason": EXCL_QUERY_CAP_CLAMPED,
                "detail": f"{requested_queries}->{query_cap}",
            }
        )

    # The kept task carries ONLY bounded data: an allowed source, its permitted fields, ICP-only geography.
    kept_geo = [india_ref.normalize_state_ut(g) for g in requested_geo]
    return {
        "sourceKey": source_key,
        "fields": kept_fields,
        "filters": {
            "industries": [str(i) for i in (filters.get("industries") or [])],
            "sizeBands": [str(b) for b in (filters.get("sizeBands") or [])],
            "roles": [str(r) for r in (filters.get("roles") or [])],
            "geography": [g for g in kept_geo if g is not None],
        },
        "maxQueries": max_queries,
    }


def plan_discovery(
    session: Session,
    facade: AiCapabilityFacade | None,
    business_id: str,
    *,
    invocation_key: str,
    task_cap: int | None = None,
    query_cap: int | None = None,
    now: datetime | None = None,
    correlation_id: str | None = None,
) -> PlanningResult:
    """Generate a bounded discovery plan for ``business_id`` through the one DCA AI chokepoint.

    Stages a new :class:`~app.db.models.DiscoveryPlan` version (uncommitted) carrying only the bounded,
    grounded tasks plus the uncertainty + an honest record of everything clamped/dropped. Returns a
    :class:`PlanningResult`; raises nothing for the ordinary denials — a disabled capability/transport, a
    rejected gateway output, or no confirmed ICP each returns a refusal that stages NO plan (fail-closed,
    no state change). ``invocation_key`` is the stable DCA-owned id the facade binding + the gateway
    idempotency key are derived from (so a retry replays, never double-spends).
    """
    effective_task_cap = clamp_task_cap(task_cap)
    effective_query_cap = clamp_query_cap(query_cap)

    # 1) Capability/transport gate (DCA-128, fail-closed). A disabled capability stages nothing.
    if facade is None or not facade.can_invoke(CAPABILITY):
        return PlanningResult(
            business_id=business_id,
            refused_reason=REASON_CAPABILITY_DISABLED,
            task_cap=effective_task_cap,
            query_cap=effective_query_cap,
        )

    # 2) Ground in the confirmed ICP (DCA-040). No confirmed targeting → nothing to plan, stage nothing.
    icp_version, icp = _confirmed_icp(session, business_id)
    if icp_version is None or icp is None:
        return PlanningResult(
            business_id=business_id,
            refused_reason=REASON_NO_CONFIRMED_ICP,
            task_cap=effective_task_cap,
            query_cap=effective_query_cap,
        )

    icp_geography = {str(g) for g in (icp.geography or [])}
    allowed = resolve_allowed_sources(session, business_id, now=now)

    # 3) Build the trusted context (ICP targeting + the allowed-source allowlist + caps) and run the ONE
    #    facade call. No untrusted content, no PII; DCA-131 minimises it. A contract violation (incompatible
    #    response / un-minimizable context) is a fail-closed refusal.
    trusted_context: dict = {
        "icpVersion": icp_version,
        "industries": list(icp.industries or []),
        "sizeBands": list(icp.size_bands or []),
        "roles": list(icp.roles or []),
        "geography": sorted(icp_geography),
        "allowedSources": [
            {"sourceKey": key, "permittedFields": fields} for key, fields in sorted(allowed.items())
        ],
        "maxTasks": effective_task_cap,
        "maxQueriesPerTask": effective_query_cap,
    }
    request = AiCapabilityRequest(
        capability=CAPABILITY,
        business_id=business_id,
        input_context=trusted_context,
        correlation_id=correlation_id,
    )
    binding = dca_job_binding(invocation_key, business_id)
    try:
        result = facade.invoke(request, binding)
    except (AiCapabilityDisabled, AiContractViolation):
        return PlanningResult(
            business_id=business_id,
            refused_reason=REASON_CAPABILITY_DISABLED,
            task_cap=effective_task_cap,
            query_cap=effective_query_cap,
        )

    # 4) Honour the gateway's safety/validation verdict. A failed body, or a failed validation screen
    #    (the capability's own rule_check caught a model that tried to widen its mandate), is unusable —
    #    store NOTHING.
    validation = result.validation
    rejected = (
        not result.succeeded
        or validation.report_status == "failed"
        or validation.status in {"failed", "invalid_schema"}
    )
    if rejected:
        return PlanningResult(
            business_id=business_id,
            refused_reason=REASON_OUTPUT_REJECTED,
            icp_version=icp_version,
            task_cap=effective_task_cap,
            query_cap=effective_query_cap,
            model_provider=result.model_provider,
            model_id=result.model_id,
            prompt_version=result.prompt_version,
            synthetic=result.synthetic,
        )

    # 5) Re-enforce every bound DCA-side: clamp/drop each task against the approved sources + confirmed
    #    ICP + caps. Even a permissive transport can never widen the plan past these.
    excluded: list[dict] = []
    kept: list[dict] = []
    for task in _model_tasks(result.structured_output):
        clamped = _clamp_task(
            task,
            allowed=allowed,
            icp_geography=icp_geography,
            query_cap=effective_query_cap,
            excluded=excluded,
        )
        if clamped is not None:
            kept.append(clamped)

    # 6) Task-count cap — surplus tasks beyond the cap are dropped (recorded), never stored.
    if len(kept) > effective_task_cap:
        for surplus in kept[effective_task_cap:]:
            excluded.append({"sourceKey": surplus["sourceKey"], "reason": EXCL_TASK_CAP_EXCEEDED})
        kept = kept[:effective_task_cap]

    structured = result.structured_output or {}
    overall_confidence = structured.get("confidence")
    rationale = str(structured.get("rationale") or "")
    provenance = {
        "source": PLAN_SOURCE,
        "capability": CAPABILITY,
        "planned": True,
        "executed": False,  # a plan is a PROPOSAL — no task here is ever run
        "autoApproved": False,  # and it is never auto-approved (promotion is a disabled future capability)
        "synthetic": result.synthetic,
        "model_provider": result.model_provider,
        "model_id": result.model_id,
        "prompt_version": result.prompt_version,
        "overall_confidence": overall_confidence,
        "icp_version": icp_version,
        "task_cap": effective_task_cap,
        "query_cap": effective_query_cap,
        "allowed_source_keys": sorted(allowed),
        "excluded": list(excluded),
    }

    # 7) Stage the DRAFT plan version (insert-only; always status='draft'; never executed/approved).
    plan = DiscoveryPlan(
        business_id=business_id,
        version=_next_version(session, business_id),
        icp_version=icp_version,
        status="draft",
        tasks=kept,
        excluded=excluded,
        task_cap=effective_task_cap,
        query_cap=effective_query_cap,
        rationale=rationale,
        overall_confidence=overall_confidence,
        source=PLAN_SOURCE,
        source_version=str(result.prompt_version or ""),
        provenance=provenance,
    )
    session.add(plan)
    return PlanningResult(
        business_id=business_id,
        plan=plan,
        tasks=kept,
        excluded=excluded,
        icp_version=icp_version,
        task_cap=effective_task_cap,
        query_cap=effective_query_cap,
        overall_confidence=overall_confidence,
        rationale=rationale,
        model_provider=result.model_provider,
        model_id=result.model_id,
        prompt_version=result.prompt_version,
        synthetic=result.synthetic,
    )


def _next_version(session: Session, business_id: str) -> int:
    current = session.scalar(
        select(func.max(DiscoveryPlan.version)).where(DiscoveryPlan.business_id == business_id)
    )
    return int(current or 0) + 1


__all__ = [
    "CAPABILITY",
    "PLAN_SOURCE",
    "PLAN_PURPOSE",
    "DEFAULT_TASK_CAP",
    "MAX_TASK_CAP",
    "DEFAULT_QUERY_CAP",
    "MAX_QUERY_CAP",
    "REASON_CAPABILITY_DISABLED",
    "REASON_OUTPUT_REJECTED",
    "REASON_NO_CONFIRMED_ICP",
    "EXCL_SOURCE_NOT_ALLOWED",
    "EXCL_FIELD_NOT_PERMITTED",
    "EXCL_NO_PERMITTED_FIELDS",
    "EXCL_GEOGRAPHY_SCOPE_EXPANSION",
    "EXCL_TASK_CAP_EXCEEDED",
    "EXCL_QUERY_CAP_CLAMPED",
    "PlanningResult",
    "clamp_task_cap",
    "clamp_query_cap",
    "resolve_allowed_sources",
    "plan_discovery",
]
