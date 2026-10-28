"""DCA-060 — store tenant-private contact points (person identities + their reachable values).

:class:`ContactPointStore` is the single sanctioned writer of
:class:`~app.db.models.PersonObservation` and its separate :class:`~app.db.models.ContactPoint`
value rows. It lets ONE business record a tenant-private person — optionally linked to one of that
business's :class:`~app.db.models.OrgObservation` organizations — and attach the contact points
(email / mobile / …) that reach them, tagged with the identity evidence that supports each, WITHOUT
creating any prospect / host record and WITHOUT ever promoting an imported value to shared data
(acquisition and consent stay disabled future capabilities).

The load-bearing guarantees (the DCA-060 acceptance criteria):

- **Contact points are tenant-private; the same global mapping cannot expose A's email to B.** The
  store is bound to one verified business scope and every read/write is confined to it (DCA-017
  scoping + DCA-018 RLS beneath it). A person, an organization link, or a contact-point lookup that
  belongs to another business resolves to not-found here — never a cross-tenant read. Even when two
  businesses point their organization observations at the SAME external/global organization id, each
  resolves only to its OWN organization, its own people, and its own contact points: there is no
  global index keyed on the value, so A's private email is never reachable from B.

- **Imported values are never auto-promoted.** A contact point (including one tagged
  ``IMPORTED``) is written only to this business's private tables. Nothing here copies a value into
  any shared/global table; promotion would be its own rights-and-consent story.

- **A denial changes nothing.** Every validation runs BEFORE the session is mutated, so a refused
  create (missing name, unknown/cross-tenant organization link, unknown person, unknown channel or
  evidence, empty value, or a duplicate contact point within the business) stages no row.

Each successful create stages an atomic DCA-026 audit entry in the SAME unit of work as the rows
(SEC-001/BR-122 discipline); the store never commits — the caller owns the transaction. References on
the audit entry are kept OPAQUE (ids / channel / evidence codes) — the raw contact VALUE and the
person's display name are tenant-private and are NEVER audited.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.sink import AuditActor, AuditSink
from app.db.models import ContactChannel, ContactEvidence, ContactPoint, PersonObservation

# The controlled vocabularies this store validates against.
CHANNELS: tuple[str, ...] = tuple(c.value for c in ContactChannel)
EVIDENCE_CLASSES: tuple[str, ...] = tuple(e.value for e in ContactEvidence)

# The audited action codes.
AUDIT_ACTION_RECORD_PERSON = "contact.person.record"
AUDIT_ACTION_RECORD_CONTACT_POINT = "contact.point.record"

# Validation refusals — each raised BEFORE the session is mutated, so a refusal stages nothing.
REASON_MISSING_PERSON_NAME = "missing_person_name"
REASON_PERSON_ORG_NOT_FOUND = "person_org_not_found"
REASON_PERSON_NOT_FOUND = "person_not_found"
REASON_UNKNOWN_CHANNEL = "unknown_channel"
REASON_UNKNOWN_EVIDENCE = "unknown_evidence"
REASON_MISSING_CONTACT_VALUE = "missing_contact_value"
REASON_DUPLICATE_CONTACT_POINT = "duplicate_contact_point"


class ContactPointError(Exception):
    """A person/contact-point create was refused with a stable code. Raised before the session is
    mutated, so a refusal leaves no row and no partial state. ``detail`` names the offending value
    when useful (never a raw contact value)."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass
class PersonInput:
    """One person observation as supplied by the caller.

    ``display_name`` is required. ``org_observation_id`` optionally links the person to one of THIS
    business's organization observations (an establishment or legal entity); it must exist in this
    business. ``role_title`` is an optional business fact (e.g. "Procurement Head").
    """

    display_name: str = ""
    org_observation_id: str | None = None
    role_title: str = ""


@dataclass
class ContactPointInput:
    """One contact point to attach to a person (``channel`` + ``value`` + identity ``evidence``)."""

    person_observation_id: str
    channel: str
    value: str
    evidence: str


def _normalize_channel(value: str) -> str:
    token = (value or "").strip().lower()
    try:
        return ContactChannel(token).value
    except ValueError as exc:
        raise ContactPointError(
            REASON_UNKNOWN_CHANNEL,
            f"unknown channel '{value}'; expected one of: {', '.join(CHANNELS)}",
            detail=value,
        ) from exc


