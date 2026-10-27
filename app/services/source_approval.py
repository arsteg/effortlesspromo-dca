"""DCA-046 — approve a source configuration (platform-operator review of source terms).

:class:`SourceApprovalStore` is the single sanctioned writer of
:class:`~app.db.models.SourceTermsApproval`. Approving a source *configuration* is the capability
that lets a reviewed :class:`~app.db.models.SourceTerms` version (DCA-045) be accepted independently:
a **platform operator** records a review carrying the four essentials — evidence, reviewer, review
date and expiry — and the endorsed terms version is promoted from ``draft`` to ``active``.

The load-bearing guarantees (the DCA-046 acceptance criteria):

- **Approval is a PLATFORM-operator action; a tenant administrator can never perform it.** Authority
  is the platform-plane scope :attr:`~app.identity.permissions.Scope.SOURCE_TERMS_APPROVE`, resolved
  from the caller's *platform* roles. A business administrator holds only business-plane scopes, so
  their roles yield no platform scope and the approval is refused BEFORE any state is read or
  written — the shared catalog is control-plane data, not a tenant resource.

- **An approval does not exceed the documented field/use rights.** The approval endorses exactly the
  rights the terms version documents (DCA-045). A caller may optionally name the rights they intend
  to approve; if that set grants a ``(field, operation)`` the version does not document as granted,
  the approval is refused (:data:`REASON_APPROVAL_EXCEEDS_RIGHTS`). The stored snapshot is always the
  documented rights, so the approval can never silently widen them.

- **Incomplete reviews are rejected before anything is staged** (stable codes): an approval for a
  terms version that does not exist, or missing its reviewer, evidence, review date or expiry, or an
  expiry at/before the review/effective date, or an attempt to re-approve an already-approved version,
  each raises :class:`SourceApprovalError` before any row is staged, so a refusal leaves no approval
  and no promotion.

Like :class:`~app.services.source_terms.SourceTermsStore`, this is platform CONTROL-PLANE data (a
shared registry, not tenant-scoped), so the store is constructed with just the session — there is no
business scope and no tenant audit entry (the approval row itself is the durable who/when/why record).
It stages into the caller's session and never commits; the caller owns the unit of work.

Promoting a terms version to ``active`` grants no acquisition right on its own: the source catalog
entry must additionally be ENABLED (DCA-044), which stays a disabled future capability. So an
approval never, by itself, authorizes acquisition — future capabilities stay disabled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import overload

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    SourceApprovalStatus,
    SourceTerms,
    SourceTermsApproval,
    SourceTermsStatus,
)
from app.identity.permissions import Scope, scopes_for_platform_roles

# Authorization refusal (raised FIRST, before any state is read or written, so an unauthorized caller
# — notably a tenant administrator — learns nothing about the configuration and changes nothing).
REASON_NOT_PLATFORM_OPERATOR = "not_platform_operator"

# Incomplete-review refusals (each raised BEFORE the session is mutated, so a refusal stages nothing).
REASON_TERMS_NOT_FOUND = "terms_version_not_found"
REASON_MISSING_REVIEWER = "missing_reviewer"
REASON_MISSING_EVIDENCE = "missing_review_evidence"
REASON_MISSING_REVIEW_DATE = "missing_review_date"
REASON_MISSING_EXPIRY = "missing_expiry"
REASON_INVALID_EXPIRY = "invalid_expiry"
REASON_APPROVAL_EXCEEDS_RIGHTS = "approval_exceeds_rights"
REASON_ALREADY_APPROVED = "already_approved"


class SourceApprovalAuthorizationError(Exception):
    """The caller is not authorized to approve a source configuration (not a platform operator).

    Raised BEFORE any configuration is read or staged, so a tenant administrator's attempt leaves no
    trace and reveals nothing about the catalog. ``code`` is :data:`REASON_NOT_PLATFORM_OPERATOR`.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = REASON_NOT_PLATFORM_OPERATOR


