"""DCA-036 — the versioned business-profile draft store.

:class:`ProfileStore` is the single sanctioned writer of
:class:`~app.db.models.BusinessProfileDraft`. It holds, per business, a *versioned* draft of the
tenant's own business profile — products, business type, value proposition, languages, and the facts
the tenant has confirmed — while retaining *unknown* aspects explicitly rather than dropping them.

The two load-bearing guarantees (the DCA-036 acceptance criteria):

- **A new draft preserves prior versions.** :meth:`ProfileStore.save_draft` always inserts a NEW row
  at ``max(version) + 1``; it never updates or deletes an existing version. Every prior draft stays
  readable via :meth:`ProfileStore.versions` / :meth:`ProfileStore.get_version`.
- **A draft cannot silently replace the confirmed profile.** The writer only ever stages
  ``status='draft'`` rows and never mutates an existing one. Promoting a draft to the accepted
  CONFIRMED profile is a DISABLED future capability: :meth:`ProfileStore.confirm` refuses with a
  stable code and stages nothing, so no confirmed profile is ever created here.

Like the DCA-026/027/028/031 writers, the store stages into the *caller's* session and never commits,
flushes a transaction of its own, opens one, or does any I/O — the caller (the HTTP route, via
``commit_idempotent``) owns the unit of work. Every read/write is confined to the one ``business_id``
the store is constructed with; there is no argument to widen the scope.

This is the tenant's OWN business content, not prospect contact data, so it is NOT opaque-reference
screened (unlike the audit/outbox payloads); the fields are free text/lists the tenant authors.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import BusinessProfileDraft, ProfileStatus

# The canonical aspects a profile describes. Emptiness of any of these is retained in ``unknowns``
# explicitly (never silently dropped), and an aspect is either populated OR unknown — never both.
CANONICAL_ASPECTS: tuple[str, ...] = (
    "products",
    "business_type",
    "value_proposition",
    "languages",
    "confirmed_facts",
)

# Stable reason code: confirming/promoting a draft to the accepted profile is a disabled future
# capability (mapped to 403 at the HTTP boundary). Reproducible denial; stages no state.
REASON_CONFIRM_DISABLED = "profile_confirmation_disabled"


class ProfileError(Exception):
    """A profile operation was refused with a stable code. Raised before the session is mutated, so a
    refusal leaves no row and no partial state."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class ProfileDraftInput:
    """One draft's content as supplied by the caller. ``unknowns`` is the caller's explicit list of
    aspects it declares not-yet-known; the store augments it with any canonical aspect left empty and
    removes any aspect that is actually populated, so the stored unknowns are honest and disjoint."""

    products: list[str] = field(default_factory=list)
    business_type: str = ""
    value_proposition: str = ""
    languages: list[str] = field(default_factory=list)
    confirmed_facts: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)


def _is_empty(aspect: str, data: ProfileDraftInput) -> bool:
    value = getattr(data, aspect)
    if isinstance(value, str):
        return not value.strip()
    return not value  # empty list


def _retained_unknowns(data: ProfileDraftInput) -> list[str]:
    """The explicit unknown set: canonical aspects left empty, plus any the caller named, minus any
    that are actually populated (a populated aspect is known, never "unknown"). Sorted & deduped."""
    declared = {u.strip() for u in data.unknowns if u and u.strip() in CANONICAL_ASPECTS}
    empty = {aspect for aspect in CANONICAL_ASPECTS if _is_empty(aspect, data)}
    populated = {aspect for aspect in CANONICAL_ASPECTS if not _is_empty(aspect, data)}
    return sorted((declared | empty) - populated)


class ProfileStore:
    """Tenant-scoped reader/writer for versioned business-profile drafts, bound to one business.

    Construct with the active session and the verified business scope (the route resolves it from the
    path like the other tenant routes). Every query is confined to that business.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def versions(self) -> list[BusinessProfileDraft]:
        """All draft versions for this business, oldest first — prior versions are always retained."""
        stmt = (
            select(BusinessProfileDraft)
            .where(BusinessProfileDraft.business_id == self._business_id)
            .where(BusinessProfileDraft.status == ProfileStatus.DRAFT.value)
            .order_by(BusinessProfileDraft.version)
        )
        return list(self._session.scalars(stmt).all())

    def get_version(self, version: int) -> BusinessProfileDraft | None:
        """One draft version within this business, or ``None`` (a cross-tenant/unknown version reads
        as not-found, never surfaced)."""
        stmt = (
            select(BusinessProfileDraft)
            .where(BusinessProfileDraft.business_id == self._business_id)
            .where(BusinessProfileDraft.version == version)
            .where(BusinessProfileDraft.status == ProfileStatus.DRAFT.value)
        )
        return self._session.scalars(stmt).one_or_none()

    def latest_draft(self) -> BusinessProfileDraft | None:
        """The highest-version draft for this business, or ``None`` if none has been saved."""
        stmt = (
            select(BusinessProfileDraft)
            .where(BusinessProfileDraft.business_id == self._business_id)
            .where(BusinessProfileDraft.status == ProfileStatus.DRAFT.value)
            .order_by(BusinessProfileDraft.version.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).one_or_none()

    def confirmed(self) -> BusinessProfileDraft | None:
        """The accepted CONFIRMED profile for this business, or ``None``.

        Confirmation is a disabled future capability, so this is always ``None`` today — the point: a
        reader can observe that saving drafts created no confirmed profile.
        """
        stmt = (
            select(BusinessProfileDraft)
            .where(BusinessProfileDraft.business_id == self._business_id)
            .where(BusinessProfileDraft.status == ProfileStatus.CONFIRMED.value)
            .order_by(BusinessProfileDraft.version.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).one_or_none()

    # -- the one write path -------------------------------------------------------------------------

    def _next_version(self) -> int:
        current = self._session.scalar(
            select(func.max(BusinessProfileDraft.version)).where(
                BusinessProfileDraft.business_id == self._business_id
            )
        )
        return int(current or 0) + 1

    def save_draft(self, data: ProfileDraftInput) -> BusinessProfileDraft:
        """Stage a NEW draft version and return it (uncommitted — the caller owns the unit of work).

        Preserves every prior version (insert-only; no update/delete). Always ``status='draft'`` — it
        never writes or touches a confirmed profile, so a draft can never silently replace one.
        Unknown aspects are retained explicitly in ``unknowns``.
        """
        draft = BusinessProfileDraft(
            business_id=self._business_id,
            version=self._next_version(),
            products=list(data.products),
            business_type=data.business_type.strip(),
            value_proposition=data.value_proposition.strip(),
            languages=list(data.languages),
            confirmed_facts=list(data.confirmed_facts),
            unknowns=_retained_unknowns(data),
            status=ProfileStatus.DRAFT.value,  # confirmation is a disabled future capability
        )
        self._session.add(draft)
        return draft

    def confirm(self, version: int) -> BusinessProfileDraft:
        """Promote a draft to the accepted confirmed profile — a DISABLED future capability.

        Always refuses with :data:`REASON_CONFIRM_DISABLED` and stages nothing, so confirming cannot
        silently replace the confirmed profile until its own story ships. ``version`` is accepted only
        to shape the future contract.
        """
        raise ProfileError(
            REASON_CONFIRM_DISABLED,
            "promoting a draft to the confirmed profile is not enabled in this build",
        )
