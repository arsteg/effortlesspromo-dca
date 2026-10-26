"""DCA-013 — database + local-dependency readiness.

Happy path reports ready; a database failure makes readiness fail (503) while liveness stays up;
optional integrations are reported separately and never gate readiness; and no response ever leaks
a connection string or credential.
"""

from __future__ import annotations

import os
import tempfile

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.core.readiness import build_readiness
from app.main import create_app
from tests.conftest import make_settings


def _broken_engine():
    # A real engine whose parent directory does not exist => SQLite fails on connect. Exercises the
    # real ``engine.connect()`` path (not a mock) the way a stopped PostgreSQL would.
    missing = os.path.join(tempfile.gettempdir(), "dca-no-such-dir-xyz", "readiness.db")
    return create_engine(f"sqlite:///{missing}")


def test_ready_when_database_is_reachable(client):
    response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"] == {"status": "ok", "required": True}


def test_optional_integrations_reported_separately_and_do_not_gate():
    # AI off, worker off, synthetic sources ON — still ready; integrations are informational only.
    settings = make_settings(ai_base_url=None, worker_enabled=False, synthetic_sources_enabled=True)
    app = create_app(settings)
    with TestClient(app) as client:
        body = client.get("/health/ready").json()
    assert body["status"] == "ready"
    integrations = body["integrations"]
    assert integrations["ai_service"] == {"enabled": False, "status": "disabled"}
    assert integrations["worker"] == {"enabled": False, "status": "disabled"}
    assert integrations["synthetic_sources"] == {"enabled": True, "status": "enabled"}


def test_database_failure_fails_readiness_but_not_liveness():
    app = create_app(make_settings())
    with TestClient(app) as client:
        # Swap in a broken engine to simulate the DB going away at runtime.
        app.state.engine = _broken_engine()

        ready = client.get("/health/ready")
        assert ready.status_code == 503
        assert ready.json()["status"] == "not_ready"
        assert ready.json()["checks"]["database"]["status"] == "error"

        # Liveness is unaffected — the process is alive, just not serving.
        live = client.get("/health/live")
        assert live.status_code == 200
        assert live.json() == {"status": "ok"}


def test_readiness_never_leaks_credentials():
    # A URL carrying a password must not appear anywhere in the readiness report.
    secret_url = "postgresql+psycopg://dca_user:SuperSecretPw@db.internal:5432/dca"
    settings = make_settings(database_url=secret_url)
    # Build the report against a broken engine so the error branch is covered too.
    report, is_ready = build_readiness(settings, _broken_engine())
    assert is_ready is False
    blob = str(report)
    for leak in ("SuperSecretPw", "dca_user", "db.internal", secret_url):
        assert leak not in blob


def test_ready_requires_no_auth(client):
    # Readiness is an infra probe: it carries no business data and needs no X-Service-Auth.
    assert client.get("/health/ready").status_code in (200, 503)
