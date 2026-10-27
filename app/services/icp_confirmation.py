"""DCA-040 — confirm one ICP version (select the active ideal-customer-profile version).

:class:`IcpConfirmationStore` is the single sanctioned writer of
:class:`~app.db.models.IcpConfirmation` — the one *active* ICP version a business has at a time. It
turns the DCA-039 version history (immutable :class:`~app.db.models.IcpDraft` rows) into a single,
explicit, auditable selection: WHICH version the business has confirmed as the one the agent should
treat as its targeting profile.

The load-bearing guarantees (the DCA-040 acceptance criteria):

- **Only a valid draft with material targeting becomes active.** :meth:`IcpConfirmationStore.confirm`
  validates FIRST — the named version must exist for THIS business, and it must carry *material
  targeting* (:func:`has_material_targeting`: at least one positive targeting dimension —
  ``industries`` / ``size_bands`` / ``roles`` / ``geography``; an empty draft, or one carrying only
  exclusions, is not a target and cannot be confirmed). A refusal raises :class:`IcpConfirmationError`
  with a stable code BEFORE anything is staged, so it leaves no row and no partial state.
- **Editing creates a new draft; it does NOT change the active version.** Drafts are insert-only
  (DCA-039), and there is exactly ONE confirmation row per business (``business_id`` unique). Saving a
  new draft version never touches this row, so the previously-confirmed version stays active until a
  NEW version is explicitly confirmed — confirming re-points the single row (an upsert), never adds a
  second active version.
- **Confirmation grants no campaign authority.** It records actor + time and selects the active
  targeting version only. The DCA has no send path; acquisition/consent/suppression stay
  platform-gated. Marking a version active can never, on its own, cause a message to be sent.

Like the DCA-039 :class:`~app.services.icp_store.IcpStore`, the store stages into the caller's session
and never commits, flushes, opens a transaction, or does any I/O — the caller (the HTTP route, via
``commit_idempotent``) owns the unit of work, so the confirmation row and its audit entry commit
together. There is no argument to widen the business scope.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.sink import AuditActor, AuditSink
from app.db.models import IcpConfirmation, IcpDraft, _now
from app.services.icp_store import IcpStore
from app.services.score_invalidation import ScoreInvalidator

# Stable confirmation codes (mapped to HTTP status at the route boundary). Each is raised BEFORE the
# session is mutated, so a refusal leaves no row and no partial state.
REASON_DRAFT_VERSION_NOT_FOUND = (
    "icp_draft_version_not_found"  # -> 404 (unknown/other-tenant version)
)
REASON_NO_MATERIAL_TARGETING = "no_material_targeting"  # -> 422 (empty / exclusions-only draft)

# The audited action code for a confirmation (a controlled code, like the platform's audit actions).
AUDIT_ACTION_CONFIRM = "icp.confirm"


class IcpConfirmationError(Exception):
    """An ICP confirmation was refused with a stable code. Raised before the session is mutated, so a
    refusal leaves no row and no partial state. ``detail`` names the offending value for the caller."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


def has_material_targeting(draft: IcpDraft) -> bool:
    """Whether a draft carries *material targeting* — at least one positive targeting dimension.

    A draft that names no industry, size band, role or geography is not a target at all (even if it
    lists exclusions — exclusions only narrow a target, they never establish one), so it cannot become
    the active ICP. This is the "confirmed material targeting" the acceptance criterion requires.
    """
    return bool(draft.industries or draft.size_bands or draft.roles or draft.geography)


