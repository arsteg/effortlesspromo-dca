"""DCA-121 — enforce crawl limits (robots rules, per-host crawl-delay / rate / concurrency).

Key: ``fetch-policy`` · source sections 6, 8, 9. Stage: Approved source access.

The crawl-etiquette half of DCA-121 (the rights-first composition is in
:mod:`app.sources.fetch_policy`). Once the rights chain has permitted an operation (DCA-048), a
would-be web fetch must ALSO respect the crawl rules the source's robots policy and rate limits
impose. This module is the single place those limits are decided and, for the rate/crawl-delay/
concurrency limits, DURABLY enforced so they hold across concurrent jobs.

Two pieces:

1. :func:`evaluate_crawl_limits` — a **pure, deterministic, network-free decider** in the spirit of
   :mod:`app.sources.destination_guard`: given a :class:`CrawlPolicy`, the observed per-host
   :class:`HostCrawlObservation` (the durable counters as they stand) and the request ``path`` + a
   ``now``, it returns an inert :class:`CrawlDecision`. It fetches nothing, parses no robots.txt
   itself (the Allow/Disallow rules are supplied, already parsed — the guard resolves nothing), and
   changes no state.

   Checked in a fixed order, STOP-AT-FIRST-FAILURE so the reason is deterministic:

   - **robots allowed-path** — the request ``path`` is matched against the policy's Allow/Disallow
     rules using the RFC 9309 longest-match rule (the most specific rule wins; an Allow wins a
     length tie). A disallowed path → ``ROBOTS_DISALLOWED`` (a hard ``DENY``). Robots is a *crawl*
     rule, decided only AFTER rights — it can never, on its own, authorize a fetch, and (per the
     composition in :mod:`app.sources.fetch_policy`) a permissive robots.txt can never override a
     license denial.
   - **crawl-delay** — a reserve inside the per-host ``crawl_delay_seconds`` of the last request →
     ``CRAWL_DELAY_NOT_ELAPSED`` (a retriable ``DEFER``).
   - **rate cap** — the current window already holds ``max_requests`` reserved requests →
     ``RATE_LIMIT_EXCEEDED`` (``DEFER``). This is the limit that must hold across concurrent jobs.
   - **concurrency cap** — ``max_concurrent`` requests are already in flight for this host →
     ``CONCURRENCY_LIMIT_EXCEEDED`` (``DEFER``).

2. :class:`HostCrawlLimiter` — the **durable** store that makes the rate / crawl-delay / concurrency
   limits hold ACROSS concurrent jobs. :meth:`HostCrawlLimiter.reserve` takes a ``FOR UPDATE`` lock on
   the ``(business, source, host)`` :class:`~app.db.models.HostCrawlState` row (creating it on first
   use), rolls the rate window if it has elapsed, composes :func:`evaluate_crawl_limits` against the
   LIVE committed counters, and ONLY on ``ALLOW`` records the reservation (bumps the window count, the
   in-flight count and ``last_request_at``). On PostgreSQL the row lock serializes concurrent reserves
   for the same host, so two jobs can never both be admitted past a cap (proven on real Postgres); on
   SQLite ``with_for_update`` is a no-op but the counter arithmetic still holds. A ``DENY``/``DEFER``
   stages NOTHING — a refused reserve changes no state. :meth:`HostCrawlLimiter.release` decrements the
   in-flight counter when a (one day) fetch completes.

Nothing here fetches, resolves, or parses content: a reserve records that a request slot was CLAIMED;
acting on an ``ALLOW`` by performing the fetch stays the caller's still-disabled future capability.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import HostCrawlState


class CrawlVerdict(str, Enum):
    """Whether a would-be request respects the host's crawl limits.

    ``ALLOW`` — the request may proceed (a slot is reserved). ``DENY`` — a hard crawl refusal that a
    retry cannot cure without a policy change (a robots-disallowed path). ``DEFER`` — a temporary,
    retriable refusal (crawl-delay not elapsed, or a rate / concurrency cap momentarily reached).
    Both ``DENY`` and ``DEFER`` reserve nothing and change no state.
    """

    ALLOW = "allow"
    DENY = "deny"
    DEFER = "defer"


class CrawlLimitReason(str, Enum):
    """The stable, PII-safe reason a request was refused by the crawl limits — one per bound.

    - ``ROBOTS_DISALLOWED`` — the request path is Disallowed by the source's robots policy (a hard
      ``DENY``); the longest-matching rule is a Disallow.
    - ``CRAWL_DELAY_NOT_ELAPSED`` — less than the per-host crawl-delay has elapsed since the last
      reserved request (``DEFER``).
    - ``RATE_LIMIT_EXCEEDED`` — the current rate window already holds the maximum reserved requests
      (``DEFER``); the limit shared across this business's concurrent jobs for the host.
    - ``CONCURRENCY_LIMIT_EXCEEDED`` — the maximum number of requests are already in flight for the
      host (``DEFER``).
    """

    ROBOTS_DISALLOWED = "robots_disallowed"
    CRAWL_DELAY_NOT_ELAPSED = "crawl_delay_not_elapsed"
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
    CONCURRENCY_LIMIT_EXCEEDED = "concurrency_limit_exceeded"


@dataclass(frozen=True)
class CrawlPolicy:
    """The crawl rules for one host — robots Allow/Disallow paths plus the rate/delay/concurrency caps.

    Supplied by the caller (derived, in a real deployment, from the source's robots.txt and licensed
    rate terms); the guard parses no robots.txt and resolves nothing itself, exactly as DCA-118 takes
    a caller-supplied allowlist rather than resolving DNS. All caps are non-negative; ``0`` means the
    bound is effectively off (a ``0`` crawl-delay imposes no wait; a ``0`` rate/concurrency cap admits
    nothing — fail-closed — which lets a policy deliberately pause crawling).

    - ``allow_rules`` / ``disallow_rules`` — robots path-prefix rules for our user-agent. Matched by
      the RFC 9309 longest-match rule: the most specific (longest) matching rule decides, and an Allow
      wins a length tie. An empty rule set Disallows nothing (robots permits the path).
    - ``crawl_delay_seconds`` — the minimum interval between two requests to the host.
    - ``max_requests`` / ``window_seconds`` — the rate cap: at most ``max_requests`` reserved requests
      per rolling ``window_seconds`` window.
    - ``max_concurrent`` — the maximum number of simultaneously in-flight requests to the host.
    - ``user_agent`` — the crawler identity the ``*_rules`` were resolved for (recorded, advisory).
    """

    crawl_delay_seconds: float = 0.0
    max_requests: int = 0
    window_seconds: int = 0
    max_concurrent: int = 0
    allow_rules: tuple[str, ...] = ()
    disallow_rules: tuple[str, ...] = ()
    user_agent: str = "*"

    @classmethod
    def of(
        cls,
        *,
        crawl_delay_seconds: float = 0.0,
        max_requests: int = 0,
        window_seconds: int = 0,
        max_concurrent: int = 0,
        allow_rules: Iterable[str] = (),
        disallow_rules: Iterable[str] = (),
        user_agent: str = "*",
    ) -> CrawlPolicy:
        return cls(
            crawl_delay_seconds=float(crawl_delay_seconds),
            max_requests=int(max_requests),
            window_seconds=int(window_seconds),
            max_concurrent=int(max_concurrent),
            allow_rules=tuple(allow_rules),
            disallow_rules=tuple(disallow_rules),
            user_agent=user_agent,
        )


@dataclass(frozen=True)
class HostCrawlObservation:
    """The per-host counters the decider reads — a snapshot of the durable crawl state (already rolled
    into the current rate window). Inert: the decider only reads these, never mutates them."""

    request_count: int = 0
    in_flight: int = 0
    last_request_at: datetime | None = None


@dataclass(frozen=True)
class CrawlDecision:
    """The inert result of evaluating one request against a host's crawl limits — nothing reserved.

    ``ALLOW`` carries no reason. ``DENY``/``DEFER`` carry the single decisive :class:`CrawlLimitReason`
    and a short, PII-safe ``detail``.
    """

    verdict: CrawlVerdict
    reason: CrawlLimitReason | None = None
    detail: str = ""

    @property
    def allowed(self) -> bool:
        return self.verdict is CrawlVerdict.ALLOW

    @property
    def reason_code(self) -> str | None:
        """The ``reason`` as its stable string value (or ``None`` when allowed) — for logging and
        manual-check output without caring which bound it was."""
        return self.reason.value if self.reason is not None else None

    @classmethod
    def allow(cls) -> CrawlDecision:
        return cls(CrawlVerdict.ALLOW)

    @classmethod
    def deny(cls, reason: CrawlLimitReason, *, detail: str = "") -> CrawlDecision:
        return cls(CrawlVerdict.DENY, reason=reason, detail=detail)

    @classmethod
    def defer(cls, reason: CrawlLimitReason, *, detail: str = "") -> CrawlDecision:
        return cls(CrawlVerdict.DEFER, reason=reason, detail=detail)


def _normalize_path(path: str) -> str:
    """A robots-matchable path: always rooted at ``/`` (an empty / query-only path is the site root)."""
    path = (path or "").strip()
    if not path:
        return "/"
    if not path.startswith("/"):
        path = "/" + path
    return path


def _robots_allows(path: str, allow_rules: Iterable[str], disallow_rules: Iterable[str]) -> bool:
    """Whether robots permits ``path`` under the RFC 9309 longest-match rule.

    The most specific (longest) matching rule decides; an Allow wins a length tie with a Disallow; no
    matching rule permits the path. An empty rule (``""``) matches every path (a site-wide rule).
    """
    best_len = -1
    allowed = True  # no rule matched ⇒ permitted
    for rule in disallow_rules:
        if path.startswith(rule) and len(rule) > best_len:
            best_len, allowed = len(rule), False
    for rule in allow_rules:
        # ``>=`` so an Allow of the same specificity wins the tie (RFC 9309).
        if path.startswith(rule) and len(rule) >= best_len:
            best_len, allowed = len(rule), True
    return allowed


def _as_utc(value: datetime) -> datetime:
    """Normalize a (possibly naive, SQLite-returned) datetime to UTC-aware for comparison."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def evaluate_crawl_limits(
    policy: CrawlPolicy,
    observation: HostCrawlObservation,
    *,
    path: str,
    now: datetime,
) -> CrawlDecision:
    """Decide whether one request respects ``policy`` given the host's current ``observation``.

    Pure and deterministic: no I/O, no state change, reproducible from the inputs alone. The checks run
    in a fixed order (robots → crawl-delay → rate → concurrency), stopping at the first failure so the
    reason is deterministic. ``observation`` is expected to already reflect the CURRENT rate window
    (the limiter rolls the window before calling); ``now`` anchors the crawl-delay comparison.
    """
    # 1) Robots allowed-path — a crawl rule, never a rights grant. A Disallowed path is a hard DENY.
    clean_path = _normalize_path(path)
    if not _robots_allows(clean_path, policy.allow_rules, policy.disallow_rules):
        return CrawlDecision.deny(CrawlLimitReason.ROBOTS_DISALLOWED, detail=clean_path)

    # 2) Crawl-delay — too soon since the last reserved request to this host.
    if policy.crawl_delay_seconds > 0 and observation.last_request_at is not None:
        elapsed = (now - _as_utc(observation.last_request_at)).total_seconds()
        if elapsed < policy.crawl_delay_seconds:
            return CrawlDecision.defer(
                CrawlLimitReason.CRAWL_DELAY_NOT_ELAPSED,
                detail=f"elapsed {elapsed:.3f}s < delay {policy.crawl_delay_seconds}s",
            )

    # 3) Rate cap — the current window already holds the maximum reserved requests. Shared across this
    #    business's concurrent jobs for the host (the durable count is what makes that hold).
    if observation.request_count >= policy.max_requests:
        return CrawlDecision.defer(
            CrawlLimitReason.RATE_LIMIT_EXCEEDED,
            detail=f"count {observation.request_count} >= cap {policy.max_requests}",
        )

    # 4) Concurrency cap — the maximum number of requests are already in flight for the host.
    if observation.in_flight >= policy.max_concurrent:
        return CrawlDecision.defer(
            CrawlLimitReason.CONCURRENCY_LIMIT_EXCEEDED,
            detail=f"in_flight {observation.in_flight} >= cap {policy.max_concurrent}",
        )

    return CrawlDecision.allow()


