"""DCA-144 — resolve ONE discovery run's converted observation batch (deterministic, read-and-conclude).

This is the next step of the still-disabled discovery pipeline after DCA-143 converts a run's DONE tasks
into provenanced observations: it RESOLVES that run's batch of organization subjects by connecting two
accepted, PURE capabilities to the batch —

- the deterministic DCA-067 identity matcher (:func:`app.matching.org_match.match_organizations` over
  :func:`~app.matching.org_match.subject_from_observation`), deciding for each batch subject whether it
  is the SAME as an existing identity in this business; and
- the DCA-069 canonical selection (:func:`app.canonical.field_selection.select_canonical_by_field`) with
  the DCA-070 user-correction overlay (:func:`app.canonical.correction_overlay.apply_user_correction`),
  computing the canonical value per field.

An AMBIGUOUS match (a deterministic ``REVIEW`` candidate — a shared name/domain with no confirming typed
identifier) is ROUTED to an existing DCA-137 :class:`~app.db.models.IdentityMatchSuggestion` review
proposal; it is NEVER merged. A strong ``MATCH`` (a compatible typed registration identifier) is recorded
as resolved-to-an-existing-entity, again without merging. There is **no network fetch, no model call and
no recipient send** — resolution reads already-collected, already-permitted observations and concludes.

Every guard is checked fail-closed, in order, so the two acceptance criteria hold:

- **A batch cannot merge consent or overwrite protected corrections.** Resolution writes no
  :class:`~app.db.models.ConsentReceipt` / :class:`~app.db.models.ProcessingRestriction` and collapses no
  identities (an ambiguous pair becomes a review-only suggestion; the human-authorized DCA-071 merge is
  the only merge path and it refuses an automation actor). Canonical values are computed through the pure
  DCA-070 overlay and written NOWHERE, so a PROTECTED user correction keeps the corrected value as
  canonical over any fresher provider observation and the stored correction history is untouched.
- **Reproduce the result and the relevant denial without unintended state changes.** Authority is checked
  FIRST; then the run must exist in THIS business (an unknown / cross-tenant run reads as not-found); then
  the run must have a converted batch (nothing to resolve otherwise). Each refusal is raised BEFORE the
  session is mutated. A run resolves ONCE — ``(business_id, run_id)`` is unique, so re-resolving returns
  the existing record and stages no duplicate suggestion and no second audit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.sink import ActorType, AuditActor, AuditSink
from app.canonical.correction_overlay import (
    ORIGIN_USER_CORRECTION,
    apply_user_correction,
    correction_from_row,
)
from app.canonical.field_selection import candidates_from_observations, select_canonical_by_field
from app.db.models import (
    FieldObservation,
    FieldSubjectType,
    ObservationBatchResolution,
    OrgObservation,
)
from app.identity.permissions import Scope, scopes_for_business_roles
from app.matching.org_match import (
    Confidence,
    MatchOutcome,
    OrgMatchResult,
    match_organizations,
    subject_from_observation,
)
from app.services.discovery_run import DiscoveryRunStore
from app.services.field_correction import FieldCorrectionStore
from app.services.field_observation import FieldObservationStore
from app.services.identity_match_suggester import IdentityMatchSuggestionStore
from app.services.org_observation import OrgObservationStore

# The subject kind a discovery batch resolves — organization identities (the DCA-143 converter records
# organization-level facts only; there is no contact in the batch).
SUBJECT_TYPE = FieldSubjectType.ORGANIZATION.value


# How a DCA-143 conversion stamps a recorded field's provenance:
# ``discovery_run:{run_id}/task:{i}#evidence={id}``. The run's batch is exactly the distinct organization
# subjects whose field observations carry this run's prefix — so the batch is precisely what this run
# converted, nothing wider.
def _run_provenance_prefix(run_id: str) -> str:
    return f"discovery_run:{run_id}/task:"


# The prompt version recorded on a routed suggestion. Resolution routes a DETERMINISTIC DCA-067 review
# candidate (no model is called), so there is no prompt version — the empty string marks that.
DETERMINISTIC_PROMPT_VERSION = ""

# The version of the resolution contract provenance rows are written under (expand→migrate→contract).
RESOLUTION_SCHEMA_VERSION = 1

# The audited action code for resolving a run's observation batch (a controlled code).
AUDIT_ACTION_RESOLVE_BATCH = "discovery_run.batch.resolved"

# Where a resolution records its origin on the provenance / citation.
RESOLUTION_SOURCE = "observation_batch_resolution"

# Stable refusal codes raised (never recorded) for a malformed / ineligible request — each BEFORE any
# state is mutated, so the attempt changes nothing.
REASON_NOT_AUTHORIZED = "batch_resolution_not_authorized"
REASON_RUN_NOT_FOUND = "discovery_run_not_found"
REASON_NOTHING_TO_RESOLVE = "observation_batch_empty"


class BatchResolutionError(Exception):
    """A batch resolution was refused with a stable ``code`` for a malformed / ineligible request (an
    unknown or cross-tenant run, or a run with no converted observation batch to resolve). Raised before
    the session is mutated, so a refusal changes nothing. ``detail`` names the offending value."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class BatchResolutionAuthorizationError(BatchResolutionError):
    """The caller is not authorized to resolve a discovery observation batch.

    A distinct type so an HTTP boundary can map it to a 403 while the other codes map to a 404/409.
    Raised FIRST — before any state is read or written. ``code`` is :data:`REASON_NOT_AUTHORIZED`.
    """

    def __init__(self, message: str) -> None:
        super().__init__(REASON_NOT_AUTHORIZED, message)


