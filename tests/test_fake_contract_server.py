"""DCA-033 — the local contract-test HTTP server harness.

Covers the acceptance surface: it can simulate every transport outcome (success / classified
4xx-5xx / timeout / unreachable), it refuses production selection, and it never invents a missing
business contract (unknown route → a deliberate 404, not a fabricated success). Everything runs
through the real :class:`~app.clients.platform_transport.PlatformHttpTransport` over the fake's
``httpx.MockTransport`` — no network, no AI, no paid action.
"""

from __future__ import annotations

import pytest

from app.clients.platform_transport import (
    PlatformHttpTransport,
    PlatformTransportError,
    ResponseClass,
    TransportErrorKind,
)
from app.core.config import Settings
from app.testing import (
    NON_PRODUCTION_BANNER,
    FakeContractServer,
    FakeServerForbiddenInProduction,
    FixtureOutcome,
    FixtureRoute,
    TimeoutPhase,
    build_fake_contract_server,
    register_identity_entitlement_fixtures,
)

BASE = "https://platform.internal/svc"
CRED = "scoped-platform-credential-SENTINEL-xyz"  # synthetic, local-only


def _server(**kwargs: object) -> FakeContractServer:
    # The transport base URL is mounted under ``/svc``; the server strips that prefix so fixtures are
    # registered relative to the service root.
    return FakeContractServer(base_path="/svc", **kwargs)  # type: ignore[arg-type]


def _transport(server: FakeContractServer) -> PlatformHttpTransport:
    return PlatformHttpTransport(BASE, CRED, transport=server.as_transport())


# -- transport outcomes ----------------------------------------------------------------------------


def test_simulates_success_response_and_records_the_call() -> None:
    server = _server()
    server.set_route("GET", "/tenants/A", FixtureOutcome.response(200, json={"tenant": "A"}))

    with _transport(server) as transport:
        result = transport.request("GET", "tenants/A", correlation_id="trace-ok")

    assert result.response_class is ResponseClass.SUCCESS
    assert result.json == {"tenant": "A"}
    assert server.call_count == 1
    call = server.last_call
    assert call is not None and call.matched is True
    assert call.correlation_id == "trace-ok"
    # The credential header is masked in the recorded call — never stored verbatim (httpx lowercases).
    assert call.headers.get("authorization") == "«redacted»"
    assert CRED not in str(call.headers)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, ResponseClass.UNAUTHORIZED),
        (403, ResponseClass.FORBIDDEN),
        (429, ResponseClass.RATE_LIMITED),
        (404, ResponseClass.CLIENT_ERROR),
        (503, ResponseClass.SERVER_ERROR),
    ],
)
def test_simulates_classified_http_outcomes(status: int, expected: ResponseClass) -> None:
    server = _server()
    server.set_route("GET", "/x", FixtureOutcome.response(status, json={"status": status}))

    with _transport(server) as transport:
        result = transport.request("GET", "x")

    assert result.status_code == status
    assert result.response_class is expected  # classified, not raised


@pytest.mark.parametrize("phase", list(TimeoutPhase))
def test_simulates_timeout_transport_failure(phase: TimeoutPhase) -> None:
    server = _server()
    server.set_route("GET", "/slow", FixtureOutcome.timeout(phase=phase))

    with _transport(server) as transport, pytest.raises(PlatformTransportError) as excinfo:
        transport.request("GET", "slow")

    # Connect- and read-phase timeouts both classify identically (stable) — the failure case.
    assert excinfo.value.kind is TransportErrorKind.TIMEOUT


def test_simulates_unreachable_transport_failure() -> None:
    server = _server()
    server.set_route("GET", "/down", FixtureOutcome.unreachable())

    with _transport(server) as transport, pytest.raises(PlatformTransportError) as excinfo:
        transport.request("GET", "down")

    assert excinfo.value.kind is TransportErrorKind.UNREACHABLE


def test_delay_is_applied_before_the_outcome() -> None:
    slept: list[float] = []
    server = _server(sleep=slept.append)
    server.set_route("GET", "/lag", FixtureOutcome.response(200, delay_seconds=2.5))

    with _transport(server) as transport:
        transport.request("GET", "lag")

    assert slept == [2.5]  # a configurable delay, not a real wall-clock wait in the test


def test_switching_a_route_to_timeout_changes_the_outcome() -> None:
    # Mirrors the manual test: a route served OK, then flipped to a timeout.
    server = _server()
    server.set_route("GET", "/ping", FixtureOutcome.response(200, json={"ok": True}))

    with _transport(server) as transport:
        assert transport.request("GET", "ping").is_success
        server.set_route("GET", "/ping", FixtureOutcome.timeout())
        with pytest.raises(PlatformTransportError) as excinfo:
            transport.request("GET", "ping")

    assert excinfo.value.kind is TransportErrorKind.TIMEOUT


# -- does not invent missing contracts (the failure case) ------------------------------------------


def test_unknown_route_returns_a_deliberate_404_not_a_fabricated_success() -> None:
    server = _server()  # no routes registered at all

    with _transport(server) as transport:
        result = transport.request("GET", "suppression/check")

    assert result.status_code == 404
    assert result.response_class is ResponseClass.CLIENT_ERROR
    assert result.json is not None
    assert result.json["error"] == "no_fixture_configured"
    assert result.json["synthetic"] is True
    # The call IS recorded, and recorded as unmatched — visible in the call log.
    assert server.call_count == 1
    assert server.last_call is not None and server.last_call.matched is False


def test_registering_a_domain_fixture_is_explicit() -> None:
    # A later adapter story registers its OWN fixture; nothing is pre-wired.
    server = _server()
    assert server.routes == ()
    server.register(FixtureRoute("POST", "/suppression/check", FixtureOutcome.response(200)))
    assert len(server.routes) == 1


# -- production refusal -----------------------------------------------------------------------------


def _settings(environment: str) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment=environment,
        database_url="sqlite+pysqlite:///:memory:",
    )


def test_build_refuses_production() -> None:
    with pytest.raises(FakeServerForbiddenInProduction):
        build_fake_contract_server(_settings("production"))


def test_build_allows_non_production_and_exposes_banner() -> None:
    server = build_fake_contract_server(_settings("development"))
    assert isinstance(server, FakeContractServer)
    assert server.banner() == NON_PRODUCTION_BANNER
    assert "NON-PRODUCTION" in server.banner()


# -- identity/entitlement fixture reuse ------------------------------------------------------------


def test_reuses_identity_and_entitlement_fixtures_only() -> None:
    from app.identity.fake import BUSINESS_A

    server = register_identity_entitlement_fixtures(_server())

    with _transport(server) as transport:
        ident = transport.request("GET", f"identity/{BUSINESS_A}")
        ent = transport.request("GET", f"entitlements/{BUSINESS_A}")

    assert ident.is_success and ident.json is not None
    assert ident.json["status"] == "active"
    assert ident.json["synthetic"] is True
    assert ent.is_success and ent.json is not None
    assert ent.json["synthetic"] is True
    assert ent.json["plan"] == "synthetic-growth"
