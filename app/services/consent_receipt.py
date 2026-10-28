"""DCA-074 — persist SCOPED, EVIDENCED permission (consent) receipts.

:class:`ConsentReceiptStore` is the single sanctioned writer of
:class:`~app.db.models.ConsentReceipt`. It lets ONE business record a tenant-private permission
*receipt*: the durable, evidenced record that a specific subject took a specific permission
:class:`~app.db.models.ConsentAction` on a specific reachable :class:`~app.db.models.ContactPoint` /
channel, for one brand the business runs, for a stated purpose, under a specific notice/form version,
in a stated language, at a stated time.

The load-bearing guarantee (the DCA-074 acceptance criterion):

- **An imported "subscribed" flag without evidence does not create a valid receipt.** A permission is
  not a boolean attribute hung on a contact — it is a scoped receipt that must cite the captured proof
  of the permission event: a real, SAME-business :class:`~app.db.models.EvidenceObject` (DCA-062), the
  record of the form submission / opt-in. A flag-only import (a bare ``subscribed = true`` with no
  evidence) is refused — :data:`REASON_MISSING_EVIDENCE` — BEFORE the session is mutated, so it stages
  no receipt. The receipt also requires the full scope — brand, contact point, channel, purpose,
  notice version, form version, language, action and the collection time — so a permission can never
  be stored as a global contact flag.

The supporting guarantees:

- **Scoped, never globally visible.** The store is bound to one verified business scope; every
  read/write is confined to it (DCA-017 scoping + DCA-018 RLS beneath it). A by-id read of another
  business's receipt resolves to ``None`` — a cross-tenant id reads exactly like not-found. The SAME
  contact value recorded by two businesses yields independent receipts (or none): one tenant's
  permission is never visible to, or assertable for, another.

- **Grounded in this business's own identities.** The ``contact_point_id`` must name a
  :class:`~app.db.models.ContactPoint` in this business; the ``channel`` must match it; the person
  subject is the contact point's owning :class:`~app.db.models.PersonObservation`. The cited
  ``evidence_id`` must resolve within this business, and — when that evidence is itself subject-linked
  — its subject must match the contact point's owner.

- **A denial changes nothing; append-only history.** Every validation runs BEFORE the session is
  mutated, so a refusal stages no receipt. A receipt is never edited or deleted — a later confirmation
  or withdrawal is a NEW row — so the permission timeline for a contact point stays reviewable.
  Recording a receipt creates no prospect / host record and authorizes no send: acting on a permission
  stays a disabled future capability behind the one send pipeline.

Each successful record stages an atomic DCA-026 audit entry in the SAME unit of work as the row
(SEC-001/BR-122). References stay OPAQUE — ids / brand / channel / purpose / action / notice+form
version / language — and never the raw contact value (RLS-protected on the contact point).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.sink import AuditActor, AuditSink
from app.db.models import (
    ConsentAction,
    ConsentReceipt,
    ContactChannel,
    ContactPoint,
    EvidenceObject,
    FieldSubjectType,
)

# The controlled vocabulary of permission actions a receipt may record.
CONSENT_ACTIONS: tuple[str, ...] = tuple(a.value for a in ConsentAction)
# The controlled vocabulary of channels a permission may cover.
CONSENT_CHANNELS: tuple[str, ...] = tuple(c.value for c in ContactChannel)

# The audited action code for recording a consent receipt.
AUDIT_ACTION_RECORD_RECEIPT = "consent.receipt.record"
# The audited action code for recording a WITHDRAWAL receipt (DCA-076). A distinct action so the trail
# shows an opt-out as its own reviewable event, never an edit of the grant.
AUDIT_ACTION_WITHDRAW_RECEIPT = "consent.receipt.withdraw"

# Validation refusals — each raised BEFORE the session is mutated, so a refusal stages nothing.
REASON_MISSING_BRAND = "missing_brand"
REASON_MISSING_CONTACT_POINT = "missing_contact_point"
REASON_CONTACT_POINT_NOT_FOUND = "contact_point_not_found"
REASON_UNKNOWN_CHANNEL = "unknown_channel"
REASON_CHANNEL_MISMATCH = "channel_mismatch"
REASON_MISSING_PURPOSE = "missing_purpose"
REASON_UNKNOWN_ACTION = "unknown_action"
REASON_MISSING_NOTICE_VERSION = "missing_notice_version"
REASON_MISSING_FORM_VERSION = "missing_form_version"
REASON_MISSING_LANGUAGE = "missing_language"
REASON_MISSING_COLLECTED_AT = "missing_collected_at"
# The load-bearing refusal: a permission without captured evidence is not a valid receipt.
REASON_MISSING_EVIDENCE = "missing_evidence"
REASON_EVIDENCE_NOT_FOUND = "evidence_not_found"
REASON_EVIDENCE_SUBJECT_MISMATCH = "evidence_subject_mismatch"


class ConsentReceiptError(Exception):
    """A consent-receipt write was refused with a stable code. Raised before the session is mutated,
    so a refusal leaves no row and no partial state. ``detail`` names the offending value when useful
    (never a raw contact value)."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass
