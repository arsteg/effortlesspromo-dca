"""DCA-045 — the source field-and-operation rights store (versioned source terms).

:class:`SourceTermsStore` is the single sanctioned writer of :class:`~app.db.models.SourceTerms`, the
platform-owned, versioned record of what a catalog :class:`~app.db.models.Source` (DCA-044) is
permitted to do with each field it returns. This story stores *terms versions* only; approving/
activating a version — and the source-enable capability it pairs with — are later, DISABLED stories.

The load-bearing guarantees (the DCA-045 acceptance criteria):

- **Rights can differ per field and per use; expiry and version are explicit — never one boolean.**
  :meth:`SourceTermsStore.permits` answers a ``(field, operation)`` question against a specific terms
  *version*: it reads the per-field override first (so a field can be display-only while another is
  exportable) and falls back to the source-wide per-operation default (deny-by-default). The right's
  validity is an EXPLICIT window (``effective_from``/``expires_at``): an expired or not-yet-effective
  version grants nothing. There is no single "we have rights" flag.

- **Versions are insert-only and preserved.** :meth:`SourceTermsStore.add_version` always inserts a
  NEW row at ``max(version) + 1`` for the source; it never updates or deletes a prior version, so a
  source's rights history stays reviewable version-by-version. Every row is ``status='draft'`` — a
  terms version is reviewable but never in force on its own (:meth:`SourceTermsStore.is_active` is
  always ``False`` here; activation is a disabled future capability).

- **Invalid terms are refused before anything is staged** (stable codes): a terms version for a source
  that is not catalogued, an unrecognized operation (in the defaults or a field override), missing
  contract evidence, a missing purpose, an invalid retention, or an expiry at/before its own
  effective date each raises :class:`SourceTermsValidationError` before any row is staged, so a
  refusal leaves no row and no partial state.

Like :class:`~app.services.source_catalog.SourceCatalogStore`, this is platform CONTROL-PLANE data
(a shared registry, not tenant-scoped), so the store is constructed with just the session — there is
no business scope and no tenant audit entry. It stages into the caller's session and never commits;
the caller owns the unit of work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Source, SourceOperation, SourceTerms, SourceTermsStatus

# The controlled vocabulary of operations a terms version may grant a right for (DCA-045).
OPERATIONS: tuple[str, ...] = tuple(op.value for op in SourceOperation)

# Stable validation codes (each raised BEFORE the session is mutated, so a refusal stages nothing).
REASON_SOURCE_NOT_FOUND = "source_not_found"
REASON_UNKNOWN_OPERATION = "unknown_operation"
REASON_MISSING_CONTRACT_EVIDENCE = "missing_contract_evidence"
REASON_MISSING_PURPOSE = "missing_purpose"
REASON_INVALID_RETENTION = "invalid_retention"
REASON_INVALID_EXPIRY = "invalid_expiry"

# The denials a consumer sees when a right is evaluated against a terms version (not a staging error).
REASON_OPERATION_NOT_PERMITTED = "operation_not_permitted"
REASON_TERMS_EXPIRED = "terms_expired"
REASON_TERMS_NOT_YET_EFFECTIVE = "terms_not_yet_effective"


class SourceTermsValidationError(Exception):
    """A terms version was refused with a stable code. Raised before the session is mutated, so a
    refusal leaves no row and no partial state. ``detail`` names the offending value."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class RightDecision:
    """The outcome of evaluating one ``(field, operation)`` right against a terms version.

    ``allowed`` is the verdict; ``reason`` is ``None`` when allowed, otherwise a stable denial code
    (:data:`REASON_OPERATION_NOT_PERMITTED` / :data:`REASON_TERMS_EXPIRED` /
    :data:`REASON_TERMS_NOT_YET_EFFECTIVE`). ``source`` records whether the grant came from a per-field
    override or the source-wide default, so the decision is auditable.
    """

    allowed: bool
    operation: str
    field: str | None = None
    reason: str | None = None
    source: str = ""  # "field_override" | "operation_default"


@dataclass
class SourceTermsInput:
    """One source terms version as supplied by the caller.

    ``status``/``version`` are intentionally NOT accepted: a terms version is always a draft and the
    store assigns the next version. ``operations`` is the source-wide per-operation default grant;
    ``field_rights`` are per-field overrides. ``contract_evidence`` and at least one ``purpose`` are
    required. The validity window (``effective_from``/``expires_at``) is explicit and optional.
    """

    source_key: str
    operations: dict[str, bool] = field(default_factory=dict)
    field_rights: dict[str, dict[str, bool]] = field(default_factory=dict)
    purposes: list[str] = field(default_factory=list)
    attribution: dict = field(default_factory=dict)
    retention: dict = field(default_factory=dict)
    contract_evidence: dict = field(default_factory=dict)
    effective_from: datetime | None = None
    expires_at: datetime | None = None


