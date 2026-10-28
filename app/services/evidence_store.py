"""DCA-062 — store only PERMITTED evidence objects, with license-aware retention.

:class:`EvidenceObjectStore` is the single sanctioned writer of
:class:`~app.db.models.EvidenceObject`. It lets ONE business capture a single piece of evidence from a
catalogued :class:`~app.db.models.Source` under a specific :class:`~app.db.models.SourceTerms` version,
and it keeps only what that version's licence permits: where raw retention is prohibited it preserves a
REFERENCE (the locator + a non-reversible digest) rather than the raw payload.

The load-bearing guarantee (the DCA-062 acceptance criterion):

- **A no-retention source leaves no raw content copy.** The retention mode is DERIVED from the cited
  source terms at capture time — never supplied by the caller. Raw retention requires BOTH the terms'
  ``store`` operation right AND a retention policy that permits keeping raw content. When either is
  withheld — the ``store`` right is denied, the retention policy is a no-retention policy, or the
  retention window is zero days — the object is stored ``REFERENCE_ONLY``: ``raw_content`` is left
  ``None`` and never written, while the locator ``content_ref`` and the SHA-256 ``content_digest`` of
  the captured bytes ARE kept, so the evidence stays traceable/verifiable without a prohibited raw copy.

The supporting guarantees:

- **Scoped, never globally visible.** The store is bound to one verified business scope; every
  read/write is confined to that business (DCA-017 scoping + DCA-018 RLS beneath it). A by-id read of
  another business's evidence object resolves to ``None`` — a cross-tenant id reads exactly like
  not-found. An optional subject is resolved WITHIN this business (a cross-tenant/unknown subject is
  refused).

- **Provenance points at a real, reviewable source.** The ``source_key`` must name a catalogued
  :class:`~app.db.models.Source` (DCA-044) and the ``source_version`` a real
  :class:`~app.db.models.SourceTerms` version (DCA-045) of it — evidence is always grounded in a source
  that can be reviewed. (Recording provenance does NOT assert the rights are in force.)

- **A denial changes nothing + no host record.** Every validation runs BEFORE the session is mutated,
  so a refusal stages no evidence row. Capturing evidence creates no prospect / host record and promotes
  nothing to any shared/canonical table; acquisition stays a disabled future capability.

Each successful capture stages an atomic DCA-026 audit entry in the SAME unit of work as the row
(SEC-001/BR-122). References stay OPAQUE — ids / source key+version / content kind / retention mode +
reason / the safe digest — and the raw payload and locator are NEVER audited.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.sink import AuditActor, AuditSink
from app.db.models import (
    EvidenceObject,
    EvidenceRetentionMode,
    FieldSubjectType,
    OrgObservation,
    PersonObservation,
    Source,
    SourceOperation,
    SourceTerms,
)

# The controlled vocabulary of subject kinds an evidence object may link to (shared with DCA-061).
SUBJECT_TYPES: tuple[str, ...] = tuple(t.value for t in FieldSubjectType)

# The audited action code for storing an evidence object.
AUDIT_ACTION_STORE_EVIDENCE = "evidence.object.store"

# Retention policy labels (case-insensitive) that explicitly forbid keeping raw content — only a
# reference may be retained. Any of these flips the object to REFERENCE_ONLY even if ``store`` is
# granted. Deny-leaning: an unrecognized policy string does NOT, by itself, prohibit raw retention
# (the ``store`` right and window still gate it), but these named policies always do.
_NO_RAW_RETENTION_POLICIES = frozenset(
    {
        "no_retention",
        "no-retention",
        "none",
        "no_store",
        "do_not_store",
        "reference_only",
        "reference-only",
        "metadata_only",
    }
)

# Validation refusals — each raised BEFORE the session is mutated, so a refusal stages nothing.
REASON_UNKNOWN_SUBJECT_TYPE = "unknown_subject_type"
REASON_SUBJECT_NOT_FOUND = "subject_not_found"
REASON_MISSING_SOURCE_REFERENCE = "missing_source_reference"
REASON_SOURCE_NOT_FOUND = "source_not_found"
REASON_SOURCE_VERSION_NOT_FOUND = "source_version_not_found"
REASON_MISSING_CONTENT_KIND = "missing_content_kind"
REASON_MISSING_CONTENT_REF = "missing_content_ref"
REASON_MISSING_RAW_CONTENT = "missing_raw_content"
REASON_MISSING_CAPTURED_AT = "missing_captured_at"
REASON_MISSING_RIGHTS_BASIS = "missing_rights_basis"

# Reason codes recorded on a REFERENCE_ONLY object (why the raw payload was dropped). Stable,
# auditable why-reference-only.
RETENTION_REASON_STORE_NOT_PERMITTED = "store_not_permitted"
RETENTION_REASON_NO_RETENTION_POLICY = "no_retention_policy"
RETENTION_REASON_ZERO_WINDOW = "zero_retention_window"


class EvidenceStoreError(Exception):
    """An evidence capture was refused with a stable code. Raised before the session is mutated, so a
    refusal leaves no row and no partial state. ``detail`` names the offending value when useful
    (never a raw payload)."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class RetentionDecision:
    """The outcome of the license-aware retention screen for one capture.

    ``mode`` is the derived :class:`~app.db.models.EvidenceRetentionMode` value; ``raw_permitted`` is
    its boolean form. ``reason`` is ``""`` when raw is retained, otherwise the stable code naming why
    the raw payload must be dropped. ``max_days`` is the terms' raw-retention window (``None`` = no
    stated limit), honoured only when raw is retained.
    """

    mode: str
    raw_permitted: bool
    reason: str
    max_days: int | None