@dataclass(frozen=True)
class CrawlReservation:
    """The inert result of a :meth:`HostCrawlLimiter.reserve` — nothing has been (or will be) fetched.

    ``decision`` is the crawl verdict. ``reserved`` is the load-bearing witness: ``True`` only when the
    request was allowed and a slot was durably recorded (the window count + in-flight count were bumped
    and ``last_request_at`` set); ``False`` on any ``DENY``/``DEFER`` — then the crawl state is
    UNCHANGED. ``request_count`` / ``in_flight`` report the host's counters as they stand AFTER the
    decision (so a caller can see the reservation it just took, or the live counts that refused it).
    """

    decision: CrawlDecision
    reserved: bool
    request_count: int = 0
    in_flight: int = 0

    @property
    def allowed(self) -> bool:
        return self.decision.allowed


class HostCrawlLimiter:
    """Durable per-host crawl-limit reservations for ONE business (tenant-scoped).

    Construct with the active session and the verified business scope (a route/worker resolves it from
    the membership-verified tenant context). Every query is confined to that business, so the rate /
    crawl-delay / concurrency limits a host enforces are shared only among that business's own
    concurrent jobs — never across tenants.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def state(self, source_key: str, host: str) -> HostCrawlState | None:
        """The crawl-state row for this business + ``(source, host)``, or ``None`` (no reserve yet)."""
        stmt = (
            select(HostCrawlState)
            .where(HostCrawlState.business_id == self._business_id)
            .where(HostCrawlState.source_key == (source_key or "").strip())
            .where(HostCrawlState.host == (host or "").strip().lower())
        )
        return self._session.scalars(stmt).one_or_none()

    def _lock_state(self, source_key: str, host: str) -> HostCrawlState | None:
        """Re-read the crawl-state row ``FOR UPDATE`` so concurrent reserves for the same host serialize.

        On PostgreSQL this makes two concurrent reserves contend for the SAME row (the second blocks
        until the first commits, then sees its reserved count), so a cap can never be collectively
        overshot. On SQLite ``with_for_update`` is silently ignored — the arithmetic still holds; only
        the cross-transaction serialization is a PostgreSQL property.
        """
        stmt = (
            select(HostCrawlState)
            .where(HostCrawlState.business_id == self._business_id)
            .where(HostCrawlState.source_key == (source_key or "").strip())
            .where(HostCrawlState.host == (host or "").strip().lower())
            .with_for_update()
        )
        return self._session.scalars(stmt).one_or_none()

    # -- the one write path -------------------------------------------------------------------------

    def reserve(
        self,
        source_key: str,
        host: str,
        policy: CrawlPolicy,
        *,
        path: str = "/",
        now: datetime | None = None,
    ) -> CrawlReservation:
        """Reserve one request slot against ``(source, host)`` under ``policy``; stage it only if allowed.

        Serialize: take a ``FOR UPDATE`` lock on the host's crawl-state row (creating it on first use),
        roll the rate window if ``policy.window_seconds`` have elapsed since it started, then compose
        :func:`evaluate_crawl_limits` against the LIVE counters. On ``ALLOW`` the reservation is
        recorded (window ``request_count`` + ``in_flight`` bumped, ``last_request_at`` set) so a
        concurrent job sees it and cannot also be admitted past a cap. On a ``DENY``/``DEFER`` NOTHING
        is staged — a refused reserve changes no state. The caller owns the unit of work; a reserve
        holds a crawl slot and fetches/sends nothing.
        """
        moment = now or datetime.now(UTC)
        key = (source_key or "").strip()
        normalized_host = (host or "").strip().lower()

        state = self._lock_state(key, normalized_host)

        # Compute the CURRENT-window observation WITHOUT mutating anything — a denial must change no
        # state, so the window roll and the count bump are applied only on an ALLOW (below). For an
        # absent row the host has never been reserved against (empty window, nothing in flight).
        if state is None:
            window_rolls = False
            effective_count = 0
            in_flight = 0
            last_request_at: datetime | None = None
        else:
            window_started = _as_utc(state.window_started_at)
            window_rolls = (
                policy.window_seconds > 0
                and (moment - window_started).total_seconds() >= policy.window_seconds
            )
            # A rolled window starts fresh at 0; otherwise the live committed count stands.
            effective_count = 0 if window_rolls else state.request_count
            in_flight = state.in_flight
            last_request_at = state.last_request_at

        observation = HostCrawlObservation(
            request_count=effective_count, in_flight=in_flight, last_request_at=last_request_at
        )
        decision = evaluate_crawl_limits(policy, observation, path=path, now=moment)

        # A refused reserve changes NOTHING — no row is created, no counter rolled or bumped.
        if not decision.allowed:
            return CrawlReservation(
                decision=decision,
                reserved=False,
                request_count=effective_count,
                in_flight=in_flight,
            )

        # Allowed: create the row on first use, roll the window if it elapsed, then record the
        # reservation durably (count + in-flight bumped, crawl-delay anchor set) so a concurrent job
        # observes it and cannot also be admitted past a cap.
        if state is None:
            # First reserve for this host: the unique constraint turns a concurrent duplicate create
            # into a rejected second row (the FOR UPDATE serialization applies once the row exists).
            state = HostCrawlState(
                business_id=self._business_id,
                source_key=key,
                host=normalized_host,
                window_started_at=moment,
                window_seconds=int(policy.window_seconds),
                request_count=0,
                in_flight=0,
                last_request_at=None,
            )
            self._session.add(state)
        elif window_rolls:
            state.window_started_at = moment
            state.window_seconds = int(policy.window_seconds)
            state.request_count = 0

        state.request_count += 1
        state.in_flight += 1
        state.last_request_at = moment
        return CrawlReservation(
            decision=decision,
            reserved=True,
            request_count=state.request_count,
            in_flight=state.in_flight,
        )

    def release(
        self, source_key: str, host: str, *, now: datetime | None = None
    ) -> HostCrawlState | None:
        """Release one in-flight slot for ``(source, host)`` (a reserved request finished / was abandoned).

        Decrements the in-flight concurrency counter (never below zero) under a ``FOR UPDATE`` lock, so
        a later reserve sees the freed capacity. Does NOT touch the window ``request_count`` — rate is
        about request frequency, not how many are in flight. Returns the updated row, or ``None`` when
        there is no crawl state to release (nothing was ever reserved).
        """
        state = self._lock_state(source_key, host)
        if state is None:
            return None
        if state.in_flight > 0:
            state.in_flight -= 1
        return state
