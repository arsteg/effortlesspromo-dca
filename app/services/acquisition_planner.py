"""DCA-139 — draft a first-party acquisition plan (the one AI-capability integration).

The single job of this story: turn the business's CONFIRMED India ICP + its own profile into a
reviewable, BOUNDED first-party acquisition **plan** — a PROPOSAL of COMPLIANT first-party
permission-capture mechanisms (forms / QR / opt-in — the stage-15 first-party capture mechanisms) a
later story could set up — through the accepted DCA AI chokepoint, with every bound re-enforced on the
DCA side before anything is stored. It composes the already-accepted AI seam end to end; it builds no
second pipeline and performs no provider/model/paid work of its own:

- **DCA-040 confirmed ICP.** The plan is grounded in the ONE confirmed ICP version
  (:class:`~app.services.icp_confirmation.IcpConfirmationStore`): its industries / size bands / roles /
  geography shape the plan. No confirmed ICP → a refusal that stages nothing.
- **DCA-036 profile.** The business's latest profile draft (business type / products / languages /
  value proposition) is used ONLY as localisation context — the business's OWN content, never prospect
  data. It is optional: a business with no profile still gets a plan grounded in its ICP.
- **DCA-128 facade.** The request runs through the ONE facade for the ``first_party_acquisition_plan``
  capability (the dev/test fixture transport today — no network, no spend; the live gateway only when
  configured). The trusted context is ONLY the ICP targeting + the profile summary + the permitted
  first-party method allowlist + the cap (DCA-131 minimises it); no prospect PII and no external content.
- **The bound is enforced twice.** The gateway capability's own ``rule_check`` rejects a plan that
  proposes a non-first-party / outbound method, fabricates a contact/recipient list, or exceeds the cap
  (a rejected output stages NOTHING here). On top of that, the DCA re-checks and **clamps/drops** every
  suggestion defensively before storing — so even a permissive transport can never widen the plan into an
  outbound-to-strangers plan or make it carry contacts.

**Acceptance guarantees (DCA-139):** the stored plan is a DRAFT proposal (``status='draft'``) that
proposes ONLY compliant first-party capture methods, carries its uncertainty (overall confidence) and an
HONEST record of everything clamped/dropped and why. A B2C zero-data plan does NOT fabricate contacts or
promise a ready-to-message list. It is **never executed** (nothing is published), **never auto-approved**
(promotion is a disabled future capability), and generates **no send and no recipient consent**
(acceptance of a prospect is not recipient consent). Idempotent: a re-run for the same confirmed ICP
returns the EXISTING plan rather than a duplicate. Everything is confined to the one ``business_id`` the
planner runs for (tenant isolation).

The planner stages into the *caller's* session and never commits — the caller owns the unit of work (so
the plan + its idempotency record commit together).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai import (
    AiCapabilityDisabled,
    AiCapabilityFacade,
    AiCapabilityRequest,
    AiContractViolation,
    dca_job_binding,
)
from app.db.models import AcquisitionPlan
from app.services.icp_confirmation import IcpConfirmationStore
from app.services.icp_store import IcpStore
from app.services.profile_store import ProfileStore

# The capability key this story integrates (must be in the DCA-128 allowlist + the gateway registry).
CAPABILITY = "first_party_acquisition_plan"

# Where a generated plan records its origin (the DCA-037-style provenance ``source``).
PLAN_SOURCE = "first_party_acquisition_plan"

# The bounded allowlist of COMPLIANT first-party permission-capture methods the plan may propose — the
# stage-15 first-party capture mechanisms (forms / QR / opt-in). This is the DCA's own declared set: the
# model may propose no method outside it (the gateway rule_check + the DCA re-enforce the bound). It
# contains ONLY first-party, opt-in capture methods — there is deliberately no outbound-to-strangers,
# purchased-list or scraped method here, so the plan can never become an outbound plan.
FIRST_PARTY_METHODS: tuple[str, ...] = (
    "website_form",
    "landing_page_form",
    "qr_code",
    "in_store_signup",
    "whatsapp_opt_in",
    "sms_opt_in",
    "newsletter_signup",
    "event_signup",
    "referral_link",
    "social_profile_link",
)

# Hard caps the plan is bounded by. The caller may request a smaller cap; it can NEVER exceed the absolute
# ceiling — so "exceed the suggestion limit" is impossible regardless of what a caller or the model asks.
DEFAULT_SUGGESTION_CAP = 5
MAX_SUGGESTION_CAP = 10

# Outbound-to-strangers / non-compliant tactics the plan may NEVER propose. The DCA scans every kept
# suggestion's text for these and drops a match defensively (the gateway rule_check screens the same set).
_OUTBOUND_FORBIDDEN_TERMS: frozenset[str] = frozenset(
    {
        "purchased list", "purchase list", "buy list", "bought list", "rented list", "rent list",
        "buy leads", "purchased leads", "buy contacts", "purchased contacts",
        "scrape", "scraping", "scraped", "harvest", "harvested", "directory harvest",
        "cold email", "cold call", "cold outreach", "cold contact", "cold message", "unsolicited",
        "email blast", "mass email", "spam", "blast to", "third-party list", "third party list",
        "send to strangers", "outbound to strangers",
    }
)  # fmt: skip
# Keys on an output / suggestion that would mean the plan fabricated contacts / promised a ready-to-message
# list — forbidden (a B2C zero-data plan fabricates none). A suggestion carrying any of these is dropped.
_FABRICATED_CONTACT_KEYS: frozenset[str] = frozenset(
    {
        "contacts", "recipients", "leads", "prospects", "emails", "phones", "phonenumbers",
        "audience", "contactlist", "mailinglist", "readylist", "leadlist",
    }
)  # fmt: skip

# Stable refusal codes — each returned BEFORE any plan is staged, so a denial changes nothing.
REASON_CAPABILITY_DISABLED = "acquisition_plan_capability_disabled"
REASON_OUTPUT_REJECTED = "acquisition_plan_output_rejected"
REASON_NO_CONFIRMED_ICP = "no_confirmed_icp"

# Stable exclusion reason codes — why a model-proposed suggestion was clamped or dropped (recorded on the
# plan's ``excluded`` list, never silently). Each is the DCA re-enforcing a bound the model tried to cross.
EXCL_METHOD_NOT_ALLOWED = "method_not_first_party"
EXCL_OUTBOUND_METHOD = "outbound_or_noncompliant_tactic"
EXCL_FABRICATED_CONTACTS = "fabricated_contacts_dropped"
EXCL_SUGGESTION_CAP_EXCEEDED = "suggestion_cap_exceeded"


def clamp_suggestion_cap(requested: int | None) -> int:
    """The effective suggestion cap: the caller's request (or default), never above the ceiling."""
    value = DEFAULT_SUGGESTION_CAP if requested is None else int(requested)
    return max(0, min(value, MAX_SUGGESTION_CAP))


