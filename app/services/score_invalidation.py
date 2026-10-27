"""DCA-043 — mark recommendation scores stale after a confirmed targeting change.

When a business confirms a *new* Ideal-Customer-Profile version (DCA-040 — a confirmed targeting
revision change), the match scores it computed against the previous targeting no longer reflect what
the business is looking for. :class:`ScoreInvalidator` is the single sanctioned writer that flips those
affected scores from ``current`` to ``stale`` so a future (disabled) rescore capability can recompute
them. It recomputes nothing itself — this story only *marks*.

The load-bearing guarantees (the DCA-043 acceptance criteria):

- **Only this business's affected scores are invalidated.** Every query is confined to the one
  ``business_id`` the invalidator is constructed with (defence-in-depth RLS backs the app-level scope on
  Postgres), so another tenant's recommendations are never touched. "Affected" is precise: a *live*
  recommendation (``suggested`` / ``accepted``) whose score is still ``current`` AND whose
  ``scored_targeting_version`` differs from the newly-confirmed version. A score already marked ``stale``
  is not re-marked (idempotent), and a dismissed/expired recommendation is not a live recommendation.

- **Approval / send permission is untouched.** Invalidation flips ``score_status`` ONLY. It never
  changes a recommendation's ``status`` — an *accepted* recommendation stays accepted, its score merely
  flagged stale — and the DCA has no send path, so marking a score stale can never grant or revoke any
  sending/campaign authority. (Acceptance is a DCA-local status, not recipient consent.)

- **A real change is audited; a no-op stages nothing.** When ≥1 score is invalidated, a single atomic
  DCA-026 audit entry is staged in the SAME unit of work as the flipped rows (opaque references only).
  When nothing is affected, no row is mutated and no audit entry is written — a confirmation that
  invalidates nothing leaves no unintended state.

Like the DCA-040/042 stores, the invalidator stages into the caller's session and never commits,
flushes, opens a transaction, or does any I/O — the caller (``IcpConfirmationStore.confirm`` within the
confirm route's unit of work) owns the transaction, so the flipped scores and their audit entry commit
together. There is no argument to widen the business scope.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.sink import AuditActor, AuditSink
from app.db.models import ProspectCandidate, ScoreStatus

# The recommendation statuses whose score still matters — a live recommendation awaiting/holding the
# business's decision. A dismissed or expired recommendation is terminal, so its score is not "affected".
LIVE_STATUSES: frozenset[str] = frozenset({"suggested", "accepted"})

# The audited action code for a score invalidation (a controlled code, like the other DCA writes).
AUDIT_ACTION_INVALIDATE_SCORES = "recommendation.scores.invalidated"


class ScoreInvalidator:
    """Tenant-scoped writer that marks affected recommendation scores stale, bound to one business.

    Construct with the active session and the verified business scope (the confirm route resolves it
    from the membership-verified path). Every query is confined to that business.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def _affected(self, new_version: int) -> list[ProspectCandidate]:
        """This business's live recommendations whose current score predates the newly-confirmed version."""
        stmt = (
            select(ProspectCandidate)
            .where(ProspectCandidate.business_id == self._business_id)
            .where(ProspectCandidate.status.in_(LIVE_STATUSES))
            .where(ProspectCandidate.score_status == ScoreStatus.CURRENT.value)
            # A score already computed against the now-confirmed version is not affected; a NULL
            # (placeholder) version always differs from a concrete version, so it is affected.
            .where(
                (ProspectCandidate.scored_targeting_version.is_(None))
                | (ProspectCandidate.scored_targeting_version != new_version)
            )
        )
        return list(self._session.scalars(stmt).all())

    def stale_count(self) -> int:
        """How many of this business's live recommendations currently carry a stale score (inspection)."""
        stmt = (
            select(ProspectCandidate)
            .where(ProspectCandidate.business_id == self._business_id)
            .where(ProspectCandidate.status.in_(LIVE_STATUSES))
            .where(ProspectCandidate.score_status == ScoreStatus.STALE.value)
        )
        return len(list(self._session.scalars(stmt).all()))

    # -- the one write path -------------------------------------------------------------------------

    def invalidate_for_targeting_change(self, new_version: int, actor: AuditActor) -> int:
        """Flip this business's affected recommendation scores to ``stale``; return how many changed.

        Marks only *affected* scores (see :meth:`_affected`): the change is score-only — it never
        touches a recommendation's ``status`` (its approval), so an accepted recommendation stays
        accepted. When ≥1 score is invalidated, stages a single atomic DCA-026 audit entry in the
        SAME unit of work; when nothing is affected, mutates nothing and writes no audit entry.
        Grants/revokes no sending authority. Staged into the caller's session (never committed).
        """
        affected = self._affected(new_version)
        if not affected:
            # No unintended state: a confirmation that invalidates nothing stages no row and no audit.
            return 0

        for candidate in affected:
            # Score-only: flip the recomputation marker. ``status`` (approval) and ``match_score`` are
            # left exactly as they were — the (disabled) rescore capability owns recomputing the score.
            candidate.score_status = ScoreStatus.STALE.value

        AuditSink(self._session, business_id=self._business_id, actor=actor).record(
            action=AUDIT_ACTION_INVALIDATE_SCORES,
            reason=f"targeting revision changed; {len(affected)} recommendation score(s) marked for recomputation",
            references={
                "newTargetingVersion": new_version,
                "invalidatedCount": len(affected),
                # Opaque row ids only — never org names or contact data.
                "candidateIds": [c.id for c in affected],
            },
        )
        return len(affected)
