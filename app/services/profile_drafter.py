"""DCA-134 — draft a business profile with evidence (the one AI-capability integration).

The single job of this story: turn authorised website / Brand-KB **evidence** into a reviewable
business-profile **draft**, through the accepted DCA AI chokepoint, with every drafted claim grounded
in a citation the DCA itself can verify. It composes the already-accepted AI seam end to end — it
builds no second pipeline and performs no provider/model/paid work of its own:

- **DCA-132 untrusted framing.** The business identity goes to the TRUSTED context; the evidence
  BODIES go ONLY to the untrusted channel (:func:`~app.ai.untrusted_framer.build_untrusted_request`),
  so injected page instructions can never act as instructions — they arrive at the model as data.
- **DCA-128 facade over the deployment transport.** The request is run through the ONE facade
  (:class:`~app.ai.contract.AiCapabilityFacade`) for the ``business_profile_draft`` capability — the
  dev/test fixture transport today (no network, no spend), the live gateway only when configured.
- **DCA-132 boundary enforcement.** The gateway's returned validation / taint / injection /
  human-review signals are honoured (:func:`~app.ai.untrusted_framer.evaluate_untrusted_result`): a
  REJECTED output stores nothing; otherwise the draft is staged but marked held-for-review.
- **DCA-133 independent citation grounding.** Each model-proposed claim is re-validated against the
  evidence the DCA actually retrieved (:func:`~app.ai.citation_validator.validate_citations`). The
  gateway's own "valid" verdict does NOT authorize a write — only a claim whose citation is confirmed
  grounded here is kept. An invented / hallucinated / unrelated / expired-rights / unknown claim is
  DROPPED with a stable reason and changes nothing downstream.
- **DCA-036 draft store.** The grounded claims are staged as a NEW
  :class:`~app.db.models.BusinessProfileDraft` version via the only sanctioned writer
  (:class:`~app.services.profile_store.ProfileStore`). The draft is always ``status='draft'`` and is
  **never auto-confirmed** — confirmation stays a disabled future capability.

**Acceptance guarantees (DCA-134):** the output remains a draft carrying its uncertainty (per-claim
confidence + an overall confidence) and its references (the grounded citations), plus an honest record
of what was dropped and why; there is no automatic confirmed targeting or spend approval. A denial
(the capability/transport disabled, or a rejected gateway output) changes NO state — it stages no
draft. Everything is confined to the one ``business_id`` the drafter runs for (tenant isolation).

The drafter stages into the *caller's* session and never commits — the route owns the unit of work
(so the draft + its idempotency record commit together).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.ai import (
    AiCapabilityDisabled,
    AiCapabilityFacade,
    AiContractViolation,
    CitedFact,
    CitedSource,
    ExternalContent,
    FactVerdict,
    UntrustedOutcome,
    build_untrusted_request,
    dca_job_binding,
    evaluate_untrusted_result,
    validate_citations,
)
from app.db.models import BusinessProfileDraft
from app.services.profile_store import ProfileDraftInput, ProfileStore

# The capability key this story integrates (must be in the DCA-128 allowlist + the gateway registry).
CAPABILITY = "business_profile_draft"

# Where a drafted profile records its origin (the DCA-036 provenance ``source``).
DRAFT_SOURCE = "business_profile_draft"

# Stable refusal codes — each returned BEFORE any draft is staged, so a denial changes nothing.
REASON_CAPABILITY_DISABLED = "profile_draft_capability_disabled"
REASON_OUTPUT_REJECTED = "profile_draft_output_rejected"

# How a grounded claim's ``field`` maps onto the canonical profile aspects. A claim whose field is not
# one of these is retained as a draft-level grounded FACT (it still carries its citation in provenance).
_LIST_FIELDS: dict[str, str] = {
    "products": "products",
    "product": "products",
    "languages": "languages",
    "language": "languages",
}
_SCALAR_FIELDS: dict[str, str] = {
    "business_type": "business_type",
    "businesstype": "business_type",
    "value_proposition": "value_proposition",
    "valueproposition": "value_proposition",
}


@dataclass(frozen=True)
class RetrievedEvidence:
    """One piece of authorised evidence the DCA retrieved — the ONLY material a claim may cite.

    ``ref`` is the stable reference a claim cites (an evidence-object id / content locator). ``content``
    is the retrieved source text (treated as inert data: framed untrusted, scanned for grounding, never
    executed). ``permitted_fields`` is the allowlist of profile fields this source may supply (so a
    claim on an off-limits field is rejected as unrelated). ``rights_valid`` is whether the source's
    rights are still in force (expired rights can ground nothing). ``provenance`` is the untrusted
    label carried on the untrusted channel (defaults to ``ref``)."""

    ref: str
    content: str
    permitted_fields: frozenset[str] = frozenset()
    rights_valid: bool = True
    provenance: str = ""

    def label(self) -> str:
        return (self.provenance or "").strip() or self.ref

    def as_cited_source(self) -> CitedSource:
        return CitedSource(
            ref=self.ref,
            content=self.content,
            permitted_fields=self.permitted_fields,
            rights_valid=self.rights_valid,
        )


@dataclass
class DraftingResult:
    """The inert, inspectable outcome of one drafting attempt.

    ``draft`` is the staged :class:`~app.db.models.BusinessProfileDraft` (``None`` on a refusal — a
    denial stages nothing). ``refused_reason`` names the stable refusal code when no draft was produced.
    ``grounded`` are the claims whose citations were confirmed (the only ones written); ``dropped`` are
    the per-claim verdicts for everything the grounding gate rejected, each with its reason. ``outcome``
    is the DCA-132 boundary verdict (held-for-review vs rejected vs auto-apply) the gateway returned;
    ``overall_confidence`` is the model's self-reported draft confidence (uncertainty signal)."""

    business_id: str
    draft: BusinessProfileDraft | None = None
    refused_reason: str | None = None
    grounded: tuple[CitedFact, ...] = ()
    dropped: tuple[FactVerdict, ...] = ()
    outcome: UntrustedOutcome | None = None
    overall_confidence: float | None = None
    model_provider: str | None = None
    model_id: str | None = None
    prompt_version: str | None = None
    synthetic: bool = False

    @property
    def produced(self) -> bool:
        return self.draft is not None


