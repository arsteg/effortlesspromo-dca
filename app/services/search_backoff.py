"""DCA-149 — stop low-yield or exhausted searches (the per-partition no-new-result / backoff state).

The single job of this story: bound a REPEATED search per ``(business_id, search partition)`` so a market
that stops producing NEW results — or only produces marginal yield — is visibly PAUSED / backed off
rather than searched (and spent on) forever, or made to fabricate records. It adds a durable, inert
state machine over one :class:`~app.db.models.SearchPartitionBackoff` row per partition; it invents no
new fetch path, makes no provider / model call, and spends / sends nothing (the DCA has no spend/fetch
path regardless — this capability only ever makes a future scheduler do LESS).

A *search partition* is a logical repeated-search unit within a business — e.g. a confirmed-ICP +
source-scope fingerprint (:func:`partition_key_for` builds a stable, bounded key, but the key is
otherwise caller-supplied). After each bounded discovery round the caller records that round's YIELD
(:class:`RoundYield` — how many NEW results it produced versus how many it saw, which can be derived
from a finalized DCA-146 :class:`~app.db.models.DiscoveryRunSummary`). The recorder classifies the round
and advances the state machine:

- A round with no new results (empty / all-duplicate) → ``no_new_results``; a round with some but too
  few new results relative to what it saw → ``marginal_yield``. Either is a LOW-YIELD round.
- A low-yield round grows a consecutive-low-yield streak. Once the streak crosses
  ``search_low_yield_rounds_before_backoff`` the partition is :attr:`~app.db.models.SearchPartitionState.
  BACKING_OFF` for a bounded, exponentially-growing window (``search_backoff_base_seconds`` doubled each
  further low-yield round, capped at ``search_backoff_max_seconds``). Once the backoff steps up to
  ``search_exhaust_after_backoffs`` the partition is :attr:`~app.db.models.SearchPartitionState.EXHAUSTED`
  — paused with NO scheduled retry (an exhausted market).
- A good-yield round RESETS the streak + backoff and recovers the partition to
  :attr:`~app.db.models.SearchPartitionState.ACTIVE`.

The two acceptance guarantees:

- **An exhausted market is visibly paused / backed off, never repeatedly spent on or invented.** The state
  (and ``next_eligible_at``) is a durable, auditable record a future (still-disabled) search scheduler
  consults BEFORE a round via :meth:`DiscoverySearchBackoff.check_eligibility`; a backing-off or exhausted
  partition is refused. Recording a round reads already-recorded yield and performs no fetch / model /
  spend / send.

- **Reproduce the result and the relevant denial without unintended state changes.** The recorder checks
  authority FIRST (the same administrator ``RUN_PLAN_APPROVE`` scope the chargeable-discovery chain
  requires); a malformed (empty) partition key is refused before any state is read. Each refusal is raised
  BEFORE the session is mutated, so it changes nothing. An eligibility CHECK is a pure read that mutates
  nothing, so both an eligible result and a denial reproduce deterministically.

Like the sibling DCA-146/147 services, the recorder stages into the *caller's* session and never commits —
the route/worker owns the unit of work, so the backoff row and its atomic DCA-026 audit entry commit
together. Every query is confined to one business; there is no argument to widen the scope.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.sink import ActorType, AuditActor, AuditSink
from app.core.config import Settings
from app.db.models import DiscoveryRunSummary, SearchPartitionBackoff, SearchPartitionState
from app.identity.permissions import Scope, scopes_for_business_roles

# Where a backoff record stamps its origin on the provenance.
BACKOFF_SOURCE = "search_backoff"

# The audited action code for recording a search round's yield against a partition.
AUDIT_ACTION_RECORD_ROUND = "discovery_search.backoff.recorded"

# Stable low-yield reason codes — WHY a round counted as low yield (``REASON_NONE`` when it did not).
REASON_NONE = "none"  # a good-yield round (resets the streak / recovers the partition)
REASON_NO_NEW_RESULTS = "no_new_results"  # empty or all-duplicate: zero (or < min) new results
REASON_MARGINAL_YIELD = "marginal_yield"  # some new results, but below the marginal-yield ratio

# Stable eligibility denial codes (``None`` when eligible) — each reproducible with no state change.
DENY_BACKING_OFF = (
    "search_backing_off"  # within a bounded backoff window (next_eligible_at in future)
)
DENY_EXHAUSTED = "search_exhausted"  # the market looks exhausted — paused with no scheduled retry

# Stable refusal codes raised (never recorded) for a malformed / unauthorized request — each BEFORE any
# state is read or mutated, so the attempt changes nothing.
REASON_NOT_AUTHORIZED = "search_backoff_not_authorized"
REASON_INVALID_PARTITION = "invalid_search_partition"


def partition_key_for(icp_version: int, source_keys: Iterable[str]) -> str:
    """Build a stable, bounded (<=64 char) partition key from a confirmed-ICP version + source scope.

    A convenience so a caller can derive the same key for the same logical repeated search (the targeting
    version plus the sorted source keys it runs against). The source keys are fingerprinted so the key
    stays bounded regardless of how many sources the scope holds. The key is opaque — never contact data.
    """
    keys = sorted({(k or "").strip() for k in source_keys if (k or "").strip()})
    digest = hashlib.sha256("|".join(keys).encode("utf-8")).hexdigest()[:16]
    return f"icp:{int(icp_version)}:src:{digest}"


class SearchBackoffError(Exception):
    """A backoff record was refused with a stable ``code`` for a malformed request (an empty partition
    key). Raised before the session is mutated, so a refusal changes nothing. ``detail`` names the
    offending value."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class SearchBackoffAuthorizationError(SearchBackoffError):
    """The caller is not authorized to record a search round's backoff state.

    A distinct type so an HTTP boundary can map it to a 403. Raised FIRST — before any state is read or
    written. ``code`` is :data:`REASON_NOT_AUTHORIZED`.
    """

    def __init__(self, message: str) -> None:
        super().__init__(REASON_NOT_AUTHORIZED, message)


