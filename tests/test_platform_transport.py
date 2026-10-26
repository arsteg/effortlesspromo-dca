"""DCA-032 — the platform HTTP transport: allowlist, credential redaction, stable error mapping.

Every HTTP exchange here runs against an ``httpx.MockTransport`` fake host (no network). The fake
records what the transport actually sent, and a call counter proves that a refused (allowlisted)
request never reaches the wire — i.e. the denial changes nothing.
"""

from __future__ import annotations

import io
import logging

import httpx
import pytest

from app.clients.platform_transport import (
    PlatformEndpointNotAllowed,
    PlatformHttpTransport,
    PlatformTransportDisabled,
    PlatformTransportError,
    ResponseClass,
    TransportErrorKind,
    build_platform_transport,
    classify_status,
)
from app.core.config import Settings
from app.core.logging import configure_logging

BASE = "https://platform.internal/svc"
CRED = "scoped-platform-credential-abc123"


class RecordingHost:
    """A fake platform host: records each request it is asked to serve and returns a canned response.

    The ``calls`` counter is the state-change probe — if an allowlist refusal ever reached the wire,
    this counter would advance.
    """

    def __init__(self, response: httpx.Response | None = None, raises: Exception | None = None):
        self.calls = 0
        self.requests: list[httpx.Request] = []
        self._response = response or httpx.Response(200, json={"ok": True})
        self._raises = raises

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.requests.append(request)
        if self._raises is not None:
            raise self._raises
        return self._response

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def make_transport(host: RecordingHost, **overrides) -> PlatformHttpTransport:
    kwargs = {"transport": host.transport()}
    kwargs.update(overrides)
    return PlatformHttpTransport(BASE, CRED, **kwargs)


# -- configuration / fail-closed -------------------------------------------------------------------


def test_disabled_without_base_url_or_credential():
    with pytest.raises(PlatformTransportDisabled):
        PlatformHttpTransport(None, CRED)
    with pytest.raises(PlatformTransportDisabled):
        PlatformHttpTransport(BASE, None)


def test_non_http_base_url_rejected():
    with pytest.raises(ValueError, match="http"):
        PlatformHttpTransport("ftp://platform.internal", CRED)


def test_build_factory_returns_none_when_unconfigured():
    unconfigured = Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="testing",
        database_url="sqlite+pysqlite:///:memory:",
    )
    assert build_platform_transport(unconfigured) is None

    configured = Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="testing",
        database_url="sqlite+pysqlite:///:memory:",
        platform_base_url=BASE,
        platform_service_credential=CRED,
    )
    host = RecordingHost()
    built = build_platform_transport(configured, transport=host.transport())
    assert isinstance(built, PlatformHttpTransport)
    built.close()


# -- allowlist: calls remain within the configured endpoint ----------------------------------------


def test_relative_path_stays_within_configured_endpoint():
    host = RecordingHost()
    with make_transport(host) as t:
        result = t.request("GET", "tenants/abc")
    assert result.is_success
    assert host.calls == 1
    assert str(host.requests[0].url) == "https://platform.internal/svc/tenants/abc"


@pytest.mark.parametrize(
    "bad_path",
    [
        "https://evil.example/steal",  # absolute URL to another origin
        "//evil.example/steal",  # protocol-relative reference
        "../../etc/passwd",  # parent traversal escaping the base path
        "tenants/../../other",  # traversal hidden mid-path
    ],
)
def test_off_allowlist_path_is_refused_before_any_io(bad_path: str):
    host = RecordingHost()
    with make_transport(host) as t, pytest.raises(PlatformEndpointNotAllowed):
        t.request("GET", bad_path)
    # The refusal happened before any socket: the fake host was never called (no state change).
    assert host.calls == 0


def test_redirect_is_not_followed_off_allowlist():
    # A platform 302 pointing at another host must NOT be chased — it is returned as a classified
    # (non-success) response, never a second request to the redirect target.
    redirect = httpx.Response(302, headers={"Location": "https://evil.example/pwn"})
    host = RecordingHost(response=redirect)
    with make_transport(host) as t:
        result = t.request("GET", "tenants/abc")
    assert host.calls == 1  # exactly one call; the redirect was not followed
    assert result.status_code == 302
    assert result.response_class is ResponseClass.CLIENT_ERROR


# -- scoped credential + correlation id ------------------------------------------------------------


def test_scoped_credential_and_correlation_headers_are_sent():
    host = RecordingHost()
    with make_transport(host) as t:
        result = t.request("POST", "tenants", json={"x": 1}, correlation_id="trace-xyz")
    sent = host.requests[0]
    assert sent.headers["Authorization"] == f"Bearer {CRED}"
    assert sent.headers["X-Correlation-Id"] == "trace-xyz"
    assert sent.headers["X-Request-Id"] == "trace-xyz"
    assert result.correlation_id == "trace-xyz"


