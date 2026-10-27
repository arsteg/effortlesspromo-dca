"""DCA-039 — the versioned ideal-customer-profile (ICP) draft store.

:class:`IcpStore` is the single sanctioned writer of :class:`~app.db.models.IcpDraft`. It holds, per
business, a *versioned* draft ICP: the structured target the agent should look for — industries, size
bands, roles, geography (Indian states/UTs — the service is India-only) and exclusions — optionally
linked to the DCA-036 business-profile draft version it was authored against.

The load-bearing guarantees (the DCA-039 acceptance criteria):

- **ICPs are business-scoped and versioned.** :meth:`IcpStore.save_draft` always inserts a NEW row at
  ``max(version) + 1`` for the one ``business_id`` the store is constructed with; it never updates or
  deletes an existing version, so every prior version stays readable and one tenant can never read
  another's versions (defence-in-depth RLS backs the app-level scope on Postgres).
- **Unsupported fields / invalid bands receive clear validation errors.** Values are validated against
  controlled vocabularies BEFORE anything is staged: an unknown size band, an unrecognized (non-India)
  geography, or a ``profile_version`` that does not name an existing business-profile draft version of
  this business each raises :class:`IcpValidationError` with a stable code — the refusal leaves no row
  and no partial state. (Unknown *request fields* are rejected one layer up by the route's
  ``extra='forbid'`` body model, a 422.)

Like the DCA-036 :class:`~app.services.profile_store.ProfileStore`, the store stages into the caller's
session and never commits, flushes, opens a transaction, or does any I/O — the caller (the HTTP route,
via ``commit_idempotent``) owns the unit of work. There is no argument to widen the business scope.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import BusinessProfileDraft, IcpDraft
from app.geography import india as india_ref

# The controlled vocabulary of organisation size bands an ICP may target. A value outside this set is
# an "invalid band" (the headline validation case). Ordered smallest→largest; India MSME-aligned plus
# the larger bands a B2B ICP commonly targets.
SIZE_BANDS: tuple[str, ...] = ("micro", "small", "medium", "large", "enterprise")

# Stable validation codes (mapped to 422 at the HTTP boundary). Each is raised BEFORE the session is
# mutated, so a refusal leaves no row and no partial state.
REASON_INVALID_SIZE_BAND = "invalid_size_band"
REASON_UNSUPPORTED_GEOGRAPHY = "unsupported_geography"
REASON_PROFILE_VERSION_NOT_FOUND = "profile_version_not_found"


class IcpValidationError(Exception):
    """An ICP draft was refused with a stable code. Raised before the session is mutated, so a refusal
    leaves no row and no partial state. ``detail`` names the offending value for the caller."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass
class IcpDraftInput:
    """One ICP draft's content as supplied by the caller.

    ``size_bands`` is validated against :data:`SIZE_BANDS`; ``geography`` against the recognized Indian
    states/UTs (:mod:`app.geography.india`) and stored normalized to ISO codes. ``industries`` /
    ``roles`` / ``exclusions`` are free-text lists (trimmed/deduped). ``profile_version``, when set,
    must name an existing business-profile draft version of the same business.
    """

    industries: list[str] = field(default_factory=list)
    size_bands: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    geography: list[str] = field(default_factory=list)
    exclusions: list[str] = field(default_factory=list)
    profile_version: int | None = None


def _clean_list(values: list[str]) -> list[str]:
    """Trim, drop blanks, and de-duplicate a free-text list while preserving first-seen order."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = (raw or "").strip()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _validate_size_bands(values: list[str]) -> list[str]:
    """Normalize to lower-case and reject any band outside the controlled vocabulary (clear 422)."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        band = (raw or "").strip().lower()
        if not band:
            continue
        if band not in SIZE_BANDS:
            raise IcpValidationError(
                REASON_INVALID_SIZE_BAND,
                f"unsupported size band '{band}'; allowed: {', '.join(SIZE_BANDS)}",
                detail=band,
            )
        if band not in seen:
            seen.add(band)
            out.append(band)
    return out