@dataclass(frozen=True)
class SearchBackoffActor:
    """The business principal recording a search round's yield.

    ``subject`` is an opaque actor id (never a name/email) recorded on the atomic audit entry. ``roles``
    are the caller's verified business-plane membership roles (from the tenant context, NEVER a bare token
    claim). :meth:`may_record` grants the action only when those roles yield
    :attr:`~app.identity.permissions.Scope.RUN_PLAN_APPROVE` — stopping / pausing a chargeable repeated
    search is part of the same administrator authority that governs the DCA-141..146 discovery chain. A
    read-only viewer and a platform operator (holding no business scope) cannot perform it.
    """

    subject: str
    roles: tuple[str, ...] = ()
    actor_type: str = "user"

    def may_record(self) -> bool:
        return Scope.RUN_PLAN_APPROVE in scopes_for_business_roles(self.roles)

    def as_audit_actor(self) -> AuditActor:
        return AuditActor(id=self.subject, type=ActorType(self.actor_type))


@dataclass(frozen=True)
class RoundYield:
    """One bounded search round's YIELD — how many NEW results it produced versus how many it saw.

    ``new_results`` is the count of genuinely NEW results the round added (e.g. the DCA-145 prospect
    suggestions a run produced); ``total_results`` is how many distinct subjects the round saw in total
    (new + already-known). Both are clamped non-negative. An EMPTY round (``total_results`` = 0) and an
    all-duplicate round (``new_results`` = 0) both read as no-new-result. These are caller-supplied /
    synthetic counts — the recorder fetches nothing.
    """

    new_results: int
    total_results: int

    @property
    def new(self) -> int:
        return max(0, int(self.new_results))

    @property
    def total(self) -> int:
        return max(0, int(self.total_results))

    @classmethod
    def from_run_summary(cls, summary: DiscoveryRunSummary) -> RoundYield:
        """Derive a round's yield from a finalized DCA-146 run summary: NEW = the suggestions it produced,
        TOTAL = the distinct subjects it resolved (so an empty / no-match run reads as no-new-result)."""
        return cls(
            new_results=int(summary.suggestion_count or 0),
            total_results=int(summary.subject_count or 0),
        )


@dataclass(frozen=True)
class RoundClassification:
    """How one round's yield was classified: whether it was low-yield and the stable reason code."""

    low_yield: bool
    reason: str
    new_results: int
    total_results: int


@dataclass
class RoundRecordResult:
    """The inert, inspectable outcome of recording ONE round against a partition.

    ``backoff`` is the advanced :class:`~app.db.models.SearchPartitionBackoff` row. The remaining fields
    echo the load-bearing state the round produced: whether it was ``low_yield`` and why, the current
    ``state`` / ``consecutive_low_yield`` / ``backoff_level``, the computed ``next_eligible_at``, and the
    ``backed_off`` / ``exhausted`` / ``recovered`` transitions this round caused. Nothing here is ever
    fetched, sent or spent.
    """

    business_id: str
    partition_key: str
    backoff: SearchPartitionBackoff
    low_yield: bool
    reason: str
    state: str
    consecutive_low_yield: int
    backoff_level: int
    next_eligible_at: datetime | None
    backed_off: bool
    exhausted: bool
    recovered: bool

    @property
    def paused(self) -> bool:
        """Whether the partition is now paused (backing off or exhausted) rather than plain active."""
        return self.state in SearchPartitionState.paused_values()


