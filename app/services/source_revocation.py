"""DCA-050 — revoke a source version (operator revocation + queued invalidation).

A platform operator can *withdraw* a previously-approved source configuration version. Unlike the
DCA-049 expiry (driven by the clock), a revocation is a deliberate operator decision, so it is recorded
as an explicit terminal transition with its own who/when/why. Two layers:

1. **Operator revocation (immediate, authoritative).** :class:`SourceRevocationStore` is the single
   sanctioned writer that moves an ACTIVE :class:`~app.db.models.SourceTermsApproval` to ``REVOKED``
   (stamping ``revoked_by`` / ``revoked_at`` / ``revocation_reason`` on the row) and demotes the
   :class:`~app.db.models.SourceTerms` version it had promoted (ACTIVE → REVOKED). The moment the
   approval is ``REVOKED`` it is no longer ACTIVE, so
   :meth:`~app.services.source_approval.SourceApprovalStore.is_in_force` returns ``False`` *immediately*
   — the DCA-048 preflight and the DCA-047 entitlement ceiling both stop granting the right at the very
   next read/use check, with **no sweep required**. This is the load-bearing DCA-050 guarantee: an
   already-running task that captured the old (now-revoked) source version is denied when it re-checks
   before using/committing what it fetched (the rights were pulled out from under it).

2. **Queued invalidation (durable housekeeping).** :class:`RevocationInvalidationSweep` is the scheduled
   cleanup the worker runs: it finds the *observations/recommendations* (:class:`ProspectCandidate`)
   acquired from a source whose rights are no longer in force (a REVOKED approval and no surviving
   in-force approval) and flips their still-``current`` score to ``stale`` — queued for a future,
   disabled re-review/re-acquire capability (exactly the DCA-043 marker). It recomputes/removes nothing
   itself: it only *marks*, so it can never grant or widen anything. Idempotent — a candidate already
   ``stale`` is not re-marked.

Authority is the platform-plane scope :attr:`~app.identity.permissions.Scope.SOURCE_TERMS_REVOKE` — a
*distinct* authority from approving (withdrawing a right is deliberately separate from granting one).
A tenant administrator holds only business-plane scopes, so their roles yield no platform scope and the
revocation is refused BEFORE any state is read or written — the shared catalog is control-plane data,
not a tenant resource.

Like the other source stores this is platform CONTROL-PLANE data (a shared registry, not tenant-scoped),
so :class:`SourceRevocationStore` is constructed with just the session: it stages the transitions into
the caller's session and does not commit — the caller owns the unit of work (the approval row itself is
the durable who/when/why record, so there is no separate tenant audit entry, exactly as DCA-046/049).

This story enables no source and ships no live adapter: revocation only ever makes a right MORE
restrictive. In the running service every production preflight still denies at the source-status gate
(no source is ever ENABLED) — future capabilities stay disabled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    ProspectCandidate,
    ScoreStatus,
    SourceApprovalStatus,
    SourceTerms,
    SourceTermsApproval,
    SourceTermsStatus,
)
from app.identity.permissions import Scope, scopes_for_platform_roles
from app.services.source_approval import SourceApprovalStore

# Authorization refusal (raised FIRST, before any state is read or written, so an unauthorized caller
# — notably a tenant administrator — learns nothing about the configuration and changes nothing).
REASON_NOT_PLATFORM_OPERATOR = "not_platform_operator"

# Revocation refusals (each raised BEFORE the session is mutated, so a refusal stages nothing).
REASON_APPROVAL_NOT_FOUND = "approval_not_found"
REASON_NOT_ACTIVE = "approval_not_active"
REASON_ALREADY_REVOKED = "already_revoked"
REASON_MISSING_REASON = "missing_revocation_reason"


class SourceRevocationAuthorizationError(Exception):
    """The caller is not authorized to revoke a source configuration (not a platform operator).

    Raised BEFORE any configuration is read or staged, so a tenant administrator's attempt leaves no
    trace and reveals nothing about the catalog. ``code`` is :data:`REASON_NOT_PLATFORM_OPERATOR`.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = REASON_NOT_PLATFORM_OPERATOR


