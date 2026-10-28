"""DCA-084 — erase permitted DCA operational data (the scoped deletion executor).

Key: ``deletion-local`` · source sections 5, 10, 11.

The DCA-082 grievance intake only RECORDS a privacy request, and the DCA-083
:class:`~app.services.privacy_scope.PrivacyScopeResolver` only IDENTIFIES the scope a request reaches —
both execute nothing. This story is the first that actually DELETES: a single sanctioned executor that,
for ONE of a business's subjects (an organization or person observation), erases the DCA's OWN
operational data about that subject — its **observations**, its **evidence** and its **projections** —
and records a durable :class:`~app.db.models.DataErasureJournal` with justified tombstone references so
the erasure stays reviewable without re-exposing the personal data it removed.

What is in scope (DCA-owned OPERATIONAL data, per subject, within the one business):

- **observations** — the identity node itself (:class:`~app.db.models.OrgObservation` /
  :class:`~app.db.models.PersonObservation`), its external-id mapping
  (:class:`~app.db.models.OrgExternalId`, org only), its contact points
  (:class:`~app.db.models.ContactPoint`, person only), and every field observation about it
  (:class:`~app.db.models.FieldObservation`).
- **evidence** — every captured evidence object about the subject (:class:`~app.db.models.EvidenceObject`).
- **projections** — the tenant's own canonical correction overlay about the subject
  (:class:`~app.db.models.FieldCorrection`).

What is deliberately NOT erased (the "permitted" boundary): the records a privacy obligation requires
be RETAINED — the DCA-026 audit trail, DCA-074 consent receipts and the DCA-078 restriction ledgers —
and any platform-owned/control-plane data. Child person observations of an erased organization are
separate subjects (each erased by its OWN request), so the executor never cascades beyond the named
subject. Acting beyond this scope stays a disabled future capability.

**Authority first, and a denial changes nothing.** :meth:`OperationalDataEraser.erase_subject` takes the
executing principal and checks authority BEFORE anything is read or deleted, so a relevant denial is
pure and reproducible (it deletes nothing and writes no journal):

- it requires a business administrator whose membership roles grant
  :attr:`~app.identity.permissions.Scope.DATA_ERASE` (an ordinary acting user, a read-only viewer and a
  platform operator holding no membership all lack it); then
- the subject must resolve WITHIN this business (an unknown or cross-tenant subject reads as not-found),
  and a non-empty justification is required — each refusal is raised before the session is mutated.

**Idempotent, no unintended state.** The executor stages into the caller's session and performs no I/O
of its own (never commits/flushes/opens a transaction) — the caller owns the unit of work, so the
deletes, the journal row and the atomic DCA-026 audit entry all commit together. Because a fresh erase
requires the subject's identity node to still exist, a second erase of an already-erased subject reads
as not-found and changes nothing (idempotent — the operational data is gone and stays gone).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.sink import ActorType, AuditActor, AuditSink, assert_opaque_text
from app.db.models import (
    ContactPoint,
    DataErasureJournal,
    EvidenceObject,
    FieldCorrection,
    FieldObservation,
    FieldSubjectType,
    OrgExternalId,
    OrgObservation,
    PersonObservation,
)
from app.identity.permissions import Scope, scopes_for_business_roles

# The controlled subject vocabulary the executor accepts (mirrors FieldSubjectType).
SUBJECT_TYPES: tuple[str, ...] = tuple(t.value for t in FieldSubjectType)

# The audited action code for an executed erasure (a controlled code, like the other DCA writes).
AUDIT_ACTION_ERASE = "operational_data.erased"

# A default request-kind when the caller does not name one (an operator-initiated erasure).
DEFAULT_REQUEST_KIND = "operator_erasure"

# Stable refusal codes. Each is raised BEFORE the session is mutated, so a refusal deletes nothing.
REASON_NOT_AUTHORIZED = "data_erasure_not_authorized"
REASON_UNKNOWN_SUBJECT_TYPE = "unknown_subject_type"
REASON_SUBJECT_NOT_FOUND = "subject_not_found"
REASON_MISSING_JUSTIFICATION = "missing_justification"


class DataErasureError(Exception):
    """An erasure was refused with a stable ``code``. Raised before the session is mutated, so a
    refusal leaves every row and no partial state. ``detail`` names the offending value when useful
    (never a raw contact value / field value)."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class DataErasureAuthorizationError(DataErasureError):
    """The caller is not authorized to erase this subject's operational data.

    A distinct type so an HTTP boundary can map it to a 403 while the other codes map to a 404/422.
    Raised before anything is read or deleted. ``code`` is :data:`REASON_NOT_AUTHORIZED`.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


@dataclass(frozen=True)
class ErasurePrincipal:
    """The business principal executing an erasure.

    ``subject`` is an opaque actor id (never a name/email) recorded on the journal and its atomic audit
    entry. ``roles`` are the caller's business-plane membership roles (from the verified
    :class:`~app.identity.tenant_context.TenantContext`, NEVER a bare token claim). :meth:`may_erase`
    grants the action only when those roles yield :attr:`~app.identity.permissions.Scope.DATA_ERASE` —
    an ordinary acting user and a read-only viewer do not hold it (erasure is an administrator action),
    and a platform operator holds no business scope for the tenant.
    """

    subject: str
    roles: tuple[str, ...] = ()
    actor_type: str = "user"

    def may_erase(self) -> bool:
        return Scope.DATA_ERASE in scopes_for_business_roles(self.roles)

    def as_actor(self) -> AuditActor:
        return AuditActor(id=self.subject, type=ActorType(self.actor_type))


@dataclass
class ErasureResult:
    """What one erasure deleted and journaled, for an inspectable record (no personal data).

    ``tombstones`` is the opaque id ledger grouped by category/table; the ``*_deleted`` counts summarise
    it. ``journal_id`` is the id of the :class:`~app.db.models.DataErasureJournal` row staged (the
    justified tombstone reference that survives the erasure).
    """

    subject_type: str
    subject_id: str
    journal_id: str
    tombstones: dict = field(default_factory=dict)
    observations_deleted: int = 0
    evidence_deleted: int = 0
    projections_deleted: int = 0

    @property
    def total_deleted(self) -> int:
        return self.observations_deleted + self.evidence_deleted + self.projections_deleted


class OperationalDataEraser:
    """The single sanctioned executor that erases a subject's DCA-owned operational data (DCA-084).

    Bound to one verified business scope (a route resolves it from the membership-verified tenant
    context). Every query is confined to that business — there is no argument to widen the scope. Stages
    into the caller's session and performs no I/O of its own (never commits/flushes/opens a transaction).
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    def erase_subject(
        self,
        subject_type: str,
        subject_id: str,
        principal: ErasurePrincipal,
        *,
        justification: str,
        request_ref: str = "",
        request_kind: str = DEFAULT_REQUEST_KIND,
        now: datetime | None = None,
    ) -> ErasureResult:
        """Authorize, resolve, then erase the subject's operational data; return the (uncommitted) result.

        Authorization is checked FIRST: a caller whose roles lack
        :attr:`~app.identity.permissions.Scope.DATA_ERASE` is refused with
        :class:`DataErasureAuthorizationError` before any state is read or deleted (the relevant
        denial). Then the subject type is validated, the subject is resolved WITHIN this business (an
        unknown / cross-tenant subject is :data:`REASON_SUBJECT_NOT_FOUND`, deleting nothing), and a
        non-empty ``justification`` is required — each refusal is raised before the session is mutated.
        On success every observation / evidence / projection row for the subject is hard-deleted, a
        :class:`~app.db.models.DataErasureJournal` row (with the opaque tombstone references) and an
        atomic DCA-026 audit entry are staged in the SAME unit of work, and the result is returned
        uncommitted. The caller owns the transaction.
        """
        moment = now or datetime.now(UTC)

        # 1) Authority FIRST — an unauthorized caller is refused with no state touched.
        if not principal.may_erase():
            raise DataErasureAuthorizationError(
                REASON_NOT_AUTHORIZED,
                "erasing operational data requires a business administrator holding "
                f"'{Scope.DATA_ERASE.value}'; an ordinary user or a read-only viewer cannot perform "
                "it, and a platform operator holds no business scope for this tenant",
            )

        normalized_type = _normalize_subject_type(subject_type)
        sid = (subject_id or "").strip()
        if not sid or not self._subject_exists(normalized_type, sid):
            # Covers an unknown id, a subject owned by another business, AND an already-erased subject
            # (both read as not-found) — an erasure can never reach an identity outside this business,
            # and a re-run of an already-erased subject changes nothing.
            raise DataErasureError(
                REASON_SUBJECT_NOT_FOUND,
                f"{normalized_type} subject '{subject_id}' does not exist in this business",
                detail=sid,
            )

        justification_text = (justification or "").strip()
        if not justification_text:
            raise DataErasureError(
                REASON_MISSING_JUSTIFICATION,
                "an erasure must carry a non-empty justification (it must record WHY)",
            )
        # Screen the justification like an audit reference: it is persisted on the journal and must not
        # carry a raw contact value / secret. A refusal here is raised before any delete.
        assert_opaque_text("justification", justification_text)

        # 2) Collect the OPAQUE row ids to erase, grouped by category — before deleting, so the journal
        # can record exactly what was removed (the justified tombstone references).
        observation_tombstones = self._collect_observations(normalized_type, sid)
        evidence_ids = self._collect_ids(
            EvidenceObject,
            select(EvidenceObject.id)
            .where(EvidenceObject.business_id == self._business_id)
            .where(EvidenceObject.subject_type == normalized_type)
            .where(EvidenceObject.subject_id == sid),
        )
        projection_ids = self._collect_ids(
            FieldCorrection,
            select(FieldCorrection.id)
            .where(FieldCorrection.business_id == self._business_id)
            .where(FieldCorrection.subject_type == normalized_type)
            .where(FieldCorrection.subject_id == sid),
        )

        observations_deleted = sum(len(ids) for ids in observation_tombstones.values())
        tombstones = {
            "observations": observation_tombstones,
            "evidence": evidence_ids,
            "projections": projection_ids,
        }

        # 3) Execute the deletions (hard delete — the operational data must no longer be accessible).
        self._delete_observations(normalized_type, sid, observation_tombstones)
        self._delete_by_ids(EvidenceObject, evidence_ids)
        self._delete_by_ids(FieldCorrection, projection_ids)

        # 4) Stage the execution journal (the justified tombstone reference that survives the erasure).
        journal_id = str(uuid.uuid4())
        journal = DataErasureJournal(
            id=journal_id,
            business_id=self._business_id,
            subject_type=normalized_type,
            subject_id=sid,
            request_kind=(request_kind or DEFAULT_REQUEST_KIND).strip(),
            request_ref=(request_ref or "").strip(),
            justification=justification_text,
            authority_plane="business",
            authority_scope=Scope.DATA_ERASE.value,
            executed_by=principal.subject,
            executed_by_type=principal.actor_type,
            observations_deleted=observations_deleted,
            evidence_deleted=len(evidence_ids),
            projections_deleted=len(projection_ids),
            total_deleted=observations_deleted + len(evidence_ids) + len(projection_ids),
            tombstones=tombstones,
            created_at=moment,
        )
        self._session.add(journal)

        # 5) Atomic audit (SEC-001/BR-122): staged into the caller's session so it commits with the
        # deletes + journal. References stay OPAQUE — subject ids, counts, journal id, request ref — and
        # the audit retains only this permitted minimal metadata, never a contact value or field value.
        AuditSink(self._session, business_id=self._business_id, actor=principal.as_actor()).record(
            action=AUDIT_ACTION_ERASE,
            reason=(
                f"erased operational data for {normalized_type} subject (journal '{journal_id}'): "
                f"{observations_deleted} observation(s), {len(evidence_ids)} evidence, "
                f"{len(projection_ids)} projection(s)"
            ),
            references={
                "erasureJournalId": journal_id,
                "subjectType": normalized_type,
                "subjectId": sid,
                "requestKind": journal.request_kind,
                "requestRef": journal.request_ref,
                "authorityPlane": "business",
                "authorityScope": Scope.DATA_ERASE.value,
                "observationsDeleted": observations_deleted,
                "evidenceDeleted": len(evidence_ids),
                "projectionsDeleted": len(projection_ids),
                "totalDeleted": journal.total_deleted,
            },
        )

        return ErasureResult(
            subject_type=normalized_type,
            subject_id=sid,
            journal_id=journal_id,
            tombstones=tombstones,
            observations_deleted=observations_deleted,
            evidence_deleted=len(evidence_ids),
            projections_deleted=len(projection_ids),
        )

    # -- internals ----------------------------------------------------------------------------------

    def _subject_exists(self, subject_type: str, subject_id: str) -> bool:
        """True if the subject's identity node exists WITHIN this business (never cross-tenant)."""
        if subject_type == FieldSubjectType.ORGANIZATION.value:
            stmt = (
                select(OrgObservation.id)
                .where(OrgObservation.business_id == self._business_id)
                .where(OrgObservation.id == subject_id)
                .limit(1)
            )
        else:  # person
            stmt = (
                select(PersonObservation.id)
                .where(PersonObservation.business_id == self._business_id)
                .where(PersonObservation.id == subject_id)
                .limit(1)
            )
        return self._session.scalars(stmt).first() is not None

    def _collect_ids(self, model, stmt) -> list[str]:
        """The opaque row ids a scoped select resolves to, as a list (the tombstone references)."""
        return [str(row) for row in self._session.scalars(stmt).all()]

    def _collect_observations(self, subject_type: str, subject_id: str) -> dict[str, list[str]]:
        """The opaque ids of every OBSERVATION row for the subject, grouped by table.

        Always includes the subject's field observations and the identity node itself; adds the
        person's contact points (person subject) or the org's external-id mappings (org subject).
        """
        tombstones: dict[str, list[str]] = {}
        tombstones["field_observations"] = self._collect_ids(
            FieldObservation,
            select(FieldObservation.id)
            .where(FieldObservation.business_id == self._business_id)
            .where(FieldObservation.subject_type == subject_type)
            .where(FieldObservation.subject_id == subject_id),
        )
        if subject_type == FieldSubjectType.ORGANIZATION.value:
            tombstones["org_external_ids"] = self._collect_ids(
                OrgExternalId,
                select(OrgExternalId.id)
                .where(OrgExternalId.business_id == self._business_id)
                .where(OrgExternalId.observation_id == subject_id),
            )
            tombstones["org_observations"] = [subject_id]
        else:  # person
            tombstones["contact_points"] = self._collect_ids(
                ContactPoint,
                select(ContactPoint.id)
                .where(ContactPoint.business_id == self._business_id)
                .where(ContactPoint.person_observation_id == subject_id),
            )
            tombstones["person_observations"] = [subject_id]
        return tombstones

    def _delete_by_ids(self, model, ids: list[str]) -> None:
        """Delete the given rows of ``model`` (scoped to this business) through the session, so each
        deletion is subject to the DCA-018 RLS WITH-CHECK beneath it (a foreign owner can never be
        reached). Loading-then-deleting keeps the ORM unit of work consistent for the caller's commit."""
        if not ids:
            return
        rows = self._session.scalars(
            select(model).where(model.business_id == self._business_id).where(model.id.in_(ids))
        ).all()
        for row in rows:
            self._session.delete(row)

    def _delete_observations(
        self, subject_type: str, subject_id: str, tombstones: dict[str, list[str]]
    ) -> None:
        """Delete every collected observation row (field observations, contact points / external ids,
        and the identity node) for the subject, within this business."""
        self._delete_by_ids(FieldObservation, tombstones.get("field_observations", []))
        if subject_type == FieldSubjectType.ORGANIZATION.value:
            self._delete_by_ids(OrgExternalId, tombstones.get("org_external_ids", []))
            self._delete_by_ids(OrgObservation, [subject_id])
        else:  # person
            self._delete_by_ids(ContactPoint, tombstones.get("contact_points", []))
            self._delete_by_ids(PersonObservation, [subject_id])


def _normalize_subject_type(value: str) -> str:
    token = (value or "").strip().lower()
    try:
        return FieldSubjectType(token).value
    except ValueError as exc:
        raise DataErasureError(
            REASON_UNKNOWN_SUBJECT_TYPE,
            f"unknown subject_type '{value}'; expected one of: {', '.join(SUBJECT_TYPES)}",
            detail=value,
        ) from exc
