"""DCA-081 — invalidate affected recommendations on restriction (the restriction fanout worker).

Key: ``restriction-fanout`` · source sections 5, 10, 11.

Recording a restriction (DCA-078) only writes the restriction ledger — it does not, by itself, touch
the recommendations already sitting in a tenant's queue. This story is the propagation: a worker that,
when a restriction is in force, **revokes the affected suggestions** (:class:`~app.db.models.ProspectCandidate`
— a live recommendation awaiting a tenant's decision) and **records a propagation acknowledgment**
(:class:`~app.db.models.RestrictionPropagation` — the per-tenant work receipt) so the invalidation is
durable and reviewable. Revoking a suggestion only ever makes processing MORE restrictive; it grants
and sends nothing (the DCA has no send path).

Two fanout planes mirror the two DCA-078 restriction kinds — and keep the *scope* boundary exact:

- :meth:`RestrictionFanout.fanout_local` — a LOCAL (tenant) restriction. Bound to the restricting
  business, it revokes ONLY that business's affected live suggestions and writes ONE acknowledgment for
  it. It can never reach another tenant — this is the "a local block affects only its scope" half of the
  acceptance criteria. Mirrors the tenant-scoped :class:`~app.services.score_invalidation.ScoreInvalidator`.

- :meth:`RestrictionFanout.fanout_platform_source` / :meth:`RestrictionFanout.sweep_platform` — a
  PLATFORM-WIDE source removal. Keyed on the control-plane set of removed sources, it spans tenants and
  revokes EVERY tenant's live suggestions acquired from a removed source, writing one acknowledgment per
  affected tenant — the "a platform-wide approved removal reaches all affected platform-controlled
  copies" half. A tenant with no copy from a removed source is never touched (no acknowledgment for it).
  This is a platform/system maintenance pass, exactly like the DCA-050
  :class:`~app.services.source_revocation.RevocationInvalidationSweep`: it runs over already-authorized
  restriction rows, only ever makes a suggestion more restrictive, and is not gated behind a tenant.

**Authority first, and a denial changes nothing.** The two *direct* fanout methods take the triggering
principal and check its authority BEFORE any state is read or written, so a relevant denial is pure and
reproducible (it stages no revocation, no acknowledgment, no audit):

- ``fanout_local`` requires a business user whose membership roles grant
  :attr:`~app.identity.permissions.Scope.PROCESSING_RESTRICT` (a read-only viewer and a platform
  operator both lack it for the tenant); and
- ``fanout_platform_source`` requires a platform operator holding
  :attr:`~app.identity.permissions.Scope.PLATFORM_SOURCE_RESTRICT` (a tenant administrator never does).

The authority plane is also cross-checked against the restriction row, so a business-plane restriction
can never be fanned out as a platform-wide removal and vice versa.

**Idempotent, no unintended state.** Only a *live* suggestion (``suggested`` / ``accepted``) is affected;
a terminal one (``dismissed`` / ``expired`` / already ``revoked``) is left exactly as it was, so a second
fanout of the same restriction revokes nothing and writes no acknowledgment. Revoking is ``status``-only:
it never touches ``match_score`` / ``score_status`` and grants no sending authority (acceptance is a
DCA-local status, not recipient consent). Every writer stages into the caller's session and performs no
I/O of its own — the caller owns the unit of work, so the revoked rows and their acknowledgment/audit
commit together (the scheduled :func:`run_restriction_fanout` entry point owns its own session).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.sink import AuditSink
from app.db.models import (
    ContactChannel,
    PlatformSourceRestriction,
    ProcessingRestriction,
    ProspectCandidate,
    RestrictionAuthorityPlane,
    RestrictionPropagation,
    RestrictionRetentionMode,
)
from app.identity.permissions import Scope
from app.services.processing_restriction import (
    PlatformRestrictionPrincipal,
    RestrictionPrincipal,
)

# The suggestion statuses still live enough to be affected by a restriction — a recommendation awaiting
# or holding the tenant's decision. A terminal suggestion (dismissed/expired/revoked) is never affected.
LIVE_STATUSES: frozenset[str] = frozenset({"suggested", "accepted"})

# The terminal status a restriction fanout moves an affected live suggestion to. Distinct from the
# tenant's own ``dismissed`` and the clock's ``expired`` so a restriction-driven revocation is told apart.
REVOKED_STATUS = "revoked"

# The audited action code for a LOCAL restriction fanout (a controlled code, like the other DCA writes).
AUDIT_ACTION_REVOKE_ON_RESTRICTION = "recommendation.revoked_on_restriction"

# Stable refusal codes. Each is raised BEFORE the session is mutated, so a refusal stages nothing.
REASON_NOT_AUTHORIZED = "restriction_fanout_not_authorized"
REASON_NOT_PLATFORM_OPERATOR = "not_platform_operator"
REASON_PLANE_MISMATCH = "restriction_authority_plane_mismatch"

# Map a persisted contact field name to the channel it is reachable on. A minimised candidate contact
# (DCA-058) holds opaque field names like ``email`` / ``phone``; this is the only place the raw keys are
# turned into a channel, so a channel-scoped local restriction can match the suggestions it covers.
_CONTACT_FIELD_CHANNEL: dict[str, str] = {
    "email": ContactChannel.EMAIL.value,
    "phone": ContactChannel.MOBILE.value,
    "mobile": ContactChannel.MOBILE.value,
    "landline": ContactChannel.LANDLINE.value,
    "whatsapp": ContactChannel.WHATSAPP.value,
}


class RestrictionFanoutError(Exception):
    """A fanout was refused with a stable code. Raised before the session is mutated, so a refusal
    leaves no revocation, no acknowledgment and no partial state. ``detail`` names the offending value."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class RestrictionFanoutAuthorizationError(RestrictionFanoutError):
    """The caller is not authorized to trigger this fanout.

    A distinct type so an HTTP boundary (where one exists) can map it to a 403. Raised before anything is
    read or written. ``code`` is :data:`REASON_NOT_AUTHORIZED` (local) or
    :data:`REASON_NOT_PLATFORM_OPERATOR` (platform-wide).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


@dataclass
class FanoutResult:
    """What one fanout revoked and acknowledged, for an inspectable record (no raw data).

    ``affected_businesses`` are the tenants that had ≥1 live suggestion revoked (for a local fanout, at
    most the one restricting business; for a platform-wide fanout, every tenant with a copy from a removed
    source). ``revoked_candidate_ids`` are the opaque row ids revoked across all affected tenants, and
    ``propagation_ids`` the opaque ids of the acknowledgments staged (one per affected tenant).
    """

    restriction_id: str
    authority_plane: str
    source_key: str = ""
    affected_businesses: list[str] = field(default_factory=list)
    revoked_candidate_ids: list[str] = field(default_factory=list)
    propagation_ids: list[str] = field(default_factory=list)

    @property
    def revoked_count(self) -> int:
        """How many suggestions this fanout revoked (0 when nothing was affected — the idempotent case)."""
        return len(self.revoked_candidate_ids)


class RestrictionFanout:
    """The single sanctioned writer that revokes affected suggestions on a restriction (DCA-081).

    Stages into the caller's session and performs no I/O of its own (never commits/flushes/opens a
    transaction) — the caller owns the unit of work, exactly like the DCA-043 score invalidator and the
    DCA-050 revocation sweep.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- channel matching (local) -------------------------------------------------------------------

    @staticmethod
    def _candidate_channels(candidate: ProspectCandidate) -> set[str]:
        """The channels a suggestion is reachable on, derived from its minimised contact fields."""
        contact = candidate.contact or {}
        channels: set[str] = set()
        for key, value in contact.items():
            if not value:
                continue
            mapped = _CONTACT_FIELD_CHANNEL.get(str(key).strip().lower())
            if mapped:
                channels.add(mapped)
        return channels

    def _affected_local(self, restriction: ProcessingRestriction) -> list[ProspectCandidate]:
        """This business's live suggestions that fall WITHIN the restriction's scope.

        A raw suggestion is a prospect that has not yet been associated with any brand or purpose, so a
        restriction narrowed to a specific ``scope_brand`` / ``scope_purpose`` matches NO suggestion — it
        targets downstream brand/purpose-scoped processing, not the prospect queue. Honouring the scope
        exactly (rather than over-revoking) is the "affects only its scope" guarantee. A tenant-wide or a
        channel-only restriction (brand and purpose both empty) matches every live suggestion, or those
        reachable on the named channel when ``scope_channel`` is set ("" = all channels).
        """
        if restriction.scope_brand or restriction.scope_purpose:
            return []

        stmt = (
            select(ProspectCandidate)
            .where(ProspectCandidate.business_id == restriction.business_id)
            .where(ProspectCandidate.status.in_(LIVE_STATUSES))
        )
        live = list(self._session.scalars(stmt).all())

        channel = (restriction.scope_channel or "").strip().lower()
        if not channel:
            return live
        return [c for c in live if channel in self._candidate_channels(c)]

    # -- the LOCAL fanout (tenant-scoped) -----------------------------------------------------------

    def fanout_local(
        self,
        restriction: ProcessingRestriction,
        principal: RestrictionPrincipal,
        *,
        now: datetime | None = None,
    ) -> FanoutResult:
        """Revoke the restricting business's affected live suggestions; return the (uncommitted) result.

        Authorization is checked FIRST: a caller whose roles lack
        :attr:`~app.identity.permissions.Scope.PROCESSING_RESTRICT` is refused with
        :class:`RestrictionFanoutAuthorizationError` before any state is read or written (the relevant
        denial). The restriction must be a BUSINESS-plane row (a platform-wide removal is fanned out via
        :meth:`fanout_platform_source`). On success every affected live suggestion of that business is
        moved to ``revoked`` (status-only — ``match_score``/``score_status`` are untouched), and, when ≥1
        was revoked, ONE :class:`~app.db.models.RestrictionPropagation` acknowledgment plus one atomic
        DCA-026 audit entry are staged in the SAME unit of work. When nothing is affected (an idempotent
        re-run, or a brand/purpose-narrowed restriction), nothing is mutated and no row is written.
        """
        if not principal.may_restrict():
            raise RestrictionFanoutAuthorizationError(
                REASON_NOT_AUTHORIZED,
                "fanning out a local processing restriction requires a business user holding "
                f"'{Scope.PROCESSING_RESTRICT.value}'; a read-only viewer or a non-member cannot "
                "perform it, and a platform operator holds no business scope for this tenant",
            )
        if restriction.authority_plane != RestrictionAuthorityPlane.BUSINESS.value:
            raise RestrictionFanoutError(
                REASON_PLANE_MISMATCH,
                "fanout_local expects a business-plane restriction; a platform-wide source removal is "
                "fanned out via fanout_platform_source",
                detail=restriction.authority_plane,
            )

        result = FanoutResult(
            restriction_id=restriction.id,
            authority_plane=RestrictionAuthorityPlane.BUSINESS.value,
        )
        affected = self._affected_local(restriction)
        if not affected:
            # No unintended state: a restriction that invalidates nothing stages no row and no audit.
            return result

        for candidate in affected:
            candidate.status = REVOKED_STATUS
            result.revoked_candidate_ids.append(candidate.id)
        result.affected_businesses.append(restriction.business_id)

        propagation = RestrictionPropagation(
            business_id=restriction.business_id,
            restriction_id=restriction.id,
            restriction_version=restriction.version,
            authority_plane=RestrictionAuthorityPlane.BUSINESS.value,
            source_key="",
            scope_brand=restriction.scope_brand,
            scope_channel=restriction.scope_channel,
            scope_purpose=restriction.scope_purpose,
            revoked_count=len(result.revoked_candidate_ids),
            revoked_candidate_ids=list(result.revoked_candidate_ids),
            acknowledged_by=principal.subject,
            acknowledged_by_type=principal.actor_type,
            created_at=now or datetime.now(UTC),
        )
        self._session.add(propagation)
        result.propagation_ids.append(propagation.id)

        # Atomic audit (SEC-001/BR-122): staged into the caller's session so it commits with the rows.
        # References stay OPAQUE — restriction id / scope dimensions / candidate ids — never contact data.
        AuditSink(
            self._session,
            business_id=restriction.business_id,
            actor=principal.as_actor(),
        ).record(
            action=AUDIT_ACTION_REVOKE_ON_RESTRICTION,
            reason=(
                f"local restriction '{restriction.id}' (version {restriction.version}) revoked "
                f"{len(result.revoked_candidate_ids)} affected suggestion(s)"
            ),
            references={
                "restrictionId": restriction.id,
                "authorityPlane": RestrictionAuthorityPlane.BUSINESS.value,
                "restrictionVersion": restriction.version,
                "scopeBrand": restriction.scope_brand,
                "scopeChannel": restriction.scope_channel,
                "scopePurpose": restriction.scope_purpose,
                "revokedCount": len(result.revoked_candidate_ids),
                "candidateIds": list(result.revoked_candidate_ids),
                "propagationId": propagation.id,
            },
        )
        return result

    # -- the PLATFORM-WIDE fanout (spans tenants) ---------------------------------------------------

    def _in_force_platform_restriction(
        self, source_key: str, moment: datetime
    ) -> PlatformSourceRestriction | None:
        """The LATEST platform-source restriction for ``source_key`` if it is still in force at ``moment``.

        Lifting a restriction is a disabled future capability, so the latest version is the standing one;
        it is in force unless it was recorded ``until_date`` with a ``retain_until`` that has now lapsed.
        """
        latest = self._session.scalars(
            select(PlatformSourceRestriction)
            .where(PlatformSourceRestriction.source_key == source_key)
            .order_by(PlatformSourceRestriction.version.desc())
            .limit(1)
        ).first()
        if latest is None:
            return None
        if (
            latest.retention_mode == RestrictionRetentionMode.UNTIL_DATE.value
            and latest.retain_until is not None
            and _as_utc(latest.retain_until) <= moment
        ):
            return None
        return latest

    def _revoke_platform_source(
        self,
        restriction: PlatformSourceRestriction,
        *,
        acknowledged_by: str,
        acknowledged_by_type: str,
        now: datetime,
        result: FanoutResult,
    ) -> None:
        """Revoke EVERY tenant's live suggestions acquired from ``restriction.source_key`` and stage one
        acknowledgment per affected tenant. A platform/system pass: it spans tenants (keyed on the
        control-plane source), stamps each acknowledgment with the owning tenant's ``business_id``, and —
        like the DCA-050 revocation sweep — records no separate tenant audit (the restriction row and the
        per-tenant acknowledgment are the durable record)."""
        candidates = self._session.scalars(
            select(ProspectCandidate)
            .where(ProspectCandidate.source_key == restriction.source_key)
            .where(ProspectCandidate.status.in_(LIVE_STATUSES))
        ).all()

        by_business: dict[str, list[str]] = defaultdict(list)
        for candidate in candidates:
            candidate.status = REVOKED_STATUS
            by_business[candidate.business_id].append(candidate.id)
            result.revoked_candidate_ids.append(candidate.id)

        for business_id in sorted(by_business):
            ids = by_business[business_id]
            propagation = RestrictionPropagation(
                business_id=business_id,
                restriction_id=restriction.id,
                restriction_version=restriction.version,
                authority_plane=RestrictionAuthorityPlane.PLATFORM.value,
                source_key=restriction.source_key,
                revoked_count=len(ids),
                revoked_candidate_ids=list(ids),
                acknowledged_by=acknowledged_by,
                acknowledged_by_type=acknowledged_by_type,
                created_at=now,
            )
            self._session.add(propagation)
            result.affected_businesses.append(business_id)
            result.propagation_ids.append(propagation.id)

    def fanout_platform_source(
        self,
        restriction: PlatformSourceRestriction,
        principal: PlatformRestrictionPrincipal,
        *,
        now: datetime | None = None,
    ) -> FanoutResult:
        """Revoke every tenant's affected suggestions from a removed source; return the result (uncommitted).

        Authorization is checked FIRST: only a platform operator holding
        :attr:`~app.identity.permissions.Scope.PLATFORM_SOURCE_RESTRICT` may fan out a platform-wide
        removal — a tenant administrator is refused before any state is read or written. The restriction
        must be a PLATFORM-plane row. On success every tenant's live suggestion acquired from the removed
        source is moved to ``revoked`` and one acknowledgment is staged per affected tenant; a tenant with
        no such suggestion is never touched. Idempotent: a second fanout revokes nothing (every copy is
        already revoked) and writes no acknowledgment.
        """
        if not principal.may_restrict_platform():
            raise RestrictionFanoutAuthorizationError(
                REASON_NOT_PLATFORM_OPERATOR,
                "fanning out a platform-wide source removal requires a platform operator "
                f"('{Scope.PLATFORM_SOURCE_RESTRICT.value}'); a tenant administrator cannot perform it",
            )
        if restriction.authority_plane != RestrictionAuthorityPlane.PLATFORM.value:
            raise RestrictionFanoutError(
                REASON_PLANE_MISMATCH,
                "fanout_platform_source expects a platform-plane restriction; a local restriction is "
                "fanned out via fanout_local",
                detail=restriction.authority_plane,
            )

        result = FanoutResult(
            restriction_id=restriction.id,
            authority_plane=RestrictionAuthorityPlane.PLATFORM.value,
            source_key=restriction.source_key,
        )
        self._revoke_platform_source(
            restriction,
            acknowledged_by=principal.subject,
            acknowledged_by_type="user",
            now=now or datetime.now(UTC),
            result=result,
        )
        return result

    def sweep_platform(self, *, now: datetime | None = None) -> list[FanoutResult]:
        """Fan out EVERY in-force platform-source removal across all tenants (the scheduled worker pass).

        A platform/system maintenance pass, NOT tenant work and NOT principal-driven: it runs over the
        already-authorized (DCA-078) platform-source restriction ledger, so no principal check is needed
        (the authority was verified when the removal was recorded). It is keyed on the control-plane set of
        in-force removed sources, only ever makes a suggestion MORE restrictive, and is idempotent — a
        second sweep at the same moment revokes nothing. Stages into the caller's session; returns one
        :class:`FanoutResult` per removed source that affected ≥1 tenant.
        """
        moment = now or datetime.now(UTC)
        source_keys = sorted(
            set(
                self._session.scalars(
                    select(PlatformSourceRestriction.source_key).group_by(
                        PlatformSourceRestriction.source_key
                    )
                ).all()
            )
        )

        results: list[FanoutResult] = []
        for source_key in source_keys:
            restriction = self._in_force_platform_restriction(source_key, moment)
            if restriction is None:
                continue
            result = FanoutResult(
                restriction_id=restriction.id,
                authority_plane=RestrictionAuthorityPlane.PLATFORM.value,
                source_key=source_key,
            )
            self._revoke_platform_source(
                restriction,
                acknowledged_by="",
                acknowledged_by_type="system",
                now=moment,
                result=result,
            )
            if result.revoked_count:
                results.append(result)
        return results


def _as_utc(value: datetime) -> datetime:
    """Normalise a possibly-naive datetime (SQLite returns naive) to an aware UTC moment for comparison."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def run_restriction_fanout(
    session_factory, settings=None, *, now: datetime | None = None
) -> list[FanoutResult]:
    """Open a session, run one platform-wide :meth:`RestrictionFanout.sweep_platform`, COMMIT and return.

    The worker-facing entry point (mirrors :func:`app.services.source_revocation.run_revocation_invalidation`
    and :func:`app.services.source_expiry.run_expiry_sweep`): a platform/system maintenance pass that owns
    its own unit of work so the scheduler can run it standalone. ``settings`` is accepted for a uniform
    worker signature but is unused — the sweep only ever makes a suggestion MORE restrictive, so it is safe
    whenever the worker loop is active and is gated behind no feature/source flag.
    """
    with session_factory() as session:
        results = RestrictionFanout(session).sweep_platform(now=now)
        session.commit()
    return results
