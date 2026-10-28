"""DCA-070 — the authorized, versioned user field-correction store.

:class:`FieldCorrectionStore` is the single sanctioned writer of
:class:`~app.db.models.FieldCorrection`. It lets an AUTHORIZED business user correct the value of ONE
field about one of that business's subjects (an :class:`~app.db.models.OrgObservation` or
:class:`~app.db.models.PersonObservation`), recording the correction as a versioned, business-private
override that sits on top of the DCA-061 provider-observation history and the DCA-069 canonical
selection.

The load-bearing guarantees (the DCA-070 acceptance criteria):

- **A refresh does not overwrite a protected correction.** A correction is a SEPARATE record, never an
  edit of an observation: recording a newer provider observation stages a new
  :class:`~app.db.models.FieldObservation` and never touches a correction. The DCA-069 overlay
  (:mod:`app.canonical.correction_overlay`) keeps the protected correction canonical over the fresher
  provider value. Protection is the DEFAULT (``protected_from_refresh=True``) — a correction is
  protected unless the user explicitly records an unprotected version.

- **A correction stays business-private.** The store is bound to one verified business scope; the
  subject is resolved WITHIN this business (an unknown or cross-tenant subject reads as not-found), and
  every read/write is confined to the business (DCA-017 scoping + DCA-018 RLS beneath it). A different
  business has no correction for the same subject/field, so its view shows the provider value.

- **Authorized + attributed.** A correction may only be recorded by a business user whose membership
  roles grant :attr:`~app.identity.permissions.Scope.FIELD_CORRECT`. Authorization is checked FIRST, so
  an unauthorized attempt (a read-only viewer, a non-member, a platform operator) is refused BEFORE the
  session is mutated and stages no row — the "relevant denial". ``actor`` records WHO corrected the
  field (opaque id + kind), on the atomic DCA-026 audit entry in the SAME unit of work as the row.

**Versioned, insert-only history.** Each correction of a (subject, field_key) inserts a NEW version at
``max(version) + 1`` for that field; prior versions are preserved and never mutated/deleted. The ACTIVE
correction for a field is its highest version.

The store stages into the *caller's* session and never commits — the caller owns the unit of work, so
the correction row and its atomic audit entry commit together. References on the audit entry stay
OPAQUE (ids / field key / version / protection flag); the raw ``corrected_value`` is treated as
potentially personal/business data and is NEVER audited. Recording a correction creates no prospect /
host record and promotes nothing to any shared/canonical table; acting on the canonical value stays a
disabled future capability.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.sink import AuditActor, AuditSink
from app.db.models import (
    FieldCorrection,
    FieldSubjectType,
    OrgObservation,
    PersonObservation,
)
from app.identity.permissions import Scope, scopes_for_business_roles

# The controlled vocabulary of subject kinds a correction can be about.
SUBJECT_TYPES: tuple[str, ...] = tuple(t.value for t in FieldSubjectType)

# The audited action code for recording a field correction (a controlled code, like the other writes).
AUDIT_ACTION_RECORD_CORRECTION = "field.correction.record"

# Stable refusal codes. Each is raised BEFORE the session is mutated, so a refusal stages no row.
REASON_NOT_AUTHORIZED = "field_correction_not_authorized"
REASON_UNKNOWN_SUBJECT_TYPE = "unknown_subject_type"
REASON_SUBJECT_NOT_FOUND = "subject_not_found"
REASON_MISSING_FIELD_KEY = "missing_field_key"
REASON_MISSING_CORRECTED_VALUE = "missing_corrected_value"


class FieldCorrectionError(Exception):
    """A field-correction create was refused with a stable code. Raised before the session is mutated,
    so a refusal leaves no row and no partial state. ``detail`` names the offending value when useful
    (never a raw corrected value)."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class FieldCorrectionAuthorizationError(FieldCorrectionError):
    """The caller's roles do not grant :attr:`~app.identity.permissions.Scope.FIELD_CORRECT`.

    A distinct type so the HTTP boundary (where one exists) can map it to a 403 while the other
    :class:`FieldCorrectionError` codes map to a 404/422. Raised before anything is read or written.
    """

    def __init__(self, message: str) -> None:
        super().__init__(REASON_NOT_AUTHORIZED, message)


