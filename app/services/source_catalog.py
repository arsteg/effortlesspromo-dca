"""DCA-044 — the source-catalog draft store.

:class:`SourceCatalogStore` is the single sanctioned writer of :class:`~app.db.models.Source`, the
platform-owned registry of prospect data sources (official registries and licensed commercial
datasets). This story stores *drafts only*; a source's rights details and the approval/enable
capability are later stories and stay DISABLED here.

The load-bearing guarantees (the DCA-044 acceptance criteria):

- **A newly created source is a DISABLED draft with no implicit permissions.** :meth:`SourceCatalogStore.create_draft`
  always writes ``status='draft'``, ``enabled=False`` and an EMPTY ``permissions`` set, regardless of
  what the caller supplies — there is no argument to create an enabled source or to attach permissions.
  So cataloguing either an official or a commercial source grants nothing on its own.

- **A draft source cannot be used.** :meth:`SourceCatalogStore.is_usable` is ``False`` for a draft, and
  :meth:`SourceCatalogStore.usable` returns only approved+enabled sources — an empty list while every
  source is a draft. :meth:`SourceCatalogStore.usability_reason` names *why* a draft is refused
  (:data:`REASON_SOURCE_NOT_ENABLED`), so a consumer that tries to use a draft gets an auditable denial
  rather than silent acquisition. Nothing in this service promotes a draft to ``ENABLED``.

- **Invalid drafts are rejected before anything is staged.** Validation runs FIRST, so a refusal raises
  :class:`SourceValidationError` with a stable code and leaves no row and no partial state: an unknown
  kind or access method, a missing/duplicate catalog key, or an India coverage that names no recognized
  Indian state/UT (the service is India-only) each has a distinct code.

This is platform CONTROL-PLANE data (a shared catalog, not tenant-scoped), so the store is constructed
with just the session — there is no business scope and no tenant audit entry. It stages into the
caller's session and never commits; the caller owns the unit of work.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Source, SourceAccessMethod, SourceKind, SourceStatus
from app.geography.india import normalize_state_ut

# Stable validation codes (each raised BEFORE the session is mutated, so a refusal stages nothing).
REASON_MISSING_KEY = "missing_source_key"
REASON_DUPLICATE_KEY = "duplicate_source_key"
REASON_UNKNOWN_KIND = "unknown_source_kind"
REASON_UNKNOWN_ACCESS_METHOD = "unknown_access_method"
REASON_INVALID_COVERAGE = "invalid_india_coverage"

# The denial a consumer sees when it tries to use a source that is still a disabled draft.
REASON_SOURCE_NOT_ENABLED = "source_not_enabled"


class SourceValidationError(Exception):
    """A source draft was refused with a stable code. Raised before the session is mutated, so a
    refusal leaves no row and no partial state. ``detail`` names the offending value."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass
class SourceDraftInput:
    """One source catalog entry as supplied by the caller.

    ``status``/``enabled``/``permissions`` are intentionally NOT accepted here: a draft is always
    disabled with no permissions. ``kind`` and ``access_method`` must be recognized values and
    ``india_coverage`` must name at least one recognized Indian state/UT or be nationwide (India-only);
    all three are validated before anything is staged.
    """

    key: str
    kind: str
    access_method: str
    name: str = ""
    trust_tier: str = ""
    capabilities: list[str] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)
    # {"nationwide": bool, "states": [state/UT codes]}; validated/normalized to recognized IN codes.
    india_coverage: dict = field(default_factory=dict)


def _validate_kind(kind: str) -> str:
    token = (kind or "").strip().lower()
    try:
        return SourceKind(token).value
    except ValueError as exc:
        allowed = ", ".join(k.value for k in SourceKind)
        raise SourceValidationError(
            REASON_UNKNOWN_KIND,
            f"unknown source kind '{kind}'; expected one of: {allowed}",
            detail=kind,
        ) from exc


def _validate_access_method(method: str) -> str:
    token = (method or "").strip().lower()
    try:
        return SourceAccessMethod(token).value
    except ValueError as exc:
        allowed = ", ".join(m.value for m in SourceAccessMethod)
        raise SourceValidationError(
            REASON_UNKNOWN_ACCESS_METHOD,
            f"unknown access method '{method}'; expected one of: {allowed}",
            detail=method,
        ) from exc


