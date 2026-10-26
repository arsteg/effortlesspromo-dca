"""AI client against the recorded contract (docs/DCA-001-integration-contracts.md §2)."""

import httpx
import pytest

from app.clients.ai_client import AiClient, AiDisabled, AiServiceUnavailable

COMPLETED = {
    "jobId": "dca:x",
    "capability": "research",
    "status": "completed",
    "structuredOutput": {"answer": 1},
    "costEstimate": {"amount": 0.01, "currency": "USD", "estimated": False},
}


def make_client(handler) -> AiClient:
    return AiClient("http://ai.internal", "secret", transport=httpx.MockTransport(handler))


def test_disabled_without_config():
    client = AiClient(None, None)
    assert client.enabled is False
    with pytest.raises(AiDisabled):
        client.invoke("research", business_id="b", job_id="j", request_id="r")


def test_invoke_sends_contract_headers_and_payload():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("X-Service-Auth")
        seen["request_id"] = request.headers.get("X-Request-Id")
        seen["body"] = request.read().decode()
        return httpx.Response(200, json=COMPLETED)

    envelope = make_client(handler).invoke(
        "research",
        business_id="biz-1",
        job_id="dca:j1",
        request_id="dca:j1",
        input_context={"q": "x"},
        dry_run=True,
    )
    assert envelope["status"] == "completed"
    assert seen["url"] == "http://ai.internal/v1/research"
    assert seen["auth"] == "secret"
    assert seen["request_id"] == "dca:j1"
    assert '"dryRun":true' in seen["body"].replace(" ", "")
    assert '"businessId":"biz-1"' in seen["body"].replace(" ", "")


def test_body_level_failure_is_returned_not_raised():
    failed = dict(
        COMPLETED, status="failed", error={"code": "validation_failed", "retryable": False}
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=failed)  # 200 envelope with status=failed, per AI-001

    envelope = make_client(handler).invoke("research", business_id="b", job_id="j", request_id="r")
    assert envelope["status"] == "failed"
    assert envelope["error"]["code"] == "validation_failed"


def test_5xx_and_transport_errors_raise_unavailable():
    def handler_500(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with pytest.raises(AiServiceUnavailable):
        make_client(handler_500).invoke("research", business_id="b", job_id="j", request_id="r")

    def handler_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom", request=request)

    with pytest.raises(AiServiceUnavailable):
        make_client(handler_timeout).invoke("research", business_id="b", job_id="j", request_id="r")


def test_4xx_is_a_caller_bug_not_retryable():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    with pytest.raises(RuntimeError, match="rejected"):
        make_client(handler).invoke("research", business_id="b", job_id="j", request_id="r")
