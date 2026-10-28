"""DCA-061 — persist observed fields with REQUIRED provenance.

:class:`FieldObservationStore` is the single sanctioned writer of
:class:`~app.db.models.FieldObservation`. It lets ONE business record a single observed field
(``field_key`` → ``field_value``) about a subject it is looking at — one of that business's
:class:`~app.db.models.OrgObservation` or :class:`~app.db.models.PersonObservation` rows — and it
REQUIRES the field to carry provenance before it is persisted, so a bare agent-written value with no
source reference is rejected rather than silently canonicalized into the business's field set.

The load-bearing guarantee (the DCA-061 acceptance criterion):

- **An agent-written field missing required provenance is rejected, never silently canonicalized.**
  Every observation must declare the source/version it came from (for a directly OBSERVED field),
  the ACTUAL retrieval/derivation reference, the observed timestamp, the collection scope, its
  inference label (observed vs inferred), and the rights basis. A create that omits any required
  piece is refused with a stable code BEFORE the session is mutated — it stages no row and the value
  never enters the canonical field set. A directly observed field that names no source is the central
  refusal (:data:`REASON_MISSING_SOURCE_REFERENCE`).

The supporting guarantees:

- **Provenance points at a real, reviewable source.** When a ``source_key`` is supplied it must name
  a catalogued :class:`~app.db.models.Source` (DCA-044) and the ``source_version`` must name a real
  :class:`~app.db.models.SourceTerms` version (DCA-045) of it — so an observed field is grounded in a
  source that can be reviewed, not an arbitrary string. (This records provenance only; it does NOT
  assert the rights are in force — enforcement of source rights lives in its own gated path.)

- **Scoped, never globally visible.** The store is bound to one verified business scope; the subject
  is resolved WITHIN this business (an unknown or cross-tenant subject reads as not-found), and every
  read/write is confined to the business (DCA-017 scoping + DCA-018 RLS beneath it).

- **A denial changes nothing + no host record.** Every validation runs BEFORE the session is mutated,
  so a refusal stages no field-observation row. Recording a field creates no prospect / host record
  and promotes nothing to any shared/canonical table; acquisition stays a disabled future capability.

Each successful create stages an atomic DCA-026 audit entry in the SAME unit of work as the row
(SEC-001/BR-122 discipline); the store never commits — the caller owns the transaction. References on
the audit entry are kept OPAQUE (ids / field key / source key+version / scope / label) — the raw
``field_value`` is treated as potentially personal/business data and is NEVER audited.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.sink import AuditActor, AuditSink
from app.db.models import (
    FieldInferenceLabel,
    FieldObservation,
    FieldSubjectType,
    OrgObservation,
    PersonObservation,
    Source,
    SourceTerms,
)
from app.geography.india import normalize_state_ut

# The controlled vocabularies this store validates against.
SUBJECT_TYPES: tuple[str, ...] = tuple(t.value for t in FieldSubjectType)
INFERENCE_LABELS: tuple[str, ...] = tuple(label.value for label in FieldInferenceLabel)

# The audited action code for recording a field observation.
AUDIT_ACTION_RECORD_FIELD = "field.observation.record"

# The scope tokens that mean "all of India" (India-only service); anything else must be a state/UT.
_NATIONWIDE_TOKENS = frozenset({"nationwide", "national", "india", "in"})
SCOPE_NATIONWIDE = "nationwide"

# Validation refusals — each raised BEFORE the session is mutated, so a refusal stages nothing.
REASON_UNKNOWN_SUBJECT_TYPE = "unknown_subject_type"
REASON_SUBJECT_NOT_FOUND = "subject_not_found"
REASON_MISSING_FIELD_KEY = "missing_field_key"
REASON_MISSING_FIELD_VALUE = "missing_field_value"
REASON_UNKNOWN_INFERENCE_LABEL = "unknown_inference_label"
# The central refusal — a directly observed field that names no source/version.
REASON_MISSING_SOURCE_REFERENCE = "missing_source_reference"
REASON_SOURCE_NOT_FOUND = "source_not_found"
REASON_SOURCE_VERSION_NOT_FOUND = "source_version_not_found"
REASON_MISSING_PROVENANCE_REF = "missing_provenance_ref"
REASON_MISSING_OBSERVED_AT = "missing_observed_at"
REASON_MISSING_SCOPE = "missing_scope"
REASON_INVALID_SCOPE = "invalid_scope"
REASON_MISSING_RIGHTS_BASIS = "missing_rights_basis"


class FieldObservationError(Exception):
    """A field-observation create was refused with a stable code. Raised before the session is
    mutated, so a refusal leaves no row and no partial state. ``detail`` names the offending value
    when useful (never a raw field value)."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass
