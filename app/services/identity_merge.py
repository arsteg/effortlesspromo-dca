"""DCA-071 — the authorized, evidenced, review-only identity-merge store.

:class:`IdentityMergeStore` is the single sanctioned writer of
:class:`~app.db.models.IdentityMerge`. It lets an AUTHORIZED, HUMAN business reviewer record that two
of that business's observed identities — two :class:`~app.db.models.OrgObservation` rows or two
:class:`~app.db.models.PersonObservation` rows — are the SAME real-world entity, on top of a reviewed
COMPATIBLE matcher conclusion.

This is the review ACTION that sits on top of a pure matcher *conclusion*. The DCA-067/068 matchers
(:func:`~app.matching.org_match.match_organizations`,
:func:`~app.matching.contact_match.match_contacts`) are deterministic, network-free functions that
only ever return ``MATCH`` / ``NO_MATCH`` / ``REVIEW`` / ``INSUFFICIENT`` and merge, transfer and
record NOTHING (``auto_merged`` is always ``False``). A merge is therefore never an automatic collapse
— it is this deliberate, authorized, evidenced human decision.

The load-bearing guarantees (the DCA-071 acceptance criteria):

- **Only a REVIEWED, COMPATIBLE pair merges.** :meth:`IdentityMergeStore.merge` re-runs the relevant
  matcher over the two STORED observations (never trusting a caller-asserted conclusion) and refuses
  unless the outcome is compatible — a ``MATCH`` or a ``REVIEW`` candidate (the "ambiguous" merge a
  human approves). A ``NO_MATCH`` (a conflicting typed registration identifier, or two different people
  on a recycled contact point) or an ``INSUFFICIENT`` pair (no linking evidence at all) is refused with
  a stable code BEFORE the session is mutated, so an incompatible merge stages no row. The matcher
  conclusion/evidence is snapshotted onto the row.

- **Unrelated consent scopes stay SEPARATE.** Recording a merge links two IDENTITIES only; it NEVER
  creates, modifies or transfers any :class:`~app.db.models.ConsentReceipt` or
  :class:`~app.db.models.ProcessingRestriction`. This store never touches those tables — merging
  identity is not merging consent, suppression or restrictions (mirrors the DCA-068 guarantee that
  identity resolution never moves one person's permission onto another).

- **Authorized by a HUMAN reviewer; an AI suggestion can never execute a merge.** Authority is checked
  FIRST: a caller must be a HUMAN user actor whose membership roles grant
  :attr:`~app.identity.permissions.Scope.IDENTITY_MERGE`. A read-only viewer, a non-member, a platform
  operator (no membership) and — explicitly — an AI/automation (``service`` / ``system``) actor are
  each refused before any state is read or written (the relevant denial), so a denial stages no row and
  no audit entry.

- **Business-private, tenant-scoped, idempotent-safe.** The store is bound to one verified business;
  both subjects are resolved WITHIN it (an unknown or cross-tenant subject reads as not-found), and a
  by-id read of another business's merge resolves to not-found. A pair is merged at most once —
  re-approving an already-merged pair (in either direction) returns the existing row rather than
  inserting a duplicate.

The store stages into the *caller's* session and never commits — the caller owns the unit of work, so
the merge row and its atomic DCA-026 audit entry commit together. Audit references stay OPAQUE (ids /
subject kind / match outcome / signal count) — never the evidence signal details or any raw value.
Recording a merge creates no prospect / host record and sends nothing; acting on the merged identity
downstream (collapsing observations, combining records) stays a DISABLED future capability, and
reversing a merge is the SEPARATE next story (DCA-072) — this story only records ``ACTIVE`` merges.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.sink import ActorType, AuditActor, AuditSink
from app.db.models import FieldSubjectType, IdentityMerge, IdentityMergeStatus
from app.identity.permissions import Scope, scopes_for_business_roles
from app.matching.contact_match import (
    ContactMatchOutcome,
    match_contacts,
    subject_from_person_observation,
)
from app.matching.org_match import MatchOutcome, match_organizations, subject_from_observation
from app.services.contact_point import ContactPointStore
from app.services.org_observation import OrgObservationStore

# The controlled vocabulary of identity kinds a merge can be about (shared with field corrections).
SUBJECT_TYPES: tuple[str, ...] = tuple(t.value for t in FieldSubjectType)

# The audited action code for recording an identity merge (a controlled code, like the other writes).
AUDIT_ACTION_RECORD_MERGE = "identity.merge.record"

# Stable refusal codes. Each is raised BEFORE the session is mutated, so a refusal stages no row.
REASON_NOT_AUTHORIZED = "identity_merge_not_authorized"
REASON_AI_ACTOR_CANNOT_MERGE = "identity_merge_ai_actor_forbidden"
REASON_UNKNOWN_SUBJECT_TYPE = "unknown_subject_type"
REASON_SUBJECT_NOT_FOUND = "subject_not_found"
REASON_SELF_MERGE = "self_merge"
REASON_INCOMPATIBLE_IDENTITIES = "incompatible_identities"

# The matcher outcomes a human may approve a merge on: a confirmed match, or an AMBIGUOUS review
# candidate (the "approve an ambiguous identity merge" case). A NO_MATCH / INSUFFICIENT pair is never
# compatible — a shared/weak signal that was REFUTED, or no linking evidence at all.
_COMPATIBLE_OUTCOMES = frozenset(
    {
        MatchOutcome.MATCH.value,
        MatchOutcome.REVIEW.value,
        ContactMatchOutcome.MATCH.value,
        ContactMatchOutcome.REVIEW.value,
    }
)


class IdentityMergeError(Exception):
    """A merge was refused with a stable code. Raised before the session is mutated, so a refusal
    leaves no row and no partial state. ``detail`` names the offending value when useful."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class IdentityMergeAuthorizationError(IdentityMergeError):
    """The caller is not authorized to record an identity merge.

    Raised FIRST — before any state is read or written — for a non-human (AI/automation) actor
    (:data:`REASON_AI_ACTOR_CANNOT_MERGE`) or a human lacking
    :attr:`~app.identity.permissions.Scope.IDENTITY_MERGE` (:data:`REASON_NOT_AUTHORIZED`). A distinct
    type so the HTTP boundary (where one exists) can map it to a 403 while the other
    :class:`IdentityMergeError` codes map to a 404/422.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


@dataclass(frozen=True)
class MergeEvidence:
    """The matcher conclusion that justified (or refused) a merge — a pure, side-effect-free snapshot.

    ``outcome`` / ``confidence`` are the matcher's categorical decision and grade; ``signals`` is the
    evidence set (kind/strength/relation/detail dicts — company facts / mailbox domains only, never a
    raw personal contact value). ``compatible`` is ``True`` only for a ``MATCH`` or ``REVIEW`` outcome.
    """

    outcome: str
    confidence: str
    signals: tuple[dict, ...]

    @property
    def compatible(self) -> bool:
        return self.outcome in _COMPATIBLE_OUTCOMES


@dataclass(frozen=True)
class IdentityReviewer:
    """The business principal approving a merge.

    ``subject`` is an opaque actor id (never a name/email) recorded on the merge and its atomic audit
    entry. ``roles`` are the caller's business-plane membership roles (from the verified
    :class:`~app.identity.tenant_context.TenantContext`, NEVER a bare token claim). ``actor_type`` is
    the kind of actor: a merge is a HUMAN review action, so only a ``user`` actor can perform it — an
    AI suggestion / automation (``service`` / ``system``) actor is refused even if it somehow carried a
    role. :meth:`may_merge` grants the action only when the actor is human AND those roles yield
    :attr:`~app.identity.permissions.Scope.IDENTITY_MERGE`.
    """

    subject: str
    roles: tuple[str, ...] = ()
    actor_type: str = "user"

    def is_human_reviewer(self) -> bool:
        return self.actor_type == ActorType.USER.value

    def has_merge_scope(self) -> bool:
        return Scope.IDENTITY_MERGE in scopes_for_business_roles(self.roles)

    def may_merge(self) -> bool:
        return self.is_human_reviewer() and self.has_merge_scope()

    def as_actor(self) -> AuditActor:
        return AuditActor(id=self.subject, type=ActorType(self.actor_type))


@dataclass
class IdentityMergeInput:
    """One identity merge as supplied by the reviewer.

    ``subject_type`` names the identity kind (``organization`` or ``person``). ``primary_subject_id`` is
    the SURVIVING identity the merge keeps canonical; ``duplicate_subject_id`` the MERGED-AWAY identity
    recorded as the same entity. Both must exist in THIS business. ``reason`` is a short opaque
    justification (optional).
    """

    subject_type: str
    primary_subject_id: str
    duplicate_subject_id: str
    reason: str = ""


def _serialize_signals(signals: object) -> tuple[dict, ...]:
    """Snapshot a matcher's evidence signals into plain, JSON-storable dicts.

    Works for both :class:`~app.matching.org_match.MatchSignal` and
    :class:`~app.matching.contact_match.ContactMatchSignal` (same ``kind``/``strength``/``relation``/
    ``detail`` shape). ``detail`` carries only company facts / mailbox domains — never a raw personal
    contact value — so the snapshot stays safe to store and surface.
    """
    return tuple(
        {
            "kind": s.kind.value,
            "strength": s.strength.value,
            "relation": s.relation.value,
            "detail": s.detail,
        }
        for s in signals  # type: ignore[attr-defined]
    )


def _normalize_subject_type(value: str) -> str:
    token = (value or "").strip().lower()
    try:
        return FieldSubjectType(token).value
    except ValueError as exc:
        raise IdentityMergeError(
            REASON_UNKNOWN_SUBJECT_TYPE,
            f"unknown subject_type '{value}'; expected one of: {', '.join(SUBJECT_TYPES)}",
            detail=value,
        ) from exc


class IdentityMergeStore:
    """Tenant-scoped reader/writer for reviewed identity merges, bound to one business.

    Construct with the active session and the verified business scope (a route resolves it from the
    membership-verified tenant context). Every query is confined to that business — there is no
    argument to widen the scope.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def get(self, merge_id: str) -> IdentityMerge | None:
        """Fetch one merge by id WITHIN this business. Another business's id (or an unknown id)
        resolves to ``None`` — a cross-tenant id reads exactly like not-found."""
        if not merge_id:
            return None
        stmt = (
            select(IdentityMerge)
            .where(IdentityMerge.business_id == self._business_id)
            .where(IdentityMerge.id == merge_id)
        )
        return self._session.scalars(stmt).one_or_none()

    def merges(self) -> list[IdentityMerge]:
        """Every merge recorded in this business, oldest first."""
        stmt = (
            select(IdentityMerge)
            .where(IdentityMerge.business_id == self._business_id)
            .order_by(IdentityMerge.created_at, IdentityMerge.id)
        )
        return list(self._session.scalars(stmt).all())

    def existing_for_pair(
        self, subject_type: str, subject_a: str, subject_b: str
    ) -> IdentityMerge | None:
        """The merge linking two subjects in EITHER direction within this business, or ``None``.

        Order-independent: ``(a, b)`` and ``(b, a)`` resolve to the same merge, which is what makes
        re-approving an already-merged pair idempotent (it returns the existing row, never a duplicate).
        """
        normalized_type = _normalize_subject_type(subject_type)
        stmt = (
            select(IdentityMerge)
            .where(IdentityMerge.business_id == self._business_id)
            .where(IdentityMerge.subject_type == normalized_type)
            .where(
                (
                    (IdentityMerge.primary_subject_id == subject_a)
                    & (IdentityMerge.duplicate_subject_id == subject_b)
                )
                | (
                    (IdentityMerge.primary_subject_id == subject_b)
                    & (IdentityMerge.duplicate_subject_id == subject_a)
                )
            )
        )
        return self._session.scalars(stmt).first()

    # -- the one write path -------------------------------------------------------------------------

    def evaluate(self, subject_type: str, primary_id: str, duplicate_id: str) -> MergeEvidence:
        """Run the relevant matcher over the two STORED observations and snapshot its conclusion.

        Pure read: it builds a matcher subject from each stored, tenant-scoped observation and returns
        the deterministic :class:`MergeEvidence`. It merges, transfers and records nothing — exactly the
        conclusion-not-action contract of DCA-067/068. The store uses this to decide whether a pair is
        COMPATIBLE enough to approve; it is exposed so a caller can preview the evidence first.
        """
        normalized_type = _normalize_subject_type(subject_type)
        if normalized_type == FieldSubjectType.ORGANIZATION.value:
            org_store = OrgObservationStore(self._session, self._business_id)
            a = org_store.get(primary_id)
            b = org_store.get(duplicate_id)
            if a is None or b is None:
                raise IdentityMergeError(
                    REASON_SUBJECT_NOT_FOUND,
                    "both organization observations must exist in this business",
                )
            org_result = match_organizations(
                subject_from_observation(org_store, a),
                subject_from_observation(org_store, b),
            )
            return MergeEvidence(
                org_result.outcome.value,
                org_result.confidence.value,
                _serialize_signals(org_result.signals),
            )

        contact_store = ContactPointStore(self._session, self._business_id)
        org_store = OrgObservationStore(self._session, self._business_id)
        a_person = contact_store.get_person(primary_id)
        b_person = contact_store.get_person(duplicate_id)
        if a_person is None or b_person is None:
            raise IdentityMergeError(
                REASON_SUBJECT_NOT_FOUND,
                "both person observations must exist in this business",
            )
        contact_result = match_contacts(
            subject_from_person_observation(contact_store, a_person, org_store=org_store),
            subject_from_person_observation(contact_store, b_person, org_store=org_store),
        )
        return MergeEvidence(
            contact_result.outcome.value,
            contact_result.confidence.value,
            _serialize_signals(contact_result.signals),
        )

    def merge(
        self,
        data: IdentityMergeInput,
        reviewer: IdentityReviewer,
        *,
        now: datetime | None = None,
    ) -> IdentityMerge:
        """Authorize, validate and stage a reviewed identity merge; return the (uncommitted) row.

        Order of gates (each leaves NO state behind when it refuses):

        1. **Authority FIRST.** A non-human (AI/automation) actor is refused
           (:data:`REASON_AI_ACTOR_CANNOT_MERGE`); a human lacking
           :attr:`~app.identity.permissions.Scope.IDENTITY_MERGE` is refused
           (:data:`REASON_NOT_AUTHORIZED`) — before any state is read or written.
        2. **Shape.** An unknown subject type, a self-merge (the same id on both sides), or a subject
           that does not exist in this business (an unknown id OR another tenant's id — both read as
           not-found) each raise with a stable code and stage no row.
        3. **Compatibility.** The matcher is re-run over the two stored observations; unless the
           conclusion is a ``MATCH`` or ``REVIEW`` (the ambiguous candidate a human approves), the merge
           is refused (:data:`REASON_INCOMPATIBLE_IDENTITIES`) — a ``NO_MATCH`` / ``INSUFFICIENT`` pair
           changes nothing.
        4. **Idempotency.** A pair already merged (in either direction) returns the EXISTING row — no
           duplicate row and no second audit entry.

        On success a new ``ACTIVE`` merge row is staged, snapshotting the match outcome/confidence and
        the evidence signals, plus an atomic DCA-026 audit entry (opaque references only — ids / subject
        kind / outcome / signal count, never the signal details or any raw value) in the SAME unit of
        work. Consent/restriction tables are NEVER touched. The caller owns the transaction.
        """
        moment = now or datetime.now(UTC)

        # 1) Authority FIRST — an AI/automation actor or an unauthorized human is refused, no state read.
        if not reviewer.is_human_reviewer():
            raise IdentityMergeAuthorizationError(
                REASON_AI_ACTOR_CANNOT_MERGE,
                "a merge is a human review action; an AI suggestion / automation actor "
                f"('{reviewer.actor_type}') can never execute one — the matchers only ever conclude, "
                "they merge nothing",
            )
        if not reviewer.has_merge_scope():
            raise IdentityMergeAuthorizationError(
                REASON_NOT_AUTHORIZED,
                "recording an identity merge requires a business user holding "
                f"'{Scope.IDENTITY_MERGE.value}'; a read-only viewer or a non-member cannot perform it",
            )

        # 2) Shape — unknown kind, self-merge, or a subject outside this business (all before any write).
        subject_type = _normalize_subject_type(data.subject_type)
        primary_id = (data.primary_subject_id or "").strip()
        duplicate_id = (data.duplicate_subject_id or "").strip()
        if not primary_id or not duplicate_id:
            raise IdentityMergeError(
                REASON_SUBJECT_NOT_FOUND,
                "a merge must name both the primary and duplicate subject ids",
            )
        if primary_id == duplicate_id:
            raise IdentityMergeError(
                REASON_SELF_MERGE,
                "a subject cannot be merged with itself",
                detail=primary_id,
            )

        # 3) Compatibility — evaluate() also enforces that BOTH subjects exist in this business
        #    (an unknown / cross-tenant id reads as not-found). The matcher conclusion is pure.
        evidence = self.evaluate(subject_type, primary_id, duplicate_id)
        if not evidence.compatible:
            raise IdentityMergeError(
                REASON_INCOMPATIBLE_IDENTITIES,
                "a merge requires a reviewed COMPATIBLE conclusion (a confirmed match or an ambiguous "
                f"review candidate); the matcher concluded '{evidence.outcome}' — refused",
                detail=evidence.outcome,
            )

        # 4) Idempotency — a pair already merged (either direction) returns the existing row, no dup.
        existing = self.existing_for_pair(subject_type, primary_id, duplicate_id)
        if existing is not None:
            return existing

        merge = IdentityMerge(
            id=str(uuid.uuid4()),
            business_id=self._business_id,
            subject_type=subject_type,
            primary_subject_id=primary_id,
            duplicate_subject_id=duplicate_id,
            match_outcome=evidence.outcome,
            match_confidence=evidence.confidence,
            evidence=[dict(s) for s in evidence.signals],
            status=IdentityMergeStatus.ACTIVE.value,
            reason=(data.reason or "").strip(),
            reviewer_id=reviewer.subject,
            reviewer_type=reviewer.actor_type,
            reviewed_at=moment,
            created_at=moment,
        )
        self._session.add(merge)

        # Atomic audit (SEC-001/BR-122): staged into the caller's session so it commits with the row.
        # References stay OPAQUE — ids, subject kind, the match outcome/confidence and the signal COUNT
        # — NEVER the evidence signal details (which can carry a company domain/state) or any raw value.
        AuditSink(self._session, business_id=self._business_id, actor=reviewer.as_actor()).record(
            action=AUDIT_ACTION_RECORD_MERGE,
            reason=f"recorded identity merge '{merge.id}' ({evidence.outcome})",
            references={
                "identityMergeId": merge.id,
                "subjectType": subject_type,
                "primarySubjectId": primary_id,
                "duplicateSubjectId": duplicate_id,
                "matchOutcome": evidence.outcome,
                "matchConfidence": evidence.confidence,
                "signalCount": len(evidence.signals),
            },
        )
        return merge
