"""DCA-053 — approve a policy version (platform-operator review of a compliance policy rulepack).

:class:`PolicyApprovalStore` is the single sanctioned writer of
:class:`~app.db.models.CompliancePolicyApproval`. Approving a *policy version* is the capability that
lets a reviewed :class:`~app.db.models.CompliancePolicyRulepack` version (DCA-052) be accepted
independently: a **platform operator** records a review carrying the four essentials — the references
it relied on, the reviewer, the review date and an explicit review expiry — and the endorsed rulepack
version is promoted from ``draft`` to ``active``.

The load-bearing guarantees (the DCA-053 acceptance criteria):

- **Draft/expired packs never authorize production behavior.** :meth:`PolicyApprovalStore.evaluate`
  answers whether a version is IN FORCE (approved, ACTIVE, and inside its approval's explicit window)
  without consulting the rules themselves — it authors no legal conclusion. A draft (unapproved)
  version is denied (:data:`REASON_NOT_APPROVED`); an approval past its review expiry is denied
  (:data:`REASON_EXPIRED`); an approval not yet effective is denied (:data:`REASON_NOT_YET_EFFECTIVE`).
  The policy ENGINE that would interpret an in-force pack's rules stays a disabled future capability.

- **Changing approved content requires a new version.** The approval is bound to the EXACT content it
  reviewed via a stable :func:`content_fingerprint`. The DCA-052 store is insert-only, so editing
  approved content means recording a NEW rulepack version — a fresh DRAFT, unapproved until separately
  reviewed. An approval can never silently follow an edited pack, and a version is approved at most
  once (:data:`REASON_ALREADY_APPROVED`, over the unique constraint).

- **Approval is a PLATFORM-operator action; a tenant administrator can never perform it.** Authority
  is the platform-plane scope :attr:`~app.identity.permissions.Scope.POLICY_RULEPACK_APPROVE`, resolved
  from the caller's *platform* roles. A business administrator holds only business-plane scopes, so
  their roles yield no platform scope and the approval is refused BEFORE any state is read or written.

- **Incomplete reviews are rejected before anything is staged** (stable codes): an approval for a
  version that does not exist, or missing its reviewer / references / review date / expiry, or an
  expiry at/before the review (or effective) date, or an attempt to re-approve an already-approved
  version, each raises :class:`PolicyApprovalError` before any row is staged — a refusal leaves no
  approval and no promotion.

Like :class:`~app.compliance.policy_rulepacks.CompliancePolicyRulepackStore`, this is platform
CONTROL-PLANE data (a shared registry, not tenant-scoped), so the store is constructed with just the
session — there is no business scope and no tenant audit entry (the approval row itself is the durable
who/when/why record). It stages into the caller's session and never commits; the caller owns the UoW.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import overload

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    CompliancePolicyApproval,
    CompliancePolicyRulepack,
    PolicyApprovalStatus,
    PolicyRulepackStatus,
)
from app.identity.permissions import Scope, scopes_for_platform_roles

INDIA_JURISDICTION = "IN"

# Authorization refusal (raised FIRST, before any state is read or written, so an unauthorized caller
# — notably a tenant administrator — learns nothing about the policy and changes nothing).
REASON_NOT_PLATFORM_OPERATOR = "not_platform_operator"

# Incomplete-review refusals (each raised BEFORE the session is mutated, so a refusal stages nothing).
REASON_VERSION_NOT_FOUND = "policy_version_not_found"
REASON_MISSING_REVIEWER = "missing_reviewer"
REASON_MISSING_REFERENCES = "missing_review_references"
REASON_MISSING_REVIEW_DATE = "missing_review_date"
REASON_MISSING_EXPIRY = "missing_expiry"
REASON_INVALID_EXPIRY = "invalid_expiry"
REASON_ALREADY_APPROVED = "already_approved"

# Evaluation (in-force) denial reasons — why a version does NOT authorize production behavior.
REASON_NOT_APPROVED = "not_approved"  # a draft / never-approved version
REASON_EXPIRED = "approval_expired"  # past the review-expiry window
REASON_NOT_YET_EFFECTIVE = "approval_not_yet_effective"  # a delayed effective date not yet reached


def content_fingerprint(rules: object) -> str:
    """A stable SHA-256 digest of a rulepack's normalized rules.

    Binds an approval to the EXACT content it reviewed: a canonical JSON encoding (sorted keys) makes
    the digest independent of key order, so two packs with identical rules fingerprint the same and any
    content change yields a different digest. Changing approved content thus requires a new version.
    """
    canonical = json.dumps(rules or [], sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class PolicyApprovalAuthorizationError(Exception):
    """The caller is not authorized to approve a policy version (not a platform operator).

    Raised BEFORE any policy is read or staged, so a tenant administrator's attempt leaves no trace and
    reveals nothing about the registry. ``code`` is :data:`REASON_NOT_PLATFORM_OPERATOR`.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = REASON_NOT_PLATFORM_OPERATOR


