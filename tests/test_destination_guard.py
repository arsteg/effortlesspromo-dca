"""DCA-118 — the pure network-destination guard (the fetch client's SSRF gate).

Unit tests over :mod:`app.sources.destination_guard`, independent of any HTTP/DNS: an allowlisted
public destination passes, and every forbidden class (non-http scheme, embedded credentials, a
non-web port, a non-allowlisted host, and each unsafe address class — private / loopback / link-local
/ cloud-metadata / IPv6 unique-local / reserved) is refused with the correct, deterministic reason.
A model-supplied URL cannot bypass the guard, and a mixed public+private DNS answer fails closed. The
decider is pure and network-free — it validates, it never connects or fetches.
"""

from __future__ import annotations

from app.sources.destination_guard import (
    DenyReason,
    DestinationVerdict,
    validate_destination,
)

ALLOWLIST = frozenset({"example.in", "data.example.in"})


def _deny(url: str, **kw) -> DenyReason | None:
    d = validate_destination(url, allowlist=ALLOWLIST, **kw)
    assert d.verdict is DestinationVerdict.DENY, d
    assert not d.allowed
    return d.reason


# --------------------------------------------------------------------------------------------------
# Allowed public destination
# --------------------------------------------------------------------------------------------------


def test_allowlisted_public_hostname_with_public_dns_is_allowed() -> None:
    d = validate_destination(
        "https://data.example.in/records?q=1",
        allowlist=ALLOWLIST,
        resolved_ips=["93.184.216.34"],
    )
    assert d.verdict is DestinationVerdict.ALLOW
    assert d.allowed
    assert d.host == "data.example.in"
    assert d.safe_ips == ("93.184.216.34",)
    assert d.reason is None


def test_subdomain_of_allowlist_entry_is_covered() -> None:
    d = validate_destination(
        "http://api.example.in/", allowlist=ALLOWLIST, resolved_ips=["8.8.8.8"]
    )
    assert d.allowed


def test_both_http_and_https_schemes_allowed() -> None:
    for url in ("http://example.in/", "https://example.in/"):
        d = validate_destination(url, allowlist=ALLOWLIST, resolved_ips=["1.1.1.1"])
        assert d.allowed, url


def test_default_and_explicit_web_ports_allowed() -> None:
    for url in ("https://example.in/", "https://example.in:443/", "http://example.in:80/"):
        d = validate_destination(url, allowlist=ALLOWLIST, resolved_ips=["1.1.1.1"])
        assert d.allowed, url


def test_allowed_public_ip_literal_on_allowlist() -> None:
    d = validate_destination("https://1.1.1.1/", allowlist={"1.1.1.1"}, resolved_ips=None)
    assert d.allowed
    assert d.safe_ips == ("1.1.1.1",)


# --------------------------------------------------------------------------------------------------
# Scheme / credentials / port / allowlist / parse refusals
# --------------------------------------------------------------------------------------------------


def test_non_http_schemes_refused() -> None:
    for url in (
        "file:///etc/passwd",
        "ftp://example.in/x",
        "gopher://example.in/",
        "data:text/plain,x",
    ):
        assert _deny(url) is DenyReason.SCHEME_NOT_ALLOWED, url


def test_schemeless_or_relative_is_malformed() -> None:
    for url in ("example.in/path", "/just/a/path", ""):
        assert _deny(url) is DenyReason.MALFORMED_URL, url


def test_embedded_credentials_refused() -> None:
    assert _deny("https://user:pass@example.in/", resolved_ips=["1.1.1.1"]) is (
        DenyReason.CREDENTIALS_IN_URL
    )
    assert _deny("https://user@example.in/", resolved_ips=["1.1.1.1"]) is (
        DenyReason.CREDENTIALS_IN_URL
    )


def test_non_web_port_refused() -> None:
    assert _deny("https://example.in:8080/", resolved_ips=["1.1.1.1"]) is (
        DenyReason.PORT_NOT_ALLOWED
    )
    assert _deny("http://example.in:22/", resolved_ips=["1.1.1.1"]) is (DenyReason.PORT_NOT_ALLOWED)


def test_host_not_on_allowlist_refused() -> None:
    assert _deny("https://evil.example.com/", resolved_ips=["1.1.1.1"]) is (
        DenyReason.HOST_NOT_ALLOWLISTED
    )
    # A substring look-alike is NOT a subdomain and is refused.
    assert _deny("https://evil-example.in/", resolved_ips=["1.1.1.1"]) is (
        DenyReason.HOST_NOT_ALLOWLISTED
    )


