"""DCA-145 — create ONE scoped prospect suggestion from a resolved discovery observation.

This is the next step of the still-disabled discovery pipeline after DCA-144 resolves a run's converted
observation batch: it CONVERTS one eligible resolved subject (an :class:`~app.db.models.OrgObservation`
in this business's batch) into a business-scoped CANDIDATE the business can review — a
:class:`~app.db.models.ProspectSuggestion` carrying a deterministic ICP ``match_score`` + ``rationale``,
the targeting ``scored_icp_version`` it was scored against, a ``schema_version``, an ``expires_at``
expiry and a ``visibility`` constraint.

There is **no network fetch, no model call and no recipient send.** Scoring is DETERMINISTIC Python over
the subject's already-recorded canonical fields (DCA-069) against the business's CONFIRMED India ICP
(DCA-040) — it never calls the AI gateway. Creating a suggestion is inert: it is **suggested-only** and
**never auto-accepted**; accepting it is a human action and acceptance is NOT recipient consent.

Every guard is checked fail-closed, in order, so the two acceptance criteria hold:

- **Research-only organizations are distinguishable from sendable contacts; no automatic audience
  membership.** A suggestion built from an organization observation is stamped
  :attr:`~app.db.models.SuggestionVisibility.RESEARCH_ONLY` — it carries no sendable contact and
  creating it adds the subject to NO audience / list / send surface (there is no such path in the DCA).
  A ``sendable`` suggestion is a disabled future capability this story never produces: making a prospect
  sendable requires recipient consent, which accepting a suggestion is not. The invariant is recorded on
  ``provenance`` (``sent`` / ``spent`` / ``autoAccepted`` / ``addedToAudience`` = ``False``) and enforced
  + tested.

- **Reproduce the result and the relevant denial without unintended state changes.** Authority is checked
  FIRST; then the run must exist in THIS business (an unknown / cross-tenant run reads as not-found); then
  the run must have been RESOLVED (DCA-144) and the subject must be an eligible member of that resolved
  batch (an unknown / cross-tenant / off-batch subject reads as not-found). Each refusal is raised BEFORE
  the session is mutated, so it changes nothing. A subject is suggested ONCE per run —
  ``(business_id, run_id, subject_id)`` is unique, so re-suggesting returns the existing row and stages no
  duplicate and no second audit.

Like the sibling DCA-143/144 services, the creator stages into the *caller's* session and never commits —
the route/worker owns the unit of work, so the suggestion and its atomic DCA-026 audit entry commit
together. There is no argument to widen the business scope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.sink import ActorType, AuditActor, AuditSink
from app.canonical.field_selection import (
    candidates_from_observations,
    select_canonical_by_field,
)
from app.db.models import (
    DiscoveryRunRequest,
    FieldObservation,
    FieldSubjectType,
    IcpDraft,
    ObservationBatchResolution,
    ProspectSuggestion,
    SuggestionVisibility,
)
from app.identity.permissions import Scope, scopes_for_business_roles
from app.services.field_observation import FieldObservationStore
from app.services.icp_store import IcpStore
from app.services.org_observation import OrgObservationStore

# The subject kind a discovery suggestion is about — organization identities only (the DCA-143 converter
# records organization-level facts; there is no contact in the batch, which is exactly why every
# suggestion is research-only).
SUBJECT_TYPE = FieldSubjectType.ORGANIZATION.value


# How a DCA-143 conversion stamps a recorded field's provenance:
# ``discovery_run:{run_id}/task:{i}#evidence={id}``. The run's batch is exactly the distinct organization
# subjects whose field observations carry this run's prefix — identical to the DCA-144 batch definition,
# so a suggestion is drawn from precisely what the run converted, nothing wider.
def _run_provenance_prefix(run_id: str) -> str:
    return f"discovery_run:{run_id}/task:"


# The ICP targeting dimensions the deterministic score considers (positive targeting only — exclusions
# narrow a target, they never establish fit). Equal-weighted over the dimensions the ICP actually sets.
DIMENSION_INDUSTRY = "industry"
DIMENSION_GEOGRAPHY = "geography"

# The neutral score used when the confirmed ICP sets none of the comparable positive dimensions (there is
# nothing to score against) — recorded honestly rather than fabricating a high or low fit.
NEUTRAL_SCORE = 0.5

# How long a suggestion stays actionable before it should be refreshed (the expiry window). Deterministic
# and self-contained — a suggestion is a review artefact, not a live send target.
SUGGESTION_TTL_DAYS = 30

# The version of the suggestion contract rows are written under (expand→migrate→contract).
SUGGESTION_SCHEMA_VERSION = 1

# The audited action code for creating a prospect suggestion (a controlled code).
AUDIT_ACTION_SUGGEST = "prospect.suggestion.created"

# Where a suggestion records its origin on the provenance.
SUGGESTION_SOURCE = "prospect_suggestion"

# Stable refusal codes raised (never recorded) for a malformed / ineligible request — each BEFORE any
# state is mutated, so the attempt changes nothing.
REASON_NOT_AUTHORIZED = "prospect_suggestion_not_authorized"
REASON_RUN_NOT_FOUND = "discovery_run_not_found"
REASON_RUN_NOT_RESOLVED = "discovery_run_not_resolved"
REASON_SUBJECT_NOT_ELIGIBLE = "subject_not_eligible"


class ProspectSuggestionError(Exception):
    """A suggestion create was refused with a stable ``code`` for a malformed / ineligible request (an
    unknown or cross-tenant run, a run whose batch has not been resolved, or a subject that is not an
    eligible member of the resolved batch). Raised before the session is mutated, so a refusal changes
    nothing. ``detail`` names the offending value."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class ProspectSuggestionAuthorizationError(ProspectSuggestionError):
    """The caller is not authorized to create a prospect suggestion.

    A distinct type so an HTTP boundary can map it to a 403 while the other codes map to a 404/409.
    Raised FIRST — before any state is read or written. ``code`` is :data:`REASON_NOT_AUTHORIZED`.
    """

    def __init__(self, message: str) -> None:
        super().__init__(REASON_NOT_AUTHORIZED, message)


