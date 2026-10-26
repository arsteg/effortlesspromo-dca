"""DCA-012 — structured correlation logging with central redaction.

Proves the two guarantees: every record is a JSON line stamped with the request's correlation id,
and synthetic tokens / emails / phone numbers never appear in a record (message, extra, or
traceback). The "failure case" is the content-sensitivity check: ordinary text is left intact, so
redaction is a targeted policy, not a blanket that would hide everything (or nothing).
"""

from __future__ import annotations

import io
import json
import logging

from fastapi import APIRouter
from fastapi.testclient import TestClient

from app.core.logging import (
    EMAIL_MASK,
    PHONE_MASK,
    REDACTED,
    Redactor,
    configure_logging,
    get_correlation_id,
)
from app.main import create_app
from tests.conftest import make_settings

# Sentinel values a human can grep for (story manual test step 2).
TOKEN = "svc-tok-SENTINEL-abc123"
EMAIL = "a.person@synthetic-lead.example"
PHONE = "+91-98765-43210"


def _capture_logger() -> tuple[logging.Logger, io.StringIO]:
    buffer = io.StringIO()
    configure_logging(make_settings(service_auth_token=TOKEN), stream=buffer, level="DEBUG")
    return logging.getLogger("dca.test"), buffer


def _lines(buffer: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


# --- the redactor policy, unit level --------------------------------------------------------------


def test_redactor_masks_email_phone_and_registered_secret():
    redactor = Redactor(secrets=(TOKEN,))
    out = redactor.redact_text(f"call token={TOKEN} to {EMAIL} / {PHONE}")
    assert TOKEN not in out and EMAIL not in out and PHONE not in out
    assert REDACTED in out and EMAIL_MASK in out and PHONE_MASK in out


def test_redactor_leaves_ordinary_text_intact():
    # Failure-case guard: a normal message, an ISO date, and a short id must NOT be redacted —
    # otherwise the policy would be an untargeted blanket.
    redactor = Redactor(secrets=(TOKEN,))
    text = "discovery completed for business 11111111 on 2026-10-26 interval=3600s found=5"
    assert redactor.redact_text(text) == text


def test_sensitive_key_value_is_redacted_whole():
    # A value under a sensitive key is dropped even if its text would not match a pattern.
    redactor = Redactor()
    assert redactor.redact_value("authorization", "Bearer short") == REDACTED
    assert redactor.redact_value("phone", "007") == REDACTED  # too few digits to pattern-match
    assert redactor.redact_value("note", "all good") == "all good"


# --- the full logging pipeline --------------------------------------------------------------------


def test_structured_record_is_json_with_correlation_and_no_leak():
    logger, buffer = _capture_logger()
    logger.info("reaching %s at %s", EMAIL, PHONE, extra={"service_auth_token": TOKEN})

    (record,) = _lines(buffer)
    assert set(record) >= {"timestamp", "level", "logger", "correlationId", "message"}
    assert record["level"] == "INFO"
    assert record["correlationId"] == "-"  # emitted outside any request
    blob = json.dumps(record)
    for sentinel in (TOKEN, EMAIL, PHONE):
        assert sentinel not in blob
    assert EMAIL_MASK in record["message"] and PHONE_MASK in record["message"]
    assert record["service_auth_token"] == REDACTED


def test_traceback_is_scrubbed_but_not_dropped():
    logger, buffer = _capture_logger()
    try:
        raise RuntimeError(f"boom contacting {EMAIL}")
    except RuntimeError:
        logger.exception("unhandled while processing")

    (record,) = _lines(buffer)
    assert record["exception"] == "RuntimeError"
    assert "traceback" in record  # operators keep the trace...
    assert EMAIL not in json.dumps(record)  # ...but the contact in it is scrubbed
    assert EMAIL_MASK in record["traceback"]


# --- correlation id flows from the request --------------------------------------------------------


def test_request_log_carries_the_request_correlation_id():
    app = create_app(make_settings(service_auth_token=TOKEN))
    probe = APIRouter()

    @probe.get("/_debug/log")
    def _log() -> dict:
        logging.getLogger("dca.test").info("inside request")
        return {"cid": get_correlation_id()}

    app.include_router(probe)
    # Point the dca handler at our buffer (replaces the stderr handler create_app installed).
    buffer = io.StringIO()
    configure_logging(make_settings(service_auth_token=TOKEN), stream=buffer)

    client = TestClient(app)
    response = client.get("/_debug/log", headers={"X-Correlation-Id": "trace-xyz"})

    assert response.json()["cid"] == "trace-xyz"
    assert response.headers["X-Correlation-Id"] == "trace-xyz"
    (record,) = _lines(buffer)
    assert record["correlationId"] == "trace-xyz"
    assert record["message"] == "inside request"