def _coerce_confidence(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


@dataclass
class AcquisitionPlanResult:
    """The inert, inspectable outcome of one planning attempt.

    ``plan`` is the staged :class:`~app.db.models.AcquisitionPlan` (``None`` on a refusal — a denial
    stages nothing). ``refused_reason`` names the stable refusal code when no plan was produced.
    ``suggestions`` are the bounded, compliant first-party suggestions actually kept; ``excluded`` is the
    per-item record of everything clamped/dropped to keep the plan first-party and in bounds.
    ``overall_confidence`` is the model's self-reported plan confidence. Nothing here is ever executed,
    published, or sent."""

    business_id: str
    plan: AcquisitionPlan | None = None
    refused_reason: str | None = None
    suggestions: list[dict] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)
    icp_version: int | None = None
    profile_version: int | None = None
    suggestion_cap: int = 0
    overall_confidence: float | None = None
    rationale: str = ""
    model_provider: str | None = None
    model_id: str | None = None
    prompt_version: str | None = None
    synthetic: bool = False

    @property
    def produced(self) -> bool:
        return self.plan is not None


class AcquisitionPlanStore:
    """Tenant-scoped reader/writer for first-party acquisition plans, bound to one business.

    Construct with the active session and the verified business scope. Every query is confined to that
    business — there is no argument to widen the scope. The store writes only draft proposals; it never
    publishes an asset, acquires a contact, or sends anything."""

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    def get(self, plan_id: str) -> AcquisitionPlan | None:
        """Fetch one plan by id WITHIN this business (a cross-tenant / unknown id → ``None``)."""
        if not plan_id:
            return None
        stmt = (
            select(AcquisitionPlan)
            .where(AcquisitionPlan.business_id == self._business_id)
            .where(AcquisitionPlan.id == plan_id)
        )
        return self._session.scalars(stmt).one_or_none()

    def plans(self) -> list[AcquisitionPlan]:
        """Every plan recorded in this business, oldest first."""
        stmt = (
            select(AcquisitionPlan)
            .where(AcquisitionPlan.business_id == self._business_id)
            .order_by(AcquisitionPlan.created_at, AcquisitionPlan.id)
        )
        return list(self._session.scalars(stmt).all())

    def existing_for_icp(self, icp_version: int) -> AcquisitionPlan | None:
        """The current plan for a confirmed ICP version in this business, or ``None`` (idempotency key)."""
        stmt = (
            select(AcquisitionPlan)
            .where(AcquisitionPlan.business_id == self._business_id)
            .where(AcquisitionPlan.icp_version == icp_version)
        )
        return self._session.scalars(stmt).first()