def test_correlation_id_minted_when_absent():
    host = RecordingHost()
    with make_transport(host) as t:
        result = t.request("GET", "tenants/abc")
    assert result.correlation_id  # a fresh id was minted
    assert host.requests[0].headers["X-Correlation-Id"] == result.correlation_id


def test_custom_credential_header_and_bare_scheme():
    host = RecordingHost()
    t = PlatformHttpTransport(
        BASE,
        CRED,
        credential_header="X-Service-Credential",
        credential_scheme="",
        transport=host.transport(),
    )
    with t:
        t.request("GET", "tenants/abc")
    sent = host.requests[0]
    assert sent.headers["X-Service-Credential"] == CRED  # no scheme word prepended
    assert "Authorization" not in sent.headers


def test_repr_never_contains_the_credential():
    host = RecordingHost()
    with make_transport(host) as t:
        assert CRED not in repr(t)


# -- credential redaction in logs ------------------------------------------------------------------


def test_credential_is_redacted_from_logs():
    buffer = io.StringIO()
    # Registering the credential as a secret is exactly what the transport does at construction;
    # configure_logging here captures the dca log stream into a buffer we can grep.
    configure_logging(
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            environment="testing",
            database_url="sqlite+pysqlite:///:memory:",
            platform_base_url=BASE,
            platform_service_credential=CRED,
        ),
        stream=buffer,
    )
    host = RecordingHost()
    with make_transport(host) as t:
        t.request("GET", "tenants/abc", correlation_id="trace-redact")
    # Emit the credential directly into a log line too, to prove the redactor scrubs it regardless.
    logging.getLogger("dca.platform").info("leaky line carrying %s", CRED)

    raw = buffer.getvalue()
    assert raw  # the transport did log the call
    assert CRED not in raw
    assert "[redacted]" in raw


# -- stable error mapping --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectTimeout("connect timed out"),
        httpx.ReadTimeout("read timed out"),
        httpx.WriteTimeout("write timed out"),
        httpx.PoolTimeout("pool timed out"),
    ],
)
def test_every_timeout_phase_classifies_as_timeout(exc: httpx.TimeoutException):
    host = RecordingHost(raises=exc)
    with make_transport(host) as t, pytest.raises(PlatformTransportError) as caught:
        t.request("GET", "tenants/abc")
    assert caught.value.kind is TransportErrorKind.TIMEOUT
    # The raised message carries only the classification — never the credential or request detail.
    assert CRED not in str(caught.value)


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("connection refused"),
        httpx.ReadError("socket read failed"),
        httpx.RemoteProtocolError("bad framing"),
    ],
)
def test_network_failures_classify_as_unreachable(exc: httpx.HTTPError):
    host = RecordingHost(raises=exc)
    with make_transport(host) as t, pytest.raises(PlatformTransportError) as caught:
        t.request("GET", "tenants/abc")
    assert caught.value.kind is TransportErrorKind.UNREACHABLE


@pytest.mark.parametrize(
    "status,expected",
    [
        (200, ResponseClass.SUCCESS),
        (204, ResponseClass.SUCCESS),
        (401, ResponseClass.UNAUTHORIZED),
        (403, ResponseClass.FORBIDDEN),
        (429, ResponseClass.RATE_LIMITED),
        (404, ResponseClass.CLIENT_ERROR),
        (400, ResponseClass.CLIENT_ERROR),
        (500, ResponseClass.SERVER_ERROR),
        (503, ResponseClass.SERVER_ERROR),
    ],
)
def test_status_classification_is_stable(status: int, expected: ResponseClass):
    assert classify_status(status) == expected
    host = RecordingHost(response=httpx.Response(status, json={"n": status}))
    with make_transport(host) as t:
        result = t.request("GET", "tenants/abc")
    assert result.response_class is expected
    assert result.status_code == status


def test_result_carries_parsed_json_and_capped_text():
    host = RecordingHost(response=httpx.Response(200, json={"hello": "world"}))
    with make_transport(host) as t:
        result = t.request("GET", "tenants/abc")
    assert result.json == {"hello": "world"}
    assert "hello" in result.text


def test_non_json_body_leaves_json_none():
    host = RecordingHost(response=httpx.Response(200, text="plain text, not json"))
    with make_transport(host) as t:
        result = t.request("GET", "tenants/abc")
    assert result.json is None
    assert result.text == "plain text, not json"