class PolicyApprovalError(Exception):
    """An approval was refused with a stable code. Raised before the session is mutated, so a refusal
    leaves no approval row and no promotion. ``detail`` names the offending value."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class PolicyReviewerPrincipal:
    """The platform principal performing the approval.

    ``roles`` are the caller's PLATFORM-plane roles (from the authenticated, signed principal — never a
    bare header). :meth:`PolicyApprovalStore.approve` grants the action only when these roles yield
    :attr:`~app.identity.permissions.Scope.POLICY_RULEPACK_APPROVE`; a tenant administrator's business
    roles never do. ``subject`` is an opaque operator id recorded as the reviewer.
    """

    subject: str
    roles: tuple[str, ...] = ()

    def may_approve(self) -> bool:
        return Scope.POLICY_RULEPACK_APPROVE in scopes_for_platform_roles(self.roles)


@dataclass
class PolicyApprovalReview:
    """One policy-version review as supplied by the operator.

    ``references`` are the review references/evidence (``{reference, kind, uri}``; a non-empty
    ``reference`` is required — the operator cites the external basis, the service authors no legal
    conclusion). ``reviewed_at`` is the review date and ``expires_at`` the approval's review expiry —
    both required. ``effective_from`` optionally delays when the approval takes effect.
    """

    reviewer_id: str
    references: dict = field(default_factory=dict)
    reviewed_at: datetime | None = None
    expires_at: datetime | None = None
    effective_from: datetime | None = None
    reviewer_type: str = "platform_operator"


@dataclass(frozen=True)
class PolicyAuthorizationDecision:
    """The result of evaluating whether a policy version would authorize production behavior.

    ``in_force`` is True only for an approved, ACTIVE version inside its approval window. ``reason`` is
    a stable denial code when ``in_force`` is False. This is a LIFECYCLE check — it never interprets
    the pack's rules (that policy engine stays disabled), so it authors no legal conclusion.
    """

    jurisdiction: str
    version: int
    in_force: bool
    reason: str | None = None
    approval_id: str | None = None


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


def _validate_references(references: dict) -> dict:
    """An approval must carry the references the operator relied on — a bare approval is refused."""
    references = references or {}
    reference = str(references.get("reference", "")).strip()
    if not reference:
        raise PolicyApprovalError(
            REASON_MISSING_REFERENCES,
            "review references with a non-empty 'reference' are required to approve a policy version",
        )
    return {
        "reference": reference,
        "kind": str(references.get("kind", "")).strip(),
        "uri": str(references.get("uri", "")).strip(),
    }


class PolicyApprovalStore:
    """Reader/writer for the platform compliance-policy approval registry (DCA-053)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- reads --------------------------------------------------------------------------------------

    def approval_for(
        self, version: int, jurisdiction: str = INDIA_JURISDICTION
    ) -> CompliancePolicyApproval | None:
        """The approval endorsing one ``(jurisdiction, version)``, or ``None``."""
        stmt = (
            select(CompliancePolicyApproval)
            .where(CompliancePolicyApproval.jurisdiction == (jurisdiction or "").strip().upper())
            .where(CompliancePolicyApproval.policy_version == version)
        )
        return self._session.scalars(stmt).one_or_none()

    def approvals(self, jurisdiction: str = INDIA_JURISDICTION) -> list[CompliancePolicyApproval]:
        """All approvals recorded for a jurisdiction, oldest version first."""
        stmt = (
            select(CompliancePolicyApproval)
            .where(CompliancePolicyApproval.jurisdiction == (jurisdiction or "").strip().upper())
            .order_by(CompliancePolicyApproval.policy_version)
        )
        return list(self._session.scalars(stmt).all())

    @staticmethod
    def is_in_force(approval: CompliancePolicyApproval, *, now: datetime | None = None) -> bool:
        """True only for an ACTIVE approval evaluated inside its own validity window.

        An approval's review expiry is explicit: outside ``[effective_from, expires_at)`` it endorses
        nothing, exactly like a source-terms approval's window (DCA-046). A non-ACTIVE approval is
        never in force.
        """
        if approval.status != PolicyApprovalStatus.ACTIVE.value:
            return False
        moment = now or datetime.now(UTC)
        effective_from = _as_utc(approval.effective_from)
        expires_at = _as_utc(approval.expires_at)
        if effective_from is not None and moment < effective_from:
            return False
        if expires_at is not None and moment >= expires_at:
            return False
        return True

    def evaluate(
        self, version: int, *, now: datetime | None = None, jurisdiction: str = INDIA_JURISDICTION
    ) -> PolicyAuthorizationDecision:
        """Would this policy version authorize production behavior right now? A LIFECYCLE check only.

        A version is in force only when it is approved, its approval is ACTIVE and ``now`` falls inside
        the approval's explicit window. A draft (unapproved) version, a lapsed approval, or an approval
        not yet effective is denied with a stable reason. This NEVER interprets the pack's rules — the
        policy engine that would do so stays a disabled future capability — so it authors no legal
        conclusion; it merely reports whether the review lifecycle permits the version to be consulted.
        """
        juris = (jurisdiction or "").strip().upper()
        moment = now or datetime.now(UTC)
        approval = self.approval_for(version, juris)
        if approval is None:
            return PolicyAuthorizationDecision(juris, version, False, REASON_NOT_APPROVED)
        if approval.status != PolicyApprovalStatus.ACTIVE.value:
            return PolicyAuthorizationDecision(juris, version, False, REASON_EXPIRED, approval.id)
        effective_from = _as_utc(approval.effective_from)
        if effective_from is not None and moment < effective_from:
            return PolicyAuthorizationDecision(
                juris, version, False, REASON_NOT_YET_EFFECTIVE, approval.id
            )
        expires_at = _as_utc(approval.expires_at)
        if expires_at is not None and moment >= expires_at:
            return PolicyAuthorizationDecision(juris, version, False, REASON_EXPIRED, approval.id)
        return PolicyAuthorizationDecision(juris, version, True, None, approval.id)

    # -- the one write path -------------------------------------------------------------------------

    def _require_version(self, version: int, jurisdiction: str) -> CompliancePolicyRulepack:
        stmt = (
            select(CompliancePolicyRulepack)
            .where(CompliancePolicyRulepack.jurisdiction == jurisdiction)
            .where(CompliancePolicyRulepack.version == version)
        )
        pack = self._session.scalars(stmt).one_or_none()
        if pack is None:
            raise PolicyApprovalError(
                REASON_VERSION_NOT_FOUND,
                f"no policy rulepack version {version} exists for '{jurisdiction}'; record it first "
                "(DCA-052)",
                detail=f"{jurisdiction}@v{version}",
            )
        return pack

    def approve(
        self,
        version: int,
        review: PolicyApprovalReview,
        principal: PolicyReviewerPrincipal,
        *,
        jurisdiction: str = INDIA_JURISDICTION,
    ) -> CompliancePolicyApproval:
        """Approve one compliance policy rulepack version; return the approval (uncommitted).

        Authorization is checked FIRST: only a platform operator holding
        :attr:`~app.identity.permissions.Scope.POLICY_RULEPACK_APPROVE` may approve — a tenant
        administrator is refused before any state is read or written. Then the review is validated (the
        version exists, reviewer / references / review date / expiry are present, the expiry is
        coherent, and the version is not already approved); any failure raises before a row is staged.
        On success the approval row is staged (binding a :func:`content_fingerprint` of the exact rules
        reviewed) AND the endorsed rulepack version is promoted ``draft`` → ``active``.

        Promotion authorizes no production behavior on its own: the policy engine that would CONSULT an
        in-force pack stays a disabled future capability.
        """
        juris = (jurisdiction or "").strip().upper()

        # 1) Authority FIRST — a tenant admin (or any non-operator) is refused with no state touched.
        if not principal.may_approve():
            raise PolicyApprovalAuthorizationError(
                "approving a policy version requires a platform operator "
                f"({Scope.POLICY_RULEPACK_APPROVE.value}); a tenant administrator cannot perform it"
            )

        # 2) The version being approved must exist (DCA-052 recorded it as a draft).
        pack = self._require_version(version, juris)

        # 3) The four review essentials must all be present and coherent (reject incomplete reviews).
        reviewer_id = (review.reviewer_id or "").strip()
        if not reviewer_id:
            raise PolicyApprovalError(
                REASON_MISSING_REVIEWER,
                "an approval must name the reviewer (an operator subject id)",
            )
        references = _validate_references(review.references)
        if review.reviewed_at is None:
            raise PolicyApprovalError(
                REASON_MISSING_REVIEW_DATE, "an approval must record the review date (reviewed_at)"
            )
        if review.expires_at is None:
            raise PolicyApprovalError(
                REASON_MISSING_EXPIRY,
                "an approval must carry an explicit review expiry (expires_at)",
            )
        reviewed_at = _as_utc(review.reviewed_at)
        expires_at = _as_utc(review.expires_at)
        effective_from = _as_utc(review.effective_from)
        lower_bound = max(d for d in (reviewed_at, effective_from) if d is not None)
        if expires_at <= lower_bound:
            raise PolicyApprovalError(
                REASON_INVALID_EXPIRY,
                "expires_at must be after the review date (and any effective_from)",
                detail=f"{lower_bound.isoformat()}..{expires_at.isoformat()}",
            )

        # 4) A version is approved at most once (defence-in-depth over the unique constraint).
        if self.approval_for(version, juris) is not None:
            raise PolicyApprovalError(
                REASON_ALREADY_APPROVED,
                f"policy version {version} for '{juris}' is already approved",
                detail=f"{juris}@v{version}",
            )

        # All checks passed — stage the approval AND promote the endorsed version to active. The
        # fingerprint binds the approval to the EXACT rules reviewed, so changing approved content
        # requires a new (unapproved) version.
        approval = CompliancePolicyApproval(
            jurisdiction=juris,
            policy_version=version,
            reviewer_id=reviewer_id,
            reviewer_type=(review.reviewer_type or "platform_operator").strip()
            or "platform_operator",
            review_references=references,
            reviewed_at=reviewed_at,
            effective_from=effective_from,
            expires_at=expires_at,
            content_fingerprint=content_fingerprint(pack.rules),
            status=PolicyApprovalStatus.ACTIVE.value,
        )
        self._session.add(approval)
        # Promotion: the reviewed version is now in force (DCA-052 left it a draft; this story is the
        # one that activates it). The policy engine consuming it stays a disabled future capability, so
        # this does not authorize production behavior on its own.
        pack.status = PolicyRulepackStatus.ACTIVE.value
        return approval