def _normalize_evidence(value: str) -> str:
    token = (value or "").strip().lower()
    try:
        return ContactEvidence(token).value
    except ValueError as exc:
        raise ContactPointError(
            REASON_UNKNOWN_EVIDENCE,
            f"unknown evidence '{value}'; expected one of: {', '.join(EVIDENCE_CLASSES)}",
            detail=value,
        ) from exc


def _normalize_value(channel: str, value: str) -> str:
    """Normalize a contact value for storage and per-business dedup. Trimmed always; lower-cased for
    email (addresses are case-insensitive) — other channels keep their original casing."""
    trimmed = (value or "").strip()
    if channel == ContactChannel.EMAIL.value:
        return trimmed.lower()
    return trimmed


class ContactPointStore:
    """Tenant-scoped reader/writer for person observations and their contact points, bound to one
    business.

    Construct with the active session and the verified business scope (a route resolves it from the
    membership-verified tenant context). Every query is confined to that business — there is no
    argument to widen the scope.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def get_person(self, person_id: str) -> PersonObservation | None:
        """Fetch one person by id WITHIN this business. Another business's id (or an unknown id)
        resolves to ``None`` — a cross-tenant id reads exactly like not-found, never surfaced."""
        if not person_id:
            return None
        stmt = (
            select(PersonObservation)
            .where(PersonObservation.business_id == self._business_id)
            .where(PersonObservation.id == person_id)
        )
        return self._session.scalars(stmt).one_or_none()

    def persons(self) -> list[PersonObservation]:
        """Every person observation for this business, oldest first."""
        stmt = (
            select(PersonObservation)
            .where(PersonObservation.business_id == self._business_id)
            .order_by(PersonObservation.created_at, PersonObservation.id)
        )
        return list(self._session.scalars(stmt).all())

    def persons_of_org(self, org_observation_id: str) -> list[PersonObservation]:
        """The people linked to a given organization observation, in this business."""
        stmt = (
            select(PersonObservation)
            .where(PersonObservation.business_id == self._business_id)
            .where(PersonObservation.org_observation_id == org_observation_id)
            .order_by(PersonObservation.created_at, PersonObservation.id)
        )
        return list(self._session.scalars(stmt).all())

    def get_contact_point(self, contact_point_id: str) -> ContactPoint | None:
        """Fetch one contact point by id WITHIN this business; cross-tenant/unknown reads as ``None``."""
        if not contact_point_id:
            return None
        stmt = (
            select(ContactPoint)
            .where(ContactPoint.business_id == self._business_id)
            .where(ContactPoint.id == contact_point_id)
        )
        return self._session.scalars(stmt).one_or_none()

    def contact_points_of(self, person_id: str) -> list[ContactPoint]:
        """The contact points attached to one person, in this business."""
        stmt = (
            select(ContactPoint)
            .where(ContactPoint.business_id == self._business_id)
            .where(ContactPoint.person_observation_id == person_id)
            .order_by(ContactPoint.channel, ContactPoint.value)
        )
        return list(self._session.scalars(stmt).all())

    def resolve_contact(self, channel: str, value: str) -> ContactPoint | None:
        """Resolve a contact point by (channel + value), scoped to THIS business.

        Returns ``None`` when no such private value exists in this business — a value another business
        recorded is invisible here (there is no global index on the value). A pure, side-effect-free
        read: this is the lookup that proves A's private email cannot be reached from B.
        """
        normalized_channel = _normalize_channel(channel)
        normalized_value = _normalize_value(normalized_channel, value)
        if not normalized_value:
            return None
        stmt = (
            select(ContactPoint)
            .where(ContactPoint.business_id == self._business_id)
            .where(ContactPoint.channel == normalized_channel)
            .where(ContactPoint.value == normalized_value)
        )
        return self._session.scalars(stmt).one_or_none()

    # -- the write paths ----------------------------------------------------------------------------

    def create_person(
        self,
        data: PersonInput,
        actor: AuditActor,
        *,
        now: datetime | None = None,
    ) -> PersonObservation:
        """Validate and stage ONE person observation; return the (uncommitted) row. The caller owns the
        unit of work, so the row and its atomic audit entry commit together.

        Every check runs before the session is mutated — a missing display name, or an
        ``org_observation_id`` that does not exist in THIS business (an unknown id or one owned by
        another business, both read as not-found), is refused with a stable code and stages no row. No
        prospect / host record is created; this records a tenant-private identity only.
        """
        moment = now or datetime.now(UTC)

        display_name = (data.display_name or "").strip()
        if not display_name:
            raise ContactPointError(
                REASON_MISSING_PERSON_NAME, "a display_name is required for a person observation"
            )

        org_id = (data.org_observation_id or "").strip() or None
        if org_id is not None and not self._org_exists(org_id):
            # Covers an unknown id AND an organization owned by another business (reads as not-found):
            # a person can never link to an organization outside this business.
            raise ContactPointError(
                REASON_PERSON_ORG_NOT_FOUND,
                f"organization observation '{org_id}' does not exist in this business",
                detail=org_id,
            )

        person_id = str(uuid.uuid4())
        person = PersonObservation(
            id=person_id,
            business_id=self._business_id,
            org_observation_id=org_id,
            display_name=display_name,
            role_title=(data.role_title or "").strip(),
            created_at=moment,
        )
        self._session.add(person)

        # Atomic audit (SEC-001/BR-122). References stay OPAQUE — the person id and the (opaque) org id
        # only; the display name is tenant-private personal data and is NEVER audited.
        AuditSink(self._session, business_id=self._business_id, actor=actor).record(
            action=AUDIT_ACTION_RECORD_PERSON,
            reason=f"recorded person observation '{person_id}'",
            references={
                "personId": person_id,
                "orgObservationId": org_id,
                "hasRoleTitle": bool((data.role_title or "").strip()),
            },
        )
        return person

    def add_contact_point(
        self,
        data: ContactPointInput,
        actor: AuditActor,
        *,
        now: datetime | None = None,
    ) -> ContactPoint:
        """Validate and stage ONE contact point onto a person; return the (uncommitted) row. The caller
        owns the unit of work, so the row and its atomic audit entry commit together.

        Every check runs before the session is mutated — a person that does not exist in THIS business
        (unknown or cross-tenant, both not-found), an unknown channel or evidence class, an empty value,
        or a duplicate contact point within this business is refused with a stable code and stages no
        row. The value is stored tenant-private and is NEVER promoted to shared data.
        """
        moment = now or datetime.now(UTC)

        person_id = (data.person_observation_id or "").strip()
        if not person_id or self.get_person(person_id) is None:
            raise ContactPointError(
                REASON_PERSON_NOT_FOUND,
                f"person observation '{person_id}' does not exist in this business",
                detail=person_id,
            )

        channel = _normalize_channel(data.channel)
        evidence = _normalize_evidence(data.evidence)
        value = _normalize_value(channel, data.value)
        if not value:
            raise ContactPointError(
                REASON_MISSING_CONTACT_VALUE,
                f"contact point for channel '{channel}' has no value",
                detail=channel,
            )

        if self._contact_exists(channel, value):
            # Duplicate detail is kept to the channel only — never the raw contact value.
            raise ContactPointError(
                REASON_DUPLICATE_CONTACT_POINT,
                f"a {channel} contact point with this value is already stored in this business",
                detail=channel,
            )

        contact_point_id = str(uuid.uuid4())
        contact_point = ContactPoint(
            id=contact_point_id,
            business_id=self._business_id,
            person_observation_id=person_id,
            channel=channel,
            value=value,
            evidence=evidence,
            created_at=moment,
        )
        self._session.add(contact_point)

        # Atomic audit (SEC-001/BR-122). References stay OPAQUE — ids, the channel and evidence codes —
        # NEVER the raw contact value (an email/phone is tenant-private and screened out of the trail).
        AuditSink(self._session, business_id=self._business_id, actor=actor).record(
            action=AUDIT_ACTION_RECORD_CONTACT_POINT,
            reason=f"recorded {channel} contact point '{contact_point_id}'",
            references={
                "contactPointId": contact_point_id,
                "personId": person_id,
                "channel": channel,
                "evidence": evidence,
            },
        )
        return contact_point

    # -- internals ----------------------------------------------------------------------------------

    def _org_exists(self, org_observation_id: str) -> bool:
        from app.db.models import OrgObservation

        stmt = (
            select(OrgObservation.id)
            .where(OrgObservation.business_id == self._business_id)
            .where(OrgObservation.id == org_observation_id)
            .limit(1)
        )
        return self._session.scalars(stmt).first() is not None

    def _contact_exists(self, channel: str, value: str) -> bool:
        stmt = (
            select(ContactPoint.id)
            .where(ContactPoint.business_id == self._business_id)
            .where(ContactPoint.channel == channel)
            .where(ContactPoint.value == value)
            .limit(1)
        )
        return self._session.scalars(stmt).first() is not None