def _normalize_operation(value: str) -> str:
    """Return the canonical operation value, or raise :data:`REASON_UNKNOWN_OPERATION`."""
    token = (value or "").strip().lower()
    try:
        return SourceOperation(token).value
    except ValueError as exc:
        raise SourceTermsValidationError(
            REASON_UNKNOWN_OPERATION,
            f"unknown operation '{value}'; expected one of: {', '.join(OPERATIONS)}",
            detail=value,
        ) from exc


def _normalize_operation_rights(operations: dict[str, bool]) -> dict[str, bool]:
    """Validate and expand source-wide grants to all operations, deny-by-default.

    Every operation is represented explicitly (missing ⇒ ``False``) so a stored version can never be
    ambiguous about a use it says nothing about — the absence of a grant is a denial, not a gap.
    """
    provided: dict[str, bool] = {}
    for raw_op, granted in (operations or {}).items():
        provided[_normalize_operation(raw_op)] = bool(granted)
    return {op: provided.get(op, False) for op in OPERATIONS}


def _normalize_field_rights(field_rights: dict[str, dict[str, bool]]) -> dict[str, dict[str, bool]]:
    """Validate per-field overrides; only the operations a field names are overridden (sparse)."""
    out: dict[str, dict[str, bool]] = {}
    for raw_field, ops in (field_rights or {}).items():
        name = (raw_field or "").strip()
        if not name:
            continue
        overrides: dict[str, bool] = {}
        for raw_op, granted in (ops or {}).items():
            overrides[_normalize_operation(raw_op)] = bool(granted)
        out[name] = overrides
    return out


def _validate_contract_evidence(evidence: dict) -> dict:
    """A terms version must carry evidence the right exists — a bare rights claim is refused."""
    evidence = evidence or {}
    reference = str(evidence.get("reference", "")).strip()
    if not reference:
        raise SourceTermsValidationError(
            REASON_MISSING_CONTRACT_EVIDENCE,
            "contract evidence with a non-empty 'reference' is required for a terms version",
        )
    return {
        "reference": reference,
        "kind": str(evidence.get("kind", "")).strip(),
        "uri": str(evidence.get("uri", "")).strip(),
    }