class SourceApprovalError(Exception):
    """An approval was refused with a stable code. Raised before the session is mutated, so a refusal
    leaves no approval row and no promotion. ``detail`` names the offending value."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ReviewerPrincipal:
    """The platform principal performing the approval.

    ``roles`` are the caller's PLATFORM-plane roles (from the authenticated, signed principal — never
    a bare header). :meth:`SourceApprovalStore.approve` grants the action only when these roles yield
    :attr:`~app.identity.permissions.Scope.SOURCE_TERMS_APPROVE`; a tenant administrator's business
    roles never do. ``subject`` is an opaque operator id recorded as the reviewer.
    """

    subject: str
    roles: tuple[str, ...] = ()

    def may_approve(self) -> bool:
        return Scope.SOURCE_TERMS_APPROVE in scopes_for_platform_roles(self.roles)


@dataclass
class SourceApprovalReview:
    """One source-configuration review as supplied by the operator.

    ``evidence`` is the operator's sign-off evidence (``{reference, kind, uri}``; a non-empty
    ``reference`` is required). ``reviewed_at`` is the review date and ``expires_at`` the approval's
    expiry — both required. ``effective_from`` optionally delays when the approval takes effect.

    ``requested_operation_rights`` / ``requested_field_rights`` are OPTIONAL: when given, they are the
    rights the operator intends to endorse and are checked against the version's documented rights —
    an attempt to approve a right the version does not document is refused. When omitted, the approval
    endorses exactly the documented rights. The stored snapshot is always the documented rights.
    """

    reviewer_id: str
    evidence: dict = field(default_factory=dict)
    reviewed_at: datetime | None = None
    expires_at: datetime | None = None
    effective_from: datetime | None = None
    reviewer_type: str = "platform_operator"
    requested_operation_rights: dict[str, bool] | None = None
    requested_field_rights: dict[str, dict[str, bool]] | None = None


def _validate_evidence(evidence: dict) -> dict:
    """An approval must carry the evidence the operator relied on — a bare approval is refused."""
    evidence = evidence or {}
    reference = str(evidence.get("reference", "")).strip()
    if not reference:
        raise SourceApprovalError(
            REASON_MISSING_EVIDENCE,
            "review evidence with a non-empty 'reference' is required to approve a configuration",
        )
    return {
        "reference": reference,
        "kind": str(evidence.get("kind", "")).strip(),
        "uri": str(evidence.get("uri", "")).strip(),
    }


@overload
def _as_utc(value: datetime) -> datetime: ...
@overload
def _as_utc(value: None) -> None: ...
@overload
def _as_utc(value: datetime | None) -> datetime | None: ...
def _as_utc(value: datetime | None) -> datetime | None:
    """Normalize a (possibly naive — SQLite) datetime to tz-aware UTC for comparison."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)  # SQLite returns naive datetimes
    return value


def _exceeds(requested: dict | None, documented: dict | None) -> bool:
    """True if ``requested`` grants any operation ``documented`` does not (deny-by-default compare)."""
    for op, granted in (requested or {}).items():
        if granted and not bool((documented or {}).get(op, False)):
            return True
    return False


