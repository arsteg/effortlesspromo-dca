"""DCA-042 — the versioned per-business spending-limit store.

:class:`BudgetLimitStore` is the single sanctioned writer of :class:`~app.db.models.BudgetLimit`. It
holds, per business, a *versioned* set of INR spend caps — a daily cap, a monthly cap and a per-run
cap — plus the timezone whose civil calendar defines the daily/monthly windows.

The load-bearing guarantees (the DCA-042 acceptance criteria):

- **Limits are business-scoped and versioned.** :meth:`BudgetLimitStore.save` always inserts a NEW row
  at ``max(version) + 1`` for the one ``business_id`` the store is constructed with; it never updates
  or deletes an existing version, so every prior version stays readable as an immutable, auditable
  history and one tenant can never read or change another's limits (defence-in-depth RLS backs the
  app-level scope on Postgres). The store stages into the caller's session and never commits — the
  caller (the HTTP route, via ``commit_idempotent``) owns the unit of work, so the limit row and its
  atomic audit entry commit together. There is no argument to widen the business scope.

- **Invalid / negative limits are rejected before anything is staged.** Validation runs FIRST, so a
  refusal raises :class:`BudgetValidationError` with a stable code and leaves no row and no partial
  state: a non-INR currency (the service is India-only), a negative cap, an incoherent ordering
  (``per_run <= daily <= monthly`` must hold), or a timezone that is not India's civil zone each has a
  distinct code mapped to a 422 at the HTTP boundary.

- **Changes are audited.** Each save stages an atomic DCA-026 audit entry recording WHO set the
  version and the opaque cap figures, in the SAME unit of work as the row. (Authorization — only an
  administrator may create a version — is enforced one layer up by the route's ``BUDGET_MANAGE`` gate,
  which audits the grant/denial too.)

Amounts are **integer minor units (INR paise)** throughout — exact, never a float — so a limit never
accrues binary-float rounding error.
"""

from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.sink import AuditActor, AuditSink
from app.db.models import BudgetLimit

# The only currency this India-only service accepts. A value other than this is a clear 422.
SUPPORTED_CURRENCY = "INR"

# India observes a single civil timezone. Caps are bounded by this zone's day/month; the store accepts
# it and normalizes the legacy ``Asia/Calcutta`` alias to the canonical name. Any other zone is refused
# (the service is India-only), so a daily/monthly window is always India-local and unambiguous.
CANONICAL_TIMEZONE = "Asia/Kolkata"
_TIMEZONE_ALIASES = {"asia/calcutta": CANONICAL_TIMEZONE, "asia/kolkata": CANONICAL_TIMEZONE}

# The audited action code for setting spending limits (a controlled code, like the other DCA writes).
AUDIT_ACTION_SET_LIMITS = "budget.limits.set"

# Stable validation codes (mapped to 422 at the HTTP boundary). Each is raised BEFORE the session is
# mutated, so a refusal leaves no row and no partial state.
REASON_UNSUPPORTED_CURRENCY = "unsupported_currency"
REASON_NEGATIVE_CAP = "negative_cap"
REASON_PER_RUN_EXCEEDS_DAILY = "per_run_exceeds_daily"
REASON_DAILY_EXCEEDS_MONTHLY = "daily_exceeds_monthly"
REASON_UNSUPPORTED_TIMEZONE = "unsupported_timezone"


