"""DCA-138 — explain business fit (the one AI-capability integration).

The single job of this story: produce a grounded, evidence-cited EXPLANATION of why a prospect fits (or
does not fit) the business's confirmed ICP, through the accepted DCA AI chokepoint. It composes the
already-accepted AI seam end to end and performs no provider/model/paid work of its own. The
load-bearing guarantees (the DCA-138 acceptance criteria):

- **Fit is STRICTLY separate from eligibility — a high fit can NEVER change a channel restriction.**
  Eligibility (the DCA-055 personal-data permission decision: may this record be acted on at all?) is a
  DIFFERENT axis from fit (how desirable is the prospect?). This service CARRIES the caller's pre-decided
  permission outcome through *verbatim* on the stored explanation's separate ``eligibility`` /
  ``eligibility_reason`` fields; it is NEVER derived from the fit, and it is NEVER sent to the model. So
  a top ``high`` fit leaves a ``blocked`` eligibility exactly ``blocked`` — fit describes desirability,
  it grants nothing (the DCA-073 quality-summary separation, applied to the AI explanation).

- **Only grounded, cited claims survive; the gateway's "valid" does not authorize a write.** Each
  model-proposed assessment is independently re-validated against the SUPPORTED FACTS the DCA actually
  holds (:func:`~app.ai.citation_validator.validate_citations`): an assessment citing a fact ref the DCA
  did not supply, or a field that fact is not about, or a span not present in the fact, is DROPPED (an
  ungrounded claim changes nothing) and recorded in ``dropped``.

- **Sensitive inferred traits are never a scoring input.** Before any egress the supported facts are
  screened through the DCA-058 field-minimization screen
  (:func:`~app.compliance.field_minimization.classify_field`); a prohibited personal class
  (children/sensitive, private mobile, unnecessary identifier) is EXCLUDED from the model context
  entirely — so it can never be cited or scored on (the gateway capability's own ``rule_check`` + the
  DCA re-enforce that a cited field must be permitted).

- **Advisory, not auto-acted, tenant-scoped, idempotent.** The explanation grants no acquisition/sending
  authority and triggers nothing. Every read/write is confined to the one ``business_id`` the service
  runs for. An explanation already stored for the subject returns the EXISTING row rather than inserting
  a duplicate.

The service stages into the *caller's* session and never commits — the caller owns the unit of work (so
the explanation + its idempotency record commit together). It raises nothing for the ordinary denials: a
disabled capability/transport, a rejected gateway output, or no supported (permitted) facts each return a
refusal that stages NO explanation (fail-closed, no state change).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai import (
    AiCapabilityDisabled,
    AiCapabilityFacade,
    AiCapabilityRequest,
    AiContractViolation,
    CitedFact,
    CitedSource,
    FactVerdict,
    dca_job_binding,
    validate_citations,
)
from app.compliance.field_minimization import PROHIBITED_CLASSES, classify_field, reason_for
from app.compliance.policy_engine import PolicyDecision
from app.db.models import FieldSubjectType, FitBand, FitExplanation
from app.quality.quality_summary import Eligibility

# The capability key this story integrates (must be in the DCA-128 allowlist + the gateway registry).
CAPABILITY = "business_fit_explanation"

# Where a stored explanation records its origin.
EXPLANATION_SOURCE = CAPABILITY

# This story explains ORGANIZATION fit (the AI reasons over company facts only). The subject-type column
# is generic; person fit is a future extension.
SUBJECT_TYPE = FieldSubjectType.ORGANIZATION.value

# The ICP dimensions the explanation is grounded against (the business's own confirmed targeting).
ICP_DIMENSIONS: tuple[str, ...] = ("industries", "sizeBands", "roles", "geography")

# A fit verdict is a DERIVED judgement, not a directly-quoted value, so every assessment is grounded as
# an INFERRED citation (its cited span must appear in the supported fact, but the verdict need not).
_FIT_INFERENCE_LABEL = "inferred"

# Max rationale length retained on a stored explanation (opaque, bounded; never free-form contact data).
_MAX_RATIONALE_CHARS = 500

# Stable refusal codes — each returned BEFORE any explanation is staged, so a denial changes nothing (a
# disabled capability / no permitted facts never even calls the model).
REASON_CAPABILITY_DISABLED = "fit_explanation_capability_disabled"
REASON_OUTPUT_REJECTED = "fit_explanation_output_rejected"
REASON_NO_SUPPORTED_FACTS = "fit_explanation_no_supported_facts"

# Stable exclusion reason code — a supported fact dropped BEFORE egress because it is a prohibited
# personal class (so a sensitive inferred trait can never become a scoring input).
EXCL_SENSITIVE_TRAIT = "excluded_sensitive_trait"

# Map a DCA-055 PolicyDecision onto the separate eligibility axis (mirrors the DCA-073 quality summary).
# A ``DENY`` surfaces as ``blocked``. Eligibility is CARRIED, never computed from the fit.
_DECISION_TO_ELIGIBILITY: dict[PolicyDecision, Eligibility] = {
    PolicyDecision.ALLOW: Eligibility.ALLOWED,
    PolicyDecision.DENY: Eligibility.BLOCKED,
    PolicyDecision.REVIEW: Eligibility.REVIEW,
    PolicyDecision.NOT_APPLICABLE: Eligibility.NOT_APPLICABLE,
}


@dataclass(frozen=True)
class SupportedFact:
    """One already-gathered, tenant-scoped SUPPORTED FACT about the prospect — the ONLY material an
    assessment may cite.

    ``ref`` is the stable evidence reference an assessment cites (an observation/evidence id the DCA
    holds). ``field`` is the (company-fact) field it is about; ``value`` is its value, the span a cited
    assessment must rest on. A fact whose ``field`` classifies as a prohibited personal class (DCA-058)
    is EXCLUDED before egress — it never reaches the model and can never be cited."""

    ref: str
    field: str
    value: str


@dataclass(frozen=True)
class EligibilityStatus:
    """The caller's pre-decided permission outcome (DCA-055), carried through the explanation UNCHANGED.

    This is NOT a fit input. ``decision`` is the DCA-055 :class:`~app.compliance.policy_engine.PolicyDecision`
    (or its string value); ``reason`` is that decision's stable reason code. The service copies both onto
    the stored explanation verbatim — it never derives eligibility from the fit and never sends it to the
    model, so no amount of fit can flip a ``blocked`` eligibility to ``allowed``."""

    decision: PolicyDecision | str | None = None
    reason: str = ""


def carry_eligibility(status: EligibilityStatus) -> tuple[str, str]:
    """Carry the caller's permission decision through UNCHANGED (never derived from the fit).

    Returns ``(eligibility_value, reason)`` where the value is the stable DCA-073 vocabulary
    (``allowed`` / ``blocked`` / ``review`` / ``not_applicable`` / ``unknown``). An unrecognized /
    missing decision fails safe to ``unknown`` (treated as not-permitted by any consumer)."""
    decision = status.decision
    if isinstance(decision, PolicyDecision):
        eligibility = _DECISION_TO_ELIGIBILITY[decision]
    elif isinstance(decision, str) and decision:
        try:
            eligibility = _DECISION_TO_ELIGIBILITY[PolicyDecision(decision)]
        except ValueError:
            eligibility = Eligibility.UNKNOWN
    else:
        eligibility = Eligibility.UNKNOWN
    return eligibility.value, status.reason


@dataclass
class FitExplanationResult:
    """The inert, inspectable outcome of one fit-explanation run (advisory; inert on a denial).

    ``explanation`` is the staged :class:`~app.db.models.FitExplanation` (``None`` on a refusal — a denial
    stages nothing). ``refused_reason`` names the stable refusal code when none was produced. ``grounded``
    are the assessments whose citations were confirmed (the only ones written); ``dropped`` are the
    per-claim verdicts for everything the grounding gate rejected. ``excluded_sensitive`` names the
    supported-fact fields dropped before egress as a prohibited personal class. ``eligibility`` /
    ``eligibility_reason`` are the caller's permission outcome carried through UNCHANGED (never derived
    from the fit). Nothing here ever changed an eligibility or acted on the prospect."""

    business_id: str
    subject_ref: str
    subject_type: str = SUBJECT_TYPE
    explanation: FitExplanation | None = None
    refused_reason: str | None = None
    grounded: tuple[dict, ...] = ()
    dropped: tuple[FactVerdict, ...] = ()
    excluded_sensitive: list[str] = field(default_factory=list)
    eligibility: str = Eligibility.UNKNOWN.value
    eligibility_reason: str = ""
    fit_band: str = FitBand.UNKNOWN.value
    overall_confidence: float | None = None
    model_provider: str | None = None
    model_id: str | None = None
    prompt_version: str | None = None
    synthetic: bool = False

    @property
    def produced(self) -> bool:
        return self.explanation is not None


class FitExplanationStore:
    """Tenant-scoped reader/writer for business-fit explanations, bound to one business.

    Construct with the active session and the verified business scope. Every query is confined to that
    business — there is no argument to widen the scope. The store writes only advisory explanations; it
    never changes eligibility and never touches consent/restriction state."""

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    def get(self, explanation_id: str) -> FitExplanation | None:
        """Fetch one explanation by id WITHIN this business (a cross-tenant / unknown id → ``None``)."""
        if not explanation_id:
            return None
        stmt = (
            select(FitExplanation)
            .where(FitExplanation.business_id == self._business_id)
            .where(FitExplanation.id == explanation_id)
        )
        return self._session.scalars(stmt).one_or_none()

    def explanations(self) -> list[FitExplanation]:
        """Every explanation recorded in this business, oldest first."""
        stmt = (
            select(FitExplanation)
            .where(FitExplanation.business_id == self._business_id)
            .order_by(FitExplanation.created_at, FitExplanation.id)
        )
        return list(self._session.scalars(stmt).all())

    def existing_for_subject(self, subject_type: str, subject_ref: str) -> FitExplanation | None:
        """The current explanation for a prospect subject in this business, or ``None`` (idempotency key)."""
        stmt = (
            select(FitExplanation)
            .where(FitExplanation.business_id == self._business_id)
            .where(FitExplanation.subject_type == subject_type)
            .where(FitExplanation.subject_ref == subject_ref)
        )
        return self._session.scalars(stmt).first()

    def stage(
        self,
        *,
        subject_ref: str,
        icp_version: int | None,
        fit_band: str,
        rationale: str,
        assessments: list[dict],
        dropped: list[dict],
        eligibility: str,
        eligibility_reason: str,
        overall_confidence: float | None,
        prompt_version: str,
        synthetic: bool,
        now: datetime,
    ) -> FitExplanation:
        """Stage ONE advisory explanation (uncommitted). ``eligibility`` is the caller's outcome, verbatim."""
        explanation = FitExplanation(
            business_id=self._business_id,
            subject_type=SUBJECT_TYPE,
            subject_ref=subject_ref,
            icp_version=icp_version,
            fit_band=fit_band,
            rationale=rationale,
            assessments=assessments,
            dropped=dropped,
            eligibility=eligibility,
            eligibility_reason=eligibility_reason,
            overall_confidence=overall_confidence,
            source=EXPLANATION_SOURCE,
            prompt_version=prompt_version,
            synthetic=synthetic,
            created_at=now,
        )
        self._session.add(explanation)
        return explanation


