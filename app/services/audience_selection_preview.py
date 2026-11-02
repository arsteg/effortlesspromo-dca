"""DCA-152 — PREVIEW an audience selection: a read-only projection of a selected subset.

The audience stage's review step, building on the DCA-145 prospect suggestions + the DCA-150 visible
candidate list. A business user is reviewing a SELECTED subset of its own prospect suggestions and
wants to see — BEFORE committing anything — what that selection would actually contain: how many
records, which are *activatable* vs merely *research-only*, which are excluded (and WHY — stale by
freshness, dismissed, not reviewable, or research-only with no sendable rights), and the would-be cost,
WITHOUT creating any audience membership or incurring any billing.

A preview is a **pure, non-mutating projection**. Like the sibling DCA-150 reader it writes nothing,
spends nothing, sends nothing and creates NO audience membership — it only reads the business's own
already-recorded suggestion rows and classifies them with the SAME deterministic visibility deciders
the candidate list uses (``VISIBLE_STATUS`` / ``DISMISSED_STATUS`` / the :class:`SuggestionVisibility`
constraint / expiry). It never calls the AI gateway and never performs a fetch.

The two load-bearing invariants (the DCA-152 acceptance criteria):

- **Non-mutating, and research-only records are separated from activatable ones.** Every selected row
  is classified into exactly one bucket. A ``research_only`` organization suggestion — every suggestion
  the discovery pipeline produces today — is NEVER activatable: it carries no sendable contact and no
  recipient consent, so it is reported in its own ``research_only`` exclusion bucket, distinct from the
  (currently always empty) *activatable* set. A ``sendable`` suggestion is a disabled future capability
  no path produces; the classifier still routes one to the activatable set so the SEPARATION is
  explicit, but previewing it still creates no membership, no send and no charge. ``billing_committed``
  and ``membership_created`` are always ``False``.

- **Reproduce the result and the relevant denial without unintended state changes.** The preview reads
  only within THIS business — an unknown or cross-tenant suggestion id is simply absent from the scoped
  read and reported as a ``not_found`` line (never an oracle for another tenant's row, never an error
  that leaks). The HTTP boundary denies a non-member (incl. a platform operator) before any row is read.
  Nothing is written, so the projection and every denial reproduce exactly.

Acting on a preview — actually building the audience, sending, or spending — stays a disabled future
capability this story never performs.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ProspectSuggestion, SuggestionVisibility
from app.services.prospect_suggestion import DISMISSED_STATUS, VISIBLE_STATUS

# The most suggestion ids one preview may evaluate — a selection is bounded so a single read can never
# pull an unbounded set. Extra ids past the cap are dropped (deterministically, keeping the first N).
MAX_SELECTION_SIZE = 500

# Stable, inspectable exclusion reason codes — WHY a selected record is not in the activatable audience.
# A record lands in exactly one bucket (the first that applies, checked in this order).
EXCLUSION_NOT_FOUND = "not_found"  # unknown / cross-tenant / nonexistent id (scoped read missed it)
EXCLUSION_DISMISSED = (
    "dismissed"  # privately dismissed (DCA-151) — no longer a reviewable candidate
)
EXCLUSION_NOT_REVIEWABLE = (
    "not_reviewable"  # some other non-'suggested' status (never self-advances)
)
EXCLUSION_EXPIRED = "expired"  # FRESHNESS: past its expiry window — must be refreshed before use
# RIGHTS: a research-only organization carries no sendable contact and no recipient consent, so it can
# never be a message recipient — the key research-only vs activatable separation.
EXCLUSION_RESEARCH_ONLY = "research_only"


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalize a possibly-naive timestamp to UTC (SQLite returns naive datetimes)."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class AudiencePreviewLine:
    """One classified row in the preview — exactly one selected suggestion id.

    Carries only the suggestion's opaque scoring/visibility metadata — NEVER a contact value (none is
    stored on a research-only organization suggestion). ``activatable`` is the research-only vs
    activatable separation; ``exclusion_reason`` is the stable code for why a non-activatable record is
    excluded (``None`` when activatable). ``found`` is ``False`` for an unknown / cross-tenant id.
    """

    suggestion_id: str
    found: bool
    activatable: bool
    exclusion_reason: str | None
    subject_type: str | None = None
    subject_id: str | None = None
    visibility: str | None = None
    status: str | None = None
    match_score: float | None = None
    fresh: bool | None = None
    expires_at: datetime | None = None

    @property
    def sendable(self) -> bool:
        return self.visibility == SuggestionVisibility.SENDABLE.value


@dataclass(frozen=True)
class AudienceSelectionPreview:
    """The inert, inspectable projection of one audience selection — what it WOULD contain.

    ``selected`` is the de-duplicated, capped count of ids evaluated; ``found`` how many resolved within
    this business; ``not_found`` the rest. ``activatable`` is the would-be sendable audience size;
    ``research_only`` the organization records that are explicitly NOT activatable; ``excluded`` is the
    per-reason tally. ``estimated_chargeable_contacts`` is the cost projection (equals ``activatable``) —
    it is a would-be figure only: ``billing_committed`` and ``membership_created`` are always ``False``.
    """

    business_id: str
    generated_at: datetime
    intended_use: str
    channel: str
    selected: int
    found: int
    not_found: int
    activatable: int
    research_only: int
    excluded: dict[str, int]
    estimated_chargeable_contacts: int
    lines: list[AudiencePreviewLine] = field(default_factory=list)
    # The invariants, stated on the projection so no consumer can mistake a preview for an action.
    billing_committed: bool = False
    membership_created: bool = False


class AudienceSelectionPreviewer:
    """Preview an audience selection for ONE business — a pure, read-only projection.

    Construct with the active session and the verified business scope (a route resolves it from the
    membership-verified tenant context). Every query is confined to this business — there is no argument
    to widen the scope — so a preview can never read another tenant's rows, and an unknown / cross-tenant
    suggestion id is simply absent from the scoped read and reported as ``not_found``. The previewer
    reads already-recorded suggestion rows and writes NOTHING: it creates no audience membership, no
    send and no charge, and makes no fetch or model call.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    def _rows_by_id(self, ids: list[str]) -> dict[str, ProspectSuggestion]:
        """The suggestions among ``ids`` that belong to THIS business, keyed by id. Tenant-scoped in
        SQL, so an id owned by another business (or none) simply does not appear — never surfaced."""
        if not ids:
            return {}
        stmt = (
            select(ProspectSuggestion)
            .where(ProspectSuggestion.business_id == self._business_id)
            .where(ProspectSuggestion.id.in_(ids))
        )
        return {row.id: row for row in self._session.scalars(stmt).all()}

    def _classify(
        self, suggestion_id: str, row: ProspectSuggestion | None, now: datetime
    ) -> AudiencePreviewLine:
        """Classify ONE selected id into the activatable set or a single exclusion bucket.

        Order (first match wins): not-found → dismissed → not-reviewable (other status) → expired
        (freshness) → research-only (no sendable rights/consent) → activatable (a visible, unexpired,
        reviewable SENDABLE suggestion — a disabled future capability no path produces today).
        """
        if row is None:
            return AudiencePreviewLine(
                suggestion_id=suggestion_id,
                found=False,
                activatable=False,
                exclusion_reason=EXCLUSION_NOT_FOUND,
            )

        expires_at = _as_utc(row.expires_at)
        fresh = expires_at is None or expires_at > now

        if row.status == DISMISSED_STATUS:
            reason: str | None = EXCLUSION_DISMISSED
        elif row.status != VISIBLE_STATUS:
            reason = EXCLUSION_NOT_REVIEWABLE
        elif not fresh:
            reason = EXCLUSION_EXPIRED
        elif row.visibility == SuggestionVisibility.RESEARCH_ONLY.value:
            reason = EXCLUSION_RESEARCH_ONLY
        else:
            reason = None  # a visible, unexpired, reviewable sendable suggestion → activatable

        return AudiencePreviewLine(
            suggestion_id=suggestion_id,
            found=True,
            activatable=reason is None,
            exclusion_reason=reason,
            subject_type=row.subject_type,
            subject_id=row.subject_id,
            visibility=row.visibility,
            status=row.status,
            match_score=row.match_score,
            fresh=fresh,
            expires_at=expires_at,
        )

    def preview(
        self,
        suggestion_ids: list[str],
        *,
        intended_use: str = "",
        channel: str = "",
        now: datetime | None = None,
    ) -> AudienceSelectionPreview:
        """Project what the SELECTED subset of this business's suggestions would contain.

        The selection is de-duplicated (order-preserving) and capped at :data:`MAX_SELECTION_SIZE`; each
        id is resolved within this business and classified deterministically. Returns the aggregate
        counts (selected / found / not-found / activatable / research-only), the per-reason exclusion
        tally, the per-id lines, and the cost projection — ``estimated_chargeable_contacts`` is the
        would-be activatable size, reported WITHOUT committing any billing or creating any membership.
        Reads only; writes, spends and sends nothing, so the projection reproduces with no state change.
        """
        moment = now or datetime.now(UTC)

        seen: list[str] = []
        for sid in suggestion_ids:
            token = (sid or "").strip()
            if token and token not in seen:
                seen.append(token)
        selection = seen[:MAX_SELECTION_SIZE]

        rows = self._rows_by_id(selection)
        lines = [self._classify(sid, rows.get(sid), moment) for sid in selection]

        found = sum(1 for line in lines if line.found)
        activatable = sum(1 for line in lines if line.activatable)
        research_only = sum(1 for line in lines if line.exclusion_reason == EXCLUSION_RESEARCH_ONLY)
        excluded = Counter(
            line.exclusion_reason for line in lines if line.exclusion_reason is not None
        )

        return AudienceSelectionPreview(
            business_id=self._business_id,
            generated_at=moment,
            intended_use=(intended_use or "").strip(),
            channel=(channel or "").strip(),
            selected=len(selection),
            found=found,
            not_found=len(selection) - found,
            activatable=activatable,
            research_only=research_only,
            excluded=dict(excluded),
            # The cost projection: what WOULD be chargeable if activated — a would-be figure only. No
            # billing is committed and no audience membership is created (both flags stay False).
            estimated_chargeable_contacts=activatable,
            lines=lines,
        )


__all__ = [
    "MAX_SELECTION_SIZE",
    "EXCLUSION_NOT_FOUND",
    "EXCLUSION_DISMISSED",
    "EXCLUSION_NOT_REVIEWABLE",
    "EXCLUSION_EXPIRED",
    "EXCLUSION_RESEARCH_ONLY",
    "AudiencePreviewLine",
    "AudienceSelectionPreview",
    "AudienceSelectionPreviewer",
]
