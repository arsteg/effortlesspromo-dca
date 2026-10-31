"""DCA-119 — re-guarding a whole redirect chain (the fetch client's per-hop re-validation).

Unit tests over :mod:`app.sources.redirect_guard`, independent of any HTTP/DNS. A chain whose every
hop stays allowlisted-and-public passes; a redirect whose target (or whose freshly-resolved DNS answer)
points at a private / loopback / cloud-metadata address is refused at that hop with the DCA-118 guard's
own reason; a chain longer than the redirect cap is refused; an https→http downgrade and credentials
appearing mid-chain are refused. The decider is pure and network-free — it re-validates each hop with
the same DCA-118 guard, it never follows a redirect, resolves a name, or fetches.
"""

from __future__ import annotations

from app.sources.destination_guard import DenyReason, DestinationVerdict
from app.sources.redirect_guard import (
    DEFAULT_MAX_REDIRECTS,
    RedirectChainDecision,
    RedirectDenyReason,
    RedirectHop,
    validate_redirect_chain,
)

ALLOWLIST = frozenset({"example.in", "data.example.in"})
PUBLIC = ("93.184.216.34",)


def _hop(url: str, ips: tuple[str, ...] | None = None) -> RedirectHop:
    return RedirectHop.of(url, ips)


def _deny(hops: list[RedirectHop], **kw) -> RedirectChainDecision:
    d = validate_redirect_chain(hops, allowlist=ALLOWLIST, **kw)
    assert d.verdict is DestinationVerdict.DENY, d
    assert not d.allowed
    return d


# --------------------------------------------------------------------------------------------------
# Allowed chains
# --------------------------------------------------------------------------------------------------


def test_single_safe_hop_is_allowed() -> None:
    d = validate_redirect_chain(
        [_hop("https://data.example.in/records", PUBLIC)], allowlist=ALLOWLIST
    )
    assert d.allowed
    assert d.reason is None
    assert len(d.hops) == 1
    assert d.final is not None
    assert d.final.safe_ips == PUBLIC


def test_chain_that_stays_public_passes() -> None:
    # http apex -> https apex -> https www, each allowlisted and resolving public. A legitimate
    # canonicalisation chain.
    d = validate_redirect_chain(
        [
            _hop("http://example.in/", ("8.8.8.8",)),
            _hop("https://example.in/", ("8.8.8.8",)),
            _hop("https://data.example.in/records", PUBLIC),
        ],
        allowlist=ALLOWLIST,
    )
    assert d.allowed
    assert [h.host for h in d.hops] == ["example.in", "example.in", "data.example.in"]
    assert d.final is not None and d.final.host == "data.example.in"


def test_redirect_count_exactly_at_cap_is_allowed() -> None:
    # 1 initial + max_redirects redirects == exactly the cap, all safe.
    hops = [_hop("https://example.in/", PUBLIC)] + [
        _hop("https://example.in/", PUBLIC) for _ in range(DEFAULT_MAX_REDIRECTS)
    ]
    d = validate_redirect_chain(hops, allowlist=ALLOWLIST)
    assert d.allowed
    assert len(d.hops) == DEFAULT_MAX_REDIRECTS + 1


# --------------------------------------------------------------------------------------------------
# A redirect into a forbidden address is blocked AT THAT HOP (the headline guarantee)
# --------------------------------------------------------------------------------------------------


def test_redirect_target_to_loopback_literal_blocked() -> None:
    # A safe-looking initial URL redirects to http://127.0.0.1/ — blocked at hop 1 as loopback.
    d = _deny(
        [
            _hop("https://data.example.in/go", PUBLIC),
            _hop("http://127.0.0.1/admin", None),
        ]
    )
    assert d.reason is DenyReason.LOOPBACK_ADDRESS
    assert d.blocked_index == 1
    assert d.blocked_url == "http://127.0.0.1/admin"


def test_redirect_target_to_cloud_metadata_literal_blocked() -> None:
    d = _deny(
        [
            _hop("https://example.in/", PUBLIC),
            _hop("http://169.254.169.254/latest/meta-data/", None),
        ]
    )
    assert d.reason is DenyReason.CLOUD_METADATA_ADDRESS
    assert d.blocked_index == 1


def test_resolved_address_of_a_later_hop_rebinds_to_private_blocked() -> None:
    # DNS rebinding: hop 0 resolves public, the same allowlisted host at hop 1 now resolves to an
    # RFC1918 address. Re-validating hop 1's fresh answer catches it.
    d = _deny(
        [
            _hop("https://data.example.in/", PUBLIC),
            _hop("https://data.example.in/", ("10.0.0.5",)),
        ]
    )
    assert d.reason is DenyReason.PRIVATE_ADDRESS
    assert d.blocked_index == 1


def test_mixed_public_private_answer_on_a_hop_fails_closed() -> None:
    d = _deny(
        [
            _hop("https://example.in/", PUBLIC),
            _hop("https://example.in/", ("93.184.216.34", "169.254.169.254")),
        ]
    )
    assert d.reason is DenyReason.CLOUD_METADATA_ADDRESS
    assert d.blocked_index == 1


