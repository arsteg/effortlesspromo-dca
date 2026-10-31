"""DCA-119 — revalidate redirects and resolved addresses (the fetch client's per-hop re-guard).

Key: ``fetch-redirect`` · source sections 6, 8, 9. Stage: Approved source access.

Builds directly on DCA-118's :mod:`app.sources.destination_guard`. The load-bearing guarantee is a
one-line extension of that story's: **an initially permitted URL cannot redirect or connect into a
forbidden address.** A redirect is not a free pass — the *target* of every redirect, and the freshly
resolved IP(s) of *every* hop, must be re-checked by the very same guard before the next connection is
attempted. This is what defeats a DNS-rebinding / parser-confusion bypass that smuggles an internal
destination in behind a safe-looking first URL: the start URL may resolve to a public address on the
first lookup, then a ``30x`` ``Location`` (or a re-resolution of the same host) points at
``127.0.0.1`` / ``169.254.169.254`` / an RFC1918 host — re-validating each hop with
:func:`~app.sources.destination_guard.validate_destination` catches it and refuses, so nothing
internal is ever connected to.

Like DCA-118 this is a **pure, deterministic, network-free decider**. It performs **no HTTP, follows
no redirect, and resolves no name itself** — a client that drove its own fetch/DNS here could be the
very leak we defend against, and would make the decision non-reproducible. Instead the caller supplies
the chain it *already* observed (or intends to follow): an ordered sequence of :class:`RedirectHop`,
each a URL plus the DNS answer (``resolved_ips``) the caller already obtained for that hop. The guard
re-validates each hop and returns an inert :class:`RedirectChainDecision`. An ``ALLOW`` is permission
to *attempt* the chain pinned to the per-hop ``safe_ips``; it is never itself a fetch. Acting on an
ALLOW stays the caller's still-disabled fetch capability.

What it enforces, in hop order, STOP-AT-FIRST-FAILURE so the denial is deterministic and matches the
hop a real client would have stopped at:

1. **Non-empty chain** — an empty chain has no destination to guard → ``MALFORMED_URL`` (reusing the
   guard's reason) with detail ``empty redirect chain``.
2. **Redirect cap** — a chain with more than :data:`DEFAULT_MAX_REDIRECTS` redirects (hops after the
   initial request) is refused → ``TOO_MANY_REDIRECTS``. The bounded prefix (the initial hop plus the
   first ``max_redirects`` redirects) is still fully re-validated first, so an unsafe hop *inside* the
   cap is reported with its own precise reason rather than being masked by the cap — exactly as a real
   client, which stops following at the cap, would behave.
3. **The DCA-118 destination guard, re-run on every hop** — the initial URL *and* each redirect target
   are each passed through :func:`validate_destination` with the caller's ``allowlist`` /
   ``allowed_ports`` and that hop's ``resolved_ips``. Any refusal (a non-http(s) scheme, embedded
   credentials appearing mid-chain, a non-web port, a non-allowlisted host, an unresolved host, or
   **any** unsafe resolved/literal address — private / loopback / link-local / cloud-metadata / IPv6
   unique-local / reserved) fails the whole chain at that hop and carries the guard's own
   :class:`~app.sources.destination_guard.DenyReason`. This runs **before** the downgrade rule so the
   "cannot redirect into a forbidden address" guarantee reports the precise address class.
4. **No scheme downgrade across a redirect** — once a hop is ``https`` a redirect to an otherwise-
   allowed plain ``http`` target is a downgrade (a classic way to strip transport security mid-chain)
   → ``SCHEME_DOWNGRADE``. The guard alone permits both schemes; the downgrade rule is a redirect-only
   property, checked only between consecutive hops.

This story adds **no** redirect-following, HTTP, DNS, socket, migration, schema, or network path. It
only VALIDATES, reusing DCA-118 for the address/URL classification rather than duplicating any of it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlsplit

from app.sources.destination_guard import (
    DEFAULT_ALLOWED_PORTS,
    DenyReason,
    DestinationDecision,
    DestinationVerdict,
    validate_destination,
)

# The maximum number of redirects (hops *after* the initial request) a chain may contain before it is
# refused outright. A bounded, conservative fetch limit: deep enough for legitimate canonicalisation
# hops (http→https, apex→www, trailing slash), shallow enough to deny a redirect-loop / amplification
# attempt. The initial request URL itself is hop 0 and is not counted as a redirect.
DEFAULT_MAX_REDIRECTS: int = 5


class RedirectDenyReason(str, Enum):
    """The stable, PII-safe reasons a redirect *chain* is refused for a chain-level property — the
    counterparts to DCA-118's per-destination :class:`~app.sources.destination_guard.DenyReason`,
    which is reused unchanged for any hop that fails the destination guard itself.

    - ``TOO_MANY_REDIRECTS`` — the chain exceeds :data:`DEFAULT_MAX_REDIRECTS` redirects.
    - ``SCHEME_DOWNGRADE`` — a redirect went from an ``https`` hop to a plain ``http`` target,
      stripping transport security mid-chain.
    """

    TOO_MANY_REDIRECTS = "too_many_redirects"
    SCHEME_DOWNGRADE = "scheme_downgrade"


@dataclass(frozen=True)
class RedirectHop:
    """One observed hop of a redirect chain: the URL and the DNS answer the caller already obtained for
    it (``None`` for a literal-IP host or when no lookup was needed). The guard resolves nothing itself;
    these stand in for the answer the caller had."""

    url: str
    resolved_ips: tuple[str, ...] | None = None

    @classmethod
    def of(cls, url: str, resolved_ips: Iterable[str] | None = None) -> RedirectHop:
        return cls(url, tuple(resolved_ips) if resolved_ips is not None else None)


@dataclass(frozen=True)
class RedirectChainDecision:
    """The inert result of re-guarding a whole redirect chain — nothing has been (or will be) fetched,
    followed, or resolved.

    On ``ALLOW`` every hop passed the DCA-118 guard within the redirect cap with no downgrade, and
    ``hops`` carries the per-hop ALLOW :class:`DestinationDecision`\\ s in order (each with the exact
    ``safe_ips`` the caller must pin that hop's connection to). On ``DENY`` it carries the single
    decisive ``reason`` (a chain-level :class:`RedirectDenyReason` or a reused per-hop
    :class:`~app.sources.destination_guard.DenyReason`), the ``blocked_index`` / ``blocked_url`` of the
    offending hop (``None`` for a whole-chain refusal with no single hop, e.g. an empty chain), and a
    short, PII-safe ``detail``.
    """

    verdict: DestinationVerdict
    reason: DenyReason | RedirectDenyReason | None = None
    blocked_index: int | None = None
    blocked_url: str = ""
    detail: str = ""
    hops: tuple[DestinationDecision, ...] = field(default_factory=tuple)

    @property
    def allowed(self) -> bool:
        return self.verdict is DestinationVerdict.ALLOW

    @property
    def reason_code(self) -> str | None:
        """The ``reason`` as its stable string value (or ``None`` when allowed) — handy for logging and
        manual-check output without caring whether it was a chain- or hop-level reason."""
        return self.reason.value if self.reason is not None else None

    @property
    def final(self) -> DestinationDecision | None:
        """The last (final destination) hop's ALLOW decision when the whole chain is allowed, else
        ``None``. The caller connects to this hop's ``safe_ips`` as the resolved endpoint."""
        return self.hops[-1] if self.allowed and self.hops else None

    @classmethod
    def allow(cls, hops: Iterable[DestinationDecision]) -> RedirectChainDecision:
        return cls(DestinationVerdict.ALLOW, hops=tuple(hops))

    @classmethod
    def deny(
        cls,
        reason: DenyReason | RedirectDenyReason,
        *,
        blocked_index: int | None = None,
        blocked_url: str = "",
        detail: str = "",
        hops: Iterable[DestinationDecision] = (),
    ) -> RedirectChainDecision:
        return cls(
            DestinationVerdict.DENY,
            reason=reason,
            blocked_index=blocked_index,
            blocked_url=blocked_url,
            detail=detail,
            hops=tuple(hops),
        )


def _scheme_of(url: str) -> str:
    """The lower-cased scheme of ``url`` for the downgrade comparison, or ``""`` if unparseable.
    Network-free; the real scheme refusal is left to the DCA-118 guard."""
    try:
        return urlsplit(url.strip() if url else "").scheme.lower()
    except ValueError:
        return ""


def validate_redirect_chain(
    hops: Sequence[RedirectHop],
    *,
    allowlist: Iterable[str],
    allowed_ports: Iterable[int] = DEFAULT_ALLOWED_PORTS,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
) -> RedirectChainDecision:
    """Re-validate an observed redirect chain, purely and without any network access.

    ``hops`` is the ordered chain the caller already observed or intends to follow: ``hops[0]`` is the
    initial request URL and each subsequent entry is a redirect *target*; each hop carries the DNS
    answer (``resolved_ips``) the caller already obtained for it. ``allowlist`` / ``allowed_ports`` are
    the same inputs DCA-118's guard takes and are applied identically to **every** hop.
    ``max_redirects`` bounds the number of redirects after the initial request (default
    :data:`DEFAULT_MAX_REDIRECTS`).

    Returns a :class:`RedirectChainDecision`. An ``ALLOW`` means every hop passed the guard within the
    cap with no scheme downgrade; a ``DENY`` names the first failing hop (in order) and its reason.
    Nothing is connected to, resolved, followed, or fetched — this only VALIDATES, and acting on an
    ``ALLOW`` stays the caller's still-disabled fetch capability.
    """
    allow = frozenset(allowlist)
    ports = tuple(allowed_ports)
    cap = max(0, int(max_redirects))

    if not hops:
        return RedirectChainDecision.deny(DenyReason.MALFORMED_URL, detail="empty redirect chain")

    validated: list[DestinationDecision] = []
    prev_scheme = ""
    for index, hop in enumerate(hops):
        # Redirect cap: the bounded prefix (hop 0 .. hop `cap`) is validated first in the loop body
        # below; reaching a further redirect refuses the whole chain. `index > cap` ⇒ this is redirect
        # number `index` > `cap`, which a real client would never have followed.
        if index > cap:
            return RedirectChainDecision.deny(
                RedirectDenyReason.TOO_MANY_REDIRECTS,
                blocked_index=index,
                blocked_url=hop.url,
                detail=f"{len(hops) - 1} redirects exceeds cap {cap}",
                hops=validated,
            )

        # Re-run the DCA-118 destination guard on this hop with its freshly-supplied DNS answer FIRST,
        # so a hop that is itself unsafe (a forbidden address / credentials / non-http scheme) is
        # reported with its own precise reason — the headline "cannot redirect into a forbidden
        # address" guarantee takes precedence over the redirect-only downgrade rule below.
        decision = validate_destination(
            hop.url,
            allowlist=allow,
            resolved_ips=hop.resolved_ips,
            allowed_ports=ports,
        )
        if not decision.allowed:
            return RedirectChainDecision.deny(
                decision.reason or DenyReason.MALFORMED_URL,
                blocked_index=index,
                blocked_url=hop.url,
                detail=decision.detail,
                hops=validated,
            )

        # Scheme downgrade across a redirect (an https hop → an otherwise-allowed http target) — a
        # redirect-only property the per-destination guard (which permits both schemes) cannot see.
        target_scheme = _scheme_of(hop.url)
        if index > 0 and prev_scheme == "https" and target_scheme == "http":
            return RedirectChainDecision.deny(
                RedirectDenyReason.SCHEME_DOWNGRADE,
                blocked_index=index,
                blocked_url=hop.url,
                detail="https -> http",
                hops=validated,
            )

        validated.append(decision)
        prev_scheme = target_scheme

    return RedirectChainDecision.allow(validated)