def test_empty_allowlist_allows_nothing() -> None:
    d = validate_destination("https://example.in/", allowlist=frozenset(), resolved_ips=["1.1.1.1"])
    assert d.verdict is DestinationVerdict.DENY
    assert d.reason is DenyReason.HOST_NOT_ALLOWLISTED


def test_allowlisted_hostname_without_dns_answer_is_unresolved() -> None:
    assert _deny("https://example.in/", resolved_ips=None) is DenyReason.UNRESOLVED
    assert _deny("https://example.in/", resolved_ips=[]) is DenyReason.UNRESOLVED


def test_public_ip_literal_not_on_allowlist_refused() -> None:
    assert _deny("https://1.1.1.1/") is DenyReason.HOST_NOT_ALLOWLISTED


# --------------------------------------------------------------------------------------------------
# Unsafe address classes — literal hosts
# --------------------------------------------------------------------------------------------------


def test_cloud_metadata_literal_refused() -> None:
    assert _deny("http://169.254.169.254/latest/meta-data/") is (DenyReason.CLOUD_METADATA_ADDRESS)


def test_loopback_literal_refused() -> None:
    assert _deny("http://127.0.0.1/") is DenyReason.LOOPBACK_ADDRESS
    assert _deny("http://[::1]/") is DenyReason.LOOPBACK_ADDRESS


def test_private_rfc1918_literals_refused() -> None:
    for url in ("http://10.0.0.5/", "http://172.16.0.1/", "http://192.168.1.1/"):
        assert _deny(url) is DenyReason.PRIVATE_ADDRESS, url


def test_link_local_literal_refused() -> None:
    assert _deny("http://169.254.0.1/") is DenyReason.LINK_LOCAL_ADDRESS


def test_ipv6_unique_local_literal_refused() -> None:
    assert _deny("http://[fd00::1]/") is DenyReason.UNIQUE_LOCAL_ADDRESS


def test_reserved_and_unspecified_literals_refused() -> None:
    assert _deny("http://0.0.0.0/") is DenyReason.RESERVED_ADDRESS


def test_ipv4_mapped_ipv6_cannot_smuggle_internal_address() -> None:
    # ::ffff:127.0.0.1 must be unwrapped and caught as loopback, not treated as a public v6 literal.
    assert _deny("http://[::ffff:127.0.0.1]/") is DenyReason.LOOPBACK_ADDRESS
    assert _deny("http://[::ffff:169.254.169.254]/") is DenyReason.CLOUD_METADATA_ADDRESS


# --------------------------------------------------------------------------------------------------
# Unsafe DNS answers — the model-supplied/rebinding bypass cannot succeed
# --------------------------------------------------------------------------------------------------


def test_allowlisted_hostname_resolving_to_metadata_is_refused() -> None:
    # The URL looks perfectly legitimate (allowlisted host, https, default port), but its DNS answer
    # points at the metadata service — the guard refuses. A model-supplied URL cannot bypass this.
    assert _deny("https://data.example.in/", resolved_ips=["169.254.169.254"]) is (
        DenyReason.CLOUD_METADATA_ADDRESS
    )


def test_allowlisted_hostname_resolving_to_loopback_is_refused() -> None:
    assert _deny("https://example.in/", resolved_ips=["127.0.0.1"]) is (DenyReason.LOOPBACK_ADDRESS)


def test_mixed_public_and_private_dns_answer_fails_closed() -> None:
    # One poisoned A record among public ones fails the whole destination.
    assert _deny("https://example.in/", resolved_ips=["93.184.216.34", "10.0.0.5"]) is (
        DenyReason.PRIVATE_ADDRESS
    )


def test_unparseable_resolved_address_refused() -> None:
    assert _deny("https://example.in/", resolved_ips=["not-an-ip"]) is DenyReason.MALFORMED_URL


# --------------------------------------------------------------------------------------------------
# Purity / determinism
# --------------------------------------------------------------------------------------------------


def test_decision_is_deterministic_and_inert() -> None:
    args = dict(allowlist=ALLOWLIST, resolved_ips=["169.254.169.254"])
    first = validate_destination("https://data.example.in/", **args)
    second = validate_destination("https://data.example.in/", **args)
    assert first == second
    assert first.reason is DenyReason.CLOUD_METADATA_ADDRESS
    assert first.safe_ips == ()