def _validate_coverage(coverage: dict) -> dict:
    """Normalize India coverage to ``{"nationwide": bool, "states": [IN codes]}``.

    The service is India-only, so coverage must be positively evidenced: either nationwide, or a set
    of recognized Indian state/UT codes (ISO 3166-2:IN). An unrecognized state code, or a coverage that
    is neither nationwide nor names any state, is refused before any row is staged — a catalogued source
    can never carry an empty or foreign coverage.
    """
    coverage = coverage or {}
    nationwide = bool(coverage.get("nationwide", False))
    raw_states = coverage.get("states") or []
    if not isinstance(raw_states, list):
        raise SourceValidationError(
            REASON_INVALID_COVERAGE, "india coverage 'states' must be a list of IN state/UT codes"
        )
    normalized: list[str] = []
    for value in raw_states:
        code = normalize_state_ut(str(value))
        if code is None:
            raise SourceValidationError(
                REASON_INVALID_COVERAGE,
                f"'{value}' is not a recognized Indian state/UT (the service is India-only)",
                detail=str(value),
            )
        if code not in normalized:
            normalized.append(code)
    if not nationwide and not normalized:
        raise SourceValidationError(
            REASON_INVALID_COVERAGE,
            "india coverage must be nationwide or name at least one recognized Indian state/UT",
        )
    return {"nationwide": nationwide, "states": normalized}


class SourceCatalogStore:
    """Reader/writer for the platform source catalog. Writes drafts only (approval is a later story)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- reads --------------------------------------------------------------------------------------

    def all(self) -> list[Source]:
        """Every catalogued source, by key."""
        return list(self._session.scalars(select(Source).order_by(Source.key)).all())

    def get(self, key: str) -> Source | None:
        """One source by its catalog key, or ``None``."""
        stmt = select(Source).where(Source.key == (key or "").strip())
        return self._session.scalars(stmt).one_or_none()

    def usable(self) -> list[Source]:
        """Only the sources that may actually be used — approved AND enabled.

        Empty while every source is a draft: a draft is never usable, so cataloguing a source adds
        nothing to this list until a future (disabled) approval capability promotes it.
        """
        return [s for s in self.all() if self.is_usable(s)]

    @staticmethod
    def is_usable(source: Source) -> bool:
        """True only for an approved, enabled source. A draft (the only kind this story writes) is
        never usable — its mere existence grants no acquisition right."""
        return source.enabled and source.status == SourceStatus.ENABLED.value

    @classmethod
    def usability_reason(cls, source: Source) -> str | None:
        """``None`` if the source may be used; otherwise a stable denial code. A draft yields
        :data:`REASON_SOURCE_NOT_ENABLED` — the auditable "cannot be used while a draft" denial."""
        if cls.is_usable(source):
            return None
        return REASON_SOURCE_NOT_ENABLED

    # -- the one write path -------------------------------------------------------------------------

    def create_draft(self, data: SourceDraftInput) -> Source:
        """Validate and stage a NEW source as a DISABLED draft; return it (uncommitted — caller owns UoW).

        Validation (key present + unique, recognized kind/access method, India coverage) runs FIRST, so
        an invalid draft raises :class:`SourceValidationError` before any row is staged. The stored row
        is ALWAYS ``status='draft'``, ``enabled=False`` and ``permissions=[]`` regardless of input — a
        new source of either kind is disabled and grants nothing.
        """
        key = (data.key or "").strip()
        if not key:
            raise SourceValidationError(REASON_MISSING_KEY, "a source catalog key is required")
        kind = _validate_kind(data.kind)
        access_method = _validate_access_method(data.access_method)
        coverage = _validate_coverage(data.india_coverage)
        if self.get(key) is not None:
            raise SourceValidationError(
                REASON_DUPLICATE_KEY,
                f"a source with key '{key}' already exists in the catalog",
                detail=key,
            )

        source = Source(
            key=key,
            name=(data.name or "").strip(),
            kind=kind,
            trust_tier=(data.trust_tier or "").strip(),
            capabilities=[str(c) for c in (data.capabilities or [])],
            fields=[str(f) for f in (data.fields or [])],
            access_method=access_method,
            india_coverage=coverage,
            # Disabled draft, no implicit permissions — fixed here, never taken from the caller.
            status=SourceStatus.DRAFT.value,
            enabled=False,
            permissions=[],
        )
        self._session.add(source)
        return source