def _icp_values(icp: Mapping[str, object] | None, dim: str) -> list[str]:
    """The confirmed-ICP values for one dimension as a list of strings (empty when absent/non-list)."""
    raw = (icp or {}).get(dim)
    if isinstance(raw, (list, tuple)):
        return [str(v) for v in raw]
    return []


def _coerce_confidence(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _screen_supported_facts(
    facts: Sequence[SupportedFact],
) -> tuple[list[SupportedFact], list[str]]:
    """Drop prohibited personal classes BEFORE egress (DCA-058), keeping only citeable company facts.

    Returns ``(permitted_facts, excluded_field_names)``. A fact missing a ref/field/value is skipped. A
    fact whose field classifies as a prohibited class is excluded (its NAME recorded, never its value) so
    a sensitive inferred trait can never become a scoring input."""
    permitted: list[SupportedFact] = []
    excluded: list[str] = []
    for fact in facts:
        ref = (fact.ref or "").strip()
        field_name = (fact.field or "").strip()
        value = (fact.value or "").strip()
        if not ref or not field_name or not value:
            continue
        if classify_field(field_name, value) in PROHIBITED_CLASSES:
            excluded.append(field_name)
            continue
        permitted.append(SupportedFact(ref=ref, field=field_name, value=value))
    return permitted, excluded


def explain_business_fit(
    session: Session,
    facade: AiCapabilityFacade | None,
    business_id: str,
    *,
    subject_ref: str,
    supported_facts: Sequence[SupportedFact],
    eligibility: EligibilityStatus,
    invocation_key: str,
    icp: Mapping[str, object] | None = None,
    icp_version: int | None = None,
    correlation_id: str | None = None,
    now: datetime | None = None,
) -> FitExplanationResult:
    """Explain ``business_id``'s fit for a prospect through the one DCA AI chokepoint (grounded, advisory).

    Stages a new :class:`~app.db.models.FitExplanation` (uncommitted) carrying only the assessments whose
    citations were confirmed grounded, the advisory fit band + model uncertainty, an honest record of what
    was dropped, and the caller's permission outcome CARRIED THROUGH UNCHANGED (never derived from the fit,
    never sent to the model). Returns a :class:`FitExplanationResult`. A disabled capability/transport, a
    rejected gateway output, or no permitted supported facts each return a refusal that stages NO
    explanation. ``invocation_key`` is the stable DCA-owned id the facade binding + the gateway idempotency
    key are derived from (so a retry replays, never double-spends)."""
    moment = now or datetime.now(UTC)
    carried_eligibility, carried_reason = carry_eligibility(eligibility)

    # 1) Capability/transport gate (DCA-128, fail-closed). A disabled capability stages nothing and never
    #    calls the model. Eligibility is already carried for the result even on a refusal (unchanged).
    if facade is None or not facade.can_invoke(CAPABILITY):
        return FitExplanationResult(
            business_id=business_id,
            subject_ref=subject_ref,
            refused_reason=REASON_CAPABILITY_DISABLED,
            eligibility=carried_eligibility,
            eligibility_reason=carried_reason,
        )

    # 2) Idempotency — an explanation already stored for this subject returns the EXISTING row (no dup, no
    #    model call). The stored eligibility stays exactly as first recorded.
    store = FitExplanationStore(session, business_id)
    existing = store.existing_for_subject(SUBJECT_TYPE, subject_ref)
    if existing is not None:
        return FitExplanationResult(
            business_id=business_id,
            subject_ref=subject_ref,
            explanation=existing,
            fit_band=existing.fit_band,
            eligibility=existing.eligibility,
            eligibility_reason=existing.eligibility_reason,
            overall_confidence=existing.overall_confidence,
            prompt_version=existing.prompt_version,
            synthetic=existing.synthetic,
        )

    # 3) Screen the supported facts (DCA-058): drop prohibited personal classes BEFORE egress so a
    #    sensitive inferred trait is never a scoring input. No permitted fact → refuse, no model call.
    permitted_facts, excluded_sensitive = _screen_supported_facts(supported_facts)
    if not permitted_facts:
        return FitExplanationResult(
            business_id=business_id,
            subject_ref=subject_ref,
            refused_reason=REASON_NO_SUPPORTED_FACTS,
            excluded_sensitive=excluded_sensitive,
            eligibility=carried_eligibility,
            eligibility_reason=carried_reason,
        )

    # 4) Build the TRUSTED context — the business's own ICP criteria + the permitted supported facts +
    #    the permitted-field allowlist. Eligibility is NEVER placed here. DCA-131 minimises it; run the
    #    ONE facade call. A contract violation (incompatible response / un-minimizable context) is a
    #    fail-closed refusal.
    icp_criteria = {dim: _icp_values(icp, dim) for dim in ICP_DIMENSIONS}
    permitted_field_names = sorted({f.field for f in permitted_facts})
    input_context: dict[str, object] = {
        "prospectRef": subject_ref,
        "icp": icp_criteria,
        "supportedFacts": [
            {"ref": f.ref, "field": f.field, "value": f.value} for f in permitted_facts
        ],
        "permittedFields": permitted_field_names,
    }
    if icp_version is not None:
        input_context["icpVersion"] = icp_version

    request = AiCapabilityRequest(
        capability=CAPABILITY,
        business_id=business_id,
        input_context=input_context,
        correlation_id=correlation_id,
    )
    binding = dca_job_binding(invocation_key, business_id)
    try:
        result = facade.invoke(request, binding)
    except (AiCapabilityDisabled, AiContractViolation):
        return FitExplanationResult(
            business_id=business_id,
            subject_ref=subject_ref,
            refused_reason=REASON_CAPABILITY_DISABLED,
            excluded_sensitive=excluded_sensitive,
            eligibility=carried_eligibility,
            eligibility_reason=carried_reason,
        )

    # 5) Honour the gateway's safety/validation verdict. A failed body, or a failed validation screen (the
    #    capability's own rule_check caught invented evidence / an excluded field / an eligibility verdict),
    #    is unusable — store NOTHING.
    validation = result.validation
    rejected = (
        not result.succeeded
        or validation.report_status == "failed"
        or validation.status in {"failed", "invalid_schema"}
    )
    if rejected:
        return FitExplanationResult(
            business_id=business_id,
            subject_ref=subject_ref,
            refused_reason=REASON_OUTPUT_REJECTED,
            excluded_sensitive=excluded_sensitive,
            eligibility=carried_eligibility,
            eligibility_reason=carried_reason,
            model_provider=result.model_provider,
            model_id=result.model_id,
            prompt_version=result.prompt_version,
            synthetic=result.synthetic,
        )

    # 6) Independent citation grounding (DCA-133): the gateway's 'valid' does NOT authorize a write. Each
    #    assessment must cite a supported fact the DCA actually holds, on that fact's own field, quoting a
    #    span present in the fact. An ungrounded assessment is DROPPED (recorded), nothing promoted.
    sources = [
        CitedSource(
            ref=f.ref, content=f.value, permitted_fields=frozenset({f.field}), rights_valid=True
        )
        for f in permitted_facts
    ]
    structured = result.structured_output or {}
    raw_assessments = [a for a in (structured.get("assessments") or []) if isinstance(a, dict)]
    facts_to_check = [
        CitedFact(
            field_key=str(a.get("field") or ""),
            field_value=str(a.get("verdict") or "assessed"),
            citation_ref=str(a.get("factRef") or ""),
            cited_text=str(a.get("citedText") or ""),
            inference_label=str(a.get("inferenceLabel") or _FIT_INFERENCE_LABEL),
        )
        for a in raw_assessments
    ]
    validation_result = validate_citations(facts_to_check, sources)

    grounded: list[dict] = []
    dropped_verdicts: list[FactVerdict] = []
    for original, verdict in zip(raw_assessments, validation_result.verdicts, strict=True):
        if verdict.accepted:
            grounded.append(
                {
                    "criterion": str(original.get("criterion") or original.get("field") or ""),
                    "field": str(original.get("field") or ""),
                    "factRef": str(original.get("factRef") or ""),
                    "citedText": str(original.get("citedText") or ""),
                    "verdict": str(original.get("verdict") or ""),
                    "inferenceLabel": str(original.get("inferenceLabel") or _FIT_INFERENCE_LABEL),
                    "confidence": _coerce_confidence(original.get("confidence")),
                }
            )
        else:
            dropped_verdicts.append(verdict)

    dropped_records = [
        {"field": v.fact.field_key, "factRef": v.fact.citation_ref, "reason": v.reason}
        for v in dropped_verdicts
    ]
    # Sensitive facts excluded before egress are also recorded honestly (names + class reason only).
    dropped_records.extend(
        {
            "field": name,
            "reason": EXCL_SENSITIVE_TRAIT,
            "class_reason": reason_for(classify_field(name)),
        }
        for name in excluded_sensitive
    )

    # 7) The advisory overall band is the model's (desirability only) — but if NOTHING grounded, it is
    #    honestly ``unknown`` (no supported claim survived). It NEVER touches eligibility.
    model_band = str(structured.get("fitBand") or FitBand.UNKNOWN.value)
    fit_band = model_band if grounded else FitBand.UNKNOWN.value
    try:
        fit_band = FitBand(fit_band).value
    except ValueError:
        fit_band = FitBand.UNKNOWN.value
    overall_confidence = _coerce_confidence(structured.get("confidence"))

    explanation = store.stage(
        subject_ref=subject_ref,
        icp_version=icp_version,
        fit_band=fit_band,
        rationale=str(structured.get("rationale") or "")[:_MAX_RATIONALE_CHARS],
        assessments=grounded,
        dropped=dropped_records,
        eligibility=carried_eligibility,  # SEPARATE axis — carried through unchanged
        eligibility_reason=carried_reason,
        overall_confidence=overall_confidence,
        prompt_version=str(result.prompt_version or ""),
        synthetic=bool(result.synthetic),
        now=moment,
    )

    return FitExplanationResult(
        business_id=business_id,
        subject_ref=subject_ref,
        explanation=explanation,
        grounded=tuple(grounded),
        dropped=tuple(dropped_verdicts),
        excluded_sensitive=excluded_sensitive,
        eligibility=carried_eligibility,
        eligibility_reason=carried_reason,
        fit_band=fit_band,
        overall_confidence=overall_confidence,
        model_provider=result.model_provider,
        model_id=result.model_id,
        prompt_version=result.prompt_version,
        synthetic=result.synthetic,
    )


__all__ = [
    "CAPABILITY",
    "EXPLANATION_SOURCE",
    "SUBJECT_TYPE",
    "REASON_CAPABILITY_DISABLED",
    "REASON_OUTPUT_REJECTED",
    "REASON_NO_SUPPORTED_FACTS",
    "EXCL_SENSITIVE_TRAIT",
    "SupportedFact",
    "EligibilityStatus",
    "carry_eligibility",
    "FitExplanationResult",
    "FitExplanationStore",
    "explain_business_fit",
]
