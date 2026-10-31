"""DCA-129 — the authenticated DCA HTTP adapter to the effortlesspromo-ai gateway.

Focused on the DCA-129 hardening surface over the plain contract covered by ``test_ai_client.py``:
allowlisted service URL, verified service-auth + correlation headers, explicit deadlines, redacted
errors, and the fail-safe failure classification — in particular that a read/write timeout (the
request may already have been submitted) is UNCERTAIN, while a not-submitted failure is cleanly
unavailable. No provider SDK/key/routing surface exists on the adapter.
"""

import httpx
import pytest
from sqlalchemy import select

from app.clients.ai_client import (
    AiClient,
    AiDisabled,
    AiEndpointNotAllowed,
    AiServiceRejected,
    AiServiceTimeout,
    AiServiceUnavailable,
)
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import AiInvocation
from app.services.ai_invoker import invoke_recorded

COMPLETED = {"jobId": "dca:x", "capability": "research", "status": "completed"}


def _client(handler) -> AiClient:
    return AiClient(
        "http://ai.internal", "service-secret-token", transport=httpx.MockTransport(handler)
    )


# --- allowlisted service URL ----------------------------------------------------------------------


def test_disabled_without_config_fails_closed():
    client = AiClient(None, None)
    assert client.enabled is False
    with pytest.raises(AiDisabled):
        client.invoke("research", business_id="b", job_id="j", request_id="r")


def test_base_url_scheme_is_validated():
    with pytest.raises(ValueError, match="scheme"):
        AiClient("ftp://ai.internal", "secret")


@pytest.mark.parametrize(
    "bad", ["../admin", "a/b", "research/..", "http://evil/x", "//evil", "a b"]
)
def test_capability_escaping_the_pinned_path_is_refused_before_io(bad):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=COMPLETED)

    with pytest.raises(AiEndpointNotAllowed):
        _client(handler).invoke(bad, business_id="b", job_id="dca:1", request_id="dca:1")
    assert calls == []  # refused before any socket was opened — nothing changed


# --- verified service-auth + correlation + deterministic request id -------------------------------


def test_sends_service_auth_correlation_and_deterministic_request_id():
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("X-Service-Auth")
        seen["request_id"] = request.headers.get("X-Request-Id")
        seen["correlation_id"] = request.headers.get("X-Correlation-Id")
        return httpx.Response(200, json=COMPLETED)

    _client(handler).invoke(
        "research",
        business_id="biz-1",
        job_id="dca:j1",
        request_id="dca:j1",
        correlation_id="trace-abc",
    )
    assert seen["url"] == "http://ai.internal/v1/research"
    assert seen["auth"] == "service-secret-token"
    assert seen["request_id"] == "dca:j1"  # deterministic idempotency key (B4) — a retry replays it
    assert (
        seen["correlation_id"] == "trace-abc"
    )  # distinct trace id, never displaces the request id


def test_correlation_id_is_minted_when_absent():
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["correlation_id"] = request.headers.get("X-Correlation-Id")
        seen["request_id"] = request.headers.get("X-Request-Id")
        return httpx.Response(200, json=COMPLETED)

    _client(handler).invoke("research", business_id="b", job_id="dca:1", request_id="dca:1")
    assert seen["correlation_id"]  # minted
    assert seen["correlation_id"] != seen["request_id"]  # never reuses the idempotency key


# --- redacted errors ------------------------------------------------------------------------------


def test_4xx_is_rejected_non_retryable_and_redacted():
    token = "service-secret-token"

    def handler(_request: httpx.Request) -> httpx.Response:
        # A hostile/echoing body: the token + PII must NOT escape into the raised error.
        return httpx.Response(401, text=f"unauthorized token={token} email=victim@example.com")

    with pytest.raises(AiServiceRejected) as excinfo:
        _client(handler).invoke("research", business_id="b", job_id="dca:1", request_id="dca:1")
    err = excinfo.value
    assert err.status_code == 401
    text = str(err)
    assert token not in text and "victim@example.com" not in text and "unauthorized" not in text
    assert "401" in text  # only the stable status code is surfaced


def test_repr_never_leaks_the_token():
    assert "service-secret-token" not in repr(_client(lambda r: httpx.Response(200)))


# --- fail-safe failure classification -------------------------------------------------------------


def test_read_timeout_is_uncertain_possibly_submitted():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(AiServiceTimeout):
        _client(handler).invoke("research", business_id="b", job_id="dca:1", request_id="dca:1")
    # It is a subclass of AiServiceUnavailable so existing handlers still catch it.
    assert issubclass(AiServiceTimeout, AiServiceUnavailable)


def test_connect_failure_is_cleanly_unavailable_not_uncertain():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(AiServiceUnavailable) as excinfo:
        _client(handler).invoke("research", business_id="b", job_id="dca:1", request_id="dca:1")
    assert not isinstance(excinfo.value, AiServiceTimeout)  # not submitted => safe to retry fresh


def test_5xx_is_unavailable():
    with pytest.raises(AiServiceUnavailable):
        _client(lambda r: httpx.Response(503)).invoke(
            "research", business_id="b", job_id="dca:1", request_id="dca:1"
        )


def test_body_level_failure_is_returned_as_data():
    failed = dict(COMPLETED, status="failed", error={"code": "validation_failed"})
    envelope = _client(lambda r: httpx.Response(200, json=failed)).invoke(
        "research", business_id="b", job_id="dca:1", request_id="dca:1"
    )
    assert envelope["status"] == "failed"  # a legitimate outcome, not raised


# --- no provider SDK / key / routing surface ------------------------------------------------------


def test_adapter_exposes_no_provider_model_or_key_surface():
    for forbidden in (
        "provider",
        "model",
        "api_key",
        "apikey",
        "route",
        "fallback",
        "chat",
        "complete",
    ):
        assert not any(forbidden in name.lower() for name in dir(AiClient))


# --- durable recorder marks a timed-out call UNCERTAIN (ai_invoker) -------------------------------


def _session():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)()


def test_read_timeout_records_row_as_uncertain_for_same_id_reconciliation():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client = _client(handler)
    with _session() as session:
        with pytest.raises(AiServiceTimeout):
            invoke_recorded(session, client, business_id="biz-1", capability="research")
        row = session.scalars(select(AiInvocation)).one()
        assert row.status == "uncertain"  # possibly submitted — do not blindly replay
        assert row.request_id  # the SAME deterministic id a reconciliation must reuse (B4)


def test_connect_failure_records_row_as_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = _client(handler)
    with _session() as session:
        with pytest.raises(AiServiceUnavailable):
            invoke_recorded(session, client, business_id="biz-1", capability="research")
        row = session.scalars(select(AiInvocation)).one()
        assert row.status == "unavailable"  # not submitted — safe to retry fresh