def _validate_geography(values: list[str]) -> list[str]:
    """Resolve each geography value to a recognized Indian state/UT code (India-only); reject others.

    An entry is accepted only if it normalizes to a known ISO 3166-2:IN state/UT (code or full name);
    anything else — a foreign region, a typo — is rejected with a clear code and the offending value.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        token = (raw or "").strip()
        if not token:
            continue
        code = india_ref.normalize_state_ut(token)
        if code is None:
            raise IcpValidationError(
                REASON_UNSUPPORTED_GEOGRAPHY,
                f"unsupported geography '{token}'; expected a recognized Indian state/UT "
                "(the service is India-only)",
                detail=token,
            )
        if code not in seen:
            seen.add(code)
            out.append(code)
    return out


class IcpStore:
    """Tenant-scoped reader/writer for versioned ICP drafts, bound to one business.

    Construct with the active session and the verified business scope (the route resolves it from the
    path like the other tenant routes). Every query is confined to that business.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def versions(self) -> list[IcpDraft]:
        """All ICP draft versions for this business, oldest first — prior versions are retained."""
        stmt = (
            select(IcpDraft)
            .where(IcpDraft.business_id == self._business_id)
            .order_by(IcpDraft.version)
        )
        return list(self._session.scalars(stmt).all())

    def get_version(self, version: int) -> IcpDraft | None:
        """One ICP draft version within this business, or ``None`` (a cross-tenant/unknown version
        reads as not-found, never surfaced)."""
        stmt = (
            select(IcpDraft)
            .where(IcpDraft.business_id == self._business_id)
            .where(IcpDraft.version == version)
        )
        return self._session.scalars(stmt).one_or_none()

    def latest_draft(self) -> IcpDraft | None:
        """The highest-version ICP draft for this business, or ``None`` if none has been saved."""
        stmt = (
            select(IcpDraft)
            .where(IcpDraft.business_id == self._business_id)
            .order_by(IcpDraft.version.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).one_or_none()

    # -- the one write path -------------------------------------------------------------------------

    def _next_version(self) -> int:
        current = self._session.scalar(
            select(func.max(IcpDraft.version)).where(IcpDraft.business_id == self._business_id)
        )
        return int(current or 0) + 1

    def _require_profile_version(self, version: int | None) -> int | None:
        """Validate an optional linked business-profile draft version exists for this business."""
        if version is None:
            return None
        exists = self._session.scalar(
            select(BusinessProfileDraft.id)
            .where(BusinessProfileDraft.business_id == self._business_id)
            .where(BusinessProfileDraft.version == version)
        )
        if exists is None:
            raise IcpValidationError(
                REASON_PROFILE_VERSION_NOT_FOUND,
                f"no business-profile draft version {version} exists for this business",
                detail=str(version),
            )
        return version

    def save_draft(self, data: IcpDraftInput) -> IcpDraft:
        """Validate and stage a NEW ICP draft version; return it (uncommitted — caller owns the UoW).

        Validation (controlled size bands, India-only geography, an existing linked profile version)
        runs FIRST, so an invalid draft raises before any row is staged. Preserves every prior version
        (insert-only; no update/delete). Always ``status='draft'`` — promotion is a disabled future
        capability, so a draft can never silently become the active targeting profile.
        """
        # Validate everything before touching the session: a refusal leaves no row, no partial state.
        size_bands = _validate_size_bands(data.size_bands)
        geography = _validate_geography(data.geography)
        profile_version = self._require_profile_version(data.profile_version)

        draft = IcpDraft(
            business_id=self._business_id,
            version=self._next_version(),
            industries=_clean_list(data.industries),
            size_bands=size_bands,
            roles=_clean_list(data.roles),
            geography=geography,
            exclusions=_clean_list(data.exclusions),
            profile_version=profile_version,
            status="draft",  # promotion to an active ICP is a disabled future capability
        )
        self._session.add(draft)
        return draft