@dataclass
class EvidenceInput:
    """One evidence object as supplied by the caller.

    ``source_key`` / ``source_version`` name the catalogued source + terms version the evidence was
    captured under (REQUIRED — evidence is always grounded in a reviewable source). ``content_kind`` is
    a short descriptive kind; ``content_ref`` is the locator preserved in either mode; ``raw_content``
    is the captured payload (used to compute the digest and kept only when the licence permits).
    ``captured_at`` is when it was captured; ``rights_basis`` is the recorded basis. ``subject_type`` /
    ``subject_id`` optionally link it to one of this business's observations.
    """

    source_key: str
    source_version: int
    content_kind: str
    content_ref: str
    raw_content: str
    captured_at: datetime
    rights_basis: str
    content_media_type: str = ""
    subject_type: str = ""
    subject_id: str = ""


def _normalize_subject_type(value: str) -> str:
    token = (value or "").strip().lower()
    try:
        return FieldSubjectType(token).value
    except ValueError as exc:
        raise EvidenceStoreError(
            REASON_UNKNOWN_SUBJECT_TYPE,
            f"unknown subject_type '{value}'; expected one of: {', '.join(SUBJECT_TYPES)}",
            detail=value,
        ) from exc


def _digest(raw_content: str) -> str:
    """A non-reversible SHA-256 (hex) fingerprint of the captured bytes — kept in BOTH modes so the
    evidence can be verified/deduplicated without retaining the raw payload."""
    return hashlib.sha256(raw_content.encode("utf-8")).hexdigest()


def decide_retention(terms: SourceTerms, content_kind: str) -> RetentionDecision:
    """Decide — deterministically, from the source terms — whether the raw payload may be retained.

    Raw retention requires BOTH the terms' ``store`` operation right (per-field override for
    ``content_kind`` wins over the source-wide default; deny-by-default) AND a retention policy that
    permits keeping raw content. It is refused — the object becomes ``REFERENCE_ONLY`` — when the
    ``store`` right is withheld (:data:`RETENTION_REASON_STORE_NOT_PERMITTED`), the retention policy is
    a named no-retention policy (:data:`RETENTION_REASON_NO_RETENTION_POLICY`), or the retention window
    is zero days (:data:`RETENTION_REASON_ZERO_WINDOW`). A pure, side-effect-free read.
    """
    op = SourceOperation.STORE.value
    # Per-field override first (so one content kind can be storable while another is reference-only),
    # then the source-wide default. Deny-by-default: absent from both => not granted.
    overrides = (terms.field_rights or {}).get(content_kind, {})
    if op in overrides:
        store_granted = bool(overrides[op])
    else:
        store_granted = bool((terms.operation_rights or {}).get(op, False))

    retention = terms.retention or {}
    policy = str(retention.get("policy", "")).strip().lower()
    raw_days = retention.get("max_days")
    try:
        max_days = None if raw_days is None else int(raw_days)
    except (TypeError, ValueError):
        max_days = None

    if not store_granted:
        reason = RETENTION_REASON_STORE_NOT_PERMITTED
    elif policy in _NO_RAW_RETENTION_POLICIES:
        reason = RETENTION_REASON_NO_RETENTION_POLICY
    elif max_days is not None and max_days <= 0:
        reason = RETENTION_REASON_ZERO_WINDOW
    else:
        return RetentionDecision(
            mode=EvidenceRetentionMode.RAW_RETAINED.value,
            raw_permitted=True,
            reason="",
            max_days=max_days,
        )
    return RetentionDecision(
        mode=EvidenceRetentionMode.REFERENCE_ONLY.value,
        raw_permitted=False,
        reason=reason,
        max_days=max_days,
    )


