"""DCA-059 — store DCA organization observations (scoped identities + external platform-ID mappings).

:class:`OrgObservationStore` is the single sanctioned writer of
:class:`~app.db.models.OrgObservation` and its separate :class:`~app.db.models.OrgExternalId` mapping
rows. It lets ONE business record a scoped organization-observation identity — a LEGAL_ENTITY or one
of its ESTABLISHMENTs — and attach the external register identifiers (CIN / PAN / GSTIN / …) that
point at it, without creating any prospect / host record (acquisition stays a disabled future
capability).

The load-bearing guarantees (the DCA-059 acceptance criteria):

- **Identities support legal-entity vs establishment distinctions.** An ESTABLISHMENT must name a
  parent that exists in THIS business and is itself a LEGAL_ENTITY; a LEGAL_ENTITY must not name a
  parent; an establishment can never parent another observation. So one legal entity can carry many
  distinct establishments (in India, typically one GSTIN per state) and the two are never conflated.

- **Identities do not imply global visibility.** The store is bound to one verified business scope and
  every read/write is confined to it (DCA-017 scoping + DCA-018 RLS beneath it). A parent id, or an
  external-id lookup, that belongs to another business resolves to not-found here — never a
  cross-tenant link or read. The SAME external id (e.g. a GSTIN) can be recorded by two businesses
  against their own observations and neither constrains the other: the mapping is per-business, not a
  global key.

- **A denial changes nothing.** Every validation runs BEFORE the session is mutated, so a refused
  create (unknown type, wrong parent shape, invalid state code, unknown/empty external scheme or a
  duplicate external id within the business) stages no observation row and no mapping row.

Each successful create stages an atomic DCA-026 audit entry in the SAME unit of work as the rows
(SEC-001/BR-122 discipline); the store never commits — the caller owns the transaction. References on
the audit entry are kept opaque (ids / type / scheme codes / counts); raw external-id VALUES are never
audited.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.sink import AuditActor, AuditSink
from app.db.models import OrgExternalId, OrgExternalScheme, OrgObservation, OrgObservationType
from app.geography.india import normalize_state_ut

# The controlled vocabularies this store validates against.
ENTITY_TYPES: tuple[str, ...] = tuple(t.value for t in OrgObservationType)
EXTERNAL_SCHEMES: tuple[str, ...] = tuple(s.value for s in OrgExternalScheme)

# The audited action code for recording an organization observation.
AUDIT_ACTION_RECORD_OBSERVATION = "org.observation.record"

# Validation refusals — each raised BEFORE the session is mutated, so a refusal stages nothing.
REASON_UNKNOWN_ENTITY_TYPE = "unknown_entity_type"
REASON_MISSING_NAME = "missing_display_name"
REASON_LEGAL_ENTITY_HAS_PARENT = "legal_entity_has_parent"
REASON_ESTABLISHMENT_REQUIRES_PARENT = "establishment_requires_parent"
REASON_PARENT_NOT_FOUND = "parent_not_found"
REASON_PARENT_NOT_LEGAL_ENTITY = "parent_not_legal_entity"
REASON_INVALID_STATE_CODE = "invalid_state_code"
REASON_UNKNOWN_EXTERNAL_SCHEME = "unknown_external_scheme"
REASON_MISSING_EXTERNAL_VALUE = "missing_external_value"
REASON_DUPLICATE_EXTERNAL_ID = "duplicate_external_id"


class OrgObservationError(Exception):
    """An observation create was refused with a stable code. Raised before the session is mutated, so
    a refusal leaves no row and no partial state. ``detail`` names the offending value when useful."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ExternalIdInput:
    """One external register identifier to map onto the observation (``scheme`` + ``value``)."""

    scheme: str
    value: str


@dataclass
class ObservationInput:
    """One organization observation as supplied by the caller.

    ``entity_type`` is ``legal_entity`` or ``establishment`` (:class:`OrgObservationType`).
    ``parent_observation_id`` is required for (and only for) an establishment — the id of its parent
    legal-entity observation within this business. ``state_code`` is the recognized Indian state/UT the
    (establishment's) registration sits in; optional. ``external_ids`` are the register identifiers to
    attach via the separate mapping table.
    """

    entity_type: str
    display_name: str = ""
    parent_observation_id: str | None = None
    state_code: str = ""
    external_ids: list[ExternalIdInput] = field(default_factory=list)