class BudgetValidationError(Exception):
    """A spending-limit version was refused with a stable code. Raised before the session is mutated,
    so a refusal leaves no row and no partial state. ``detail`` names the offending value."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass
class BudgetLimitInput:
    """One set of spending limits as supplied by the caller.

    Amounts are INR **minor units (paise)** — integers, exact. ``currency`` must be ``INR`` and
    ``timezone`` must be India's civil zone; both are validated (and the timezone normalized) before
    anything is staged.
    """

    daily_cap_minor: int
    monthly_cap_minor: int
    per_run_cap_minor: int
    currency: str = SUPPORTED_CURRENCY
    timezone: str = CANONICAL_TIMEZONE


def _validate_currency(currency: str) -> str:
    """Accept only INR (India-only); normalize case. Anything else is a clear 422."""
    value = (currency or "").strip().upper()
    if value != SUPPORTED_CURRENCY:
        raise BudgetValidationError(
            REASON_UNSUPPORTED_CURRENCY,
            f"unsupported currency '{currency}'; this build is India-only ({SUPPORTED_CURRENCY})",
            detail=currency,
        )
    return value


def _validate_timezone(timezone: str) -> str:
    """Accept only India's civil zone, resolving the legacy alias; validate it exists in the tz db.

    A caps window is India-local by definition (the service is India-only). The canonical name is
    ``Asia/Kolkata``; ``Asia/Calcutta`` is accepted as its historical alias and normalized. Any other
    (foreign or unknown) zone is refused with a clear code, before any row is staged.
    """
    token = (timezone or "").strip()
    canonical = _TIMEZONE_ALIASES.get(token.lower())
    if canonical is None:
        raise BudgetValidationError(
            REASON_UNSUPPORTED_TIMEZONE,
            f"unsupported timezone '{timezone}'; expected India's civil zone "
            f"('{CANONICAL_TIMEZONE}', the service is India-only)",
            detail=timezone,
        )
    try:
        # Defence-in-depth: prove the canonical name actually resolves in the installed tz database,
        # so a stored zone is never one a reader cannot load.
        ZoneInfo(canonical)
    except ZoneInfoNotFoundError as exc:  # pragma: no cover - canonical zone is always present
        raise BudgetValidationError(
            REASON_UNSUPPORTED_TIMEZONE,
            f"timezone '{canonical}' is not available in the time-zone database",
            detail=canonical,
        ) from exc
    return canonical


def _validate_caps(daily: int, monthly: int, per_run: int) -> None:
    """Reject negative or incoherent caps (each a clear 422), before any row is staged.

    Caps are non-negative integer minor units, and the three windows must be coherent — a per-run cap
    can never exceed the daily cap, and a daily cap can never exceed the monthly cap — so a stored
    limit set can never describe an impossible budget.
    """
    for label, value in (("daily", daily), ("monthly", monthly), ("perRun", per_run)):
        if value < 0:
            raise BudgetValidationError(
                REASON_NEGATIVE_CAP,
                f"{label} cap must be a non-negative amount in INR minor units (got {value})",
                detail=label,
            )
    if per_run > daily:
        raise BudgetValidationError(
            REASON_PER_RUN_EXCEEDS_DAILY,
            f"per-run cap ({per_run}) cannot exceed the daily cap ({daily})",
            detail="perRun",
        )
    if daily > monthly:
        raise BudgetValidationError(
            REASON_DAILY_EXCEEDS_MONTHLY,
            f"daily cap ({daily}) cannot exceed the monthly cap ({monthly})",
            detail="daily",
        )


class BudgetLimitStore:
    """Tenant-scoped reader/writer for versioned spending limits, bound to one business.

    Construct with the active session and the verified business scope (the route resolves it from the
    membership-verified tenant context). Every query is confined to that business.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def versions(self) -> list[BudgetLimit]:
        """All spending-limit versions for this business, oldest first — prior versions are retained."""
        stmt = (
            select(BudgetLimit)
            .where(BudgetLimit.business_id == self._business_id)
            .order_by(BudgetLimit.version)
        )
        return list(self._session.scalars(stmt).all())

    def get_version(self, version: int) -> BudgetLimit | None:
        """One spending-limit version within this business, or ``None`` (a cross-tenant/unknown version
        reads as not-found, never surfaced)."""
        stmt = (
            select(BudgetLimit)
            .where(BudgetLimit.business_id == self._business_id)
            .where(BudgetLimit.version == version)
        )
        return self._session.scalars(stmt).one_or_none()

    def latest(self) -> BudgetLimit | None:
        """The highest-version (in-force) limit set for this business, or ``None`` if none is set."""
        stmt = (
            select(BudgetLimit)
            .where(BudgetLimit.business_id == self._business_id)
            .order_by(BudgetLimit.version.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).one_or_none()

    # -- the one write path -------------------------------------------------------------------------

    def _next_version(self) -> int:
        current = self._session.scalar(
            select(func.max(BudgetLimit.version)).where(
                BudgetLimit.business_id == self._business_id
            )
        )
        return int(current or 0) + 1

    def save(self, data: BudgetLimitInput, actor: AuditActor) -> BudgetLimit:
        """Validate and stage a NEW spending-limit version; return it (uncommitted — caller owns the UoW).

        Validation (INR only, non-negative + coherent caps, India timezone) runs FIRST, so an invalid
        limit set raises :class:`BudgetValidationError` before any row is staged. Preserves every prior
        version (insert-only; no update/delete). Stages an atomic DCA-026 audit entry recording the
        actor and the opaque cap figures in the SAME unit of work as the row.
        """
        currency = _validate_currency(data.currency)
        timezone = _validate_timezone(data.timezone)
        _validate_caps(data.daily_cap_minor, data.monthly_cap_minor, data.per_run_cap_minor)

        version = self._next_version()
        limit = BudgetLimit(
            business_id=self._business_id,
            version=version,
            currency=currency,
            daily_cap_minor=data.daily_cap_minor,
            monthly_cap_minor=data.monthly_cap_minor,
            per_run_cap_minor=data.per_run_cap_minor,
            timezone=timezone,
            actor_id=actor.id,
            actor_type=actor.type.value,
        )
        self._session.add(limit)

        # Atomic audit (SEC-001/BR-122 discipline): staged into the caller's session so it commits with
        # the limit row. References are opaque (version + integer cap figures + currency/zone).
        AuditSink(self._session, business_id=self._business_id, actor=actor).record(
            action=AUDIT_ACTION_SET_LIMITS,
            reason=f"spending limits set to version {version}",
            references={
                "version": version,
                "currency": currency,
                "dailyCapMinor": data.daily_cap_minor,
                "monthlyCapMinor": data.monthly_cap_minor,
                "perRunCapMinor": data.per_run_cap_minor,
                "timezone": timezone,
            },
        )
        return limit