def _cited_facts(structured_output: dict | None) -> list[CitedFact]:
    """Map the gateway's ``claims`` output into DCA-133 :class:`CitedFact` objects (no mutation)."""
    claims = (structured_output or {}).get("claims") or []
    facts: list[CitedFact] = []
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        value = claim.get("value")
        facts.append(
            CitedFact(
                field_key=str(claim.get("field") or ""),
                field_value=None if value is None else str(value),
                citation_ref=str(claim.get("citationRef") or ""),
                cited_text=str(claim.get("citedText") or ""),
                inference_label=str(claim.get("inferenceLabel") or "observed"),
            )
        )
    return facts


def _apply_grounded_claims(
    grounded: Sequence[CitedFact],
) -> tuple[list[str], str, str, list[str], list[str]]:
    """Fold the grounded claims into the canonical profile aspects + extra grounded facts.

    Returns ``(products, business_type, value_proposition, languages, extra_facts)``. A scalar aspect
    takes the FIRST grounded value; list aspects accumulate (deduped, order-preserving); any grounded
    claim on a non-canonical field is retained as a draft-level grounded fact string."""
    products: list[str] = []
    languages: list[str] = []
    business_type = ""
    value_proposition = ""
    extra_facts: list[str] = []
    for fact in grounded:
        value = (fact.field_value or "").strip()
        if not value:
            continue
        key = fact.field_key.strip().lower()
        if key in _LIST_FIELDS:
            bucket = products if _LIST_FIELDS[key] == "products" else languages
            if value not in bucket:
                bucket.append(value)
        elif key in _SCALAR_FIELDS:
            if _SCALAR_FIELDS[key] == "business_type" and not business_type:
                business_type = value
            elif _SCALAR_FIELDS[key] == "value_proposition" and not value_proposition:
                value_proposition = value
        else:
            entry = f"{fact.field_key}: {value}"
            if entry not in extra_facts:
                extra_facts.append(entry)
    return products, business_type, value_proposition, languages, extra_facts


def _reference(fact: CitedFact) -> dict:
    """A log-/review-safe reference for a grounded claim (its field + citation, retained on the draft)."""
    return {
        "field": fact.field_key,
        "value": fact.field_value,
        "citationRef": fact.citation_ref,
        "citedText": fact.cited_text,
        "inferenceLabel": fact.inference_label,
    }