class ConsentReceiptInput:
    """One permission receipt as supplied by the caller.

    ``brand_key`` names the brand (within this business) the permission was given to. ``contact_point_id``
    is the reachable value the permission covers (REQUIRED — a permission is per contact point, never a
    global attribute); ``channel`` must match that contact point. ``purpose`` is the stated purpose;
    ``action`` is the :class:`~app.db.models.ConsentAction`. ``notice_version`` / ``form_version`` are
    the notice and collection-form versions shown; ``language`` is the language it was presented in.
    ``evidence_id`` is the captured proof (an :class:`~app.db.models.EvidenceObject` in this business) —
    REQUIRED, so a flag-only import with no evidence is refused. ``collected_at`` is when the subject
    acted.
    """

    brand_key: str
    contact_point_id: str
    channel: str
    purpose: str
    action: str
    notice_version: str
    form_version: str
    language: str
    evidence_id: str
    collected_at: datetime


def _normalize_action(value: str) -> str:
    token = (value or "").strip().lower()
    try:
        return ConsentAction(token).value
    except ValueError as exc:
        raise ConsentReceiptError(
            REASON_UNKNOWN_ACTION,
            f"unknown consent action '{value}'; expected one of: {', '.join(CONSENT_ACTIONS)}",
            detail=value,
        ) from exc


def _normalize_channel(value: str) -> str:
    token = (value or "").strip().lower()
    try:
        return ContactChannel(token).value
    except ValueError as exc:
        raise ConsentReceiptError(
            REASON_UNKNOWN_CHANNEL,
            f"unknown channel '{value}'; expected one of: {', '.join(CONSENT_CHANNELS)}",
            detail=value,
        ) from exc