@dataclass(frozen=True)
class BatchResolutionActor:
    """The business principal resolving a run's observation batch.

    ``subject`` is an opaque actor id (never a name/email) recorded on the resolution record and its
    atomic audit entries. ``roles`` are the caller's verified business-plane membership roles (from the
    tenant context, NEVER a bare token claim). :meth:`may_resolve` grants the action only when those
    roles yield :attr:`~app.identity.permissions.Scope.RUN_PLAN_APPROVE` — resolving a run's converted
    batch is part of the same chargeable-discovery authority that approved the plan (DCA-141), executed
    it (DCA-142) and converted its tasks (DCA-143). A read-only viewer and a platform operator (holding
    no business scope) cannot perform it.
    """

    subject: str
    roles: tuple[str, ...] = ()
    actor_type: str = "user"

    def may_resolve(self) -> bool:
        return Scope.RUN_PLAN_APPROVE in scopes_for_business_roles(self.roles)

    def as_audit_actor(self) -> AuditActor:
        return AuditActor(id=self.subject, type=ActorType(self.actor_type))


@dataclass
class BatchResolutionResult:
    """The inert, inspectable outcome of one resolve attempt.

    ``resolution`` is the staged :class:`~app.db.models.ObservationBatchResolution` record.
    ``already_resolved`` is ``True`` when this was an idempotent no-op (the run had already been
    resolved). The counts mirror the record: ``subject_count`` subjects in the batch,
    ``matched_count`` / ``ambiguous_count`` / ``distinct_count`` how each resolved, and
    ``review_suggestions`` the review proposals routed this run. Nothing here is ever merged, sent or
    charged.
    """

    business_id: str
    resolution: ObservationBatchResolution
    subject_count: int
    matched_count: int
    ambiguous_count: int
    distinct_count: int
    review_suggestions: int
    fields_canonicalized: int
    corrections_preserved: int
    already_resolved: bool = False


@dataclass
class _SubjectResolution:
    """The per-subject working outcome while resolving the batch (internal, never persisted verbatim)."""

    subject_id: str
    outcome: str  # "matched" | "ambiguous" | "distinct"
    matched_ids: list[str] = field(default_factory=list)
    ambiguous_ids: list[str] = field(default_factory=list)


# Rank for choosing a subject's single headline outcome when it relates to several existing identities:
# a strong MATCH dominates an ambiguous REVIEW, which dominates "distinct".
_OUTCOME_MATCHED = "matched"
_OUTCOME_AMBIGUOUS = "ambiguous"
_OUTCOME_DISTINCT = "distinct"
_OUTCOME_RANK = {_OUTCOME_MATCHED: 0, _OUTCOME_AMBIGUOUS: 1, _OUTCOME_DISTINCT: 2}