class FieldObservationInput:
    """One observed field as supplied by the caller, with its provenance.

    ``subject_type`` / ``subject_id`` name the subject the field is about (an organization or person
    observation in THIS business). ``field_key`` / ``field_value`` are the observed field and its
    value. ``inference_label`` is ``observed`` or ``inferred``. ``source_key`` / ``source_version``
    name the catalogued source and terms version the field came from — REQUIRED for an ``observed``
    field (the central provenance requirement). ``provenance_ref`` is the ACTUAL retrieval reference
    (observed) or derivation basis (inferred). ``observed_at`` is when it was observed/inferred.
    ``scope`` is the India-only collection scope (``nationwide`` or a recognized state/UT). The
    rights/retention metadata is ``rights_basis`` (required) + optional ``retention_until``.
    """

    subject_type: str
    subject_id: str
    field_key: str
    field_value: str
    inference_label: str
    provenance_ref: str
    observed_at: datetime
    scope: str
    rights_basis: str
    source_key: str = ""
    source_version: int | None = None
    retention_until: datetime | None = None


def _normalize_subject_type(value: str) -> str:
    token = (value or "").strip().lower()
    try:
        return FieldSubjectType(token).value
    except ValueError as exc:
        raise FieldObservationError(
            REASON_UNKNOWN_SUBJECT_TYPE,
            f"unknown subject_type '{value}'; expected one of: {', '.join(SUBJECT_TYPES)}",
            detail=value,
        ) from exc


def _normalize_label(value: str) -> str:
    token = (value or "").strip().lower()
    try:
        return FieldInferenceLabel(token).value
    except ValueError as exc:
        raise FieldObservationError(
            REASON_UNKNOWN_INFERENCE_LABEL,
            f"unknown inference_label '{value}'; expected one of: {', '.join(INFERENCE_LABELS)}",
            detail=value,
        ) from exc


def _normalize_scope(value: str) -> str:
    """Normalize the India-only collection scope. ``nationwide`` (and its synonyms) collapse to
    ``nationwide``; anything else must be a recognized Indian state/UT code — a missing or foreign
    scope is refused (the service is India-only), so a field can never carry an empty/unknown scope."""
    token = (value or "").strip()
    if not token:
        raise FieldObservationError(REASON_MISSING_SCOPE, "a collection scope is required")
    if token.lower() in _NATIONWIDE_TOKENS:
        return SCOPE_NATIONWIDE
    resolved = normalize_state_ut(token)
    if resolved is None:
        raise FieldObservationError(
            REASON_INVALID_SCOPE,
            f"'{value}' is not 'nationwide' nor a recognized Indian state/UT code",
            detail=value,
        )
    return resolved