class ConsentReceiptStore:
    """Tenant-scoped reader/writer for consent receipts, bound to one business.

    Construct with the active session and the verified business scope (a route resolves it from the
    membership-verified tenant context). Every query is confined to that business — there is no
    argument to widen the scope.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def get(self, receipt_id: str) -> ConsentReceipt | None:
        """Fetch one receipt by id WITHIN this business. Another business's id (or an unknown id)
        resolves to ``None`` — a cross-tenant id reads exactly like not-found."""
        if not receipt_id:
            return None
        stmt = (
            select(ConsentReceipt)
            .where(ConsentReceipt.business_id == self._business_id)
            .where(ConsentReceipt.id == receipt_id)
        )
        return self._session.scalars(stmt).one_or_none()

    def receipts(self) -> list[ConsentReceipt]:
        """Every consent receipt for this business, oldest first."""
        stmt = (
            select(ConsentReceipt)
            .where(ConsentReceipt.business_id == self._business_id)
            .order_by(ConsentReceipt.created_at, ConsentReceipt.id)
        )
        return list(self._session.scalars(stmt).all())

    def for_contact_point(self, contact_point_id: str) -> list[ConsentReceipt]:
        """The permission receipts recorded for one contact point in this business, oldest first —
        the reviewable permission timeline for that reachable value."""
        stmt = (
            select(ConsentReceipt)
            .where(ConsentReceipt.business_id == self._business_id)
            .where(ConsentReceipt.contact_point_id == contact_point_id)
            .order_by(ConsentReceipt.created_at, ConsentReceipt.id)
        )
        return list(self._session.scalars(stmt).all())

    # -- the one write path -------------------------------------------------------------------------

    def record(
        self,
        data: ConsentReceiptInput,
        actor: AuditActor,
        *,
        now: datetime | None = None,
    ) -> ConsentReceipt:
        """Validate the full scope + captured evidence and stage ONE consent receipt; return the
        (uncommitted) row. The caller owns the unit of work, so the row and its atomic audit entry
        commit together.

        Every check runs before the session is mutated. A record is refused — and stages NO row —
        when it is missing the brand / contact point / purpose / notice version / form version /
        language / collection time, names an unknown action or channel, cites a contact point that
        does not exist in this business or whose channel does not match, or — the load-bearing gate —
        cites NO captured evidence (:data:`REASON_MISSING_EVIDENCE`) or an evidence object that does
        not resolve in this business (:data:`REASON_EVIDENCE_NOT_FOUND`). A flag-only "subscribed"
        import, which carries no evidence, therefore creates no valid receipt. No prospect / host
        record is created and no send is authorized.
        """
        moment = now or datetime.now(UTC)

        brand_key = (data.brand_key or "").strip().lower()
        if not brand_key:
            raise ConsentReceiptError(
                REASON_MISSING_BRAND, "a consent receipt must name the brand it was given to"
            )

        contact_point_id = (data.contact_point_id or "").strip()
        if not contact_point_id:
            raise ConsentReceiptError(
                REASON_MISSING_CONTACT_POINT,
                "a consent receipt must name the contact_point it covers (permissions are per "
                "contact point, never a global contact attribute)",
            )
        contact_point = self._contact_point(contact_point_id)
        if contact_point is None:
            raise ConsentReceiptError(
                REASON_CONTACT_POINT_NOT_FOUND,
                f"contact point '{contact_point_id}' does not exist in this business",
                detail=contact_point_id,
            )

        channel = _normalize_channel(data.channel)
        if channel != contact_point.channel:
            raise ConsentReceiptError(
                REASON_CHANNEL_MISMATCH,
                f"channel '{channel}' does not match the contact point's channel "
                f"'{contact_point.channel}'",
                detail=channel,
            )

        purpose = (data.purpose or "").strip().lower()
        if not purpose:
            raise ConsentReceiptError(
                REASON_MISSING_PURPOSE, "a stated purpose is required for a consent receipt"
            )

        action = _normalize_action(data.action)

        notice_version = (data.notice_version or "").strip()
        if not notice_version:
            raise ConsentReceiptError(
                REASON_MISSING_NOTICE_VERSION,
                "the notice/privacy-notice version shown is required",
            )
        form_version = (data.form_version or "").strip()
        if not form_version:
            raise ConsentReceiptError(
                REASON_MISSING_FORM_VERSION, "the collection form/flow version is required"
            )
        language = (data.language or "").strip().lower()
        if not language:
            raise ConsentReceiptError(
                REASON_MISSING_LANGUAGE,
                "the language the notice/form was presented in is required",
            )

        if data.collected_at is None:
            raise ConsentReceiptError(
                REASON_MISSING_COLLECTED_AT, "a collected_at timestamp is required"
            )

        # The load-bearing gate: a permission MUST cite captured evidence. A flag-only import carries
        # none and therefore creates no valid receipt.
        evidence_id = (data.evidence_id or "").strip()
        if not evidence_id:
            raise ConsentReceiptError(
                REASON_MISSING_EVIDENCE,
                "a consent receipt must cite the captured evidence of the permission event; a "
                "'subscribed' flag with no evidence is not a valid receipt",
            )
        evidence = self._evidence(evidence_id)
        if evidence is None:
            raise ConsentReceiptError(
                REASON_EVIDENCE_NOT_FOUND,
                f"evidence object '{evidence_id}' does not exist in this business",
                detail=evidence_id,
            )

        # The subject is the contact point's owning person (within this business).
        subject_type = FieldSubjectType.PERSON.value
        subject_id = contact_point.person_observation_id

        # When the cited evidence is itself subject-linked, it must match this receipt's subject — a
        # receipt is grounded in evidence ABOUT the person it records a permission for.
        if evidence.subject_type and evidence.subject_id:
            if evidence.subject_type != subject_type or evidence.subject_id != subject_id:
                raise ConsentReceiptError(
                    REASON_EVIDENCE_SUBJECT_MISMATCH,
                    "the cited evidence is about a different subject than the contact point's owner",
                    detail=evidence_id,
                )

        receipt_id = str(uuid.uuid4())
        receipt = ConsentReceipt(
            id=receipt_id,
            business_id=self._business_id,
            brand_key=brand_key,
            subject_type=subject_type,
            subject_id=subject_id,
            contact_point_id=contact_point_id,
            channel=channel,
            purpose=purpose,
            action=action,
            notice_version=notice_version,
            form_version=form_version,
            language=language,
            evidence_id=evidence_id,
            collected_at=data.collected_at,
            created_at=moment,
        )
        self._session.add(receipt)

        # Atomic audit (SEC-001/BR-122): staged into the caller's session so it commits with the row.
        # References stay OPAQUE — ids, brand, channel, purpose, action, notice/form version, language
        # — never the raw contact value (RLS-protected on the contact point) or any personal data.
        AuditSink(self._session, business_id=self._business_id, actor=actor).record(
            action=AUDIT_ACTION_RECORD_RECEIPT,
            reason=f"recorded {action} consent receipt '{receipt_id}'",
            references={
                "receiptId": receipt_id,
                "brandKey": brand_key,
                "subjectType": subject_type,
                "subjectId": subject_id,
                "contactPointId": contact_point_id,
                "channel": channel,
                "purpose": purpose,
                "action": action,
                "noticeVersion": notice_version,
                "formVersion": form_version,
                "language": language,
                "evidenceId": evidence_id,
            },
        )
        return receipt

    def record_withdrawal(
        self,
        *,
        brand_key: str,
        contact_point_id: str,
        channel: str,
        purpose: str,
        actor: AuditActor,
        collected_at: datetime,
        withdrawn_via: str = "authenticated",
        notice_version: str = "",
        form_version: str = "",
        language: str = "",
        evidence_id: str = "",
        now: datetime | None = None,
    ) -> ConsentReceipt:
        """Append a WITHDRAW receipt for ONE scope and return the (uncommitted) row (DCA-076).

        A withdrawal is the fail-safe direction — honouring an opt-out must never be harder than
        granting — so, UNLIKE :meth:`record`, it does NOT require a cited notice/form/language or a
        captured :class:`~app.db.models.EvidenceObject`: a subject can always restrict a scope. It
        still validates the scope the way a grant does — the ``contact_point_id`` must name a
        :class:`~app.db.models.ContactPoint` in THIS business and ``channel`` must match it — so a
        withdrawal can never be attributed to another tenant's contact point (a cross-tenant / unknown
        id reads as not-found and stages nothing). An optional ``evidence_id``, when supplied, is
        validated to resolve in this business exactly like a grant's.

        The row is WITHDRAW, append-only (the prior grant is preserved), so
        :func:`~app.consent.derive_permission_state` over the timeline immediately reports the scope
        WITHDRAWN. An atomic DCA-026 audit entry (opaque references only, never the raw contact value)
        is staged in the SAME unit of work. The caller owns the transaction; recording a withdrawal
        authorizes no send and creates no prospect / host record.
        """
        moment = now or datetime.now(UTC)

        brand = (brand_key or "").strip().lower()
        if not brand:
            raise ConsentReceiptError(
                REASON_MISSING_BRAND,
                "a withdrawal must name the brand whose permission it restricts",
            )

        cp_id = (contact_point_id or "").strip()
        if not cp_id:
            raise ConsentReceiptError(
                REASON_MISSING_CONTACT_POINT,
                "a withdrawal must name the contact_point it restricts (permissions are per contact "
                "point, never a global contact attribute)",
            )
        contact_point = self._contact_point(cp_id)
        if contact_point is None:
            raise ConsentReceiptError(
                REASON_CONTACT_POINT_NOT_FOUND,
                f"contact point '{cp_id}' does not exist in this business",
                detail=cp_id,
            )

        normalized_channel = _normalize_channel(channel)
        if normalized_channel != contact_point.channel:
            raise ConsentReceiptError(
                REASON_CHANNEL_MISMATCH,
                f"channel '{normalized_channel}' does not match the contact point's channel "
                f"'{contact_point.channel}'",
                detail=normalized_channel,
            )

        normalized_purpose = (purpose or "").strip().lower()
        if not normalized_purpose:
            raise ConsentReceiptError(
                REASON_MISSING_PURPOSE,
                "a withdrawal is scoped to one purpose; a stated purpose is required",
            )

        if collected_at is None:
            raise ConsentReceiptError(
                REASON_MISSING_COLLECTED_AT, "a collected_at timestamp is required"
            )

        # Evidence is OPTIONAL for a withdrawal, but when cited it must resolve in this business.
        ev_id = (evidence_id or "").strip()
        if ev_id and self._evidence(ev_id) is None:
            raise ConsentReceiptError(
                REASON_EVIDENCE_NOT_FOUND,
                f"evidence object '{ev_id}' does not exist in this business",
                detail=ev_id,
            )

        subject_type = FieldSubjectType.PERSON.value
        subject_id = contact_point.person_observation_id

        receipt_id = str(uuid.uuid4())
        receipt = ConsentReceipt(
            id=receipt_id,
            business_id=self._business_id,
            brand_key=brand,
            subject_type=subject_type,
            subject_id=subject_id,
            contact_point_id=cp_id,
            channel=normalized_channel,
            purpose=normalized_purpose,
            action=ConsentAction.WITHDRAW.value,
            notice_version=(notice_version or "").strip(),
            form_version=(form_version or "").strip(),
            language=(language or "").strip().lower(),
            evidence_id=ev_id,
            collected_at=collected_at,
            created_at=moment,
        )
        self._session.add(receipt)

        AuditSink(self._session, business_id=self._business_id, actor=actor).record(
            action=AUDIT_ACTION_WITHDRAW_RECEIPT,
            reason=f"recorded withdraw consent receipt '{receipt_id}'",
            references={
                "receiptId": receipt_id,
                "brandKey": brand,
                "subjectType": subject_type,
                "subjectId": subject_id,
                "contactPointId": cp_id,
                "channel": normalized_channel,
                "purpose": normalized_purpose,
                "action": ConsentAction.WITHDRAW.value,
                "withdrawnVia": (withdrawn_via or "").strip().lower() or "authenticated",
            },
        )
        return receipt

    # -- internals ----------------------------------------------------------------------------------

    def _contact_point(self, contact_point_id: str) -> ContactPoint | None:
        stmt = (
            select(ContactPoint)
            .where(ContactPoint.business_id == self._business_id)
            .where(ContactPoint.id == contact_point_id)
            .limit(1)
        )
        return self._session.scalars(stmt).one_or_none()

    def _evidence(self, evidence_id: str) -> EvidenceObject | None:
        stmt = (
            select(EvidenceObject)
            .where(EvidenceObject.business_id == self._business_id)
            .where(EvidenceObject.id == evidence_id)
            .limit(1)
        )
        return self._session.scalars(stmt).one_or_none()