@dataclass(frozen=True)
class ProspectSuggestionActor:
    """The business principal creating a prospect suggestion.

    ``subject`` is an opaque actor id (never a name/email) recorded on the suggestion's atomic audit
    entry. ``roles`` are the caller's verified business-plane membership roles (from the tenant context,
    NEVER a bare token claim). :meth:`may_suggest` grants the action only when those roles yield
    :attr:`~app.identity.permissions.Scope.RUN_PLAN_APPROVE` — suggesting from a run's resolved batch is
    part of the same chargeable-discovery authority that approved the plan (DCA-141), executed it
    (DCA-142), converted its tasks (DCA-143) and resolved them (DCA-144). A read-only viewer and a
    platform operator (holding no business scope) cannot perform it.
    """

    subject: str
    roles: tuple[str, ...] = ()
    actor_type: str = "user"

    def may_suggest(self) -> bool:
        return Scope.RUN_PLAN_APPROVE in scopes_for_business_roles(self.roles)

    def as_audit_actor(self) -> AuditActor:
        return AuditActor(id=self.subject, type=ActorType(self.actor_type))


@dataclass
class ProspectSuggestionResult:
    """The inert, inspectable outcome of one suggest attempt.

    ``suggestion`` is the staged :class:`~app.db.models.ProspectSuggestion`. ``already_suggested`` is
    ``True`` when this was an idempotent no-op (the subject had already been suggested for this run).
    The remaining fields echo the suggestion's load-bearing attributes — the ICP ``match_score`` /
    ``rationale``, the ``matched_dimensions`` that drove the score, the ``scored_icp_version`` targeting
    version, the ``visibility`` constraint, whether it is ``sendable`` (always ``False`` here) and the
    ``expires_at`` expiry. Nothing here is ever sent, spent or accepted.
    """

    business_id: str
    suggestion: ProspectSuggestion
    match_score: float
    rationale: str
    matched_dimensions: list[str] = field(default_factory=list)
    scored_icp_version: int | None = None
    visibility: str = SuggestionVisibility.RESEARCH_ONLY.value
    expires_at: datetime | None = None
    already_suggested: bool = False

    @property
    def sendable(self) -> bool:
        """Whether the suggestion is a sendable recipient — always ``False`` for a research-only org."""
        return self.visibility == SuggestionVisibility.SENDABLE.value