class EvidenceObjectStore:
    """Tenant-scoped reader/writer for evidence objects, bound to one business.

    Construct with the active session and the verified business scope (a route resolves it from the
    membership-verified tenant context). Every query is confined to that business — there is no
    argument to widen the scope.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def get(self, evidence_id: str) -> EvidenceObject | None:
        """Fetch one evidence object by id WITHIN this business. Another business's id (or an unknown
        id) resolves to ``None`` — a cross-tenant id reads exactly like not-found."""
        if not evidence_id:
            return None
        stmt = (
            select(EvidenceObject)
            .where(EvidenceObject.business_id == self._business_id)
            .where(EvidenceObject.id == evidence_id)
        )
        return self._session.scalars(stmt).one_or_none()

    def objects(self) -> list[EvidenceObject]:
        """Every evidence object for this business, oldest first."""
        stmt = (
            select(EvidenceObject)
            .where(EvidenceObject.business_id == self._business_id)
            .order_by(EvidenceObject.created_at, EvidenceObject.id)
        )
        return list(self._session.scalars(stmt).all())

    def for_subject(self, subject_type: str, subject_id: str) -> list[EvidenceObject]:
        """The evidence objects recorded about one subject in this business."""
        normalized_type = _normalize_subject_type(subject_type)
        stmt = (
            select(EvidenceObject)
            .where(EvidenceObject.business_id == self._business_id)
            .where(EvidenceObject.subject_type == normalized_type)
            .where(EvidenceObject.subject_id == subject_id)
            .order_by(EvidenceObject.created_at, EvidenceObject.id)
        )
        return list(self._session.scalars(stmt).all())

    # -- the one write path -------------------------------------------------------------------------

    def store(
        self,
        data: EvidenceInput,
        actor: AuditActor,
        *,
        now: datetime | None = None,
    ) -> EvidenceObject:
        """Validate the provenance, apply the license-aware retention screen, and stage ONE evidence
        object; return the (uncommitted) row. The caller owns the unit of work, so the row and its
        atomic audit entry commit together.

        Every check runs before the session is mutated. A capture is refused — and stages NO row —
        when it is missing its source reference, cites a source/version that does not exist, is missing
        the content kind / locator / raw payload / capture time / rights basis, or names a subject that
        does not exist in this business. The retention mode is DERIVED from the cited terms
        (:func:`decide_retention`): where raw retention is prohibited the raw payload is dropped
        (``raw_content`` stays ``None``) and only the locator + digest are kept. No prospect / host
        record is created.
        """
        moment = now or datetime.now(UTC)

        source_key = (data.source_key or "").strip()
        source_version = data.source_version
        if not source_key or source_version is None:
            raise EvidenceStoreError(
                REASON_MISSING_SOURCE_REFERENCE,
                "an evidence object must name the source_key and source_version it was captured from",
            )
        if not self._source_exists(source_key):
            raise EvidenceStoreError(
                REASON_SOURCE_NOT_FOUND,
                f"no catalogued source with key '{source_key}' exists",
                detail=source_key,
            )
        terms = self._source_terms(source_key, source_version)
        if terms is None:
            raise EvidenceStoreError(
                REASON_SOURCE_VERSION_NOT_FOUND,
                f"source '{source_key}' has no terms version {source_version}",
                detail=source_key,
            )

        content_kind = (data.content_kind or "").strip().lower()
        if not content_kind:
            raise EvidenceStoreError(
                REASON_MISSING_CONTENT_KIND, "a content_kind is required for an evidence object"
            )
        content_ref = (data.content_ref or "").strip()
        if not content_ref:
            raise EvidenceStoreError(
                REASON_MISSING_CONTENT_REF,
                "a content_ref (the locator preserved in either mode) is required",
                detail=content_kind,
            )
        raw_content = data.raw_content or ""
        if not raw_content.strip():
            raise EvidenceStoreError(
                REASON_MISSING_RAW_CONTENT,
                "raw_content is required so a content digest can be computed",
                detail=content_kind,
            )

        if data.captured_at is None:
            raise EvidenceStoreError(
                REASON_MISSING_CAPTURED_AT, "a captured_at timestamp is required"
            )

        rights_basis = (data.rights_basis or "").strip()
        if not rights_basis:
            raise EvidenceStoreError(
                REASON_MISSING_RIGHTS_BASIS,
                "a rights_basis is required (the recorded rights/retention basis for the capture)",
                detail=content_kind,
            )

        # Optional subject linkage — resolved WITHIN this business (cross-tenant/unknown => refused).
        subject_type = (data.subject_type or "").strip()
        subject_id = (data.subject_id or "").strip()
        if subject_type or subject_id:
            subject_type = _normalize_subject_type(subject_type)
            if not subject_id or not self._subject_exists(subject_type, subject_id):
                raise EvidenceStoreError(
                    REASON_SUBJECT_NOT_FOUND,
                    f"{subject_type or 'subject'} '{subject_id}' does not exist in this business",
                    detail=subject_id,
                )

        # License-aware retention screen — DERIVED from the cited terms, never caller-chosen.
        decision = decide_retention(terms, content_kind)
        content_digest = _digest(raw_content)
        # Raw payload kept ONLY when permitted; a no-retention source leaves no raw copy.
        stored_raw = raw_content if decision.raw_permitted else None
        retention_until: datetime | None = None
        if decision.raw_permitted and decision.max_days is not None and decision.max_days > 0:
            retention_until = data.captured_at + timedelta(days=decision.max_days)

        evidence_id = str(uuid.uuid4())
        evidence = EvidenceObject(
            id=evidence_id,
            business_id=self._business_id,
            source_key=source_key,
            source_version=source_version,
            subject_type=subject_type,
            subject_id=subject_id,
            content_kind=content_kind,
            content_ref=content_ref,
            content_digest=content_digest,
            content_media_type=(data.content_media_type or "").strip(),
            raw_content=stored_raw,
            retention_mode=decision.mode,
            retention_reason=decision.reason,
            rights_basis=rights_basis,
            captured_at=data.captured_at,
            retention_until=retention_until,
            created_at=moment,
        )
        self._session.add(evidence)

        # Atomic audit (SEC-001/BR-122): staged into the caller's session so it commits with the row.
        # References stay OPAQUE — ids, source key+version, content kind, retention mode+reason and the
        # SAFE digest. The raw payload and the locator (content_ref) are NEVER audited.
        AuditSink(self._session, business_id=self._business_id, actor=actor).record(
            action=AUDIT_ACTION_STORE_EVIDENCE,
            reason=f"stored {decision.mode} evidence object '{evidence_id}'",
            references={
                "evidenceId": evidence_id,
                "sourceKey": source_key,
                "sourceVersion": source_version,
                "contentKind": content_kind,
                "contentDigest": content_digest,
                "retentionMode": decision.mode,
                "retentionReason": decision.reason,
                "rawRetained": decision.raw_permitted,
                "subjectType": subject_type,
                "subjectId": subject_id,
                "hasRetentionUntil": retention_until is not None,
            },
        )
        return evidence

    # -- internals ----------------------------------------------------------------------------------

    def _source_exists(self, source_key: str) -> bool:
        # The source catalog is platform control-plane data (not tenant-scoped); provenance may point
        # at any catalogued source — this only verifies the reference is real.
        stmt = select(Source.id).where(Source.key == source_key).limit(1)
        return self._session.scalars(stmt).first() is not None

    def _source_terms(self, source_key: str, version: int) -> SourceTerms | None:
        stmt = (
            select(SourceTerms)
            .where(SourceTerms.source_key == source_key)
            .where(SourceTerms.version == version)
            .limit(1)
        )
        return self._session.scalars(stmt).one_or_none()

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