@dataclass
class EligibilityDecision:
    """The inert, inspectable outcome of a pure eligibility CHECK (it mutates nothing).

    ``eligible`` is whether a fresh search is permitted now; ``reason`` names the stable denial code when
    it is not (:data:`DENY_BACKING_OFF` / :data:`DENY_EXHAUSTED`), else ``None``. ``state`` is the
    partition's current backoff state and ``next_eligible_at`` when it may search again (``None`` for a
    never-recorded or exhausted partition).
    """

    business_id: str
    partition_key: str
    eligible: bool
    state: str
    reason: str | None = None
    next_eligible_at: datetime | None = None
    exists: bool = False


def classify_round(settings: Settings, round_yield: RoundYield) -> RoundClassification:
    """Classify one round's yield as low-yield (and why) using the DCA-149 thresholds (pure, no I/O).

    ``no_new_results`` when the round produced fewer than ``search_low_yield_min_new_results`` NEW results
    (an empty or all-duplicate round is the zero case); ``marginal_yield`` when it produced some new
    results but their NEW/total ratio is below ``search_marginal_yield_min_ratio``; otherwise a good
    (not-low) round. Deterministic — the same inputs always yield the same classification.
    """
    new = round_yield.new
    total = round_yield.total
    if new < max(1, int(settings.search_low_yield_min_new_results)):
        return RoundClassification(True, REASON_NO_NEW_RESULTS, new, total)
    ratio = (new / total) if total > 0 else 1.0
    if total > 0 and ratio < float(settings.search_marginal_yield_min_ratio):
        return RoundClassification(True, REASON_MARGINAL_YIELD, new, total)
    return RoundClassification(False, REASON_NONE, new, total)