@dataclass(frozen=True)
class FieldCorrector:
    """The business principal correcting a field.

    ``subject`` is an opaque actor id (never a name/email) recorded on the correction and its atomic
    audit entry. ``roles`` are the caller's business-plane membership roles (from the verified
    :class:`~app.identity.tenant_context.TenantContext`, NEVER a bare token claim). :meth:`may_correct`
    grants the action only when those roles yield
    :attr:`~app.identity.permissions.Scope.FIELD_CORRECT` — a read-only viewer's roles do not, and a
    platform operator holds no business scope for the tenant.
    """

    subject: str
    roles: tuple[str, ...] = ()
    actor_type: str = "user"

    def may_correct(self) -> bool:
        return Scope.FIELD_CORRECT in scopes_for_business_roles(self.roles)

    def as_actor(self) -> AuditActor:
        from app.audit.sink import ActorType

        return AuditActor(id=self.subject, type=ActorType(self.actor_type))


@dataclass
class FieldCorrectionInput:
    """One field correction as supplied by the caller.

    ``subject_type`` / ``subject_id`` name the subject the field is about (an organization or person
    observation in THIS business). ``field_key`` is the corrected field; ``corrected_value`` the user's
    authoritative value. ``protected_from_refresh`` defaults to ``True`` (default refresh protection) —
    pass ``False`` only to deliberately let a later provider refresh apply again. ``reason`` is a short
    opaque justification (optional).
    """

    subject_type: str
    subject_id: str
    field_key: str
    corrected_value: str
    protected_from_refresh: bool = True
    reason: str = ""


def _normalize_subject_type(value: str) -> str:
    token = (value or "").strip().lower()
    try:
        return FieldSubjectType(token).value
    except ValueError as exc:
        raise FieldCorrectionError(
            REASON_UNKNOWN_SUBJECT_TYPE,
            f"unknown subject_type '{value}'; expected one of: {', '.join(SUBJECT_TYPES)}",
            detail=value,
        ) from exc