def draft_profile_from_evidence(
    session: Session,
    facade: AiCapabilityFacade | None,
    business_id: str,
    *,
    business_name: str,
    evidence: Sequence[RetrievedEvidence],
    invocation_key: str,
    domain: str | None = None,
    draft_fields: Sequence[str] = (),
    correlation_id: str | None = None,
) -> DraftingResult:
    """Draft a business profile for ``business_id`` grounded in the supplied authorised evidence.

    Stages a new :class:`~app.db.models.BusinessProfileDraft` version (uncommitted) carrying only the
    claims whose citations were confirmed grounded, plus the uncertainty + references + an honest record
    of what was dropped. Returns a :class:`DraftingResult`; raises nothing for the ordinary denials —
    a disabled capability/transport or a rejected gateway output returns a refusal that stages NO draft
    (fail-closed, no state change). ``invocation_key`` is the stable DCA-owned id the facade binding and
    the gateway idempotency key are derived from (so a retry replays, never double-spends)."""
    # 1) Capability/transport gate (DCA-128, fail-closed). A disabled capability stages nothing.
    if facade is None or not facade.can_invoke(CAPABILITY):
        return DraftingResult(business_id=business_id, refused_reason=REASON_CAPABILITY_DISABLED)

    sources = [e.as_cited_source() for e in evidence]
    trusted_context: dict = {
        "businessName": business_name,
        "evidenceRefs": [e.ref for e in evidence],
    }
    if domain:
        trusted_context["domain"] = domain
    if draft_fields:
        trusted_context["draftFields"] = list(draft_fields)

    # 2) Build the request keeping evidence BODIES on the untrusted channel (DCA-132), then run the ONE
    #    facade call (DCA-131 minimises the trusted context; DCA-130-style bounds apply). A contract
    #    violation (incompatible response / un-minimizable context) is a fail-closed refusal.
    request = build_untrusted_request(
        CAPABILITY,
        business_id,
        trusted_context=trusted_context,
        external=[ExternalContent(provenance=e.label(), content=e.content) for e in evidence],
        correlation_id=correlation_id,
    )
    binding = dca_job_binding(invocation_key, business_id)
    try:
        result = facade.invoke(request, binding)
    except (AiCapabilityDisabled, AiContractViolation):
        return DraftingResult(business_id=business_id, refused_reason=REASON_CAPABILITY_DISABLED)

    # 3) Honour the gateway's returned safety signals (DCA-132). A rejected (failed/invalid) output is
    #    unsafe/unusable — store NOTHING. A held-for-review output is still a reviewable draft.
    outcome = evaluate_untrusted_result(result)
    if outcome.rejected:
        return DraftingResult(
            business_id=business_id,
            refused_reason=REASON_OUTPUT_REJECTED,
            outcome=outcome,
            model_provider=result.model_provider,
            model_id=result.model_id,
            prompt_version=result.prompt_version,
            synthetic=result.synthetic,
        )

    # 4) Independent citation grounding (DCA-133): the gateway's 'valid' does NOT authorize a write.
    facts = _cited_facts(result.structured_output)
    validation = validate_citations(facts, sources)
    grounded = validation.accepted
    dropped = validation.rejected

    # 5) Fold grounded claims into the canonical aspects; everything else stays an honest unknown.
    products, business_type, value_proposition, languages, extra_facts = _apply_grounded_claims(
        grounded
    )

    structured = result.structured_output or {}
    overall_confidence = structured.get("confidence")
    provenance = {
        "source": DRAFT_SOURCE,
        "capability": CAPABILITY,
        "drafted": True,
        "imported": False,
        "synthetic": result.synthetic,
        "model_provider": result.model_provider,
        "model_id": result.model_id,
        "prompt_version": result.prompt_version,
        "overall_confidence": overall_confidence,
        # Uncertainty + references: the grounded citations retained on the draft, and the dropped claims.
        "references": [_reference(f) for f in grounded],
        "dropped": [
            {"field": v.fact.field_key, "value": v.fact.field_value, "reason": v.reason}
            for v in dropped
        ],
        # The model's own unknown list, plus the DCA-132 review disposition (a draft is never auto-applied).
        "model_unknowns": list(structured.get("unknowns") or []),
        "held_for_review": bool(outcome.held_for_review),
        "review_reasons": list(outcome.reasons),
    }

    # 6) Stage the draft via the ONLY sanctioned writer (DCA-036). Always status='draft'; never confirmed.
    draft = ProfileStore(session, business_id).save_draft(
        ProfileDraftInput(
            products=products,
            business_type=business_type,
            value_proposition=value_proposition,
            languages=languages,
            confirmed_facts=extra_facts,
            source=DRAFT_SOURCE,
            source_version=str(result.prompt_version or ""),
            provenance=provenance,
        )
    )
    return DraftingResult(
        business_id=business_id,
        draft=draft,
        grounded=grounded,
        dropped=dropped,
        outcome=outcome,
        overall_confidence=overall_confidence,
        model_provider=result.model_provider,
        model_id=result.model_id,
        prompt_version=result.prompt_version,
        synthetic=result.synthetic,
    )


__all__ = [
    "CAPABILITY",
    "DRAFT_SOURCE",
    "REASON_CAPABILITY_DISABLED",
    "REASON_OUTPUT_REJECTED",
    "RetrievedEvidence",
    "DraftingResult",
    "draft_profile_from_evidence",
]