def score_against_icp(
    icp: IcpDraft | None, canonical_fields: dict[str, str], state_code: str
) -> tuple[float, list[str], str]:
    """Deterministically score a subject's fit against the confirmed ICP (pure, no AI, no I/O).

    Considers the ICP's positive targeting dimensions that are actually set — ``industry`` (the
    subject's canonical ``industry`` field vs the ICP industries) and ``geography`` (the subject's state/
    UT, from its establishment ``state_code`` or a canonical ``state`` field, vs the ICP geography). The
    score is the fraction of the comparable dimensions the subject matches (equal-weighted); when the ICP
    sets none of them, there is nothing to score against and the score is :data:`NEUTRAL_SCORE`. Returns
    ``(score, matched_dimension_codes, rationale)``; the rationale enumerates which dimensions matched —
    it names dimension codes only, never a contact value.
    """
    present: list[str] = []
    matched: list[str] = []

    industries = {str(v).strip().lower() for v in (icp.industries if icp else []) if str(v).strip()}
    geography = {str(v).strip().upper() for v in (icp.geography if icp else []) if str(v).strip()}

    if industries:
        present.append(DIMENSION_INDUSTRY)
        subject_industry = canonical_fields.get("industry", "").strip().lower()
        if subject_industry and subject_industry in industries:
            matched.append(DIMENSION_INDUSTRY)

    if geography:
        present.append(DIMENSION_GEOGRAPHY)
        subject_geo = (state_code or canonical_fields.get("state", "")).strip().upper()
        if subject_geo and subject_geo in geography:
            matched.append(DIMENSION_GEOGRAPHY)

    if not present:
        return (
            NEUTRAL_SCORE,
            [],
            (
                "no comparable ICP targeting dimensions were set; recorded a neutral fit pending targeting"
            ),
        )

    score = round(len(matched) / len(present), 4)
    unmatched = [d for d in present if d not in matched]
    parts: list[str] = []
    if matched:
        parts.append("matched " + ", ".join(matched))
    if unmatched:
        parts.append("did not match " + ", ".join(unmatched))
    rationale = (
        f"ICP fit {score:.2f}: {'; '.join(parts)} "
        f"({len(matched)}/{len(present)} targeting dimension(s) matched)"
    )
    return score, matched, rationale