def _validate_purposes(purposes: list[str]) -> list[str]:
    """Rights are purpose-bound: at least one non-empty purpose is required (trimmed, deduped)."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in purposes or []:
        value = (raw or "").strip()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    if not out:
        raise SourceTermsValidationError(
            REASON_MISSING_PURPOSE, "a terms version must name at least one purpose for the rights"
        )
    return out


def _validate_retention(retention: dict) -> dict:
    """Normalize retention to ``{"max_days": int|None, "policy": str}``; reject a negative window."""
    retention = retention or {}
    raw_days = retention.get("max_days")
    max_days: int | None
    if raw_days is None:
        max_days = None
    else:
        try:
            max_days = int(raw_days)
        except (TypeError, ValueError) as exc:
            raise SourceTermsValidationError(
                REASON_INVALID_RETENTION,
                f"retention 'max_days' must be an integer number of days, got {raw_days!r}",
                detail=str(raw_days),
            ) from exc
        if max_days < 0:
            raise SourceTermsValidationError(
                REASON_INVALID_RETENTION,
                f"retention 'max_days' cannot be negative (got {max_days})",
                detail=str(max_days),
            )
    return {"max_days": max_days, "policy": str(retention.get("policy", "")).strip()}


def _validate_window(
    effective_from: datetime | None, expires_at: datetime | None
) -> tuple[datetime | None, datetime | None]:
    """The validity window must be coherent: an expiry cannot be at/before the effective date."""
    if effective_from is not None and expires_at is not None and expires_at <= effective_from:
        raise SourceTermsValidationError(
            REASON_INVALID_EXPIRY,
            "expires_at must be after effective_from",
            detail=f"{effective_from.isoformat()}..{expires_at.isoformat()}",
        )
    return effective_from, expires_at


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalize a (possibly naive — SQLite) datetime to tz-aware UTC for comparison."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)  # SQLite returns naive datetimes
    return value


class SourceTermsStore:
    """Reader/writer for the platform source-terms registry. Writes draft versions only (approval
    is a later, disabled story)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- reads --------------------------------------------------------------------------------------

    def versions(self, source_key: str) -> list[SourceTerms]:
        """All terms versions for one source, oldest first — every prior version is retained."""
        stmt = (
            select(SourceTerms)
            .where(SourceTerms.source_key == (source_key or "").strip())
            .order_by(SourceTerms.version)
        )
        return list(self._session.scalars(stmt).all())

    def get_version(self, source_key: str, version: int) -> SourceTerms | None:
        """One terms version of a source, or ``None``."""
        stmt = (
            select(SourceTerms)
            .where(SourceTerms.source_key == (source_key or "").strip())
            .where(SourceTerms.version == version)
        )
        return self._session.scalars(stmt).one_or_none()

    def latest(self, source_key: str) -> SourceTerms | None:
        """The highest-version terms for a source, or ``None`` if none has been recorded."""
        stmt = (
            select(SourceTerms)
            .where(SourceTerms.source_key == (source_key or "").strip())
            .order_by(SourceTerms.version.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).one_or_none()

    @staticmethod
    def is_active(terms: SourceTerms) -> bool:
        """True only for an ACTIVE terms version. Every version this story writes is a draft, so this
        is always ``False`` — recording rights never puts them in force (activation is disabled)."""
        return terms.status == SourceTermsStatus.ACTIVE.value

    # -- rights evaluation (reads; no state change) -------------------------------------------------

    @classmethod
    def permits(
        cls,
        terms: SourceTerms,
        field: str,
        operation: str,
        *,
        now: datetime | None = None,
    ) -> RightDecision:
        """Decide whether ``terms`` grants ``operation`` on ``field`` — a pure, side-effect-free read.

        Field overrides win over the source-wide default (so one field can be display-only while
        another is exportable); an operation absent from both is denied (deny-by-default). The validity
        window is honoured: a version evaluated outside ``[effective_from, expires_at)`` grants nothing,
        with a stable code. Raises :class:`SourceTermsValidationError` only for an unknown operation
        string (a programming error, not a data denial).
        """
        op = _normalize_operation(operation)
        moment = now or datetime.now(UTC)
        effective_from = _as_utc(terms.effective_from)
        expires_at = _as_utc(terms.expires_at)
        if effective_from is not None and moment < effective_from:
            return RightDecision(
                allowed=False,
                operation=op,
                field=field,
                reason=REASON_TERMS_NOT_YET_EFFECTIVE,
            )
        if expires_at is not None and moment >= expires_at:
            return RightDecision(
                allowed=False, operation=op, field=field, reason=REASON_TERMS_EXPIRED
            )

        overrides = (terms.field_rights or {}).get(field, {})
        if op in overrides:
            granted = bool(overrides[op])
            return RightDecision(
                allowed=granted,
                operation=op,
                field=field,
                reason=None if granted else REASON_OPERATION_NOT_PERMITTED,
                source="field_override",
            )
        granted = bool((terms.operation_rights or {}).get(op, False))
        return RightDecision(
            allowed=granted,
            operation=op,
            field=field,
            reason=None if granted else REASON_OPERATION_NOT_PERMITTED,
            source="operation_default",
        )

    # -- the one write path -------------------------------------------------------------------------

    def _require_source(self, source_key: str) -> str:
        key = (source_key or "").strip()
        if not key:
            raise SourceTermsValidationError(
                REASON_SOURCE_NOT_FOUND, "a source_key is required for a terms version"
            )
        exists = self._session.scalar(select(Source.id).where(Source.key == key))
        if exists is None:
            raise SourceTermsValidationError(
                REASON_SOURCE_NOT_FOUND,
                f"no catalogued source with key '{key}' exists; catalogue it first (DCA-044)",
                detail=key,
            )
        return key

    def _next_version(self, source_key: str) -> int:
        current = self._session.scalar(
            select(func.max(SourceTerms.version)).where(SourceTerms.source_key == source_key)
        )
        return int(current or 0) + 1

    def add_version(self, data: SourceTermsInput) -> SourceTerms:
        """Validate and stage a NEW terms version for a source; return it (uncommitted — caller owns UoW).

        Validation (the source is catalogued, operations are recognized, contract evidence and a
        purpose are present, retention and the validity window are coherent) runs FIRST, so an invalid
        version raises :class:`SourceTermsValidationError` before any row is staged. The row is always
        ``status='draft'`` at ``max(version) + 1`` — insert-only, so a version never overwrites a prior
        one, and recording rights never puts them in force.
        """
        source_key = self._require_source(data.source_key)
        operation_rights = _normalize_operation_rights(data.operations)
        field_rights = _normalize_field_rights(data.field_rights)
        contract_evidence = _validate_contract_evidence(data.contract_evidence)
        purposes = _validate_purposes(data.purposes)
        retention = _validate_retention(data.retention)
        effective_from, expires_at = _validate_window(data.effective_from, data.expires_at)

        terms = SourceTerms(
            source_key=source_key,
            version=self._next_version(source_key),
            operation_rights=operation_rights,
            field_rights=field_rights,
            purposes=purposes,
            attribution={
                "required": bool((data.attribution or {}).get("required", False)),
                "notice": str((data.attribution or {}).get("notice", "")).strip(),
            },
            retention=retention,
            contract_evidence=contract_evidence,
            effective_from=effective_from,
            expires_at=expires_at,
            # Draft only — promotion to ACTIVE is a disabled future capability.
            status=SourceTermsStatus.DRAFT.value,
        )
        self._session.add(terms)
        return terms
