"""DCA-086 — prevent reacquisition of restricted identities (protected minimal tombstones).

Key: ``tombstone`` · source sections 5, 10, 11.

DCA-084 erases a subject's operational data and DCA-085 propagates that deletion outward — but neither
stops the NEXT discovery cycle from fetching the same identity from the same source and silently
*resurrecting* it. This module closes that gap with a **protected minimal tombstone**: a durable
"do not re-acquire this identity" marker that outlives the deletion, plus the **scope-aware
comparison** the discovery cycle consults before suggesting a candidate.

Two pieces, mirroring the DCA-078/084 writer style:

- :class:`RestrictedIdentityTombstoneStore` — the single sanctioned writer. For ONE of a business's
  identities it records a :class:`~app.db.models.RestrictedIdentityTombstone` carrying only a
  deterministic, pseudonymous :func:`fingerprint_identity` of the identifier (the raw identifier is
  NEVER stored), the sourcing ``origin`` it speaks for, a declared ``purpose`` and ``retention``, and
  opaque provenance. Authority is checked FIRST (the SAME ``data.erase`` administrator authority that
  executes an erasure — **no new scope**), so a relevant denial writes nothing. Insert-only and
  idempotent: re-restricting the same identity reuses the existing tombstone.

- :func:`reacquisition_decision` — the deterministic, read-only, scope-aware comparison. It fingerprints
  a candidate identity and asks whether an ACTIVE tombstone exists for the SAME business, subject kind,
  ``origin`` AND fingerprint. A match blocks reacquisition (stable reason :data:`REASON_RESTRICTED_IDENTITY`);
  a different identity, a different origin, a lapsed tombstone or a different tenant does not — so
  unrelated lawful scoped data is preserved.

**The fingerprint is a pseudonym, never anonymous.** :func:`fingerprint_identity` is a plain SHA-256
over the normalized identifier, so it is *re-derivable from the identifier* — it is personal data in
pseudonymous form, not anonymised data. It is treated as such everywhere: the tombstone is tenant-owned
(RLS-protected), minimised, retained only for the declared purpose, never exported, and the audit trail
records the tombstone's provenance WITHOUT the fingerprint. Calling the hash "anonymous" would be
exactly the mislabelling this story forbids.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.sink import ActorType, AuditActor, AuditSink
from app.db.models import (
    FieldSubjectType,
    ReacquisitionPurpose,
    RestrictedIdentityOrigin,
    RestrictedIdentityTombstone,
    RestrictionRetentionMode,
)
from app.identity.permissions import Scope, scopes_for_business_roles

# The controlled vocabularies the writer accepts.
SUBJECT_TYPES: tuple[str, ...] = tuple(t.value for t in FieldSubjectType)
ORIGINS: tuple[str, ...] = tuple(o.value for o in RestrictedIdentityOrigin)
PURPOSES: tuple[str, ...] = tuple(p.value for p in ReacquisitionPurpose)
RETENTION_MODES: tuple[str, ...] = tuple(m.value for m in RestrictionRetentionMode)

# The fingerprint algorithm label recorded on every tombstone (explicitly a documented pseudonym).
HASH_ALGORITHM = "sha256"

# Audited action code for recording a reacquisition tombstone (a controlled code).
AUDIT_ACTION_RECORD_TOMBSTONE = "reacquisition.tombstone.record"

# The stable reason a reacquisition is blocked (auditable, test-asserted) — mirrors the rulepack codes.
REASON_RESTRICTED_IDENTITY = "restricted_identity"

# Stable refusal codes. Each is raised BEFORE the session is mutated, so a refusal stages no row.
REASON_NOT_AUTHORIZED = "reacquisition_tombstone_not_authorized"
REASON_UNKNOWN_SUBJECT_TYPE = "unknown_subject_type"
REASON_UNKNOWN_ORIGIN = "unknown_origin"
REASON_UNKNOWN_PURPOSE = "unknown_purpose"
REASON_MISSING_IDENTIFIER = "missing_identifier"
REASON_UNKNOWN_RETENTION_MODE = "unknown_retention_mode"
REASON_MISSING_RETAIN_UNTIL = "missing_retain_until"
REASON_UNEXPECTED_RETAIN_UNTIL = "unexpected_retain_until"

# A unit-separator byte joins the fingerprint's parts so distinct inputs can never alias (e.g. the
# pair ("organization", "x") and ("organizationx", "") produce different digests).
_SEP = "\x1f"


def fingerprint_identity(subject_type: str, scheme: str, raw_identifier: str) -> str:
    """The deterministic, PSEUDONYMOUS fingerprint of one identity (hex SHA-256).

    Normalizes the identifier (trim + case-fold, so ``U123`` and ``u123 `` collide the way a registry id
    legitimately should) and hashes it together with the subject kind and the identifier scheme, so the
    SAME identity hashes to the SAME value at restriction time and at reimport time — which is what makes
    the scope-aware comparison work. The digest is *re-derivable from the identifier*, so it is personal
    data in pseudonymous form (NOT anonymous); callers treat it accordingly (minimise, scope, don't
    export, keep out of the audit trail).
    """
    normalized = (raw_identifier or "").strip().casefold()
    probe = f"{(subject_type or '').strip().lower()}{_SEP}{(scheme or '').strip().lower()}{_SEP}{normalized}"
    return hashlib.sha256(probe.encode("utf-8")).hexdigest()


def _normalize_subject_type(value: str) -> str:
    token = (value or "").strip().lower()
    try:
        return FieldSubjectType(token).value
    except ValueError as exc:
        raise ReacquisitionError(
            REASON_UNKNOWN_SUBJECT_TYPE,
            f"unknown subject_type '{value}'; expected one of: {', '.join(SUBJECT_TYPES)}",
            detail=value,
        ) from exc


def _normalize_origin(value: str) -> str:
    token = (value or "").strip().lower()
    try:
        return RestrictedIdentityOrigin(token).value
    except ValueError as exc:
        raise ReacquisitionError(
            REASON_UNKNOWN_ORIGIN,
            f"unknown origin '{value}'; expected one of: {', '.join(ORIGINS)}",
            detail=value,
        ) from exc


def _normalize_purpose(value: str) -> str:
    token = (value or "").strip().lower() or ReacquisitionPurpose.PREVENT_REACQUISITION.value
    try:
        return ReacquisitionPurpose(token).value
    except ValueError as exc:
        raise ReacquisitionError(
            REASON_UNKNOWN_PURPOSE,
            f"unknown purpose '{value}'; expected one of: {', '.join(PURPOSES)}",
            detail=value,
        ) from exc


def _normalize_retention(
    retention_mode: str, retain_until: datetime | None
) -> tuple[str, datetime | None]:
    """Validate retention metadata the same way the DCA-078 restriction ledger does.

    ``until_date`` REQUIRES a ``retain_until``; ``until_lifted`` must NOT carry one (it is indefinite).
    An unknown mode is refused. Each refusal is raised before the session is mutated.
    """
    token = (retention_mode or "").strip().lower() or RestrictionRetentionMode.UNTIL_LIFTED.value
    try:
        mode = RestrictionRetentionMode(token)
    except ValueError as exc:
        raise ReacquisitionError(
            REASON_UNKNOWN_RETENTION_MODE,
            f"unknown retention_mode '{retention_mode}'; expected one of: {', '.join(RETENTION_MODES)}",
            detail=retention_mode,
        ) from exc
    if mode is RestrictionRetentionMode.UNTIL_DATE and retain_until is None:
        raise ReacquisitionError(
            REASON_MISSING_RETAIN_UNTIL,
            "retention_mode 'until_date' requires a retain_until timestamp",
        )
    if mode is RestrictionRetentionMode.UNTIL_LIFTED and retain_until is not None:
        raise ReacquisitionError(
            REASON_UNEXPECTED_RETAIN_UNTIL,
            "retention_mode 'until_lifted' is indefinite and must not carry a retain_until",
        )
    return mode.value, retain_until


def _is_active(tombstone: RestrictedIdentityTombstone, now: datetime) -> bool:
    """Is a tombstone still in force at ``now``?

    ``until_lifted`` stands indefinitely (lifting is a disabled future capability). ``until_date`` is in
    force up to and including ``retain_until``; past it the tombstone has lapsed and no longer blocks.
    """
    if tombstone.retention_mode == RestrictionRetentionMode.UNTIL_DATE.value:
        ru = tombstone.retain_until
        if ru is None:
            return True
        if ru.tzinfo is None:  # SQLite returns naive datetimes — normalize before comparing.
            ru = ru.replace(tzinfo=UTC)
        return now <= ru
    return True


class ReacquisitionError(Exception):
    """A tombstone write was refused with a stable ``code``. Raised before the session is mutated, so a
    refusal leaves no row and no partial state. ``detail`` names the offending value when useful (never
    a raw identifier/contact value)."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class ReacquisitionAuthorizationError(ReacquisitionError):
    """The caller is not authorized to record a reacquisition tombstone.

    A distinct type so an HTTP boundary can map it to a 403 while the other codes map to a 422. Raised
    before anything is read or written. ``code`` is :data:`REASON_NOT_AUTHORIZED`.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


@dataclass(frozen=True)
class ReacquisitionPrincipal:
    """The business principal recording a reacquisition tombstone.

    ``subject`` is an opaque actor id (never a name/email) recorded on the tombstone and its atomic audit
    entry. ``roles`` are the caller's business-plane membership roles (from the verified
    :class:`~app.identity.tenant_context.TenantContext`, NEVER a bare token claim). Preventing
    reacquisition is part of the restriction/erasure authority, so :meth:`may_restrict_reacquisition`
    reuses the SAME :attr:`~app.identity.permissions.Scope.DATA_ERASE` administrator scope (no new scope):
    an ordinary acting user and a read-only viewer do not hold it, and a platform operator holds no
    business scope for the tenant.
    """

    subject: str
    roles: tuple[str, ...] = ()
    actor_type: str = "user"

    def may_restrict_reacquisition(self) -> bool:
        return Scope.DATA_ERASE in scopes_for_business_roles(self.roles)

    def as_actor(self) -> AuditActor:
        return AuditActor(id=self.subject, type=ActorType(self.actor_type))


@dataclass(frozen=True)
class ReacquisitionDecision:
    """The scope-aware comparison's verdict for one candidate identity (pure, read-only).

    ``blocked`` is True ONLY when an ACTIVE tombstone matches this business + subject kind + origin +
    fingerprint. ``tombstone_id`` is the matched tombstone (for audit/explainability) or ``None``.
    """

    blocked: bool
    reason: str
    tombstone_id: str | None = None

    OK = "ok"


class RestrictedIdentityTombstoneStore:
    """The single sanctioned reader/writer of reacquisition tombstones, bound to one business.

    Construct with the active session and the verified business scope (a route resolves it from the
    membership-verified tenant context). Every query is confined to that business — there is no argument
    to widen the scope. Stages into the caller's session and performs no I/O of its own (never commits/
    flushes/opens a transaction); the caller owns the unit of work.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def get(self, tombstone_id: str) -> RestrictedIdentityTombstone | None:
        """Fetch one tombstone by id WITHIN this business. A cross-tenant/unknown id resolves to ``None``."""
        if not tombstone_id:
            return None
        stmt = (
            select(RestrictedIdentityTombstone)
            .where(RestrictedIdentityTombstone.business_id == self._business_id)
            .where(RestrictedIdentityTombstone.id == tombstone_id)
        )
        return self._session.scalars(stmt).one_or_none()

    def tombstones(self) -> list[RestrictedIdentityTombstone]:
        """Every reacquisition tombstone for this business, oldest first — the reviewable ledger."""
        stmt = (
            select(RestrictedIdentityTombstone)
            .where(RestrictedIdentityTombstone.business_id == self._business_id)
            .order_by(RestrictedIdentityTombstone.created_at, RestrictedIdentityTombstone.id)
        )
        return list(self._session.scalars(stmt).all())

    def _existing(
        self, subject_type: str, origin: str, identity_hash: str
    ) -> RestrictedIdentityTombstone | None:
        stmt = (
            select(RestrictedIdentityTombstone)
            .where(RestrictedIdentityTombstone.business_id == self._business_id)
            .where(RestrictedIdentityTombstone.subject_type == subject_type)
            .where(RestrictedIdentityTombstone.origin == origin)
            .where(RestrictedIdentityTombstone.identity_hash == identity_hash)
        )
        return self._session.scalars(stmt).one_or_none()

    # -- the one write path -------------------------------------------------------------------------

    def record(
        self,
        subject_type: str,
        identifier: str,
        principal: ReacquisitionPrincipal,
        *,
        origin: str = RestrictedIdentityOrigin.PLATFORM.value,
        identity_scheme: str = "",
        purpose: str = ReacquisitionPurpose.PREVENT_REACQUISITION.value,
        retention_mode: str = RestrictionRetentionMode.UNTIL_LIFTED.value,
        retain_until: datetime | None = None,
        origin_kind: str = "",
        origin_ref: str = "",
        now: datetime | None = None,
    ) -> RestrictedIdentityTombstone:
        """Authorize, validate and stage a reacquisition tombstone; return the (uncommitted) row.

        Authorization is checked FIRST: a caller whose roles lack
        :attr:`~app.identity.permissions.Scope.DATA_ERASE` is refused with
        :class:`ReacquisitionAuthorizationError` before any state is read or written (the relevant
        denial). Then the subject type / origin / purpose / retention are validated and a non-empty
        identifier is required — each refusal is raised before the session is mutated, so it stages NO
        row. The raw ``identifier`` is fingerprinted immediately and NEVER stored. On success a tombstone
        plus an atomic DCA-026 audit entry (opaque references, WITHOUT the fingerprint) are staged in the
        SAME unit of work. Idempotent: if a tombstone for the SAME (subject kind, origin, fingerprint)
        already exists in this business it is returned unchanged (no duplicate row, no second audit).
        """
        moment = now or datetime.now(UTC)

        # 1) Authority FIRST — an unauthorized caller is refused with no state touched.
        if not principal.may_restrict_reacquisition():
            raise ReacquisitionAuthorizationError(
                REASON_NOT_AUTHORIZED,
                "recording a reacquisition tombstone requires a business administrator holding "
                f"'{Scope.DATA_ERASE.value}'; an ordinary user or a read-only viewer cannot perform it, "
                "and a platform operator holds no business scope for this tenant",
            )

        normalized_type = _normalize_subject_type(subject_type)
        normalized_origin = _normalize_origin(origin)
        normalized_purpose = _normalize_purpose(purpose)
        mode, normalized_retain = _normalize_retention(retention_mode, retain_until)

        raw = (identifier or "").strip()
        if not raw:
            raise ReacquisitionError(
                REASON_MISSING_IDENTIFIER,
                "a reacquisition tombstone must name the identity to restrict",
            )

        scheme = (identity_scheme or "").strip().lower()
        identity_hash = fingerprint_identity(normalized_type, scheme, raw)

        # Idempotent: re-restricting the SAME identity reuses the existing tombstone (no duplicate row).
        existing = self._existing(normalized_type, normalized_origin, identity_hash)
        if existing is not None:
            return existing

        tombstone = RestrictedIdentityTombstone(
            id=str(uuid.uuid4()),
            business_id=self._business_id,
            subject_type=normalized_type,
            identity_scheme=scheme,
            identity_hash=identity_hash,
            hash_algorithm=HASH_ALGORITHM,
            origin=normalized_origin,
            purpose=normalized_purpose,
            retention_mode=mode,
            retain_until=normalized_retain,
            origin_kind=(origin_kind or "").strip(),
            origin_ref=(origin_ref or "").strip(),
            authority_plane="business",
            authority_scope=Scope.DATA_ERASE.value,
            created_by=principal.subject,
            created_by_type=principal.actor_type,
            created_at=moment,
        )
        self._session.add(tombstone)

        # Atomic audit (SEC-001/BR-122): staged into the caller's session so it commits with the row.
        # References stay OPAQUE — the tombstone id, subject kind, origin, purpose, scheme, provenance —
        # and DELIBERATELY omit the identity fingerprint (a pseudonym is still personal data; the audit
        # trail keeps only the minimal provenance it needs, never the identity itself).
        AuditSink(self._session, business_id=self._business_id, actor=principal.as_actor()).record(
            action=AUDIT_ACTION_RECORD_TOMBSTONE,
            reason=(
                f"recorded reacquisition tombstone '{tombstone.id}' for a {normalized_type} identity "
                f"({normalized_origin}-origin)"
            ),
            references={
                "tombstoneId": tombstone.id,
                "subjectType": normalized_type,
                "origin": normalized_origin,
                "identityScheme": scheme,
                "purpose": normalized_purpose,
                "retentionMode": mode,
                "originKind": tombstone.origin_kind,
                "originRef": tombstone.origin_ref,
                "authorityPlane": "business",
                "authorityScope": Scope.DATA_ERASE.value,
            },
        )
        return tombstone