def _normalize_entity_type(value: str) -> str:
    token = (value or "").strip().lower()
    try:
        return OrgObservationType(token).value
    except ValueError as exc:
        raise OrgObservationError(
            REASON_UNKNOWN_ENTITY_TYPE,
            f"unknown entity_type '{value}'; expected one of: {', '.join(ENTITY_TYPES)}",
            detail=value,
        ) from exc


def _normalize_scheme(value: str) -> str:
    token = (value or "").strip().lower()
    try:
        return OrgExternalScheme(token).value
    except ValueError as exc:
        raise OrgObservationError(
            REASON_UNKNOWN_EXTERNAL_SCHEME,
            f"unknown external id scheme '{value}'; expected one of: {', '.join(EXTERNAL_SCHEMES)}",
            detail=value,
        ) from exc


class OrgObservationStore:
    """Tenant-scoped reader/writer for organization observations, bound to one business.

    Construct with the active session and the verified business scope (a route resolves it from the
    membership-verified tenant context). Every query is confined to that business — there is no
    argument to widen the scope.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def get(self, observation_id: str) -> OrgObservation | None:
        """Fetch one observation by id WITHIN this business. Another business's id (or an unknown id)
        resolves to ``None`` — a cross-tenant id is reported exactly like not-found, never surfaced."""
        if not observation_id:
            return None
        stmt = (
            select(OrgObservation)
            .where(OrgObservation.business_id == self._business_id)
            .where(OrgObservation.id == observation_id)
        )
        return self._session.scalars(stmt).one_or_none()

    def observations(self) -> list[OrgObservation]:
        """Every observation for this business, oldest first."""
        stmt = (
            select(OrgObservation)
            .where(OrgObservation.business_id == self._business_id)
            .order_by(OrgObservation.created_at, OrgObservation.id)
        )
        return list(self._session.scalars(stmt).all())

    def establishments_of(self, parent_observation_id: str) -> list[OrgObservation]:
        """The establishment observations hanging off a given legal-entity parent, in this business."""
        stmt = (
            select(OrgObservation)
            .where(OrgObservation.business_id == self._business_id)
            .where(OrgObservation.entity_type == OrgObservationType.ESTABLISHMENT.value)
            .where(OrgObservation.parent_observation_id == parent_observation_id)
            .order_by(OrgObservation.created_at, OrgObservation.id)
        )
        return list(self._session.scalars(stmt).all())

    def external_ids_of(self, observation_id: str) -> list[OrgExternalId]:
        """The external register identifiers mapped to one observation, in this business."""
        stmt = (
            select(OrgExternalId)
            .where(OrgExternalId.business_id == self._business_id)
            .where(OrgExternalId.observation_id == observation_id)
            .order_by(OrgExternalId.scheme, OrgExternalId.value)
        )
        return list(self._session.scalars(stmt).all())

    def resolve_external(self, scheme: str, value: str) -> OrgObservation | None:
        """Resolve an external id (scheme + value) to its observation, scoped to this business.

        Returns ``None`` when no mapping exists in THIS business — an id another business recorded is
        invisible here (the mapping is per-business, not a global key). A pure, side-effect-free read.
        """
        normalized_scheme = _normalize_scheme(scheme)
        normalized_value = (value or "").strip().upper()
        if not normalized_value:
            return None
        stmt = (
            select(OrgExternalId)
            .where(OrgExternalId.business_id == self._business_id)
            .where(OrgExternalId.scheme == normalized_scheme)
            .where(OrgExternalId.value == normalized_value)
        )
        mapping = self._session.scalars(stmt).one_or_none()
        if mapping is None:
            return None
        return self.get(mapping.observation_id)

    # -- the one write path -------------------------------------------------------------------------

    def create(
        self,
        data: ObservationInput,
        actor: AuditActor,
        *,
        now: datetime | None = None,
    ) -> OrgObservation:
        """Validate and stage ONE observation (+ its external-id mappings); return the (uncommitted)
        observation. The caller owns the unit of work, so the rows and their atomic audit entry commit
        together.

        Every check runs before the session is mutated — an unknown type, the wrong parent shape (a
        legal entity with a parent, an establishment without one, a parent that is not a legal entity
        of THIS business), an unrecognized Indian state code, an unknown/empty external scheme or value,
        or a duplicate external id within this business is refused with a stable code and stages no row.
        No prospect / host record is created; this records an identity and its mappings only.
        """
        moment = now or datetime.now(UTC)

        entity_type = _normalize_entity_type(data.entity_type)
        display_name = (data.display_name or "").strip()
        if not display_name:
            raise OrgObservationError(
                REASON_MISSING_NAME, "a display_name is required for an observation"
            )

        parent_id = (data.parent_observation_id or "").strip() or None

        # Legal-entity vs establishment shape — the load-bearing distinction.
        if entity_type == OrgObservationType.LEGAL_ENTITY.value:
            if parent_id is not None:
                raise OrgObservationError(
                    REASON_LEGAL_ENTITY_HAS_PARENT,
                    "a legal_entity observation must not name a parent; only an establishment does",
                    detail=parent_id,
                )
        else:  # establishment
            if parent_id is None:
                raise OrgObservationError(
                    REASON_ESTABLISHMENT_REQUIRES_PARENT,
                    "an establishment observation must name its parent legal_entity",
                )
            parent = self.get(parent_id)
            if parent is None:
                # Covers an unknown id AND a parent owned by another business (reads as not-found):
                # an establishment can never link to a parent outside this business.
                raise OrgObservationError(
                    REASON_PARENT_NOT_FOUND,
                    f"parent observation '{parent_id}' does not exist in this business",
                    detail=parent_id,
                )
            if parent.entity_type != OrgObservationType.LEGAL_ENTITY.value:
                raise OrgObservationError(
                    REASON_PARENT_NOT_LEGAL_ENTITY,
                    f"parent observation '{parent_id}' is not a legal_entity; an establishment can "
                    "only hang off a legal entity",
                    detail=parent_id,
                )

        # Optional Indian state/UT code (India-only): if supplied it must be recognized.
        state_code = ""
        if (data.state_code or "").strip():
            resolved = normalize_state_ut(data.state_code)
            if resolved is None:
                raise OrgObservationError(
                    REASON_INVALID_STATE_CODE,
                    f"'{data.state_code}' is not a recognized Indian state/UT code",
                    detail=data.state_code,
                )
            state_code = resolved

        # External-id mappings — validate every one (and reject duplicates) before staging anything.
        normalized_ids: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for raw in data.external_ids or []:
            scheme = _normalize_scheme(raw.scheme)
            value = (raw.value or "").strip().upper()
            if not value:
                raise OrgObservationError(
                    REASON_MISSING_EXTERNAL_VALUE,
                    f"external id for scheme '{scheme}' has no value",
                    detail=scheme,
                )
            pair = (scheme, value)
            if pair in seen:
                continue  # same (scheme, value) supplied twice in one request — collapse, not an error
            seen.add(pair)
            if self._external_exists(scheme, value):
                raise OrgObservationError(
                    REASON_DUPLICATE_EXTERNAL_ID,
                    f"external id {scheme}:{value} is already mapped in this business",
                    detail=f"{scheme}:{value}",
                )
            normalized_ids.append(pair)

        # All checks passed — stage the identity, its mappings, and an atomic audit entry.
        observation_id = str(uuid.uuid4())
        observation = OrgObservation(
            id=observation_id,
            business_id=self._business_id,
            entity_type=entity_type,
            parent_observation_id=parent_id,
            display_name=display_name,
            state_code=state_code,
            created_at=moment,
        )
        self._session.add(observation)
        for scheme, value in normalized_ids:
            self._session.add(
                OrgExternalId(
                    id=str(uuid.uuid4()),
                    business_id=self._business_id,
                    observation_id=observation_id,
                    scheme=scheme,
                    value=value,
                    created_at=moment,
                )
            )

        # Atomic audit (SEC-001/BR-122): staged into the caller's session so it commits with the rows.
        # References stay OPAQUE — ids, the type, the scheme codes and counts — never the raw external
        # id VALUES (a GSTIN/CIN is a company fact, but we keep the trail to opaque references only).
        AuditSink(self._session, business_id=self._business_id, actor=actor).record(
            action=AUDIT_ACTION_RECORD_OBSERVATION,
            reason=f"recorded {entity_type} observation '{observation_id}'",
            references={
                "observationId": observation_id,
                "entityType": entity_type,
                "parentObservationId": parent_id,
                "stateCode": state_code,
                "externalSchemes": sorted({scheme for scheme, _ in normalized_ids}),
                "externalIdCount": len(normalized_ids),
            },
        )
        return observation

    def _external_exists(self, scheme: str, value: str) -> bool:
        stmt = (
            select(OrgExternalId.id)
            .where(OrgExternalId.business_id == self._business_id)
            .where(OrgExternalId.scheme == scheme)
            .where(OrgExternalId.value == value)
            .limit(1)
        )
        return self._session.scalars(stmt).first() is not None
