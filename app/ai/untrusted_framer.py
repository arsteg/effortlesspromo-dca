"""DCA-132 — frame fetched external content as UNTRUSTED before it reaches the AI gateway.

Key: ``ai-untrusted`` · source sections 4, 8, 9. Stage: existing AI-service integration.

**Load-bearing guarantee:** any content the DCA did not author itself — a scraped page body, a
licensed-provider record, a prospect reply, an uploaded document — leaves the DCA ONLY through the
verified gateway envelope's ``untrusted[]`` channel, carrying its provenance LABEL, and NEVER as part
of the trusted ``inputContext`` (the instruction/context path). So injected page instructions arrive
at the model as DATA, fenced and sanitised by the existing gateway defenses (effortlesspromo-ai
``gateway/untrusted.py`` — per-invocation canary, zero-width/bidi/markup strip, URL defang, length
cap, taint propagation), and can never act as instructions that authorize a tool, a source, a policy
change or a spend. The DCA does NOT build a second model pipeline or re-implement those defenses; it
simply routes external content to the one channel the gateway already isolates.

This module is the counterpart of the DCA-131 context minimizer at the SAME gateway seam: the
minimizer screens the TRUSTED ``inputContext`` (keep only authorized evidence/provenance), while this
framer handles the UNTRUSTED side (route external text to ``untrusted[]``, bounded, labelled). It is a
**pure, deterministic** screen — no I/O, no model/paid work, no mutation of its input:

1. **Provenance is mandatory (fail-closed).** Every piece of external content must carry a non-empty
   provenance label (:class:`ExternalContent.provenance`); content with no provenance is REFUSED
   (:data:`REASON_MISSING_PROVENANCE`) rather than shipped unlabelled — "content not provably
   first-party is treated as untrusted" means it must still name where it came from.
2. **Trusted context stays clean.** :func:`build_untrusted_request` is the one builder that assembles a
   capability request from a trusted context PLUS external content: the trusted context goes to
   ``input_context`` and the external content goes ONLY to ``untrusted``. There is no code path here
   that places external text into ``input_context``.
3. **Bounded size (reuse the untrusted cap).** The total serialized size of the framed untrusted
   content (its labels + bodies, measured exactly as the DCA-130 bound invoker measures it) is bounded
   by :data:`DEFAULT_MAX_UNTRUSTED_CHARS` — the same 20 000-char cap as the AI service's
   ``untrusted_max_chars`` and the DCA-130/131 payload/context bounds. An oversize batch is REFUSED
   before egress (:data:`REASON_UNTRUSTED_TOO_LARGE`) so the gateway never sees an unbounded payload.

On the RESPONSE side the DCA boundary ENFORCES the signals the gateway returned about a call that
consumed untrusted content — it never second-guesses the gateway's 6-screen verdict, it only honours
it: :func:`evaluate_untrusted_result` folds ``validationStatus`` / ``validationReport`` / ``taint`` /
``injectionSuspected`` / ``humanReviewRequired`` into an :class:`UntrustedOutcome`. A failed/invalid
output is REJECTED (never used); a review-required / tainted / injection-suspected output is HELD for
review and can NEVER silently auto-apply; only a clean, valid, untainted result may auto-apply. The
signals are preserved verbatim on the outcome so a consumer / audit line can retain them for review.

Refusals are raised as :class:`UntrustedFramingError`, a subclass of
:class:`~app.ai.contract.AiContractViolation`, so the facade / DCA-130 bound invoker treat an
un-frameable or oversize untrusted batch as a fail-closed, non-retryable contract violation (no
egress, no spend).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.ai.context_minimizer import DEFAULT_MAX_CONTEXT_CHARS
from app.ai.contract import (
    AiCapabilityRequest,
    AiCapabilityResult,
    AiContractViolation,
    AiUntrustedItem,
)

# The serialized-size bound for the framed untrusted content (labels + bodies, characters). Reuses the
# SAME 20 000-char cap the AI service applies to untrusted content (``untrusted_max_chars``), the
# DCA-131 context bound (:data:`~app.ai.context_minimizer.DEFAULT_MAX_CONTEXT_CHARS`) and the DCA-130
# invocation payload bound — one shared cap, so a batch that would be refused downstream is refused here
# first, before any gateway call.
DEFAULT_MAX_UNTRUSTED_CHARS = DEFAULT_MAX_CONTEXT_CHARS

# Stable refusal codes (fail-closed, test-asserted — change deliberately).
REASON_MISSING_PROVENANCE = "untrusted_missing_provenance"
REASON_UNTRUSTED_TOO_LARGE = "untrusted_content_too_large"

# Stable boundary-outcome reason codes (why an output was rejected / held — preserved for review).
REASON_BODY_FAILED = "body_failed"
REASON_VALIDATION_FAILED = "validation_failed"
REASON_INJECTION_SUSPECTED = "injection_suspected"
REASON_TAINTED = "taint"
REASON_HUMAN_REVIEW_REQUIRED = "human_review_required"
REASON_LOW_CONFIDENCE = "low_confidence"

# Validation statuses (envelope ``validationStatus``) the DCA treats as a hard REJECT of the output.
_REJECT_VALIDATION_STATUSES: frozenset[str] = frozenset({"failed", "invalid_schema"})


class UntrustedFramingError(AiContractViolation):
    """External content could not be framed for egress — refused BEFORE any gateway call (fail closed).

    A subclass of :class:`~app.ai.contract.AiContractViolation` so the facade / DCA-130 invoker treat an
    un-frameable (missing provenance) or oversize untrusted batch as a non-retryable contract violation:
    no egress, no spend. ``code`` is one of :data:`REASON_MISSING_PROVENANCE` /
    :data:`REASON_UNTRUSTED_TOO_LARGE`; ``detail`` names the offending size when useful. It NEVER carries
    a raw content value.
    """

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ExternalContent:
    """One piece of fetched/external content to frame as untrusted — a provenance LABEL + raw text.

    ``provenance`` is the mandatory, non-empty source label (e.g. ``"scraped_page"``,
    ``"licensed_provider:acme"``, ``"prospect_reply"``) that rides on the untrusted channel so a model /
    reviewer always knows the text is external data, not an instruction. ``content`` is the raw external
    text exactly as fetched; the DCA does not sanitise it here — the gateway's untrusted frame does that
    (canary fence, zero-width/bidi/markup strip, URL defang, length cap). Treated as inert data end to
    end: this module parses, decodes, renders and evaluates NOTHING in ``content``.
    """

    provenance: str
    content: str


@dataclass(frozen=True)
class UntrustedOutcome:
    """The DCA-boundary verdict on a gateway result that consumed untrusted content (DCA-132 ACs).

    Exactly one of :attr:`may_auto_apply` / :attr:`held_for_review` / :attr:`rejected` is the governing
    disposition (checked in the order reject → hold → auto-apply). The raw gateway signals are preserved
    verbatim so a consumer / audit line can retain them for review and never has to re-derive them:

    - :attr:`rejected` — a failed body or a failed/invalid-schema validation verdict: the output is
      unsafe/invalid and MUST NOT be used.
    - :attr:`held_for_review` — the gateway flagged ``humanReviewRequired`` / ``injectionSuspected`` /
      ``taint`` / a low-confidence validation: the output can NEVER silently auto-apply; a human decides.
    - :attr:`may_auto_apply` — only a clean, valid, untainted, review-free result. Injected page
      instructions, by construction, surface as taint/injection and so can never reach this state.
    """

    may_auto_apply: bool
    held_for_review: bool
    rejected: bool
    reasons: tuple[str, ...]
    validation_status: str
    report_status: str | None
    taint: bool
    injection_suspected: bool
    human_review_required: bool


def frame_external_content(
    items: tuple[ExternalContent, ...] | list[ExternalContent],
    *,
    max_chars: int = DEFAULT_MAX_UNTRUSTED_CHARS,
) -> tuple[AiUntrustedItem, ...]:
    """Map fetched external content to the envelope's ``untrusted[]`` items — pure, deterministic.

    Every item must carry a non-empty provenance label (missing → :class:`UntrustedFramingError`
    fail-closed); input order is preserved 1:1. The total serialized size (labels + bodies, measured as
    the DCA-130 bound invoker measures it) is bounded by ``max_chars``; an oversize batch is REFUSED
    before any egress — the gateway never sees an unbounded untrusted payload. No I/O, no model/paid
    work, no mutation of the input, nothing in ``content`` is parsed/decoded/rendered/evaluated.
    """
    framed: list[AiUntrustedItem] = []
    total = 0
    for item in items:
        provenance = (item.provenance or "").strip()
        if not provenance:
            raise UntrustedFramingError(
                REASON_MISSING_PROVENANCE,
                "external content must carry a non-empty provenance label before it can be framed as "
                "untrusted (content not provably first-party is still labelled, never shipped blind)",
            )
        content = item.content or ""
        total += len(provenance) + len(content)
        framed.append(AiUntrustedItem(source=provenance, content=content))

    if total > max_chars:
        raise UntrustedFramingError(
            REASON_UNTRUSTED_TOO_LARGE,
            f"framed untrusted content is {total} characters, over the {max_chars} egress bound "
            "(refused before any gateway call — the model never sees an unbounded untrusted payload)",
            detail=str(total),
        )
    return tuple(framed)


def build_untrusted_request(
    capability: str,
    business_id: str,
    *,
    trusted_context: dict[str, Any] | None = None,
    external: tuple[ExternalContent, ...] | list[ExternalContent] = (),
    model_hint: str | None = None,
    prompt_version: str | None = None,
    dry_run: bool = False,
    correlation_id: str | None = None,
    max_untrusted_chars: int = DEFAULT_MAX_UNTRUSTED_CHARS,
) -> AiCapabilityRequest:
    """Assemble a capability request keeping external content OUT of the trusted context (DCA-132).

    This is the one builder that composes a request from a trusted context PLUS external content: the
    ``trusted_context`` becomes ``input_context`` (later minimized by DCA-131), and ``external`` is
    framed by :func:`frame_external_content` into ``untrusted`` ONLY. There is no code path here that
    places external text into ``input_context`` — the separation is structural, not a convention. The
    trusted context is copied (never mutated) and carries no external text.
    """
    return AiCapabilityRequest(
        capability=capability,
        business_id=business_id,
        input_context=dict(trusted_context or {}),
        untrusted=frame_external_content(external, max_chars=max_untrusted_chars),
        model_hint=model_hint,
        prompt_version=prompt_version,
        dry_run=dry_run,
        correlation_id=correlation_id,
    )


def evaluate_untrusted_result(result: AiCapabilityResult) -> UntrustedOutcome:
    """Enforce the gateway's returned validation/taint/injection/human-review signals at the DCA boundary.

    Pure and deterministic; it honours (never second-guesses) the gateway's verdict. Disposition order:

    * REJECT when the body failed (``status != completed``) or the validation verdict is
      failed/invalid-schema or the validation report failed — an unsafe/invalid output is never used.
    * otherwise HOLD FOR REVIEW when the gateway flagged ``humanReviewRequired`` / ``injectionSuspected``
      / ``taint`` / a ``low_confidence`` validation — such an output can never silently auto-apply.
    * otherwise MAY AUTO-APPLY — a clean, valid, untainted, review-free result.

    Every governing signal is recorded in :attr:`UntrustedOutcome.reasons` and the raw flags are
    preserved on the outcome so a consumer / audit line can retain them for review.
    """
    validation = result.validation
    reasons: list[str] = []

    if not result.succeeded:
        reasons.append(REASON_BODY_FAILED)
    if validation.status in _REJECT_VALIDATION_STATUSES:
        reasons.append(REASON_VALIDATION_FAILED)
    if validation.report_status == "failed":
        reasons.append(REASON_VALIDATION_FAILED)
    rejected = bool(reasons)

    hold_reasons: list[str] = []
    if result.human_review_required or validation.human_review_required:
        hold_reasons.append(REASON_HUMAN_REVIEW_REQUIRED)
    if validation.injection_suspected:
        hold_reasons.append(REASON_INJECTION_SUSPECTED)
    if validation.taint:
        hold_reasons.append(REASON_TAINTED)
    if validation.status == "low_confidence":
        hold_reasons.append(REASON_LOW_CONFIDENCE)

    held_for_review = not rejected and bool(hold_reasons)
    # The reasons list carries the full picture (reject + any hold signals) for the audit trail, with
    # stable ordering and no duplicates.
    ordered: list[str] = []
    for reason in (*reasons, *hold_reasons):
        if reason not in ordered:
            ordered.append(reason)

    return UntrustedOutcome(
        may_auto_apply=not rejected and not held_for_review,
        held_for_review=held_for_review,
        rejected=rejected,
        reasons=tuple(ordered),
        validation_status=validation.status,
        report_status=validation.report_status,
        taint=validation.taint,
        injection_suspected=validation.injection_suspected,
        human_review_required=result.human_review_required or validation.human_review_required,
    )


__all__ = [
    "DEFAULT_MAX_UNTRUSTED_CHARS",
    "REASON_MISSING_PROVENANCE",
    "REASON_UNTRUSTED_TOO_LARGE",
    "REASON_BODY_FAILED",
    "REASON_VALIDATION_FAILED",
    "REASON_INJECTION_SUSPECTED",
    "REASON_TAINTED",
    "REASON_HUMAN_REVIEW_REQUIRED",
    "REASON_LOW_CONFIDENCE",
    "UntrustedFramingError",
    "ExternalContent",
    "UntrustedOutcome",
    "frame_external_content",
    "build_untrusted_request",
    "evaluate_untrusted_result",
]
