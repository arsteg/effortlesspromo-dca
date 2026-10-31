"""DCA-118 — validate network destinations (the centralized fetch client's destination guard).

Key: ``fetch-ssrf`` · source sections 6, 8, 9. Stage: Approved source access.

The single, load-bearing guarantee: **a forbidden network destination is rejected BEFORE any
connection, and a model-supplied URL cannot bypass the guard.** Every would-be fetch — whatever
produced the URL, including an LLM, a crawled page, or a redirect target — must be passed through
:func:`validate_destination` first, and only an explicit ``ALLOW`` verdict may be connected to.

This is a **pure, deterministic, network-free decider** in the spirit of
:mod:`app.jobs.retry` / :mod:`app.compliance.eligibility_outcome` / :mod:`app.normalization.domains`:
it parses and classifies a destination and returns an inert :class:`DestinationDecision`. It performs
**no DNS resolution of its own** (a resolver that the guard itself drives could be the very leak we
are defending against, and would make the decision non-reproducible). Instead the caller supplies the
DNS answer it already obtained (``resolved_ips``); the guard verifies that answer. It opens **no
socket, sends nothing, and executes no fetch** — actual fetching stays a disabled future capability.
Deciding ``ALLOW`` is permission to *attempt* a connection the caller must still re-pin to these exact
IPs; it is never itself a fetch.

What it rejects (the acceptance criteria), checked in a fixed order, STOP-AT-FIRST-FAILURE so the
denial reason is deterministic:

1. **Malformed / hostless URL** → ``MALFORMED_URL`` (a schemeless/relative string, which has no
   scheme and no authority to guard, lands here).
2. **Scheme** — only ``http`` / ``https`` are fetchable; a present-but-wrong scheme (``file``,
   ``ftp``, ``gopher``, ``data``, ``dict``, …) → ``SCHEME_NOT_ALLOWED``.
3. **Embedded credentials** — a ``user:pass@host`` userinfo is a classic SSRF/confusion vector and is
   never allowed → ``CREDENTIALS_IN_URL``.
4. **Port** — only the default web ports (:data:`DEFAULT_ALLOWED_PORTS` = 80/443, plus an implicit
   default) are fetchable; a non-standard port (e.g. an internal admin port) → ``PORT_NOT_ALLOWED``.
5. **Address safety + allowlist** — the host is then resolved to the IP(s) that would actually be
   connected to and each is classified:

   - A **literal IP host** is classified directly (no allowlist of *domains* can vouch for a raw IP,
     so an unsafe literal is denied by its address class first, and a *public* literal that is not
     itself allowlisted is ``HOST_NOT_ALLOWLISTED``).
   - A **hostname** must be on the caller's domain ``allowlist`` (empty allowlist ⇒ nothing is
     allowed — fail-closed) → ``HOST_NOT_ALLOWLISTED`` otherwise. Only then is its DNS answer checked:
     ``resolved_ips`` MUST be supplied (a hostname with no resolved address → ``UNRESOLVED``), and
     **every** resolved address must be public. If *any* resolved address is private/loopback/
     link-local/metadata/… the destination is denied with that address class — this is what defeats a
     DNS-rebinding / "mixed public+private answer" attempt: one poisoned A record fails the whole
     destination.

   Address classes that are refused: cloud-metadata (``169.254.169.254`` & friends —
   ``CLOUD_METADATA_ADDRESS``), loopback (``LOOPBACK_ADDRESS``), link-local (``LINK_LOCAL_ADDRESS``),
   RFC1918 / IPv6 unique-local private (``PRIVATE_ADDRESS`` / ``UNIQUE_LOCAL_ADDRESS``), and the
   unspecified / multicast / otherwise non-global reserved space (``RESERVED_ADDRESS``). IPv4-mapped
   IPv6 (``::ffff:a.b.c.d``) is unwrapped before classification so it cannot smuggle an internal IPv4.

A denial changes nothing and is perfectly reproducible from the inputs alone.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlsplit

# A parsed IP literal / resolved address (either family) — what ``ipaddress.ip_address`` returns.
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

# The only schemes a fetch client may open. Everything else is refused up front.
ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

# The only ports a fetch may target. ``None`` (no explicit port in the URL) is always allowed and maps
# to the scheme default; an explicit port must be one of these. Non-web ports (internal admin panels,
# databases, caches) are a primary SSRF target and are refused.
DEFAULT_ALLOWED_PORTS: frozenset[int] = frozenset({80, 443})

# Well-known cloud instance-metadata endpoints. These are link-local (and so already caught by the
# link-local screen) but are called out with a dedicated reason because they are the single highest-value
# SSRF target (credential theft). ``fd00:ec2::254`` is AWS's IPv6 metadata address; ``100.100.100.200``
# is Alibaba Cloud's. Stored as normalized :class:`ipaddress` objects for exact comparison.
_METADATA_ADDRESSES: frozenset[IPAddress] = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("fd00:ec2::254"),
        ipaddress.ip_address("100.100.100.200"),
    }
)


class DestinationVerdict(str, Enum):
    """Whether a destination may be connected to. ``ALLOW`` is permission to *attempt* the connection
    (which the caller must still pin to the verified IPs); it is never itself a fetch."""

    ALLOW = "allow"
    DENY = "deny"


class DenyReason(str, Enum):
    """The stable, PII-safe reason a destination was refused — the single place each SSRF class is named.

    Parse / shape refusals:
    - ``MALFORMED_URL`` — unparseable, or no host at all (a schemeless/relative string lands here).
    - ``SCHEME_NOT_ALLOWED`` — the scheme is not ``http``/``https``.
    - ``CREDENTIALS_IN_URL`` — the URL carried ``user[:pass]@`` userinfo.
    - ``PORT_NOT_ALLOWED`` — an explicit port outside the allowed set (also a non-integer/out-of-range port).
    - ``HOST_NOT_ALLOWLISTED`` — a hostname not on the caller's domain allowlist, or a public IP literal
      that is not itself allowlisted (an empty allowlist allows nothing — fail-closed).
    - ``UNRESOLVED`` — an allowlisted hostname with no supplied DNS answer; the guard resolves nothing
      itself, so it cannot verify where the host points and refuses rather than connect blind.

    Address-class refusals (an unsafe literal host, or ANY unsafe resolved address of a hostname):
    - ``CLOUD_METADATA_ADDRESS`` — a cloud instance-metadata endpoint (``169.254.169.254`` & friends).
    - ``LOOPBACK_ADDRESS`` — ``127.0.0.0/8`` / ``::1``.
    - ``LINK_LOCAL_ADDRESS`` — ``169.254.0.0/16`` / ``fe80::/10``.
    - ``PRIVATE_ADDRESS`` — RFC1918 (``10/8``, ``172.16/12``, ``192.168/16``).
    - ``UNIQUE_LOCAL_ADDRESS`` — IPv6 unique-local ``fc00::/7``.
    - ``RESERVED_ADDRESS`` — unspecified (``0.0.0.0`` / ``::``), multicast, or otherwise non-global
      reserved space.
    """

    MALFORMED_URL = "malformed_url"
    SCHEME_NOT_ALLOWED = "scheme_not_allowed"
    CREDENTIALS_IN_URL = "credentials_in_url"
    PORT_NOT_ALLOWED = "port_not_allowed"
    HOST_NOT_ALLOWLISTED = "host_not_allowlisted"
    UNRESOLVED = "unresolved"
    CLOUD_METADATA_ADDRESS = "cloud_metadata_address"
    LOOPBACK_ADDRESS = "loopback_address"
    LINK_LOCAL_ADDRESS = "link_local_address"
    PRIVATE_ADDRESS = "private_address"
    UNIQUE_LOCAL_ADDRESS = "unique_local_address"
    RESERVED_ADDRESS = "reserved_address"


@dataclass(frozen=True)
class DestinationDecision:
    """The inert result of guarding one destination — nothing has been (or will be) connected to.

    On ``ALLOW`` the decision carries the ``host`` and the ``safe_ips`` (the exact addresses verified
    to be public — for a literal-IP host, the host itself; for a hostname, every supplied resolved
    address) so a caller can pin its connection to precisely these and nothing else. On ``DENY`` it
    carries the single decisive ``reason`` and a short, PII-safe ``detail``; ``safe_ips`` is empty.
    """

    verdict: DestinationVerdict
    reason: DenyReason | None = None
    host: str = ""
    safe_ips: tuple[str, ...] = field(default_factory=tuple)
    detail: str = ""

    @property
    def allowed(self) -> bool:
        return self.verdict is DestinationVerdict.ALLOW

    @classmethod
    def allow(cls, host: str, safe_ips: Iterable[str]) -> DestinationDecision:
        return cls(DestinationVerdict.ALLOW, host=host, safe_ips=tuple(safe_ips))

    @classmethod
    def deny(cls, reason: DenyReason, *, host: str = "", detail: str = "") -> DestinationDecision:
        return cls(DestinationVerdict.DENY, reason=reason, host=host, detail=detail)


def _normalize_allowlist(allowlist: Iterable[str]) -> frozenset[str]:
    """Lower-case + trailing-dot-strip the allowlist entries so matching is case/FQDN-insensitive."""
    return frozenset(entry.strip().lower().rstrip(".") for entry in allowlist if entry.strip())


def _host_on_allowlist(host: str, allowlist: frozenset[str]) -> bool:
    """Whether ``host`` is covered by the domain allowlist — an exact match or a subdomain of an entry.

    ``api.data.example.in`` is covered by ``example.in`` (suffix on a label boundary); ``evil-example.in``
    is NOT covered by ``example.in`` (the dot-boundary check prevents a substring match). An empty
    allowlist covers nothing (fail-closed)."""
    host = host.lower().rstrip(".")
    return any(host == entry or host.endswith("." + entry) for entry in allowlist)


def _classify_address(ip: IPAddress) -> DenyReason | None:
    """Classify one resolved/literal IP: a :class:`DenyReason` if it is forbidden, ``None`` if it is a
    safe public (globally routable) address.

    IPv4-mapped IPv6 (``::ffff:a.b.c.d``) is unwrapped first so an internal IPv4 cannot hide inside a v6
    literal. The checks are ordered most-specific-first (metadata before the broader link-local that
    contains it) so the reported reason is the most precise one."""
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped

    if ip in _METADATA_ADDRESSES:
        return DenyReason.CLOUD_METADATA_ADDRESS
    if ip.is_loopback:
        return DenyReason.LOOPBACK_ADDRESS
    if ip.is_link_local:
        return DenyReason.LINK_LOCAL_ADDRESS
    # IPv6 unique-local (fc00::/7) — a private range reported distinctly from RFC1918.
    if ip.version == 6 and ip in ipaddress.ip_network("fc00::/7"):
        return DenyReason.UNIQUE_LOCAL_ADDRESS
    if ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        return DenyReason.RESERVED_ADDRESS
    if ip.is_private:
        return DenyReason.PRIVATE_ADDRESS
    if ip.is_global:
        return None
    # Not globally routable and not otherwise classified — treat as reserved (fail-closed).
    return DenyReason.RESERVED_ADDRESS


def _parse_ip(value: str) -> IPAddress | None:
    """Parse ``value`` as an IP literal, or ``None`` if it is a hostname. Network-free."""
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def validate_destination(
    url: str,
    *,
    allowlist: Iterable[str],
    resolved_ips: Sequence[str] | None = None,
    allowed_ports: Iterable[int] = DEFAULT_ALLOWED_PORTS,
) -> DestinationDecision:
    """Decide whether ``url`` may be fetched — purely, deterministically, without any network access.

    ``allowlist`` is the set of domains this caller is permitted to reach (hostnames must be an exact
    match or a subdomain of an entry; an empty allowlist permits nothing). ``resolved_ips`` is the DNS
    answer the caller already obtained for a hostname host — the guard resolves nothing itself; it
    verifies the answer supplied. ``allowed_ports`` overrides the default 80/443 set.

    Returns a :class:`DestinationDecision`. An ``ALLOW`` carries the host and the exact public
    ``safe_ips`` the caller must pin to; a ``DENY`` carries the single decisive reason. Nothing is
    connected to, resolved, or fetched — this only VALIDATES, and acting on an ``ALLOW`` stays the
    caller's still-disabled fetch capability.
    """
    allow = _normalize_allowlist(allowlist)
    ports = frozenset(int(p) for p in allowed_ports)

    # 1) Parse. A relative/schemeless string has no host and is refused as malformed.
    try:
        parts = urlsplit(url.strip() if url else "")
    except ValueError as exc:
        return DestinationDecision.deny(DenyReason.MALFORMED_URL, detail=str(exc))

    # 2) Scheme — only http(s) are fetchable. A schemeless/relative string (empty scheme) has no
    #    authority to guard and is malformed; a present-but-wrong scheme is a scheme refusal.
    scheme = parts.scheme.lower()
    if not scheme:
        return DestinationDecision.deny(DenyReason.MALFORMED_URL, detail="missing scheme")
    if scheme not in ALLOWED_SCHEMES:
        return DestinationDecision.deny(DenyReason.SCHEME_NOT_ALLOWED, detail=scheme)

    # A hostless authority (after a valid scheme) is malformed.
    try:
        host = parts.hostname
    except ValueError as exc:
        return DestinationDecision.deny(DenyReason.MALFORMED_URL, detail=str(exc))
    if not host:
        return DestinationDecision.deny(DenyReason.MALFORMED_URL, detail="missing host")
    host = host.lower().rstrip(".")

    # 3) Embedded credentials — a classic SSRF/confusion vector, never allowed.
    if parts.username or parts.password:
        return DestinationDecision.deny(DenyReason.CREDENTIALS_IN_URL, host=host)

    # 4) Port — an out-of-range/non-integer port raises here; treat as a port refusal.
    try:
        port = parts.port
    except ValueError:
        return DestinationDecision.deny(DenyReason.PORT_NOT_ALLOWED, host=host, detail="invalid")
    if port is not None and port not in ports:
        return DestinationDecision.deny(DenyReason.PORT_NOT_ALLOWED, host=host, detail=str(port))

    # 5) Address safety + allowlist.
    literal = _parse_ip(host)
    if literal is not None:
        # A literal IP host: no allowlist of DOMAINS can vouch for a raw address, so classify the
        # address FIRST (an unsafe literal is denied by its precise class — e.g. a pasted metadata IP
        # is CLOUD_METADATA_ADDRESS, not merely "not allowlisted").
        reason = _classify_address(literal)
        if reason is not None:
            return DestinationDecision.deny(reason, host=host, detail=str(literal))
        # A public literal is allowed only if the exact address string is itself on the allowlist.
        if not _host_on_allowlist(host, allow):
            return DestinationDecision.deny(DenyReason.HOST_NOT_ALLOWLISTED, host=host)
        return DestinationDecision.allow(host, (str(literal),))

    # A hostname: it must be allowlisted BEFORE we even consider its DNS answer.
    if not _host_on_allowlist(host, allow):
        return DestinationDecision.deny(DenyReason.HOST_NOT_ALLOWLISTED, host=host)

    # The guard resolves nothing itself; a hostname with no supplied answer cannot be verified.
    if not resolved_ips:
        return DestinationDecision.deny(DenyReason.UNRESOLVED, host=host)

    # Every resolved address must be public: one private/metadata/loopback answer fails the whole
    # destination (this is what defeats DNS rebinding / a mixed public+private answer).
    safe: list[str] = []
    for raw in resolved_ips:
        ip = _parse_ip(raw)
        if ip is None:
            return DestinationDecision.deny(
                DenyReason.MALFORMED_URL, host=host, detail=f"unparseable resolved address: {raw}"
            )
        reason = _classify_address(ip)
        if reason is not None:
            return DestinationDecision.deny(reason, host=host, detail=str(ip))
        safe.append(str(ip))

    return DestinationDecision.allow(host, safe)