def _confirmed_icp(session: Session, business_id: str):
    """The confirmed ICP version + its targeting draft for ``business_id``, or ``(None, None)``."""
    version = IcpConfirmationStore(session, business_id).active_version()
    if version is None:
        return None, None
    draft = IcpStore(session, business_id).get_version(version)
    return version, draft


def _model_suggestions(structured_output: dict | None) -> list[dict]:
    """The raw suggestion list the gateway returned (no mutation; each item sanity-checked to be a dict)."""
    suggestions = (structured_output or {}).get("suggestions") or []
    return [s for s in suggestions if isinstance(s, dict)]


def _clean_suggestion(
    suggestion: dict,
    *,
    permitted_methods: set[str],
    excluded: list[dict],
) -> dict | None:
    """Clean ONE model-proposed suggestion to the DCA bounds, or drop it (recording why). Returns the kept
    suggestion.

    Enforces, DCA-side: the method must be a permitted COMPLIANT first-party method; a suggestion carrying
    a fabricated contact/recipient key is dropped (no ready-to-message list); any suggestion whose text
    proposes an outbound-to-strangers / purchased-list / scraped tactic is dropped (first-party only).
    Every drop appends a stable record to ``excluded`` — nothing is removed silently."""
    method = str(suggestion.get("method") or "").strip()
    if method not in permitted_methods:
        excluded.append({"method": method, "reason": EXCL_METHOD_NOT_ALLOWED})
        return None

    # The plan may never fabricate a contact/recipient list — not even inside one suggestion.
    if any(str(k).strip().lower() in _FABRICATED_CONTACT_KEYS for k in suggestion):
        excluded.append({"method": method, "reason": EXCL_FABRICATED_CONTACTS})
        return None

    offer = str(suggestion.get("offer") or "").strip()
    placement = str(suggestion.get("placement") or "").strip()
    raw_channel = suggestion.get("channel")
    channel = str(raw_channel).strip() if raw_channel else None
    rationale = str(suggestion.get("rationale") or "").strip()

    haystack = " ".join([method, offer, placement, channel or "", rationale]).lower()
    for term in _OUTBOUND_FORBIDDEN_TERMS:
        if term in haystack:
            excluded.append({"method": method, "reason": EXCL_OUTBOUND_METHOD, "detail": term})
            return None

    return {
        "method": method,
        "offer": offer,
        "placement": placement,
        "channel": channel,
        "rationale": rationale,
        "confidence": _coerce_confidence(suggestion.get("confidence")),
    }


