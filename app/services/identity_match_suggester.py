"""DCA-137 — suggest ambiguous identity matches (the one AI-capability integration).

The single job of this story: turn a set of tenant-authorized candidate ORGANIZATION identities into
reviewable, AMBIGUOUS same-entity match SUGGESTIONS, through the accepted DCA AI chokepoint — to FEED,
never replace, the deterministic DCA-067/068 matchers and the human-authorized, review-only DCA-071
merge. It composes the already-accepted AI seam end to end and performs no provider/model/paid work of
its own. The load-bearing guarantees (the DCA-137 acceptance criteria):

- **An AI suggestion can NEVER execute a merge.** This service writes only
  :class:`~app.db.models.IdentityMatchSuggestion` REVIEW proposals. It creates NO
  :class:`~app.db.models.IdentityMerge`, collapses no observations and sends nothing. Recording a merge
  stays the deliberate, HUMAN-authorized DCA-071 action (:class:`~app.services.identity_merge.IdentityMergeStore`),
  which itself refuses an AI/automation actor outright — so a suggestion can never become an automatic
  collapse. Proposing is not merging.

- **A strong deterministic refutation always wins.** For every pair the model proposes, the service
  re-runs the pure DCA-067 matcher (:func:`~app.matching.org_match.match_organizations`) over the two
  STORED, tenant-scoped observations and keeps the suggestion ONLY when the conclusion is compatible — a
  ``MATCH`` or an ambiguous ``REVIEW`` candidate. A ``NO_MATCH`` (a CONFLICTING typed registration
  identifier) or an ``INSUFFICIENT`` pair is DROPPED (recorded in ``excluded``, never stored): an AI
  suggestion can never override the deterministic NO_MATCH.

- **It cannot inspect unentitled records or invent an id.** Only the caller-supplied candidate ids that
  resolve to an observation IN THIS business are used (an unknown / cross-tenant id reads as not-found
  and is excluded). The model may only pair those authorized candidates; a proposed pair referencing any
  id outside the authorized set is dropped DCA-side (and the gateway capability's own ``rule_check``
  rejects it too — belt and braces).

- **No permission is ever transferred; everything is tenant-scoped and idempotent.** Surfacing a
  suggestion touches no :class:`~app.db.models.ConsentReceipt` / :class:`~app.db.models.ProcessingRestriction`
  — one identity's consent/suppression never reaches another. Every read/write is confined to the one
  ``business_id`` the service runs for. A pair already suggested (in either order) returns the EXISTING
  row rather than inserting a duplicate.

The service stages into the *caller's* session and never commits — the caller owns the unit of work (so
the suggestions + the idempotency record commit together). It raises nothing for the ordinary denials: a
disabled capability/transport, a rejected gateway output, or fewer than two authorized candidates each
return a refusal that stages NO suggestion (fail-closed, no state change).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai import (
    AiCapabilityDisabled,
    AiCapabilityFacade,
    AiCapabilityRequest,
    AiContractViolation,
    dca_job_binding,
)
from app.db.models import (
    FieldSubjectType,
    IdentityMatchSuggestion,
    IdentityMatchSuggestionStatus,
)
from app.matching.org_match import MatchOutcome, match_organizations, subject_from_observation
from app.services.org_observation import OrgObservationStore

# The capability key this story integrates (must be in the DCA-128 allowlist + the gateway registry).
CAPABILITY = "ambiguous_identity_match_suggestion"

# Where a stored suggestion records its origin.
SUGGESTION_SOURCE = CAPABILITY

# This story proposes ORGANIZATION pairs: the AI reasons over company-fact descriptors only (no personal
# contact data is ever sent). The subject-type column is generic; person suggestions are a future
# extension (the DCA-068 contact matcher and the review-only merge already support that kind).
SUBJECT_TYPE = FieldSubjectType.ORGANIZATION.value

# The deterministic DCA-067 outcomes a suggestion may stand on: a confirmed match or an AMBIGUOUS review
# candidate. A NO_MATCH (a conflicting legal-entity identifier) or an INSUFFICIENT pair is NEVER
# compatible — the AI cannot override a deterministic refutation.
_COMPATIBLE_OUTCOMES = frozenset({MatchOutcome.MATCH.value, MatchOutcome.REVIEW.value})

# Max rationale length retained on a stored suggestion (opaque, bounded; never free-form contact data).
_MAX_RATIONALE_CHARS = 500

# Stable refusal codes — each returned BEFORE any suggestion is staged, so a denial changes nothing (a
# disabled capability / too few candidates never even calls the model).
REASON_CAPABILITY_DISABLED = "identity_match_suggest_capability_disabled"
REASON_OUTPUT_REJECTED = "identity_match_suggest_output_rejected"
REASON_NO_CANDIDATES = "identity_match_suggest_no_candidates"

# Stable exclusion reason codes — why a candidate / proposed pair was dropped (recorded on ``excluded``,
# never silently). Each is the DCA re-enforcing a bound FEEDING, never trusting, the model.
EXCL_SUBJECT_NOT_FOUND = (
    "subject_not_found"  # a candidate id is unknown / another tenant's — not-found
)
EXCL_NOT_A_CANDIDATE = (
    "not_an_authorized_candidate"  # a proposed pair references an un-authorized id
)
EXCL_SELF_PAIR = "self_pair"  # a pair of one subject with itself
EXCL_DETERMINISTIC_REFUTED = "deterministic_refutation"  # DCA-067 concluded no_match / insufficient


def _serialize_signals(signals: object) -> list[dict]:
    """Snapshot a matcher's evidence signals into plain, JSON-storable dicts (company facts only)."""
    return [
        {
            "kind": s.kind.value,
            "strength": s.strength.value,
            "relation": s.relation.value,
            "detail": s.detail,
        }
        for s in signals  # type: ignore[attr-defined]
    ]