class SourceRevocationError(Exception):
    """A revocation was refused with a stable code. Raised before the session is mutated, so a refusal
    leaves the approval exactly as it was. ``detail`` names the offending value."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class RevokerPrincipal:
    """The platform principal performing the revocation.

    ``roles`` are the caller's PLATFORM-plane roles (from the authenticated, signed principal — never a
    bare header). :meth:`SourceRevocationStore.revoke` grants the action only when these roles yield
    :attr:`~app.identity.permissions.Scope.SOURCE_TERMS_REVOKE`; a tenant administrator's business roles
    never do. ``subject`` is an opaque operator id recorded as the revoker.
    """

    subject: str
    roles: tuple[str, ...] = ()

    def may_revoke(self) -> bool:
        return Scope.SOURCE_TERMS_REVOKE in scopes_for_platform_roles(self.roles)


@dataclass(frozen=True)
class RevocationResult:
    """What one :meth:`SourceRevocationStore.revoke` transitioned, for an inspectable record.

    ``demoted_terms`` is ``True`` when the terms version the approval had promoted was ACTIVE and was
    demoted ACTIVE → REVOKED alongside the approval (it is ``False`` if that version was already past its
    own window, e.g. EXPIRED, and so left untouched). ``revoked_at`` is the moment recorded.
    """

    source_key: str
    terms_version: int
    revoked_by: str
    revoked_at: datetime
    demoted_terms: bool


class SourceRevocationStore:
    """The single sanctioned writer of a source-configuration revocation (DCA-050).

    Platform control-plane; session-only (stages into the caller's session, never commits).
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._approvals = SourceApprovalStore(session)

    def revoke(
        self,
        source_key: str,
        terms_version: int,
        principal: RevokerPrincipal,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> RevocationResult:
        """Revoke one approved source-terms version; return the result (uncommitted).

        Authorization is checked FIRST: only a platform operator holding
        :attr:`~app.identity.permissions.Scope.SOURCE_TERMS_REVOKE` may revoke — a tenant administrator
        is refused before any state is read or written. Then the approval is validated (it exists and is
        still ACTIVE — an already-revoked or already-expired approval is refused, so a revocation is
        never applied twice and never silently resurrects a lapsed right) and a non-empty reason is
        required. On success the approval is moved ACTIVE → REVOKED (stamping who/when/why) and the terms
        version it promoted is demoted ACTIVE → REVOKED. Because the approval is no longer ACTIVE,
        :meth:`SourceApprovalStore.is_in_force` returns ``False`` for it immediately — the next read/use
        check denies with no sweep required (the DCA-050 acceptance criterion).
        """
        # 1) Authority FIRST — a tenant admin (or any non-operator) is refused with no state touched.
        if not principal.may_revoke():
            raise SourceRevocationAuthorizationError(
                "revoking a source configuration requires a platform operator "
                f"({Scope.SOURCE_TERMS_REVOKE.value}); a tenant administrator cannot perform it"
            )

        key = (source_key or "").strip()
        reason_text = (reason or "").strip()
        if not reason_text:
            raise SourceRevocationError(
                REASON_MISSING_REASON,
                "a revocation must carry a non-empty reason (an opaque code/explanation of why the "
                "right was withdrawn)",
            )

        # 2) The approval being revoked must exist (DCA-046 recorded it).
        approval = self._approvals.approval_for(key, terms_version)
        if approval is None:
            raise SourceRevocationError(
                REASON_APPROVAL_NOT_FOUND,
                f"no approval exists for source '{key}' terms version {terms_version}; nothing to revoke",
                detail=f"{key}@v{terms_version}",
            )

        # 3) Only an ACTIVE approval can be revoked — an already-revoked one is refused (idempotent,
        #    stages nothing), an already-expired one is left as the lapse record it already is.
        if approval.status == SourceApprovalStatus.REVOKED.value:
            raise SourceRevocationError(
                REASON_ALREADY_REVOKED,
                f"approval for '{key}' terms version {terms_version} is already revoked",
                detail=f"{key}@v{terms_version}",
            )
        if approval.status != SourceApprovalStatus.ACTIVE.value:
            raise SourceRevocationError(
                REASON_NOT_ACTIVE,
                f"only an active approval can be revoked; '{key}' terms version {terms_version} is "
                f"'{approval.status}'",
                detail=approval.status,
            )

        moment = now or datetime.now(UTC)

        # 4) Stage the withdrawal: the approval is REVOKED (with its who/when/why) and the terms version
        #    it promoted is demoted. ``is_in_force`` keys on the status, so the right is pulled instantly.
        approval.status = SourceApprovalStatus.REVOKED.value
        approval.revoked_at = moment
        approval.revoked_by = principal.subject
        approval.revocation_reason = reason_text

        demoted = False
        terms = self._session.scalars(
            select(SourceTerms)
            .where(SourceTerms.source_key == key)
            .where(SourceTerms.version == terms_version)
        ).one_or_none()
        if terms is not None and terms.status == SourceTermsStatus.ACTIVE.value:
            terms.status = SourceTermsStatus.REVOKED.value
            demoted = True

        return RevocationResult(
            source_key=key,
            terms_version=terms_version,
            revoked_by=principal.subject,
            revoked_at=moment,
            demoted_terms=demoted,
        )


# --------------------------------------------------------------------------------------------------
# Queued invalidation of affected observations/recommendations (the durable housekeeping layer).
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RevocationInvalidationResult:
    """What one :meth:`RevocationInvalidationSweep.sweep` invalidated, for an inspectable record.

    ``invalidated_candidate_ids`` are the opaque row ids of the observations/recommendations whose score
    was flipped ``current`` → ``stale``; ``revoked_source_keys`` are the source keys found to have a
    revoked-and-not-in-force approval at ``evaluated_at``.
    """

    evaluated_at: datetime
    revoked_source_keys: list[str] = field(default_factory=list)
    invalidated_candidate_ids: list[str] = field(default_factory=list)

    @property
    def invalidated(self) -> int:
        """How many observations the sweep invalidated (0 when nothing was affected — the idempotent case)."""
        return len(self.invalidated_candidate_ids)


class RevocationInvalidationSweep:
    """Queue invalidation of observations acquired from a now-revoked source (DCA-050).

    A platform/system maintenance pass, NOT tenant work: like the DCA-049 expiry sweep it runs once per
    cycle (not per tenant) and only ever makes an observation MORE restrictive (flips a ``current`` score
    to ``stale``), so it is not gated behind any feature/source flag and writes no tenant audit — the
    revocation's own approval row already records the who/when/why. It stages the flips into the caller's
    session and does not commit; the caller owns the unit of work.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._approvals = SourceApprovalStore(session)

    def _revoked_source_keys(self, moment: datetime) -> list[str]:
        """Source keys that have a REVOKED approval and NO surviving in-force approval at ``moment``.

        A source with another version still in force is NOT invalidated — only a source whose rights are
        genuinely no longer in force. Evaluated via the authoritative :meth:`SourceApprovalStore.is_in_force`
        so the set never disagrees with what the preflight would grant.
        """
        revoked_keys = set(
            self._session.scalars(
                select(SourceTermsApproval.source_key).where(
                    SourceTermsApproval.status == SourceApprovalStatus.REVOKED.value
                )
            ).all()
        )
        affected: list[str] = []
        for key in sorted(revoked_keys):
            in_force = any(
                self._approvals.is_in_force(a, now=moment) for a in self._approvals.approvals(key)
            )
            if not in_force:
                affected.append(key)
        return affected

    def sweep(self, *, now: datetime | None = None) -> RevocationInvalidationResult:
        """Flip to ``stale`` every still-``current`` observation from a revoked, no-longer-in-force
        source; stage only. Idempotent: an already-``stale`` observation is not re-marked, so a second
        run at the same ``now`` invalidates nothing."""
        moment = now or datetime.now(UTC)
        result = RevocationInvalidationResult(evaluated_at=moment)
        result.revoked_source_keys.extend(self._revoked_source_keys(moment))
        if not result.revoked_source_keys:
            return result

        # A platform maintenance pass intentionally spans tenants (it is keyed on the control-plane set
        # of revoked sources, not on one business); it only ever flips a score flag MORE restrictive.
        candidates = self._session.scalars(
            select(ProspectCandidate)
            .where(ProspectCandidate.source_key.in_(result.revoked_source_keys))
            .where(ProspectCandidate.score_status == ScoreStatus.CURRENT.value)
        ).all()
        for candidate in candidates:
            candidate.score_status = ScoreStatus.STALE.value
            result.invalidated_candidate_ids.append(candidate.id)
        return result


def run_revocation_invalidation(
    session_factory, settings=None, *, now: datetime | None = None
) -> RevocationInvalidationResult:
    """Open a session, run one :class:`RevocationInvalidationSweep`, COMMIT and return the result.

    The worker-facing entry point (mirrors :func:`app.services.source_expiry.run_expiry_sweep`): it owns
    its own unit of work so the scheduled maintenance pass can run it standalone. ``settings`` is accepted
    for a uniform worker signature but is unused — the sweep only ever makes an observation MORE
    restrictive, so it is safe to run whenever the worker loop is active and is not gated behind any flag.
    """
    with session_factory() as session:
        result = RevocationInvalidationSweep(session).sweep(now=now)
        session.commit()
    return result