def test_redirect_target_host_not_allowlisted_blocked() -> None:
    d = _deny(
        [
            _hop("https://example.in/", PUBLIC),
            _hop("https://evil.example.com/", PUBLIC),
        ]
    )
    assert d.reason is DenyReason.HOST_NOT_ALLOWLISTED
    assert d.blocked_index == 1


def test_initial_hop_itself_unsafe_blocked_at_index_zero() -> None:
    d = _deny([_hop("http://10.0.0.5/", None), _hop("https://example.in/", PUBLIC)])
    assert d.reason is DenyReason.PRIVATE_ADDRESS
    assert d.blocked_index == 0


# --------------------------------------------------------------------------------------------------
# Redirect cap
# --------------------------------------------------------------------------------------------------


def test_too_many_redirects_refused() -> None:
    hops = [_hop("https://example.in/", PUBLIC)] + [
        _hop("https://example.in/", PUBLIC) for _ in range(DEFAULT_MAX_REDIRECTS + 1)
    ]
    d = _deny(hops)
    assert d.reason is RedirectDenyReason.TOO_MANY_REDIRECTS
    assert d.blocked_index == DEFAULT_MAX_REDIRECTS + 1


def test_custom_lower_cap_enforced() -> None:
    hops = [
        _hop("https://example.in/", PUBLIC),
        _hop("https://example.in/", PUBLIC),
        _hop("https://example.in/", PUBLIC),
    ]
    d = _deny(hops, max_redirects=1)
    assert d.reason is RedirectDenyReason.TOO_MANY_REDIRECTS
    assert d.blocked_index == 2


def test_unsafe_hop_within_cap_beats_too_many_redirects() -> None:
    # An unsafe hop inside the bounded prefix is reported with its own reason, not masked by a chain
    # that is also over-long: stop-at-first-failure in hop order.
    hops = [
        _hop("https://example.in/", PUBLIC),
        _hop("http://127.0.0.1/", None),
    ] + [_hop("https://example.in/", PUBLIC) for _ in range(DEFAULT_MAX_REDIRECTS + 2)]
    d = _deny(hops)
    assert d.reason is DenyReason.LOOPBACK_ADDRESS
    assert d.blocked_index == 1


# --------------------------------------------------------------------------------------------------
# Scheme downgrade / credentials appearing mid-chain
# --------------------------------------------------------------------------------------------------


def test_https_to_http_downgrade_mid_chain_blocked() -> None:
    d = _deny(
        [
            _hop("https://example.in/", PUBLIC),
            _hop("http://example.in/", PUBLIC),
        ]
    )
    assert d.reason is RedirectDenyReason.SCHEME_DOWNGRADE
    assert d.blocked_index == 1


def test_http_to_https_upgrade_is_fine() -> None:
    d = validate_redirect_chain(
        [
            _hop("http://example.in/", PUBLIC),
            _hop("https://example.in/", PUBLIC),
        ],
        allowlist=ALLOWLIST,
    )
    assert d.allowed


def test_credentials_appearing_mid_chain_blocked() -> None:
    d = _deny(
        [
            _hop("https://example.in/", PUBLIC),
            _hop("https://user:pass@example.in/", PUBLIC),
        ]
    )
    assert d.reason is DenyReason.CREDENTIALS_IN_URL
    assert d.blocked_index == 1


def test_non_http_scheme_target_mid_chain_blocked() -> None:
    d = _deny(
        [
            _hop("https://example.in/", PUBLIC),
            _hop("file:///etc/passwd", None),
        ]
    )
    assert d.reason is DenyReason.SCHEME_NOT_ALLOWED
    assert d.blocked_index == 1


def test_allowlisted_hostname_hop_without_dns_answer_is_unresolved() -> None:
    d = _deny(
        [
            _hop("https://example.in/", PUBLIC),
            _hop("https://data.example.in/", None),
        ]
    )
    assert d.reason is DenyReason.UNRESOLVED
    assert d.blocked_index == 1


# --------------------------------------------------------------------------------------------------
# Edge / purity / determinism
# --------------------------------------------------------------------------------------------------


def test_empty_chain_is_malformed() -> None:
    d = _deny([])
    assert d.reason is DenyReason.MALFORMED_URL
    assert d.blocked_index is None


def test_decision_is_deterministic_and_inert() -> None:
    hops = [
        _hop("https://data.example.in/", PUBLIC),
        _hop("http://169.254.169.254/", None),
    ]
    first = validate_redirect_chain(hops, allowlist=ALLOWLIST)
    second = validate_redirect_chain(hops, allowlist=ALLOWLIST)
    assert first == second
    assert first.reason is DenyReason.CLOUD_METADATA_ADDRESS
    assert first.blocked_index == 1
    assert first.reason_code == "cloud_metadata_address"


def test_reason_code_is_none_when_allowed() -> None:
    d = validate_redirect_chain([_hop("https://example.in/", PUBLIC)], allowlist=ALLOWLIST)
    assert d.allowed
    assert d.reason_code is None