@dataclass
class SuggestingResult:
    """The inert, inspectable outcome of one suggestion run (nothing merged; inert on a denial).

    ``suggestions`` are the review-only candidates kept (new or, on a replay, the existing rows);
    ``refused_reason`` names the stable refusal code when none were produced; ``excluded`` is the
    per-item record of every candidate / proposed pair dropped and why (an un-found candidate, an
    un-authorized id, a self-pair, a deterministic NO_MATCH/INSUFFICIENT refutation). ``candidate_ids``
    are the authorized candidates actually considered. Nothing here ever executed a merge.
    """

    business_id: str
    subject_type: str = SUBJECT_TYPE
    suggestions: list[IdentityMatchSuggestion] = field(default_factory=list)
    refused_reason: str | None = None
    excluded: list[dict] = field(default_factory=list)
    candidate_ids: list[str] = field(default_factory=list)
    overall_confidence: float | None = None
    model_provider: str | None = None
    model_id: str | None = None
    prompt_version: str | None = None
    synthetic: bool = False

    @property
    def produced(self) -> bool:
        return self.refused_reason is None

    @property
    def suggestion_count(self) -> int:
        return len(self.suggestions)


class IdentityMatchSuggestionStore:
    """Tenant-scoped reader/writer for AI-proposed identity-match suggestions, bound to one business.

    Construct with the active session and the verified business scope. Every query is confined to that
    business — there is no argument to widen the scope. The store writes only review-only PROPOSALS; it
    never records a merge and never touches consent/restriction state.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    def get(self, suggestion_id: str) -> IdentityMatchSuggestion | None:
        """Fetch one suggestion by id WITHIN this business. Another business's id (or an unknown id)
        resolves to ``None`` — a cross-tenant id reads exactly like not-found."""
        if not suggestion_id:
            return None
        stmt = (
            select(IdentityMatchSuggestion)
            .where(IdentityMatchSuggestion.business_id == self._business_id)
            .where(IdentityMatchSuggestion.id == suggestion_id)
        )
        return self._session.scalars(stmt).one_or_none()

    def suggestions(self) -> list[IdentityMatchSuggestion]:
        """Every suggestion recorded in this business, oldest first."""
        stmt = (
            select(IdentityMatchSuggestion)
            .where(IdentityMatchSuggestion.business_id == self._business_id)
            .order_by(IdentityMatchSuggestion.created_at, IdentityMatchSuggestion.id)
        )
        return list(self._session.scalars(stmt).all())

    def existing_for_pair(
        self, subject_type: str, subject_a: str, subject_b: str
    ) -> IdentityMatchSuggestion | None:
        """The suggestion linking two subjects in EITHER order within this business, or ``None``.

        Order-independent, which is what makes re-suggesting an already-proposed pair idempotent (it
        returns the existing row, never a duplicate)."""
        stmt = (
            select(IdentityMatchSuggestion)
            .where(IdentityMatchSuggestion.business_id == self._business_id)
            .where(IdentityMatchSuggestion.subject_type == subject_type)
            .where(
                (
                    (IdentityMatchSuggestion.subject_a_id == subject_a)
                    & (IdentityMatchSuggestion.subject_b_id == subject_b)
                )
                | (
                    (IdentityMatchSuggestion.subject_a_id == subject_b)
                    & (IdentityMatchSuggestion.subject_b_id == subject_a)
                )
            )
        )
        return self._session.scalars(stmt).first()

    def stage(
        self,
        *,
        subject_a_id: str,
        subject_b_id: str,
        match_outcome: str,
        match_confidence: str,
        evidence: list[dict],
        ai_confidence: float | None,
        rationale: str,
        prompt_version: str,
        synthetic: bool,
        now: datetime,
    ) -> IdentityMatchSuggestion:
        """Stage ONE review-only suggestion (uncommitted). The caller must pass a CANONICAL pair order."""
        suggestion = IdentityMatchSuggestion(
            business_id=self._business_id,
            subject_type=SUBJECT_TYPE,
            subject_a_id=subject_a_id,
            subject_b_id=subject_b_id,
            match_outcome=match_outcome,
            match_confidence=match_confidence,
            evidence=evidence,
            ai_confidence=ai_confidence,
            rationale=rationale,
            status=IdentityMatchSuggestionStatus.PROPOSED.value,
            prompt_version=prompt_version,
            synthetic=synthetic,
            created_at=now,
        )
        self._session.add(suggestion)
        return suggestion


def _coerce_confidence(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def suggest_identity_matches(
    session: Session,
    facade: AiCapabilityFacade | None,
    business_id: str,
    *,
    candidate_ids: Sequence[str],
    invocation_key: str,
    now: datetime | None = None,
    correlation_id: str | None = None,
) -> SuggestingResult:
    """Suggest ambiguous ORGANIZATION identity matches for ``business_id`` through the one DCA AI chokepoint.

    Stages new :class:`~app.db.models.IdentityMatchSuggestion` REVIEW proposals (uncommitted) for the
    candidate pairs the model surfaced that the deterministic DCA-067 matcher does NOT refute — snapshotting
    the deterministic outcome/confidence + the (company-fact-only) evidence signals + the model's advisory
    confidence/rationale. Returns a :class:`SuggestingResult`. A disabled capability/transport, a rejected
    gateway output, or fewer than two authorized candidates each return a refusal that stages NO suggestion.
    ``invocation_key`` is the stable DCA-owned id the facade binding + the gateway idempotency key are
    derived from (so a retry replays, never double-spends).
    """
    moment = now or datetime.now(UTC)

    # 1) Capability/transport gate (DCA-128, fail-closed). A disabled capability stages nothing and never
    #    calls the model.
    if facade is None or not facade.can_invoke(CAPABILITY):
        return SuggestingResult(business_id=business_id, refused_reason=REASON_CAPABILITY_DISABLED)

    # 2) Resolve the caller-supplied candidates WITHIN this business (tenant-scoped; an unknown /
    #    cross-tenant id reads as not-found and is excluded — never sent to the model, never suggested).
    org_store = OrgObservationStore(session, business_id)
    resolved: dict[str, object] = {}
    excluded: list[dict] = []
    seen: set[str] = set()
    for raw in candidate_ids:
        cid = (raw or "").strip()
        if not cid or cid in seen:
            continue
        seen.add(cid)
        observation = org_store.get(cid)
        if observation is None:
            excluded.append({"subjectId": cid, "reason": EXCL_SUBJECT_NOT_FOUND})
            continue
        resolved[cid] = observation

    # Fewer than two resolvable candidates → no pair is possible: refuse, stage nothing, NO model call.
    if len(resolved) < 2:
        return SuggestingResult(
            business_id=business_id,
            refused_reason=REASON_NO_CANDIDATES,
            excluded=excluded,
            candidate_ids=list(resolved),
        )

    # 3) Build the trusted context — ONLY company-fact descriptors for the authorized candidates (no
    #    personal contact data, no external content). DCA-131 minimises it; run the ONE facade call. A
    #    contract violation (incompatible response / un-minimizable context) is a fail-closed refusal.
    candidates_context = [
        {
            "id": obs.id,  # type: ignore[attr-defined]
            "name": obs.display_name or "",  # type: ignore[attr-defined]
            "state": obs.state_code or "",  # type: ignore[attr-defined]
        }
        for obs in resolved.values()
    ]
    request = AiCapabilityRequest(
        capability=CAPABILITY,
        business_id=business_id,
        input_context={"subjectType": SUBJECT_TYPE, "candidates": candidates_context},
        correlation_id=correlation_id,
    )
    binding = dca_job_binding(invocation_key, business_id)
    try:
        result = facade.invoke(request, binding)
    except (AiCapabilityDisabled, AiContractViolation):
        return SuggestingResult(
            business_id=business_id,
            refused_reason=REASON_CAPABILITY_DISABLED,
            excluded=excluded,
            candidate_ids=list(resolved),
        )

    # 4) Honour the gateway's safety/validation verdict. A failed body, or a failed validation screen (the
    #    capability's own rule_check caught a model that referenced an un-authorized id), is unusable —
    #    store NOTHING.
    validation = result.validation
    rejected = (
        not result.succeeded
        or validation.report_status == "failed"
        or validation.status in {"failed", "invalid_schema"}
    )
    if rejected:
        return SuggestingResult(
            business_id=business_id,
            refused_reason=REASON_OUTPUT_REJECTED,
            excluded=excluded,
            candidate_ids=list(resolved),
            model_provider=result.model_provider,
            model_id=result.model_id,
            prompt_version=result.prompt_version,
            synthetic=result.synthetic,
        )

    # 5) Vet every proposed pair DCA-side: it must reference TWO authorized candidates, not be a self-pair,
    #    and SURVIVE the deterministic DCA-067 re-match over the stored observations. A NO_MATCH /
    #    INSUFFICIENT conclusion is a deterministic refutation the AI can never override — dropped.
    authorized = set(resolved)
    store = IdentityMatchSuggestionStore(session, business_id)
    kept: list[IdentityMatchSuggestion] = []
    structured = result.structured_output or {}
    for proposal in structured.get("suggestions") or []:
        if not isinstance(proposal, dict):
            continue
        a = str(proposal.get("subjectAId") or "").strip()
        b = str(proposal.get("subjectBId") or "").strip()
        if a not in authorized or b not in authorized:
            excluded.append({"subjectAId": a, "subjectBId": b, "reason": EXCL_NOT_A_CANDIDATE})
            continue
        if a == b:
            excluded.append({"subjectAId": a, "subjectBId": b, "reason": EXCL_SELF_PAIR})
            continue

        lo, hi = sorted((a, b))  # canonical pair order (order-independent idempotency)
        verdict = match_organizations(
            subject_from_observation(org_store, resolved[lo]),  # type: ignore[arg-type]
            subject_from_observation(org_store, resolved[hi]),  # type: ignore[arg-type]
        )
        if verdict.outcome.value not in _COMPATIBLE_OUTCOMES:
            # A conflicting typed identifier (NO_MATCH) or no linking evidence (INSUFFICIENT): the
            # deterministic refutation wins — the AI suggestion is dropped, never stored, nothing merged.
            excluded.append(
                {
                    "subjectAId": lo,
                    "subjectBId": hi,
                    "reason": EXCL_DETERMINISTIC_REFUTED,
                    "detail": verdict.outcome.value,
                }
            )
            continue

        # Idempotency — a pair already suggested (either order) returns the EXISTING row, no duplicate.
        existing = store.existing_for_pair(SUBJECT_TYPE, lo, hi)
        if existing is not None:
            kept.append(existing)
            continue

        kept.append(
            store.stage(
                subject_a_id=lo,
                subject_b_id=hi,
                match_outcome=verdict.outcome.value,
                match_confidence=verdict.confidence.value,
                evidence=_serialize_signals(verdict.signals),
                ai_confidence=_coerce_confidence(proposal.get("confidence")),
                rationale=str(proposal.get("rationale") or "")[:_MAX_RATIONALE_CHARS],
                prompt_version=str(result.prompt_version or ""),
                synthetic=bool(result.synthetic),
                now=moment,
            )
        )

    return SuggestingResult(
        business_id=business_id,
        suggestions=kept,
        excluded=excluded,
        candidate_ids=list(resolved),
        overall_confidence=_coerce_confidence(structured.get("confidence")),
        model_provider=result.model_provider,
        model_id=result.model_id,
        prompt_version=result.prompt_version,
        synthetic=result.synthetic,
    )


__all__ = [
    "CAPABILITY",
    "SUGGESTION_SOURCE",
    "SUBJECT_TYPE",
    "REASON_CAPABILITY_DISABLED",
    "REASON_OUTPUT_REJECTED",
    "REASON_NO_CANDIDATES",
    "EXCL_SUBJECT_NOT_FOUND",
    "EXCL_NOT_A_CANDIDATE",
    "EXCL_SELF_PAIR",
    "EXCL_DETERMINISTIC_REFUTED",
    "SuggestingResult",
    "IdentityMatchSuggestionStore",
    "suggest_identity_matches",
]