def reacquisition_decision(
    session: Session,
    business_id: str,
    subject_type: str,
    identifier: str,
    origin: str,
    *,
    identity_scheme: str = "",
    now: datetime | None = None,
) -> ReacquisitionDecision:
    """The deterministic, scope-aware comparison: is re-acquiring this identity blocked?

    Fingerprints the candidate identity and looks for an ACTIVE tombstone for the SAME business, subject
    kind, ``origin`` and fingerprint. A match returns ``blocked=True`` with
    :data:`REASON_RESTRICTED_IDENTITY`; anything else (a different identity, a different origin, a lapsed
    tombstone, another tenant — the scope is enforced in the query) returns ``blocked=False``. Pure and
    read-only: it reads nothing it mutates and changes nothing, so both the block and the allow are
    perfectly reproducible, and unrelated lawful scoped data is preserved.
    """
    moment = now or datetime.now(UTC)
    normalized_type = (subject_type or "").strip().lower()
    normalized_origin = (origin or "").strip().lower()
    scheme = (identity_scheme or "").strip().lower()
    raw = (identifier or "").strip()
    if not raw:
        # Nothing to compare — a candidate without an identifier cannot be matched to a tombstone.
        return ReacquisitionDecision(False, ReacquisitionDecision.OK)

    identity_hash = fingerprint_identity(normalized_type, scheme, raw)
    stmt = (
        select(RestrictedIdentityTombstone)
        .where(RestrictedIdentityTombstone.business_id == business_id)
        .where(RestrictedIdentityTombstone.subject_type == normalized_type)
        .where(RestrictedIdentityTombstone.origin == normalized_origin)
        .where(RestrictedIdentityTombstone.identity_hash == identity_hash)
    )
    for tombstone in session.scalars(stmt).all():
        if _is_active(tombstone, moment):
            return ReacquisitionDecision(
                True, REASON_RESTRICTED_IDENTITY, tombstone_id=tombstone.id
            )
    return ReacquisitionDecision(False, ReacquisitionDecision.OK)
