"""DCA-117 — expose business spend summaries.

Key: ``spend-view`` · source sections 8, 13. Stage: Cost reservations and provider accounting.

One capability: a READ-ONLY aggregation of a business's own spend ledger — how much is currently
RESERVED (an ``ACTIVE`` hold not yet reconciled), how much was INCURRED (a ``SETTLED`` hold's actual
cost), how much is UNCERTAIN (a ``UNCERTAIN`` hold whose external result could not be confirmed) and how
much headroom REMAINS — broken down by run, civil day and civil month. It summarizes exactly the DCA-113
:class:`~app.db.models.BudgetReservation` ledger as DCA-114 reconciliation left it, against the DCA-042
:class:`~app.db.models.BudgetLimit` caps in force. It writes NOTHING, spends nothing and sends nothing —
it is a pure read a reviewer / later Web/Admin consumer can inspect and reproduce.

The load-bearing guarantees (the DCA-117 acceptance criteria):

- **The numbers reconcile with the ledger.** The per-status, per-day, per-month and per-run figures are
  summed directly from the committed reservation rows, using the SAME effective-amount convention the
  DCA-113 reserve path enforces (:meth:`~app.cost.reserve.BudgetReservationStore._reserved_total`):
  ``ACTIVE``/``UNCERTAIN`` count their full reserved amount, ``SETTLED`` counts its settled amount, and
  only ``RELEASED`` counts zero. So ``committed`` (what still counts toward the caps) equals
  ``reserved + incurred + uncertain``, and the remaining headroom the summary reports matches what the
  next reservation would see.

- **Uncertain spend is never shown as free budget.** A ``UNCERTAIN`` hold (a dispatched-then-unconfirmed
  provider call whose cost MAY exist — DCA-114/115) is reported in its OWN ``uncertain`` bucket AND
  counted in ``committed`` (so it reduces ``remaining``); it is NEVER folded into ``released`` / free
  budget. ``released`` is strictly the freed holds (``RELEASED``). A caller can therefore never mistake
  an unconfirmed possible charge for reclaimed budget.

- **Tenant-scoped.** The store is bound to one business and every query is confined to its
  ``business_id`` — there is no argument to widen the scope, so another tenant's spend is structurally
  invisible here. The HTTP surface additionally gates on an administrator read scope and path-business
  membership, so one tenant can never read another's cost details.

Summarizing reads only this ledger; it performs no provider/model/paid call and holds, charges, spends
and sends nothing (the DCA has no such path — all of that stays a disabled future capability).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.cost.reserve import BudgetReservationStore
from app.db.models import BudgetReservation, ReservationStatus

_ACTIVE = ReservationStatus.ACTIVE.value
_SETTLED = ReservationStatus.SETTLED.value
_RELEASED = ReservationStatus.RELEASED.value
_UNCERTAIN = ReservationStatus.UNCERTAIN.value

# The effective SETTLED amount a hold counts toward the caps: its settled amount, or (defensively) its
# reserved amount if a settled row somehow carries no settled amount. Mirrors the DCA-113 reserve path.
_SETTLED_EFFECTIVE = func.coalesce(
    BudgetReservation.settled_amount_minor, BudgetReservation.amount_minor
)


@dataclass(frozen=True)
class SpendBucket:
    """A read-only roll-up of one spend window (the grand total, one civil day, or one civil month).

    Amounts are exact INR **minor units (paise)**. The buckets are disjoint by status:
    ``reserved_minor`` is the ``ACTIVE`` (held, not yet reconciled) amount, ``incurred_minor`` the
    ``SETTLED`` actual cost, ``uncertain_minor`` the ``UNCERTAIN`` preserved amount, ``released_minor``
    the ``RELEASED`` freed amount. ``committed_minor`` is what still counts toward the caps
    (``reserved + incurred + uncertain`` — note it POINTEDLY includes uncertain, never released).
    ``overspend_minor`` is the flagged, clamped over-run across the window. ``reservation_count`` is how
    many reservations fall in the window.
    """

    key: str
    reserved_minor: int
    incurred_minor: int
    uncertain_minor: int
    released_minor: int
    committed_minor: int
    overspend_minor: int
    reservation_count: int


@dataclass(frozen=True)
class RunSpendLine:
    """One run's spend line — a single :class:`~app.db.models.BudgetReservation` (one run holds one cost).

    ``run_key`` is the caller's idempotency key (the logical run identity); ``reservation_id`` the row id.
    ``reserved_minor`` is the original held amount; ``incurred_minor`` the settled actual cost (``None``
    unless the hold is ``SETTLED``); ``committed_minor`` what the row still counts toward the caps (its
    full amount while ACTIVE/UNCERTAIN, the settled amount once SETTLED, 0 once RELEASED). ``uncertain``
    flags a preserved possible-cost hold. All amounts are exact INR minor units (paise)."""

    reservation_id: str
    run_key: str
    provider: str
    status: str
    day_key: str
    month_key: str
    reserved_minor: int
    incurred_minor: int | None
    committed_minor: int
    overspend_minor: int
    uncertain: bool


@dataclass(frozen=True)
class SpendSummary:
    """A business's read-only spend summary over its DCA-113/114 reservation ledger.

    ``configured`` is ``False`` when the business has no in-force :class:`~app.db.models.BudgetLimit`; the
    cap / remaining fields are then ``None`` (there is nothing to measure headroom against) but the
    ledger roll-ups are still reported. The cap / remaining fields describe the CURRENT civil day and
    month (in the caps' India-local timezone), matching what the next reservation would see. The
    ``total`` / ``by_day`` / ``by_month`` / ``by_run`` roll-ups cover the WHOLE ledger (all time)."""

    business_id: str
    generated_at: datetime
    configured: bool
    budget_version: int | None
    currency: str | None
    timezone: str | None
    day_key: str | None
    month_key: str | None
    daily_cap_minor: int | None
    monthly_cap_minor: int | None
    per_run_cap_minor: int | None
    daily_remaining_minor: int | None
    monthly_remaining_minor: int | None
    total: SpendBucket
    by_run: tuple[RunSpendLine, ...]
    by_day: tuple[SpendBucket, ...]
    by_month: tuple[SpendBucket, ...]


class BudgetSpendSummaryStore:
    """Tenant-scoped, READ-ONLY aggregator of a business's spend ledger, bound to one business.

    Construct with the active session and the verified business scope (a route resolves it from the
    membership-verified tenant context). Every query is confined to that business — there is no argument
    to widen the scope, so another tenant's spend is invisible here. It never writes, commits, spends or
    sends; it only reads the committed DCA-113/114 ledger and the DCA-042 caps.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    def summarize(self, *, now: datetime | None = None) -> SpendSummary:
        """Aggregate this business's reservation ledger into a reviewable spend summary.

        The caps / remaining headroom describe the CURRENT civil day & month (reusing the DCA-113
        :meth:`~app.cost.reserve.BudgetReservationStore.window` snapshot so the numbers match what the
        next reservation would see — uncertain holds already reduce that remaining). The status / day /
        month / run roll-ups cover the whole ledger. Pure read: nothing is written, spent or sent.
        """
        moment = now or datetime.now(UTC)
        window = BudgetReservationStore(self._session, self._business_id).window(now=moment)

        return SpendSummary(
            business_id=self._business_id,
            generated_at=moment,
            configured=window.configured,
            budget_version=window.budget_version,
            currency=self._currency(),
            timezone=window.timezone,
            day_key=window.day_key,
            month_key=window.month_key,
            daily_cap_minor=window.daily_cap_minor,
            monthly_cap_minor=window.monthly_cap_minor,
            per_run_cap_minor=window.per_run_cap_minor,
            daily_remaining_minor=window.daily_remaining_minor,
            monthly_remaining_minor=window.monthly_remaining_minor,
            total=self._total_bucket(),
            by_run=self._run_lines(),
            by_day=self._grouped_buckets(BudgetReservation.day_key),
            by_month=self._grouped_buckets(BudgetReservation.month_key),
        )

    # -- internals ----------------------------------------------------------------------------------

    def _currency(self) -> str | None:
        """The currency the ledger is denominated in (India-only ⇒ INR), read from any reservation row;
        ``None`` only when the business has no reservations at all."""
        stmt = (
            select(BudgetReservation.currency)
            .where(BudgetReservation.business_id == self._business_id)
            .limit(1)
        )
        return self._session.scalars(stmt).first()

    def _sum_columns(self) -> list:
        """The per-status sum expressions, in the fixed order :meth:`_bucket` consumes."""
        return [
            func.coalesce(
                func.sum(
                    case(
                        (BudgetReservation.status == _ACTIVE, BudgetReservation.amount_minor),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(
                func.sum(case((BudgetReservation.status == _SETTLED, _SETTLED_EFFECTIVE), else_=0)),
                0,
            ),
            func.coalesce(
                func.sum(
                    case(
                        (BudgetReservation.status == _UNCERTAIN, BudgetReservation.amount_minor),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(
                func.sum(
                    case(
                        (BudgetReservation.status == _RELEASED, BudgetReservation.amount_minor),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(func.sum(BudgetReservation.overspend_minor), 0),
            func.count(),
        ]

    def _bucket(self, key: str, row: tuple) -> SpendBucket:
        """Build a bucket from a (reserved, incurred, uncertain, released, overspend, count) row.

        ``committed`` POINTEDLY sums reserved + incurred + uncertain — an uncertain (possible) charge
        still counts toward the caps and is never treated as free budget (only ``released`` is freed)."""
        reserved, incurred, uncertain, released, overspend, count = (int(v) for v in row)
        return SpendBucket(
            key=key,
            reserved_minor=reserved,
            incurred_minor=incurred,
            uncertain_minor=uncertain,
            released_minor=released,
            committed_minor=reserved + incurred + uncertain,
            overspend_minor=overspend,
            reservation_count=count,
        )

    def _total_bucket(self) -> SpendBucket:
        """The grand total across the whole ledger for this business."""
        stmt = select(*self._sum_columns()).where(
            BudgetReservation.business_id == self._business_id
        )
        row = self._session.execute(stmt).one()
        return self._bucket("", tuple(row))

    def _grouped_buckets(self, group_col) -> tuple[SpendBucket, ...]:
        """Per-window buckets grouped by ``group_col`` (the day_key or month_key), ordered by the key."""
        stmt = (
            select(group_col, *self._sum_columns())
            .where(BudgetReservation.business_id == self._business_id)
            .group_by(group_col)
            .order_by(group_col)
        )
        return tuple(
            self._bucket(row[0], tuple(row[1:])) for row in self._session.execute(stmt).all()
        )

    def _run_lines(self) -> tuple[RunSpendLine, ...]:
        """One line per reservation (one run), oldest first — the reviewable per-run breakdown."""
        stmt = (
            select(BudgetReservation)
            .where(BudgetReservation.business_id == self._business_id)
            .order_by(BudgetReservation.created_at, BudgetReservation.id)
        )
        lines: list[RunSpendLine] = []
        for r in self._session.scalars(stmt).all():
            if r.status == _RELEASED:
                committed = 0
            elif r.status == _SETTLED:
                committed = int(
                    r.settled_amount_minor if r.settled_amount_minor is not None else r.amount_minor
                )
            else:  # ACTIVE / UNCERTAIN — the full hold still counts.
                committed = int(r.amount_minor)
            lines.append(
                RunSpendLine(
                    reservation_id=r.id,
                    run_key=r.idempotency_key,
                    provider=r.provider,
                    status=r.status,
                    day_key=r.day_key,
                    month_key=r.month_key,
                    reserved_minor=int(r.amount_minor),
                    incurred_minor=(
                        int(
                            r.settled_amount_minor
                            if r.settled_amount_minor is not None
                            else r.amount_minor
                        )
                        if r.status == _SETTLED
                        else None
                    ),
                    committed_minor=committed,
                    overspend_minor=int(r.overspend_minor or 0),
                    uncertain=r.status == _UNCERTAIN,
                )
            )
        return tuple(lines)
