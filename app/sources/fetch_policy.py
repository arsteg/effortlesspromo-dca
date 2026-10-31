"""DCA-121 — enforce approved access AND crawl limits before fetching a web source.

Key: ``fetch-policy`` · source sections 6, 8, 9. Stage: Approved source access.

The headline of DCA-121: a single gate that composes the SOURCE-RIGHTS chain (DCA-044..048) with the
CRAWL-ETIQUETTE limits (:mod:`app.sources.crawl_limits`) into one deterministic decision, with
authority and rights checked **FIRST**. The load-bearing guarantee (the DCA-121 acceptance criterion):

- **Robots allowance cannot override a license denial.** The rights chain is evaluated FIRST, by the
  unchanged DCA-048 :class:`~app.services.source_preflight.SourcePreflight`. If the rights chain
  denies (no in-force platform approval, no business entitlement, operation/field/purpose not granted,
  a draft/disabled or unknown source), the gate returns that denial and the crawl limiter is **never
  touched** — a permissive ``robots.txt`` or an "official origin" can never, on their own, authorize a
  fetch the rights chain withholds, and a rights denial changes no crawl state.
- **Rate limits apply across concurrent jobs.** Only once rights permit does the gate reserve a
  request slot through the DURABLE :class:`~app.sources.crawl_limits.HostCrawlLimiter`, which enforces
  the per-host crawl-delay / rate / concurrency limits across concurrent jobs via a ``FOR UPDATE`` lock
  on the shared per-host crawl-state row.

Like the DCA-048 preflight, this gate performs **no fetch**: it returns a :class:`FetchPolicyDecision`
(and, when allowed, the durable reservation the caller must later release). Acting on an ``allowed``
decision by performing the web fetch stays the caller's still-disabled future capability — and because
rights are checked first and no source is ever ENABLED in the running service, the gate denies every
fetch at the rights phase today.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime
from enum import Enum
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from app.services.source_preflight import (
    PreflightDecision,
    PreflightRequest,
    SourcePreflight,
)
from app.sources.crawl_limits import CrawlPolicy, CrawlReservation, HostCrawlLimiter


class FetchPhase(str, Enum):
    """Which phase decided a fetch-policy outcome.

    - ``RIGHTS`` — decided by the source-rights chain (DCA-044..048). An allow here means rights
      permit and the gate went on to the crawl phase; a deny here means the crawl limiter was never
      consulted (robots/rate cannot override a license denial).
    - ``CRAWL`` — rights permitted, so the outcome was decided by the per-host crawl limits.
    """

    RIGHTS = "rights"
    CRAWL = "crawl"


@dataclass(frozen=True)
class FetchPolicyRequest:
    """One would-be web fetch, evaluated before any connection.

    ``preflight`` is the DCA-048 rights request (source / operation / purpose / field / advisory
    origin signals). ``url`` is the target the crawl limits apply to — its host (lower-cased) keys the
    durable per-host rate state and its path is matched against the robots rules. ``policy`` is the
    host's crawl policy (robots Allow/Disallow + crawl-delay / rate / concurrency caps), supplied by
    the caller exactly as DCA-118 takes a caller-supplied allowlist (the gate resolves nothing itself).
    """

    preflight: PreflightRequest
    url: str
    policy: CrawlPolicy = dc_field(default_factory=CrawlPolicy)


@dataclass(frozen=True)
class FetchPolicyDecision:
    """The deterministic outcome of the composed gate — inert, nothing fetched.

    ``allowed`` is the overall verdict. ``phase`` is which phase decided it (:class:`FetchPhase`).
    ``reason`` is the single decisive stable reason on a denial (a DCA-048 rights reason when
    ``phase == RIGHTS``, or a :class:`~app.sources.crawl_limits.CrawlLimitReason` value when
    ``phase == CRAWL``), ``None`` when allowed. ``rights`` is the underlying
    :class:`~app.services.source_preflight.PreflightDecision`; ``reservation`` is the durable crawl
    reservation when the crawl phase ran (``None`` when rights denied, so the limiter was never
    touched). ``host`` is the normalized target host.
    """

    allowed: bool
    phase: FetchPhase
    reason: str | None = None
    detail: str | None = None
    host: str = ""
    rights: PreflightDecision | None = None
    reservation: CrawlReservation | None = None


def _host_of(url: str) -> str:
    """The lower-cased host of ``url`` (trailing dot stripped), or ``""`` if unparseable/hostless."""
    try:
        parsed = urlsplit((url or "").strip())
        host = parsed.hostname
    except ValueError:
        return ""
    return (host or "").lower().rstrip(".")


def _path_of(url: str) -> str:
    """The path of ``url`` for robots matching (``"/"`` when absent), ignoring query/fragment."""
    try:
        return urlsplit((url or "").strip()).path or "/"
    except ValueError:
        return "/"


class FetchPolicyGate:
    """The composed authorize-before-fetch gate for ONE business (tenant-scoped).

    Construct with the active session and the verified business scope. Composes the DCA-048 rights
    preflight (checked FIRST) with the durable per-host crawl limiter — the single place a would-be web
    fetch is cleared for both approved access and crawl etiquette.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._preflight = SourcePreflight(session, business_id)
        self._limiter = HostCrawlLimiter(session, business_id)

    def authorize_fetch(
        self, request: FetchPolicyRequest, *, now: datetime | None = None
    ) -> FetchPolicyDecision:
        """Decide whether ``request`` may be fetched: rights FIRST, then crawl limits.

        1. **Rights (DCA-048).** Run :meth:`SourcePreflight.authorize`. If it denies, return the denial
           with ``phase=RIGHTS`` and DO NOT touch the crawl limiter — a robots allowance can never
           override a license denial, and the rights denial changes no crawl state (a pure read).
        2. **Crawl limits.** Only if rights permit, reserve a request slot through the durable
           :class:`~app.sources.crawl_limits.HostCrawlLimiter` (robots path / crawl-delay / rate /
           concurrency), enforced across concurrent jobs. The reservation is recorded only on an
           ``ALLOW``; a crawl ``DENY``/``DEFER`` reserves nothing.

        Returns a :class:`FetchPolicyDecision`. Nothing is connected to or fetched — acting on an
        ``allowed`` decision stays the caller's still-disabled fetch capability.
        """
        host = _host_of(request.url)

        # 1) Rights FIRST — a pure read; a denial never consults the crawl limiter.
        rights = self._preflight.authorize(request.preflight, now=now)
        if not rights.allowed:
            return FetchPolicyDecision(
                allowed=False,
                phase=FetchPhase.RIGHTS,
                reason=rights.reason,
                detail=rights.detail,
                host=host,
                rights=rights,
                reservation=None,
            )

        # 2) Crawl limits — only reached once rights permit.
        reservation = self._limiter.reserve(
            request.preflight.source_key,
            host,
            request.policy,
            path=_path_of(request.url),
            now=now,
        )
        return FetchPolicyDecision(
            allowed=reservation.allowed,
            phase=FetchPhase.CRAWL,
            reason=reservation.decision.reason_code,
            detail=reservation.decision.detail or None,
            host=host,
            rights=rights,
            reservation=reservation,
        )