class FieldObservationStore:
    """Tenant-scoped reader/writer for field observations, bound to one business.

    Construct with the active session and the verified business scope (a route resolves it from the
    membership-verified tenant context). Every query is confined to that business — there is no
    argument to widen the scope.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def get(self, observation_id: str) -> FieldObservation | None:
        """Fetch one field observation by id WITHIN this business. Another business's id (or an
        unknown id) resolves to ``None`` — a cross-tenant id reads exactly like not-found."""
        if not observation_id:
            return None
        stmt = (
            select(FieldObservation)
            .where(FieldObservation.business_id == self._business_id)
            .where(FieldObservation.id == observation_id)
        )
        return self._session.scalars(stmt).one_or_none()

    def observations(self) -> list[FieldObservation]:
        """Every field observation for this business, oldest first."""
        stmt = (
            select(FieldObservation)
            .where(FieldObservation.business_id == self._business_id)
            .order_by(FieldObservation.created_at, FieldObservation.id)
        )
        return list(self._session.scalars(stmt).all())

    def for_subject(self, subject_type: str, subject_id: str) -> list[FieldObservation]:
        """The field observations recorded about one subject in this business (append-only history)."""
        normalized_type = _normalize_subject_type(subject_type)
        stmt = (
            select(FieldObservation)
            .where(FieldObservation.business_id == self._business_id)
            .where(FieldObservation.subject_type == normalized_type)
            .where(FieldObservation.subject_id == subject_id)
            .order_by(FieldObservation.field_key, FieldObservation.observed_at)
        )
        return list(self._session.scalars(stmt).all())

    # -- the one write path -------------------------------------------------------------------------

    def record(
        self,
        data: FieldObservationInput,
        actor: AuditActor,
        *,
        now: datetime | None = None,
    ) -> FieldObservation:
        """Validate the provenance and stage ONE field observation; return the (uncommitted) row. The
        caller owns the unit of work, so the row and its atomic audit entry commit together.

        Every check runs before the session is mutated. A field is refused — and stages NO row — when
        it is missing any required provenance: an unknown subject type, a subject that does not exist
        in this business, an empty field key or value, an unknown inference label, a directly OBSERVED
        field that names no source/version (:data:`REASON_MISSING_SOURCE_REFERENCE` — the central
        refusal), a ``source_key`` that is not catalogued or a ``source_version`` that does not exist,
        a missing retrieval/derivation reference, a missing observed timestamp, a missing/invalid
        collection scope, or a missing rights basis. A refused agent-written value is NEVER silently
        canonicalized. No prospect / host record is created; this records a provenanced field only.
        """
        moment = now or datetime.now(UTC)

        subject_type = _normalize_subject_type(data.subject_type)
        subject_id = (data.subject_id or "").strip()
        if not subject_id or not self._subject_exists(subject_type, subject_id):
            # Covers an unknown id AND a subject owned by another business (both read as not-found):
            # a field can never be attached to an identity outside this business.
            raise FieldObservationError(
                REASON_SUBJECT_NOT_FOUND,
                f"{subject_type} subject '{subject_id}' does not exist in this business",
                detail=subject_id,
            )

        field_key = (data.field_key or "").strip().lower()
        if not field_key:
            raise FieldObservationError(
                REASON_MISSING_FIELD_KEY, "a field_key is required for a field observation"
            )
        field_value = (data.field_value or "").strip()
        if not field_value:
            raise FieldObservationError(
                REASON_MISSING_FIELD_VALUE,
                f"field '{field_key}' has no value",
                detail=field_key,
            )

        label = _normalize_label(data.inference_label)

        # Source/version provenance. Required for a directly OBSERVED field (the central requirement);
        # optional for an INFERRED one (it is derived, not fetched), but if a source is named it must
        # resolve to a real catalogued source + terms version.
        source_key = (data.source_key or "").strip()
        source_version = data.source_version
        if label == FieldInferenceLabel.OBSERVED.value and (
            not source_key or source_version is None
        ):
            raise FieldObservationError(
                REASON_MISSING_SOURCE_REFERENCE,
                "an observed field must name the source_key and source_version it was observed from",
                detail=field_key,
            )
        if source_key:
            if not self._source_exists(source_key):
                raise FieldObservationError(
                    REASON_SOURCE_NOT_FOUND,
                    f"no catalogued source with key '{source_key}' exists",
                    detail=source_key,
                )
            if source_version is None or not self._source_version_exists(
                source_key, source_version
            ):
                raise FieldObservationError(
                    REASON_SOURCE_VERSION_NOT_FOUND,
                    f"source '{source_key}' has no terms version {source_version}",
                    detail=source_key,
                )
        else:
            # An inferred field with no originating source carries no version either.
            source_version = None

        provenance_ref = (data.provenance_ref or "").strip()
        if not provenance_ref:
            raise FieldObservationError(
                REASON_MISSING_PROVENANCE_REF,
                "a provenance_ref (the actual retrieval reference / derivation basis) is required",
                detail=field_key,
            )

        if data.observed_at is None:
            raise FieldObservationError(
                REASON_MISSING_OBSERVED_AT, "an observed_at timestamp is required"
            )

        scope = _normalize_scope(data.scope)

        rights_basis = (data.rights_basis or "").strip()
        if not rights_basis:
            raise FieldObservationError(
                REASON_MISSING_RIGHTS_BASIS,
                "a rights_basis is required (the recorded rights/retention basis for the field)",
                detail=field_key,
            )

        # All provenance present — stage the observation and an atomic audit entry.
        observation_id = str(uuid.uuid4())
        observation = FieldObservation(
            id=observation_id,
            business_id=self._business_id,
            subject_type=subject_type,
            subject_id=subject_id,
            field_key=field_key,
            field_value=field_value,
            inference_label=label,
            source_key=source_key,
            source_version=source_version,
            provenance_ref=provenance_ref,
            observed_at=data.observed_at,
            scope=scope,
            rights_basis=rights_basis,
            retention_until=data.retention_until,
            created_at=moment,
        )
        self._session.add(observation)

        # Atomic audit (SEC-001/BR-122): staged into the caller's session so it commits with the row.
        # References stay OPAQUE — ids, the field key, source key+version, scope and label — NEVER the
        # raw field_value (which may be personal/business data and is screened out of the trail).
        AuditSink(self._session, business_id=self._business_id, actor=actor).record(
            action=AUDIT_ACTION_RECORD_FIELD,
            reason=f"recorded {label} field observation '{observation_id}'",
            references={
                "fieldObservationId": observation_id,
                "subjectType": subject_type,
                "subjectId": subject_id,
                "fieldKey": field_key,
                "inferenceLabel": label,
                "sourceKey": source_key,
                "sourceVersion": source_version,
                "scope": scope,
                "hasRetentionUntil": data.retention_until is not None,
            },
        )
        return observation

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

    def _source_exists(self, source_key: str) -> bool:
        # The source catalog is platform control-plane data (not tenant-scoped); provenance may point
        # at any catalogued source — this only verifies the reference is real, not that rights are held.
        stmt = select(Source.id).where(Source.key == source_key).limit(1)
        return self._session.scalars(stmt).first() is not None

    def _source_version_exists(self, source_key: str, version: int) -> bool:
        stmt = (
            select(SourceTerms.id)
            .where(SourceTerms.source_key == source_key)
            .where(SourceTerms.version == version)
            .limit(1)
        )
        return self._session.scalars(stmt).first() is not None
