"""DCA-005 — application factory + minimal liveness entry point.

Scope of this story is only the app shell and liveness. Readiness and business routes exist in the
repo from later foundation work and are covered by their own tests; these assertions pin just the
DCA-005 acceptance criteria: a stable liveness success, startup independent of EffortlessPromo, and
the start-time failure case.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from tests.conftest import make_settings


def test_liveness_returns_stable_success(client):
    # Stable: same shape and body on repeat calls, no auth header, no state change.
    for _ in range(3):
        response = client.get("/health/live")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


def test_app_starts_independently_of_effortlesspromo():
    # No AI service URL, no inbound token => the app still boots and serves liveness. Liveness must
    # not depend on effortlesspromo-ai or any configured secret.
    settings = make_settings(service_auth_token=None, ai_base_url=None)
    assert settings.ai_enabled is False
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/health/live").json() == {"status": "ok"}


def test_liveness_is_read_only(client):
    # Relevant denial: liveness is GET-only; a write verb is rejected and changes nothing.
    assert client.post("/health/live").status_code == 405


def test_factory_refuses_to_start_on_invalid_config():
    # Failure case: the factory validates config before building the app, so a misconfigured
    # instance never comes up serving liveness.
    bad = Settings(_env_file=None, environment=None, database_url="sqlite://")
    with pytest.raises(RuntimeError, match="EFFORTLESSPROMO_DCA_ENVIRONMENT"):
        create_app(bad)