class SourceApprovalStore:
    """Reader/writer for the platform source-configuration approval registry (DCA-046)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- reads --------------------------------------------------------------------------------------

    def approval_for(self, source_key: str, terms_version: int) -> SourceTermsApproval | None:
        """The approval endorsing one ``(source_key, terms_version)``, or ``None``."""
        stmt = (
            select(SourceTermsApproval)
            .where(SourceTermsApproval.source_key == (source_key or "").strip())
            .where(SourceTermsApproval.terms_version == terms_version)
        )
        return self._session.scalars(stmt).one_or_none()

    def approvals(self, source_key: str) -> list[SourceTermsApproval]:
        """All approvals recorded for one source, oldest version first."""
        stmt = (
            select(SourceTermsApproval)
            .where(SourceTermsApproval.source_key == (source_key or "").strip())
            .order_by(SourceTermsApproval.terms_version)
        )
        return list(self._session.scalars(stmt).all())

    @staticmethod
    def is_in_force(approval: SourceTermsApproval, *, now: datetime | None = None) -> bool:
        """True only for an ACTIVE approval evaluated inside its own validity window.

        An approval's expiry is explicit: outside ``[effective_from, expires_at)`` it endorses
        nothing, exactly like a terms version's window (DCA-045). A revoked approval is never in force.
        """
        if approval.status != SourceApprovalStatus.ACTIVE.value:
            return False
        moment = now or datetime.now(UTC)
        effective_from = _as_utc(approval.effective_from)
        expires_at = _as_utc(approval.expires_at)
        if effective_from is not None and moment < effective_from:
            return False
        if expires_at is not None and moment >= expires_at:
            return False
        return True

    # -- the one write path -------------------------------------------------------------------------

    def _require_terms(self, source_key: str, terms_version: int) -> SourceTerms:
        key = (source_key or "").strip()
        stmt = (
            select(SourceTerms)
            .where(SourceTerms.source_key == key)
            .where(SourceTerms.version == terms_version)
        )
        terms = self._session.scalars(stmt).one_or_none()
        if terms is None:
            raise SourceApprovalError(
                REASON_TERMS_NOT_FOUND,
                f"no terms version {terms_version} exists for source '{key}'; record it first "
                "(DCA-045)",
                detail=f"{key}@v{terms_version}",
            )
        return terms

    def approve(
        self,
        source_key: str,
        terms_version: int,
        review: SourceApprovalReview,
        principal: ReviewerPrincipal,
    ) -> SourceTermsApproval:
        """Approve one source-terms configuration version; return the approval (uncommitted).

        Authorization is checked FIRST: only a platform operator holding
        :attr:`~app.identity.permissions.Scope.SOURCE_TERMS_APPROVE` may approve — a tenant
        administrator is refused before any state is read or written. Then the review is validated
        (the version exists, reviewer / evidence / review date / expiry are present, the expiry is
        coherent, the endorsed rights do not exceed the documented rights, and the version is not
        already approved); any failure raises before a row is staged. On success the approval row is
        staged AND the endorsed :class:`SourceTerms` version is promoted ``draft`` → ``active``.

        Promotion grants no acquisition right: the source catalog entry must additionally be ENABLED
        (DCA-044), a disabled future capability, so an approval never authorizes acquisition on its own.
        """
        # 1) Authority FIRST — a tenant admin (or any non-operator) is refused with no state touched.
        if not principal.may_approve():
            raise SourceApprovalAuthorizationError(
                "approving a source configuration requires a platform operator "
                f"({Scope.SOURCE_TERMS_APPROVE.value}); a tenant administrator cannot perform it"
            )

        # 2) The version being approved must exist (DCA-045 recorded it).
        terms = self._require_terms(source_key, terms_version)
        key = terms.source_key

        # 3) The four review essentials must all be present and coherent (reject incomplete reviews).
        reviewer_id = (review.reviewer_id or "").strip()
        if not reviewer_id:
            raise SourceApprovalError(
                REASON_MISSING_REVIEWER,
                "an approval must name the reviewer (an operator subject id)",
            )
        evidence = _validate_evidence(review.evidence)
        if review.reviewed_at is None:
            raise SourceApprovalError(
                REASON_MISSING_REVIEW_DATE, "an approval must record the review date (reviewed_at)"
            )
        if review.expires_at is None:
            raise SourceApprovalError(
                REASON_MISSING_EXPIRY, "an approval must carry an explicit expiry (expires_at)"
            )
        reviewed_at = _as_utc(review.reviewed_at)
        expires_at = _as_utc(review.expires_at)
        effective_from = _as_utc(review.effective_from)
        # Expiry must be after the review date (and after any delayed effective date).
        lower_bound = max(d for d in (reviewed_at, effective_from) if d is not None)
        if expires_at <= lower_bound:
            raise SourceApprovalError(
                REASON_INVALID_EXPIRY,
                "expires_at must be after the review date (and any effective_from)",
                detail=f"{lower_bound.isoformat()}..{expires_at.isoformat()}",
            )

        # 4) The approval cannot exceed the documented field/use rights (DCA-045).
        if _exceeds(review.requested_operation_rights, terms.operation_rights):
            raise SourceApprovalError(
                REASON_APPROVAL_EXCEEDS_RIGHTS,
                "an approval cannot grant an operation the terms version does not document",
            )
        for fld, ops in (review.requested_field_rights or {}).items():
            documented_field = (terms.field_rights or {}).get(fld, {})
            # A field override may grant an operation that falls back to the source-wide default, so
            # compare against the effective documented grant (field override OR source-wide default).
            effective_documented = {
                op: bool(documented_field.get(op, (terms.operation_rights or {}).get(op, False)))
                for op in (ops or {})
            }
            if _exceeds(ops, effective_documented):
                raise SourceApprovalError(
                    REASON_APPROVAL_EXCEEDS_RIGHTS,
                    f"an approval cannot grant a right on field '{fld}' the version does not document",
                    detail=fld,
                )

        # 5) A version is approved at most once (defence-in-depth over the unique constraint).
        if self.approval_for(key, terms_version) is not None:
            raise SourceApprovalError(
                REASON_ALREADY_APPROVED,
                f"terms version {terms_version} for source '{key}' is already approved",
                detail=f"{key}@v{terms_version}",
            )

        # All checks passed — stage the approval AND promote the endorsed version to active. The
        # stored snapshot is the DOCUMENTED rights, so the approval records exactly what it endorsed.
        approval = SourceTermsApproval(
            source_key=key,
            terms_version=terms_version,
            reviewer_id=reviewer_id,
            reviewer_type=(review.reviewer_type or "platform_operator").strip()
            or "platform_operator",
            review_evidence=evidence,
            reviewed_at=reviewed_at,
            effective_from=effective_from,
            expires_at=expires_at,
            approved_operation_rights=dict(terms.operation_rights or {}),
            approved_field_rights=dict(terms.field_rights or {}),
            status=SourceApprovalStatus.ACTIVE.value,
        )
        self._session.add(approval)
        # Promotion: the reviewed version is now in force (DCA-045 left it a draft; this story is the
        # one that activates it). The source catalog entry staying ENABLED is a separate, disabled
        # capability, so this does not authorize acquisition.
        terms.status = SourceTermsStatus.ACTIVE.value
        return approval