class FieldCorrectionStore:
    """Tenant-scoped reader/writer for versioned field corrections, bound to one business.

    Construct with the active session and the verified business scope (a route resolves it from the
    membership-verified tenant context). Every query is confined to that business — there is no
    argument to widen the scope.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def get(self, correction_id: str) -> FieldCorrection | None:
        """Fetch one correction by id WITHIN this business. Another business's id (or an unknown id)
        resolves to ``None`` — a cross-tenant id reads exactly like not-found."""
        if not correction_id:
            return None
        stmt = (
            select(FieldCorrection)
            .where(FieldCorrection.business_id == self._business_id)
            .where(FieldCorrection.id == correction_id)
        )
        return self._session.scalars(stmt).one_or_none()

    def history_for_field(
        self, subject_type: str, subject_id: str, field_key: str
    ) -> list[FieldCorrection]:
        """Every correction version for one (subject, field) in this business, oldest first."""
        normalized_type = _normalize_subject_type(subject_type)
        key = (field_key or "").strip().lower()
        stmt = (
            select(FieldCorrection)
            .where(FieldCorrection.business_id == self._business_id)
            .where(FieldCorrection.subject_type == normalized_type)
            .where(FieldCorrection.subject_id == subject_id)
            .where(FieldCorrection.field_key == key)
            .order_by(FieldCorrection.version)
        )
        return list(self._session.scalars(stmt).all())

    def active_for_field(
        self, subject_type: str, subject_id: str, field_key: str
    ) -> FieldCorrection | None:
        """The ACTIVE (highest-version) correction for one (subject, field) in this business, or
        ``None`` when the business has never corrected that field. This is what the DCA-069 overlay
        consults; a different business reads ``None`` here (its correction is business-private)."""
        normalized_type = _normalize_subject_type(subject_type)
        key = (field_key or "").strip().lower()
        stmt = (
            select(FieldCorrection)
            .where(FieldCorrection.business_id == self._business_id)
            .where(FieldCorrection.subject_type == normalized_type)
            .where(FieldCorrection.subject_id == subject_id)
            .where(FieldCorrection.field_key == key)
            .order_by(FieldCorrection.version.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).one_or_none()

    def active_for_subject(
        self, subject_type: str, subject_id: str
    ) -> dict[str, FieldCorrection]:
        """The active correction for EACH corrected field of one subject, keyed by ``field_key``.

        A convenience the overlay uses to apply corrections across a subject's whole field set in one
        read. Only the highest version of each field is returned; losing versions stay in history.
        """
        normalized_type = _normalize_subject_type(subject_type)
        stmt = (
            select(FieldCorrection)
            .where(FieldCorrection.business_id == self._business_id)
            .where(FieldCorrection.subject_type == normalized_type)
            .where(FieldCorrection.subject_id == subject_id)
            .order_by(FieldCorrection.field_key, FieldCorrection.version)
        )
        latest: dict[str, FieldCorrection] = {}
        for row in self._session.scalars(stmt).all():
            latest[row.field_key] = row  # ascending version => last write per key wins
        return latest

    # -- the one write path -------------------------------------------------------------------------

    def _next_version(self, subject_type: str, subject_id: str, field_key: str) -> int:
        current = self._session.scalar(
            select(func.max(FieldCorrection.version))
            .where(FieldCorrection.business_id == self._business_id)
            .where(FieldCorrection.subject_type == subject_type)
            .where(FieldCorrection.subject_id == subject_id)
            .where(FieldCorrection.field_key == field_key)
        )
        return int(current or 0) + 1

    def record(
        self,
        data: FieldCorrectionInput,
        corrector: FieldCorrector,
        *,
        now: datetime | None = None,
    ) -> FieldCorrection:
        """Authorize, validate and stage a NEW correction version; return the (uncommitted) row.

        Authorization is checked FIRST: a caller whose roles lack
        :attr:`~app.identity.permissions.Scope.FIELD_CORRECT` is refused with
        :class:`FieldCorrectionAuthorizationError` before any state is read or written (the relevant
        denial). Then validation runs before the session is mutated — an unknown subject type, a
        subject that does not exist in this business, an empty field key or corrected value each raise
        with a stable code and stage NO row. On success a new version is inserted at ``max(version)+1``
        for the (subject, field), preserving every prior version, plus an atomic DCA-026 audit entry
        (opaque references only — never the raw corrected value) in the SAME unit of work. The caller
        owns the transaction.
        """
        moment = now or datetime.now(UTC)

        # 1) Authority FIRST — an unauthorized caller is refused with no state touched.
        if not corrector.may_correct():
            raise FieldCorrectionAuthorizationError(
                "correcting a field requires a business user holding "
                f"'{Scope.FIELD_CORRECT.value}'; a read-only viewer or a non-member cannot perform it"
            )

        subject_type = _normalize_subject_type(data.subject_type)
        subject_id = (data.subject_id or "").strip()
        if not subject_id or not self._subject_exists(subject_type, subject_id):
            # Covers an unknown id AND a subject owned by another business (both read as not-found):
            # a correction can never be attached to an identity outside this business.
            raise FieldCorrectionError(
                REASON_SUBJECT_NOT_FOUND,
                f"{subject_type} subject '{subject_id}' does not exist in this business",
                detail=subject_id,
            )

        field_key = (data.field_key or "").strip().lower()
        if not field_key:
            raise FieldCorrectionError(
                REASON_MISSING_FIELD_KEY, "a field_key is required for a correction"
            )
        corrected_value = (data.corrected_value or "").strip()
        if not corrected_value:
            raise FieldCorrectionError(
                REASON_MISSING_CORRECTED_VALUE,
                f"field '{field_key}' has no corrected value",
                detail=field_key,
            )

        version = self._next_version(subject_type, subject_id, field_key)
        correction = FieldCorrection(
            id=str(uuid.uuid4()),
            business_id=self._business_id,
            subject_type=subject_type,
            subject_id=subject_id,
            field_key=field_key,
            corrected_value=corrected_value,
            version=version,
            protected_from_refresh=bool(data.protected_from_refresh),
            reason=(data.reason or "").strip(),
            actor_id=corrector.subject,
            actor_type=corrector.actor_type,
            corrected_at=moment,
            created_at=moment,
        )
        self._session.add(correction)

        # Atomic audit (SEC-001/BR-122): staged into the caller's session so it commits with the row.
        # References stay OPAQUE — ids, the field key, version and the protection flag — NEVER the raw
        # corrected_value (potentially personal/business data, screened out of the trail).
        AuditSink(self._session, business_id=self._business_id, actor=corrector.as_actor()).record(
            action=AUDIT_ACTION_RECORD_CORRECTION,
            reason=f"recorded field correction '{correction.id}' (version {version})",
            references={
                "fieldCorrectionId": correction.id,
                "subjectType": subject_type,
                "subjectId": subject_id,
                "fieldKey": field_key,
                "version": version,
                "protectedFromRefresh": bool(data.protected_from_refresh),
            },
        )
        return correction

    # -- internals ----------------------------------------------------------------------------------

    def _subject_exists(self, subject_type: str, subject_id: str) -> bool:
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