def draft_acquisition_plan(
    session: Session,
    facade: AiCapabilityFacade | None,
    business_id: str,
    *,
    invocation_key: str,
    suggestion_cap: int | None = None,
    now: datetime | None = None,
    correlation_id: str | None = None,
) -> AcquisitionPlanResult:
    """Draft a bounded first-party acquisition plan for ``business_id`` through the one DCA AI chokepoint.

    Stages a new :class:`~app.db.models.AcquisitionPlan` (uncommitted) carrying only the bounded,
    compliant first-party suggestions plus the uncertainty + an honest record of everything clamped/dropped.
    Returns an :class:`AcquisitionPlanResult`; raises nothing for the ordinary denials — a disabled
    capability/transport, a rejected gateway output, or no confirmed ICP each returns a refusal that stages
    NO plan (fail-closed, no state change). Idempotent: a plan already stored for the confirmed ICP version
    returns the EXISTING row (no model call, no duplicate). ``invocation_key`` is the stable DCA-owned id
    the facade binding + the gateway idempotency key are derived from (so a retry replays, never
    double-spends). The plan is a PROPOSAL — nothing is published, no contact fabricated, and no send or
    recipient consent is ever generated."""
    effective_cap = clamp_suggestion_cap(suggestion_cap)

    # 1) Capability/transport gate (DCA-128, fail-closed). A disabled capability stages nothing.
    if facade is None or not facade.can_invoke(CAPABILITY):
        return AcquisitionPlanResult(
            business_id=business_id,
            refused_reason=REASON_CAPABILITY_DISABLED,
            suggestion_cap=effective_cap,
        )

    # 2) Ground in the confirmed ICP (DCA-040). No confirmed targeting → nothing to plan, stage nothing.
    icp_version, icp = _confirmed_icp(session, business_id)
    if icp_version is None or icp is None:
        return AcquisitionPlanResult(
            business_id=business_id,
            refused_reason=REASON_NO_CONFIRMED_ICP,
            suggestion_cap=effective_cap,
        )

    # 3) Idempotency — a plan already stored for this confirmed ICP version returns the EXISTING row (no
    #    dup, no model call). A fresh confirmed ICP version is what triggers a new plan.
    store = AcquisitionPlanStore(session, business_id)
    existing = store.existing_for_icp(icp_version)
    if existing is not None:
        return AcquisitionPlanResult(
            business_id=business_id,
            plan=existing,
            suggestions=list(existing.suggestions or []),
            excluded=list(existing.excluded or []),
            icp_version=existing.icp_version,
            profile_version=existing.profile_version,
            suggestion_cap=existing.suggestion_cap,
            overall_confidence=existing.overall_confidence,
            rationale=existing.rationale,
            prompt_version=existing.source_version,
        )

    # 4) Localisation context — the business's OWN latest profile draft (never prospect data; optional).
    profile = ProfileStore(session, business_id).latest_draft()
    profile_version = profile.version if profile is not None else None

    # 5) Build the trusted context (ICP targeting + profile summary + the permitted first-party method
    #    allowlist + the cap) and run the ONE facade call. No untrusted content, no PII; DCA-131 minimises
    #    it. A contract violation (incompatible response / un-minimizable context) is a fail-closed refusal.
    trusted_context: dict = {
        "icpVersion": icp_version,
        "industries": list(icp.industries or []),
        "sizeBands": list(icp.size_bands or []),
        "roles": list(icp.roles or []),
        "geography": list(icp.geography or []),
        "permittedMethods": list(FIRST_PARTY_METHODS),
        "maxSuggestions": effective_cap,
    }
    if profile is not None:
        trusted_context["businessType"] = profile.business_type or ""
        trusted_context["products"] = list(profile.products or [])
        trusted_context["languages"] = list(profile.languages or [])
        trusted_context["valueProposition"] = profile.value_proposition or ""

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
        return AcquisitionPlanResult(
            business_id=business_id,
            refused_reason=REASON_CAPABILITY_DISABLED,
            suggestion_cap=effective_cap,
        )

    # 6) Honour the gateway's safety/validation verdict. A failed body, or a failed validation screen (the
    #    capability's own rule_check caught a non-first-party / outbound / fabricated-contact plan), is
    #    unusable — store NOTHING.
    validation = result.validation
    rejected = (
        not result.succeeded
        or validation.report_status == "failed"
        or validation.status in {"failed", "invalid_schema"}
    )
    if rejected:
        return AcquisitionPlanResult(
            business_id=business_id,
            refused_reason=REASON_OUTPUT_REJECTED,
            icp_version=icp_version,
            profile_version=profile_version,
            suggestion_cap=effective_cap,
            model_provider=result.model_provider,
            model_id=result.model_id,
            prompt_version=result.prompt_version,
            synthetic=result.synthetic,
        )

    # 7) Re-enforce every bound DCA-side: clamp/drop each suggestion against the permitted first-party
    #    method allowlist + the no-outbound / no-fabricated-contact rules. Even a permissive transport can
    #    never widen the plan into an outbound-to-strangers plan or make it carry contacts.
    permitted_methods = set(FIRST_PARTY_METHODS)
    excluded: list[dict] = []
    kept: list[dict] = []
    for suggestion in _model_suggestions(result.structured_output):
        cleaned = _clean_suggestion(
            suggestion, permitted_methods=permitted_methods, excluded=excluded
        )
        if cleaned is not None:
            kept.append(cleaned)

    # 8) Suggestion-count cap — surplus suggestions beyond the cap are dropped (recorded), never stored.
    if len(kept) > effective_cap:
        for surplus in kept[effective_cap:]:
            excluded.append({"method": surplus["method"], "reason": EXCL_SUGGESTION_CAP_EXCEEDED})
        kept = kept[:effective_cap]

    structured = result.structured_output or {}
    overall_confidence = _coerce_confidence(structured.get("confidence"))
    rationale = str(structured.get("rationale") or "")
    provenance = {
        "source": PLAN_SOURCE,
        "capability": CAPABILITY,
        "planned": True,
        "executed": False,  # a plan is a PROPOSAL — nothing here is ever run
        "published": False,  # no asset is published
        "sent": False,  # and no message is ever sent
        "autoApproved": False,  # and it is never auto-approved (promotion is a disabled future capability)
        "fabricatedContacts": False,  # a zero-data plan fabricates no contacts / ready list
        "synthetic": result.synthetic,
        "model_provider": result.model_provider,
        "model_id": result.model_id,
        "prompt_version": result.prompt_version,
        "overall_confidence": overall_confidence,
        "icp_version": icp_version,
        "profile_version": profile_version,
        "suggestion_cap": effective_cap,
        "permitted_methods": list(FIRST_PARTY_METHODS),
        "excluded": list(excluded),
    }

    # 9) Stage the DRAFT plan (insert-only; always status='draft'; never executed/published/approved).
    plan = AcquisitionPlan(
        business_id=business_id,
        icp_version=icp_version,
        profile_version=profile_version,
        status="draft",
        suggestions=kept,
        excluded=excluded,
        suggestion_cap=effective_cap,
        rationale=rationale,
        overall_confidence=overall_confidence,
        source=PLAN_SOURCE,
        source_version=str(result.prompt_version or ""),
        provenance=provenance,
    )
    session.add(plan)
    return AcquisitionPlanResult(
        business_id=business_id,
        plan=plan,
        suggestions=kept,
        excluded=excluded,
        icp_version=icp_version,
        profile_version=profile_version,
        suggestion_cap=effective_cap,
        overall_confidence=overall_confidence,
        rationale=rationale,
        model_provider=result.model_provider,
        model_id=result.model_id,
        prompt_version=result.prompt_version,
        synthetic=result.synthetic,
    )


__all__ = [
    "CAPABILITY",
    "PLAN_SOURCE",
    "FIRST_PARTY_METHODS",
    "DEFAULT_SUGGESTION_CAP",
    "MAX_SUGGESTION_CAP",
    "REASON_CAPABILITY_DISABLED",
    "REASON_OUTPUT_REJECTED",
    "REASON_NO_CONFIRMED_ICP",
    "EXCL_METHOD_NOT_ALLOWED",
    "EXCL_OUTBOUND_METHOD",
    "EXCL_FABRICATED_CONTACTS",
    "EXCL_SUGGESTION_CAP_EXCEEDED",
    "AcquisitionPlanResult",
    "AcquisitionPlanStore",
    "clamp_suggestion_cap",
    "draft_acquisition_plan",
]