class DiscoverySearchBackoff:
    """Record / inspect the no-new-result backoff state of search partitions, for ONE business (tenant-scoped).

    Construct with the active session and the verified business scope (a route/worker resolves it from the
    membership-verified tenant context). Every query is confined to this business — there is no argument to
    widen the scope. The recorder stages into the caller's session and never commits; it advances only an
    inert state machine and so fetches / sends / spends nothing.
    """

    def __init__(self, session: Session, business_id: str, settings: Settings) -> None:
        self._session = session
        self._business_id = business_id
        self._settings = settings

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def get(self, partition_key: str) -> SearchPartitionBackoff | None:
        """The backoff row for one partition WITHIN this business, or ``None`` (an unknown / cross-tenant
        partition reads as not-found, never surfaced)."""
        key = (partition_key or "").strip()
        if not key:
            return None
        stmt = (
            select(SearchPartitionBackoff)
            .where(SearchPartitionBackoff.business_id == self._business_id)
            .where(SearchPartitionBackoff.partition_key == key)
        )
        return self._session.scalars(stmt).one_or_none()

    def partitions(self) -> list[SearchPartitionBackoff]:
        """Every backoff row recorded in this business, newest-updated first."""
        stmt = (
            select(SearchPartitionBackoff)
            .where(SearchPartitionBackoff.business_id == self._business_id)
            .order_by(SearchPartitionBackoff.updated_at.desc(), SearchPartitionBackoff.id)
        )
        return list(self._session.scalars(stmt).all())

    def check_eligibility(
        self, partition_key: str, *, now: datetime | None = None
    ) -> EligibilityDecision:
        """Whether a fresh search is permitted for ``partition_key`` right now — a PURE read (mutates nothing).

        A never-recorded partition is eligible (ACTIVE). A BACKING_OFF partition is refused until its
        ``next_eligible_at`` passes (:data:`DENY_BACKING_OFF`), then eligible again. An EXHAUSTED partition
        is refused with no scheduled retry (:data:`DENY_EXHAUSTED`). Both the eligible result and the denial
        reproduce deterministically because nothing is staged.
        """
        moment = now or datetime.now(UTC)
        key = (partition_key or "").strip()
        row = self.get(key)
        if row is None:
            return EligibilityDecision(
                business_id=self._business_id,
                partition_key=key,
                eligible=True,
                state=SearchPartitionState.ACTIVE.value,
                exists=False,
            )
        if row.state == SearchPartitionState.EXHAUSTED.value:
            return EligibilityDecision(
                business_id=self._business_id,
                partition_key=key,
                eligible=False,
                state=row.state,
                reason=DENY_EXHAUSTED,
                next_eligible_at=None,
                exists=True,
            )
        if row.state == SearchPartitionState.BACKING_OFF.value:
            next_at = _as_utc(row.next_eligible_at)
            if next_at is not None and moment < next_at:
                return EligibilityDecision(
                    business_id=self._business_id,
                    partition_key=key,
                    eligible=False,
                    state=row.state,
                    reason=DENY_BACKING_OFF,
                    next_eligible_at=next_at,
                    exists=True,
                )
            # The backoff window has elapsed — a retry is permitted. The state stays BACKING_OFF until the
            # next recorded round either recovers it (good yield) or grows the window (another low yield).
            return EligibilityDecision(
                business_id=self._business_id,
                partition_key=key,
                eligible=True,
                state=row.state,
                next_eligible_at=next_at,
                exists=True,
            )
        return EligibilityDecision(
            business_id=self._business_id,
            partition_key=key,
            eligible=True,
            state=row.state,
            exists=True,
        )

    # -- the one write path -------------------------------------------------------------------------

    def record_round(
        self,
        partition_key: str,
        round_yield: RoundYield,
        actor: SearchBackoffActor,
        *,
        run_id: str | None = None,
        now: datetime | None = None,
    ) -> RoundRecordResult:
        """Record one bounded search round's yield against ``partition_key`` and advance the backoff state.

        Order (each gate leaves prior state unchanged when it refuses):

        1. **Authority FIRST.** A caller lacking :attr:`~app.identity.permissions.Scope.RUN_PLAN_APPROVE`
           is refused (:class:`SearchBackoffAuthorizationError`) before any state is read.
        2. **A valid partition.** An empty partition key is refused (:data:`REASON_INVALID_PARTITION`).
        3. **Classify + advance.** The round is classified (low-yield + reason); a good round resets the
           streak / backoff and recovers the partition to ACTIVE; a low-yield round grows the streak and,
           once it crosses the threshold, backs the partition off (exponential window) or — at the ceiling —
           marks it EXHAUSTED. The row is upserted in place (one row per partition) and an atomic DCA-026
           audit entry is staged in the caller's unit of work.

        Recording reads already-recorded yield and performs NO fetch, model call, spend or send; it never
        invents a result.
        """
        moment = now or datetime.now(UTC)

        # 1) Authority FIRST — an unauthorized caller is refused with no state touched.
        if not actor.may_record():
            raise SearchBackoffAuthorizationError(
                "recording a search round's backoff state requires a business administrator holding "
                f"'{Scope.RUN_PLAN_APPROVE.value}'; a read-only viewer or a platform operator "
                "(holding no business scope) cannot perform it"
            )

        # 2) A valid partition key — refuse an empty one before any state is read.
        key = (partition_key or "").strip()
        if not key:
            raise SearchBackoffError(
                REASON_INVALID_PARTITION,
                "a search partition key is required",
                detail=partition_key,
            )

        classification = classify_round(self._settings, round_yield)

        row = self.get(key)
        if row is None:
            row = SearchPartitionBackoff(
                business_id=self._business_id,
                partition_key=key,
                state=SearchPartitionState.ACTIVE.value,
                rounds_recorded=0,
                consecutive_low_yield=0,
                backoff_level=0,
            )
            self._session.add(row)

        prev_state = row.state
        backed_off = False
        exhausted = False
        recovered = False

        if not classification.low_yield:
            # A good-yield round recovers the partition: reset the streak + backoff, back to ACTIVE.
            recovered = prev_state in SearchPartitionState.paused_values()
            row.state = SearchPartitionState.ACTIVE.value
            row.consecutive_low_yield = 0
            row.backoff_level = 0
            row.next_eligible_at = None
        else:
            row.consecutive_low_yield = int(row.consecutive_low_yield or 0) + 1
            threshold = max(1, int(self._settings.search_low_yield_rounds_before_backoff))
            if row.consecutive_low_yield >= threshold:
                row.backoff_level = int(row.backoff_level or 0) + 1
                if row.backoff_level >= max(1, int(self._settings.search_exhaust_after_backoffs)):
                    # The backoff stepped up to the ceiling — an exhausted market, paused with no retry.
                    row.state = SearchPartitionState.EXHAUSTED.value
                    row.next_eligible_at = None
                    exhausted = True
                else:
                    window = self._backoff_window_seconds(row.backoff_level)
                    row.state = SearchPartitionState.BACKING_OFF.value
                    row.next_eligible_at = moment + timedelta(seconds=window)
                    backed_off = True
            else:
                # Still accumulating the low-yield streak — not yet over the bound, stays ACTIVE.
                row.state = SearchPartitionState.ACTIVE.value
                row.next_eligible_at = None

        row.rounds_recorded = int(row.rounds_recorded or 0) + 1
        row.last_new_count = classification.new_results
        row.last_total_count = classification.total_results
        row.last_low_yield = classification.low_yield
        row.last_stop_reason = classification.reason
        row.last_round_at = moment
        if run_id:
            row.last_run_id = run_id
        row.provenance = {
            "source": BACKOFF_SOURCE,
            "fromState": prev_state,
            "toState": row.state,
            "reason": classification.reason,
            "lowYield": classification.low_yield,
            "backedOff": backed_off,
            "exhausted": exhausted,
            "recovered": recovered,
            # Load-bearing invariants: recording reads recorded yield and never sends or charges, and it
            # never invents a result (the DCA has no spend/fetch path regardless).
            "sent": False,
            "spent": False,
            "invented": False,
        }

        self._record_audit(row, classification, actor, run_id=run_id)

        return RoundRecordResult(
            business_id=self._business_id,
            partition_key=key,
            backoff=row,
            low_yield=classification.low_yield,
            reason=classification.reason,
            state=row.state,
            consecutive_low_yield=row.consecutive_low_yield,
            backoff_level=row.backoff_level,
            next_eligible_at=_as_utc(row.next_eligible_at),
            backed_off=backed_off,
            exhausted=exhausted,
            recovered=recovered,
        )

    # -- internals ----------------------------------------------------------------------------------

    def _backoff_window_seconds(self, backoff_level: int) -> int:
        """The bounded, exponentially-growing backoff window for a given step: base * 2^(level-1), capped
        at the hard maximum. Level 1 → base; each further step doubles until the cap."""
        base = int(self._settings.search_backoff_base_seconds)
        cap = int(self._settings.search_backoff_max_seconds)
        step = max(1, int(backoff_level))
        window = base * (2 ** (step - 1))
        return min(window, cap)

    def _record_audit(
        self,
        row: SearchPartitionBackoff,
        classification: RoundClassification,
        actor: SearchBackoffActor,
        *,
        run_id: str | None,
    ) -> None:
        """Stage the atomic DCA-026 audit entry for one recorded round, in the caller's unit of work.

        References stay OPAQUE — the partition key, the states, the counts and (as an epoch int) the next
        eligible time — never a contact value. The next-eligible time is recorded as an integer epoch so
        it is a plainly-opaque reference rather than a digit-dense string the audit screen would flag.
        """
        next_at = _as_utc(row.next_eligible_at)
        references: dict = {
            "partitionKey": row.partition_key,
            "roundReason": classification.reason,
            "state": row.state,
            "consecutiveLowYield": row.consecutive_low_yield,
            "backoffLevel": row.backoff_level,
            "roundsRecorded": row.rounds_recorded,
            "newResults": classification.new_results,
            "totalResults": classification.total_results,
            "lowYield": classification.low_yield,
            "nextEligibleEpoch": int(next_at.timestamp()) if next_at is not None else None,
        }
        if run_id:
            references["runId"] = run_id
        AuditSink(
            self._session, business_id=self._business_id, actor=actor.as_audit_actor()
        ).record(
            action=AUDIT_ACTION_RECORD_ROUND,
            reason=(
                f"search round on partition '{row.partition_key}' → {row.state} "
                f"({classification.reason}): {classification.new_results} new of "
                f"{classification.total_results}; streak {row.consecutive_low_yield}, "
                f"backoff level {row.backoff_level}"
            ),
            references=references,
        )


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalize a (possibly naive — SQLite) stored datetime to tz-aware UTC for safe comparison."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "BACKOFF_SOURCE",
    "AUDIT_ACTION_RECORD_ROUND",
    "REASON_NONE",
    "REASON_NO_NEW_RESULTS",
    "REASON_MARGINAL_YIELD",
    "DENY_BACKING_OFF",
    "DENY_EXHAUSTED",
    "REASON_NOT_AUTHORIZED",
    "REASON_INVALID_PARTITION",
    "partition_key_for",
    "SearchBackoffError",
    "SearchBackoffAuthorizationError",
    "SearchBackoffActor",
    "RoundYield",
    "RoundClassification",
    "RoundRecordResult",
    "EligibilityDecision",
    "classify_round",
    "DiscoverySearchBackoff",
]