class ObservationBatchResolver:
    """Resolve ONE discovery run's converted observation batch, for ONE business (tenant-scoped).

    Construct with the active session and the verified business scope (a route/worker resolves it from
    the membership-verified tenant context). Composes the deterministic DCA-067 matcher, the DCA-069/070
    canonical selection and the DCA-137 suggestion store over the organization observations the run
    converted. Every query is confined to this business — there is no argument to widen the scope. The
    resolver stages into the caller's session and never commits; it merges nothing and sends nothing.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id
        self._orgs = OrgObservationStore(session, business_id)
        self._fields = FieldObservationStore(session, business_id)
        self._corrections = FieldCorrectionStore(session, business_id)
        self._suggestions = IdentityMatchSuggestionStore(session, business_id)

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def resolution_for(self, run_id: str) -> ObservationBatchResolution | None:
        """The resolution record for one run within this business, or ``None`` (the idempotency read;
        a cross-tenant / unknown run reads as not-found)."""
        if not run_id:
            return None
        stmt = (
            select(ObservationBatchResolution)
            .where(ObservationBatchResolution.business_id == self._business_id)
            .where(ObservationBatchResolution.run_id == run_id)
        )
        return self._session.scalars(stmt).one_or_none()

    def _batch_subject_ids(self, run_id: str) -> list[str]:
        """The distinct organization subjects of this run's converted field observations, in a stable
        order. The batch is EXACTLY what DCA-143 recorded for the run (provenance-prefixed), never wider;
        every read is tenant-scoped, so a cross-tenant subject can never enter the batch."""
        stmt = (
            select(FieldObservation.subject_id)
            .where(FieldObservation.business_id == self._business_id)
            .where(FieldObservation.subject_type == SUBJECT_TYPE)
            .where(FieldObservation.provenance_ref.like(f"{_run_provenance_prefix(run_id)}%"))
            .distinct()
        )
        ids = list(self._session.scalars(stmt).all())
        ids.sort()
        return ids

    # -- the one resolve path -----------------------------------------------------------------------

    def resolve_run(
        self,
        run_id: str,
        actor: BatchResolutionActor,
        *,
        now: datetime | None = None,
    ) -> BatchResolutionResult:
        """Resolve the converted observation batch of run ``run_id`` into identity + canonical outcomes.

        Order (each gate leaves prior state unchanged when it refuses):

        1. **Authority FIRST.** A caller lacking :attr:`~app.identity.permissions.Scope.RUN_PLAN_APPROVE`
           is refused (:class:`BatchResolutionAuthorizationError`) before any state is read.
        2. **The run exists in THIS business.** Otherwise refused (:data:`REASON_RUN_NOT_FOUND`) — an
           unknown or cross-tenant run reads exactly like not-found.
        3. **Idempotent replay.** An existing resolution for the run is returned unchanged (no duplicate
           suggestion, no new audit) — a run resolves once.
        4. **The batch is non-empty.** The run must have a converted batch (DCA-143); otherwise refused
           (:data:`REASON_NOTHING_TO_RESOLVE`) rather than fabricating an empty resolution.
        5. **Resolve.** Each batch subject is matched (DCA-067) against this business's existing
           identities — a strong MATCH records resolved-to-existing, an ambiguous REVIEW is ROUTED to a
           review-only suggestion (never merged) — and its canonical values are computed (DCA-069/070),
           keeping a protected correction over any provider refresh.

        On success an atomic DCA-026 audit entry is staged in the caller's unit of work. Resolution reads
        already-permitted observations and performs no merge, fetch, model call, spend or send.
        """
        moment = now or datetime.now(UTC)

        # 1) Authority FIRST — an unauthorized caller is refused with no state touched.
        if not actor.may_resolve():
            raise BatchResolutionAuthorizationError(
                "resolving an observation batch requires a business administrator holding "
                f"'{Scope.RUN_PLAN_APPROVE.value}'; a read-only viewer or a platform operator "
                "(holding no business scope) cannot perform it"
            )

        # 2) The run must exist WITHIN this business (an unknown / cross-tenant run is not-found).
        run = DiscoveryRunStore(self._session, self._business_id).get(run_id)
        if run is None:
            raise BatchResolutionError(
                REASON_RUN_NOT_FOUND,
                "no such discovery run in this business (an unknown or cross-tenant id is not-found)",
                detail=run_id,
            )

        # 3) Idempotent replay — a run resolves ONCE; return the existing record unchanged.
        existing = self.resolution_for(run_id)
        if existing is not None:
            return BatchResolutionResult(
                business_id=self._business_id,
                resolution=existing,
                subject_count=existing.subject_count,
                matched_count=existing.matched_count,
                ambiguous_count=existing.ambiguous_count,
                distinct_count=existing.distinct_count,
                review_suggestions=0,
                fields_canonicalized=existing.fields_canonicalized,
                corrections_preserved=existing.corrections_preserved,
                already_resolved=True,
            )

        # 4) The run must have a converted batch to resolve (honest: an empty batch is not fabricated).
        subject_ids = self._batch_subject_ids(run_id)
        if not subject_ids:
            raise BatchResolutionError(
                REASON_NOTHING_TO_RESOLVE,
                f"discovery run '{run_id}' has no converted observation batch to resolve — convert its "
                "DONE tasks (DCA-143) first",
                detail=run_id,
            )

        # 5a) Deterministic identity matching + routing ambiguous pairs to review-only suggestions.
        all_orgs = self._orgs.observations()
        subjects_by_id: dict[str, OrgObservation] = {o.id: o for o in all_orgs}
        actor_audit = actor.as_audit_actor()
        review_suggestions = 0
        routed_pairs: set[tuple[str, str]] = set()
        resolutions: list[_SubjectResolution] = []

        for subject_id in subject_ids:
            subject_obs = subjects_by_id.get(subject_id)
            if subject_obs is None:
                # Defensive: a batch subject with no readable org observation cannot be matched; count it
                # as distinct (it resolves to no existing entity) rather than skip it silently.
                resolutions.append(_SubjectResolution(subject_id, _OUTCOME_DISTINCT))
                continue
            subject = subject_from_observation(self._orgs, subject_obs)
            outcome = _OUTCOME_DISTINCT
            matched_ids: list[str] = []
            ambiguous_ids: list[str] = []
            for other in all_orgs:
                if other.id == subject_id:
                    continue
                result = match_organizations(subject, subject_from_observation(self._orgs, other))
                if result.outcome is MatchOutcome.MATCH and result.confidence is Confidence.HIGH:
                    matched_ids.append(other.id)
                    outcome = _best(outcome, _OUTCOME_MATCHED)
                elif result.outcome is MatchOutcome.REVIEW:
                    ambiguous_ids.append(other.id)
                    outcome = _best(outcome, _OUTCOME_AMBIGUOUS)
                    if self._route_review(subject_id, other.id, result, routed_pairs, moment):
                        review_suggestions += 1
            resolutions.append(_SubjectResolution(subject_id, outcome, matched_ids, ambiguous_ids))

        matched_count = sum(1 for r in resolutions if r.outcome == _OUTCOME_MATCHED)
        ambiguous_count = sum(1 for r in resolutions if r.outcome == _OUTCOME_AMBIGUOUS)
        distinct_count = sum(1 for r in resolutions if r.outcome == _OUTCOME_DISTINCT)

        # 5b) Canonical selection per subject, through the pure DCA-070 overlay (a protected correction is
        #     never overwritten by a provider value — and nothing is written back).
        fields_canonicalized, corrections_preserved = self._summarize_canonical(subject_ids)

        resolution = self._stage_resolution(
            run_id=run_id,
            subject_count=len(subject_ids),
            matched_count=matched_count,
            ambiguous_count=ambiguous_count,
            distinct_count=distinct_count,
            review_suggestion_count=review_suggestions,
            fields_canonicalized=fields_canonicalized,
            corrections_preserved=corrections_preserved,
            actor=actor,
            actor_audit=actor_audit,
            moment=moment,
        )
        return BatchResolutionResult(
            business_id=self._business_id,
            resolution=resolution,
            subject_count=len(subject_ids),
            matched_count=matched_count,
            ambiguous_count=ambiguous_count,
            distinct_count=distinct_count,
            review_suggestions=review_suggestions,
            fields_canonicalized=fields_canonicalized,
            corrections_preserved=corrections_preserved,
        )

    # -- internals ----------------------------------------------------------------------------------

    def _route_review(
        self,
        subject_id: str,
        other_id: str,
        result: OrgMatchResult,
        routed_pairs: set[tuple[str, str]],
        moment: datetime,
    ) -> bool:
        """Route ONE ambiguous (deterministic ``REVIEW``) pair to a review-only DCA-137 suggestion.

        The pair is keyed in CANONICAL order (so A↔B is one pair), routed at most once per resolution,
        and deduplicated against any already-staged suggestion (:meth:`existing_for_pair`), so re-running
        never duplicates it. Returns ``True`` when a NEW suggestion was staged. This records a REVIEW
        proposal only — it merges nothing and touches no consent/restriction state.
        """
        pair = (subject_id, other_id) if subject_id <= other_id else (other_id, subject_id)
        if pair in routed_pairs:
            return False
        routed_pairs.add(pair)
        if self._suggestions.existing_for_pair(SUBJECT_TYPE, pair[0], pair[1]) is not None:
            return False
        evidence = [
            {
                "kind": signal.kind.value,
                "strength": signal.strength.value,
                "relation": signal.relation.value,
                "detail": signal.detail,
            }
            for signal in result.signals
        ]
        self._suggestions.stage(
            subject_a_id=pair[0],
            subject_b_id=pair[1],
            match_outcome=result.outcome.value,
            match_confidence=result.confidence.value,
            evidence=evidence,
            ai_confidence=None,  # deterministic DCA-067 candidate — no model was consulted
            rationale="deterministic DCA-067 review candidate routed by observation-batch resolution",
            prompt_version=DETERMINISTIC_PROMPT_VERSION,
            synthetic=False,
            now=moment,
        )
        return True

    def _summarize_canonical(self, subject_ids: list[str]) -> tuple[int, int]:
        """Compute the canonical value per field for each batch subject (DCA-069) through the DCA-070
        user-correction overlay, WITHOUT writing anything. Returns ``(fields_canonicalized,
        corrections_preserved)`` — fields that resolved to a canonical value, and fields whose canonical
        value is a protected user correction (held over any provider refresh)."""
        fields_canonicalized = 0
        corrections_preserved = 0
        for subject_id in subject_ids:
            observations = self._fields.for_subject(SUBJECT_TYPE, subject_id)
            if not observations:
                continue
            selections = select_canonical_by_field(candidates_from_observations(observations))
            corrections = self._corrections.active_for_subject(SUBJECT_TYPE, subject_id)
            for field_key, selection in selections.items():
                outcome = apply_user_correction(
                    selection, correction_from_row(corrections.get(field_key))
                )
                if outcome.value is not None:
                    fields_canonicalized += 1
                if outcome.origin == ORIGIN_USER_CORRECTION:
                    corrections_preserved += 1
        return fields_canonicalized, corrections_preserved

    def _stage_resolution(
        self,
        *,
        run_id: str,
        subject_count: int,
        matched_count: int,
        ambiguous_count: int,
        distinct_count: int,
        review_suggestion_count: int,
        fields_canonicalized: int,
        corrections_preserved: int,
        actor: BatchResolutionActor,
        actor_audit: AuditActor,
        moment: datetime,
    ) -> ObservationBatchResolution:
        """Stage the resolution record + its atomic DCA-026 audit entry in the caller's unit of work."""
        provenance = {
            "source": RESOLUTION_SOURCE,
            "runId": run_id,
            # Load-bearing invariants: resolution reads already-permitted observations and never merges,
            # sends or charges. ``resolved`` records that the batch was resolved into conclusions.
            "resolved": True,
            "merged": False,
            "sent": False,
            "spent": False,
            "autoApproved": False,
        }
        resolution = ObservationBatchResolution(
            business_id=self._business_id,
            run_id=run_id,
            subject_count=subject_count,
            matched_count=matched_count,
            ambiguous_count=ambiguous_count,
            distinct_count=distinct_count,
            review_suggestion_count=review_suggestion_count,
            fields_canonicalized=fields_canonicalized,
            corrections_preserved=corrections_preserved,
            schema_version=RESOLUTION_SCHEMA_VERSION,
            provenance=provenance,
            created_at=moment,
        )
        self._session.add(resolution)

        # Atomic audit (SEC-001/BR-122): staged into the caller's session so it commits with the record.
        # References stay OPAQUE — the run id + counts only — never a contact value. Resolving a batch
        # routes review proposals but merges / sends / charges nothing.
        AuditSink(self._session, business_id=self._business_id, actor=actor_audit).record(
            action=AUDIT_ACTION_RESOLVE_BATCH,
            reason=(
                f"resolved run '{run_id}' batch of {subject_count} subject(s): "
                f"{matched_count} matched, {ambiguous_count} ambiguous "
                f"({review_suggestion_count} review suggestion(s) routed), {distinct_count} distinct"
            ),
            references={
                "runId": run_id,
                "subjectCount": subject_count,
                "matchedCount": matched_count,
                "ambiguousCount": ambiguous_count,
                "distinctCount": distinct_count,
                "reviewSuggestionCount": review_suggestion_count,
                "fieldsCanonicalized": fields_canonicalized,
                "correctionsPreserved": corrections_preserved,
            },
        )
        return resolution


def _best(current: str, candidate: str) -> str:
    """Return the stronger of two per-subject outcomes (MATCH > REVIEW > distinct)."""
    return candidate if _OUTCOME_RANK[candidate] < _OUTCOME_RANK[current] else current


__all__ = [
    "SUBJECT_TYPE",
    "RESOLUTION_SCHEMA_VERSION",
    "AUDIT_ACTION_RESOLVE_BATCH",
    "RESOLUTION_SOURCE",
    "REASON_NOT_AUTHORIZED",
    "REASON_RUN_NOT_FOUND",
    "REASON_NOTHING_TO_RESOLVE",
    "BatchResolutionError",
    "BatchResolutionAuthorizationError",
    "BatchResolutionActor",
    "BatchResolutionResult",
    "ObservationBatchResolver",
]
