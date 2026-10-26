"""Test harness: in-memory SQLite, explicit Settings, synthetic sources ON, worker OFF."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

TEST_TOKEN = "test-service-token"
AUTH = {"X-Service-Auth": TEST_TOKEN}


def make_settings(**overrides) -> Settings:
    base = {
        "environment": "testing",
        "database_url": "sqlite+pysqlite:///:memory:",
        "service_auth_token": TEST_TOKEN,
        "synthetic_sources_enabled": True,
        "feature_enabled": True,  # DCA-021: the test profile turns the feature on (like sources)
        "worker_enabled": False,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def client(settings) -> TestClient:
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client