class IcpConfirmationStore:
    """Tenant-scoped reader/writer for the one active ICP version, bound to one business.

    Construct with the active session and the verified business scope (the route resolves it from the
    path like the other tenant routes). Every query is confined to that business.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def active(self) -> IcpConfirmation | None:
        """The business's current confirmation (active version), or ``None`` if none is confirmed."""
        stmt = select(IcpConfirmation).where(IcpConfirmation.business_id == self._business_id)
        return self._session.scalars(stmt).one_or_none()

    def active_version(self) -> int | None:
        """The active ICP version number for this business, or ``None`` if nothing is confirmed."""
        confirmation = self.active()
        return confirmation.version if confirmation is not None else None

    # -- the one write path -------------------------------------------------------------------------

    def confirm(self, version: int, actor: AuditActor) -> IcpConfirmation:
        """Validate and stage the selection of ``version`` as the active ICP; return the (uncommitted)
        confirmation row and stage an atomic audit entry.

        Validation runs FIRST (the named version must exist for this business AND carry material
        targeting), so an invalid confirmation raises :class:`IcpConfirmationError` before any row is
        staged. The business has at most one confirmation row: a first confirm inserts it, a later
        confirm re-points the SAME row (an upsert) to the newly-confirmed version — a new version
        becomes active only when it is itself confirmed. Records ``actor`` + confirmation time and
        audits the action in the caller's unit of work. Grants no campaign/sending authority.

        DCA-043: a confirmed targeting *revision change* (the newly-confirmed version differs from the
        previously-active one, including a first confirmation) marks this business's affected
        recommendation scores stale for recomputation in the SAME unit of work. That is a score-only
        change — it never alters any recommendation's approval and grants no sending authority. The
        count of invalidated scores is exposed on the returned row as ``invalidated_score_count`` (a
        transient, non-persisted attribute) for the route's response.
        """
        # Capture the previously-active version BEFORE the row is re-pointed, so we can tell whether
        # this confirm is a genuine targeting revision change (vs re-confirming the same version).
        previous_version = self.active_version()
        draft = IcpStore(self._session, self._business_id).get_version(version)
        if draft is None:
            # Unknown version, or another tenant's version (reads as not-found) — never confirmed.
            raise IcpConfirmationError(
                REASON_DRAFT_VERSION_NOT_FOUND,
                f"no ICP draft version {version} exists for this business",
                detail=str(version),
            )
        if not has_material_targeting(draft):
            # An empty / exclusions-only draft is not a target and cannot become the active ICP.
            raise IcpConfirmationError(
                REASON_NO_MATERIAL_TARGETING,
                "ICP draft has no material targeting (needs at least one industry, size band, role "
                "or geography) and cannot be confirmed",
                detail=str(version),
            )

        confirmation = self.active()
        if confirmation is None:
            confirmation = IcpConfirmation(
                business_id=self._business_id,
                version=version,
                actor_id=actor.id,
                actor_type=actor.type.value,
            )
            self._session.add(confirmation)
        else:
            # Re-point the single per-business row to the newly-confirmed version. ``updated_at``
            # advances via the model's onupdate; ``confirmed_at`` is set explicitly to a fresh,
            # model-consistent "now". The previously-active version is simply superseded.
            confirmation.version = version
            confirmation.actor_id = actor.id
            confirmation.actor_type = actor.type.value
            confirmation.confirmed_at = _now()

        # Atomic audit (SEC-001/BR-122 discipline): staged into the caller's session so it commits with
        # the confirmation. References are opaque (version + draft row id), never contact data.
        AuditSink(self._session, business_id=self._business_id, actor=actor).record(
            action=AUDIT_ACTION_CONFIRM,
            reason=f"active ICP version set to {version}",
            references={"version": version, "icpDraftId": draft.id},
        )

        # DCA-043 — a genuine targeting revision change invalidates this business's affected
        # recommendation scores (score-only; approval/send untouched). Re-confirming the SAME version
        # is not a change and invalidates nothing. The invalidator stages its own atomic audit entry
        # only when it actually marks ≥1 score, so a no-op leaves no extra state.
        invalidated = 0
        if previous_version != version:
            invalidated = ScoreInvalidator(
                self._session, self._business_id
            ).invalidate_for_targeting_change(new_version=version, actor=actor)
        # Transient (non-persisted) attribute carrying the count to the route's response view. Set via
        # ``setattr`` so it is not mistaken for a mapped column and never persisted.
        setattr(confirmation, "invalidated_score_count", invalidated)
        return confirmation