class ProspectSuggestionCreator:
    """Create ONE scoped prospect suggestion from a resolved discovery observation, for ONE business.

    Construct with the active session and the verified business scope (a route/worker resolves it from
    the membership-verified tenant context). Composes the DCA-069 canonical selection and the confirmed
    DCA-040 ICP over the organization observations the run resolved. Every query is confined to this
    business — there is no argument to widen the scope. The creator stages into the caller's session and
    never commits; it suggests only and sends nothing.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id
        self._orgs = OrgObservationStore(session, business_id)

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def suggestion_for(self, run_id: str, subject_id: str) -> ProspectSuggestion | None:
        """The suggestion for one (run, subject) within this business, or ``None`` (the idempotency
        read; a cross-tenant / unknown id reads as not-found)."""
        if not run_id or not subject_id:
            return None
        stmt = (
            select(ProspectSuggestion)
            .where(ProspectSuggestion.business_id == self._business_id)
            .where(ProspectSuggestion.run_id == run_id)
            .where(ProspectSuggestion.subject_id == subject_id)
        )
        return self._session.scalars(stmt).one_or_none()

    def _run(self, run_id: str) -> DiscoveryRunRequest | None:
        if not run_id:
            return None
        stmt = (
            select(DiscoveryRunRequest)
            .where(DiscoveryRunRequest.business_id == self._business_id)
            .where(DiscoveryRunRequest.id == run_id)
        )
        return self._session.scalars(stmt).one_or_none()

    def _resolution(self, run_id: str) -> ObservationBatchResolution | None:
        stmt = (
            select(ObservationBatchResolution)
            .where(ObservationBatchResolution.business_id == self._business_id)
            .where(ObservationBatchResolution.run_id == run_id)
        )
        return self._session.scalars(stmt).one_or_none()

    def _batch_subject_ids(self, run_id: str) -> set[str]:
        """The distinct organization subjects of this run's converted field observations (the eligible
        set). Exactly the DCA-144 batch (provenance-prefixed), tenant-scoped, so a cross-tenant subject
        can never enter it."""
        stmt = (
            select(FieldObservation.subject_id)
            .where(FieldObservation.business_id == self._business_id)
            .where(FieldObservation.subject_type == SUBJECT_TYPE)
            .where(FieldObservation.provenance_ref.like(f"{_run_provenance_prefix(run_id)}%"))
            .distinct()
        )
        return set(self._session.scalars(stmt).all())

    def _canonical_fields(self, subject_id: str, moment: datetime) -> dict[str, str]:
        """The subject's canonical field values (DCA-069) keyed by field_key — read-only, no mutation."""
        observations = FieldObservationStore(self._session, self._business_id).for_subject(
            SUBJECT_TYPE, subject_id
        )
        selections = select_canonical_by_field(
            candidates_from_observations(observations), now=moment
        )
        return {
            field_key: selection.winner.value
            for field_key, selection in selections.items()
            if selection.winner is not None
        }

    # -- the one suggest path -----------------------------------------------------------------------

    def suggest(
        self,
        run_id: str,
        subject_id: str,
        actor: ProspectSuggestionActor,
        *,
        now: datetime | None = None,
    ) -> ProspectSuggestionResult:
        """Create the scoped prospect suggestion for ``subject_id`` of run ``run_id``.

        Order (each gate leaves prior state unchanged when it refuses):

        1. **Authority FIRST.** A caller lacking :attr:`~app.identity.permissions.Scope.RUN_PLAN_APPROVE`
           is refused (:class:`ProspectSuggestionAuthorizationError`) before any state is read.
        2. **The run exists in THIS business.** Otherwise refused (:data:`REASON_RUN_NOT_FOUND`) — an
           unknown or cross-tenant run reads exactly like not-found.
        3. **The run has been resolved.** The run's batch must have a DCA-144
           :class:`~app.db.models.ObservationBatchResolution`; otherwise refused
           (:data:`REASON_RUN_NOT_RESOLVED`) — a suggestion is drawn from a RESOLVED batch, never a raw
           one.
        4. **The subject is eligible.** It must be a readable organization observation in this business
           AND a member of the run's resolved batch; otherwise refused
           (:data:`REASON_SUBJECT_NOT_ELIGIBLE`) — an unknown / cross-tenant / off-batch subject reads as
           not-found.
        5. **Idempotent replay.** An existing suggestion for (run, subject) is returned unchanged (no
           duplicate, no new audit) — a subject is suggested once per run.
        6. **Score + stamp.** The subject's canonical fields (DCA-069) are scored deterministically
           against the run's confirmed ICP version (DCA-040); the suggestion is stamped research-only
           (no sendable contact, no audience membership) with its score/rationale/version/expiry.

        On success an atomic DCA-026 audit entry is staged in the caller's unit of work. Suggesting reads
        already-permitted observations and performs no fetch, model call, spend, send or auto-acceptance.
        """
        moment = now or datetime.now(UTC)

        # 1) Authority FIRST — an unauthorized caller is refused with no state touched.
        if not actor.may_suggest():
            raise ProspectSuggestionAuthorizationError(
                "creating a prospect suggestion requires a business administrator holding "
                f"'{Scope.RUN_PLAN_APPROVE.value}'; a read-only viewer or a platform operator "
                "(holding no business scope) cannot perform it"
            )

        # 2) The run must exist WITHIN this business (an unknown / cross-tenant run is not-found).
        run = self._run(run_id)
        if run is None:
            raise ProspectSuggestionError(
                REASON_RUN_NOT_FOUND,
                "no such discovery run in this business (an unknown or cross-tenant id is not-found)",
                detail=run_id,
            )

        # 3) The run's batch must have been resolved (DCA-144) — a suggestion comes from a resolved batch.
        if self._resolution(run_id) is None:
            raise ProspectSuggestionError(
                REASON_RUN_NOT_RESOLVED,
                f"discovery run '{run_id}' has no resolved observation batch — resolve it (DCA-144) "
                "before suggesting prospects from it",
                detail=run_id,
            )

        # 4) The subject must be an eligible member of the resolved batch AND readable in this business.
        subject = self._orgs.get(subject_id)
        if subject is None or subject_id not in self._batch_subject_ids(run_id):
            raise ProspectSuggestionError(
                REASON_SUBJECT_NOT_ELIGIBLE,
                f"subject '{subject_id}' is not an eligible resolved member of run '{run_id}' in this "
                "business (an unknown, cross-tenant or off-batch subject is not-found)",
                detail=subject_id,
            )

        # 5) Idempotent replay — a subject is suggested ONCE per run; return the existing row unchanged.
        existing = self.suggestion_for(run_id, subject_id)
        if existing is not None:
            return ProspectSuggestionResult(
                business_id=self._business_id,
                suggestion=existing,
                match_score=existing.match_score,
                rationale=existing.rationale,
                matched_dimensions=list(existing.provenance.get("matchedDimensions", [])),
                scored_icp_version=existing.scored_icp_version,
                visibility=existing.visibility,
                expires_at=existing.expires_at,
                already_suggested=True,
            )

        # 6) Deterministic ICP fit score (no AI) over the subject's canonical fields, against the run's
        #    confirmed targeting version. The suggestion is research-only: an organization observation
        #    carries no sendable contact, so it is never a message recipient and joins no audience.
        icp_version = run.icp_version
        icp = IcpStore(self._session, self._business_id).get_version(icp_version)
        canonical = self._canonical_fields(subject_id, moment)
        match_score, matched_dimensions, rationale = score_against_icp(
            icp, canonical, subject.state_code or ""
        )
        expires_at = moment + timedelta(days=SUGGESTION_TTL_DAYS)

        suggestion = self._stage_suggestion(
            run_id=run_id,
            subject_id=subject_id,
            icp_version=icp_version,
            match_score=match_score,
            matched_dimensions=matched_dimensions,
            rationale=rationale,
            expires_at=expires_at,
            actor=actor,
            moment=moment,
        )
        return ProspectSuggestionResult(
            business_id=self._business_id,
            suggestion=suggestion,
            match_score=match_score,
            rationale=rationale,
            matched_dimensions=matched_dimensions,
            scored_icp_version=icp_version,
            visibility=SuggestionVisibility.RESEARCH_ONLY.value,
            expires_at=expires_at,
        )

    # -- internals ----------------------------------------------------------------------------------

    def _stage_suggestion(
        self,
        *,
        run_id: str,
        subject_id: str,
        icp_version: int,
        match_score: float,
        matched_dimensions: list[str],
        rationale: str,
        expires_at: datetime,
        actor: ProspectSuggestionActor,
        moment: datetime,
    ) -> ProspectSuggestion:
        """Stage the suggestion row + its atomic DCA-026 audit entry in the caller's unit of work."""
        provenance = {
            "source": SUGGESTION_SOURCE,
            "runId": run_id,
            "subjectType": SUBJECT_TYPE,
            "scoredIcpVersion": icp_version,
            "matchedDimensions": list(matched_dimensions),
            # Load-bearing invariants: a suggestion is research-only, is NEVER a sendable recipient, joins
            # NO audience, and is never auto-accepted / sent / charged. ``suggested`` records that the
            # resolved subject was turned into a reviewable candidate.
            "suggested": True,
            "sendable": False,
            "addedToAudience": False,
            "autoAccepted": False,
            "sent": False,
            "spent": False,
        }
        suggestion = ProspectSuggestion(
            business_id=self._business_id,
            run_id=run_id,
            subject_type=SUBJECT_TYPE,
            subject_id=subject_id,
            visibility=SuggestionVisibility.RESEARCH_ONLY.value,
            match_score=match_score,
            rationale=rationale,
            scored_icp_version=icp_version,
            schema_version=SUGGESTION_SCHEMA_VERSION,
            status="suggested",
            provenance=provenance,
            created_at=moment,
            expires_at=expires_at,
        )
        self._session.add(suggestion)

        # Atomic audit (SEC-001/BR-122): staged into the caller's session so it commits with the row.
        # References stay OPAQUE — the run id, subject id, visibility, score, targeting version and the
        # matched dimension codes — never a contact value. Suggesting creates a review candidate but
        # sends / charges / accepts nothing.
        AuditSink(
            self._session, business_id=self._business_id, actor=actor.as_audit_actor()
        ).record(
            action=AUDIT_ACTION_SUGGEST,
            reason=(
                f"suggested subject '{subject_id}' from run '{run_id}' "
                f"(research-only, ICP fit {match_score:.2f})"
            ),
            references={
                "runId": run_id,
                "subjectId": subject_id,
                "subjectType": SUBJECT_TYPE,
                "visibility": SuggestionVisibility.RESEARCH_ONLY.value,
                "sendable": False,
                "addedToAudience": False,
                "matchScore": match_score,
                "scoredIcpVersion": icp_version,
                "matchedDimensions": list(matched_dimensions),
            },
        )
        return suggestion


__all__ = [
    "SUBJECT_TYPE",
    "DIMENSION_INDUSTRY",
    "DIMENSION_GEOGRAPHY",
    "NEUTRAL_SCORE",
    "SUGGESTION_TTL_DAYS",
    "SUGGESTION_SCHEMA_VERSION",
    "AUDIT_ACTION_SUGGEST",
    "SUGGESTION_SOURCE",
    "REASON_NOT_AUTHORIZED",
    "REASON_RUN_NOT_FOUND",
    "REASON_RUN_NOT_RESOLVED",
    "REASON_SUBJECT_NOT_ELIGIBLE",
    "ProspectSuggestionError",
    "ProspectSuggestionAuthorizationError",
    "ProspectSuggestionActor",
    "ProspectSuggestionResult",
    "ProspectSuggestionCreator",
    "score_against_icp",
]
