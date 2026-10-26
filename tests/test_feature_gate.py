"""DCA-021 — the Data Concierge feature gate.

The feature is off by default at the platform level; with it off, even a fully enabled business
cannot start work. Turning it on is the platform enablement — each business is still narrowed in by
its own ``enabled`` flag, and enabling one business never enables another. Denials create no state.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.feature import dca_feature_enabled, work_allowed
from app.main import create_app
from tests.conftest import AUTH, make_settings

A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _register_enable_icp(client: TestClient, business_id: str, *, enable: bool) -> None:
    assert (
        client.post("/v1/tenants", json={"businessId": business_id}, headers=AUTH).status_code
        == 201
    )
    if enable:
        assert client.post(f"/v1/tenants/{business_id}/enable", headers=AUTH).status_code == 200
    body = {"industries": [], "regions": [], "roles": []}
    assert client.put(f"/v1/tenants/{business_id}/icp", json=body, headers=AUTH).status_code == 200


# --- unit: default off + predicate ---------------------------------------------------------------


def test_feature_is_off_by_default():
    settings = Settings(
        _env_file=None, environment="testing", database_url="sqlite+pysqlite:///:memory:"
    )  # type: ignore[call-arg]
    assert settings.feature_enabled is False
    assert dca_feature_enabled(settings) is False


def test_work_allowed_requires_both_flag_and_business():
    on = make_settings(feature_enabled=True)
    off = make_settings(feature_enabled=False)
    assert work_allowed(on, business_enabled=True) is True
    assert work_allowed(on, business_enabled=False) is False  # business not narrowed in
    assert work_allowed(off, business_enabled=True) is False  # platform flag off


# --- route: platform flag off blocks even an enabled business ------------------------------------


def test_disabled_feature_blocks_an_otherwise_enabled_business():
    app = create_app(make_settings(feature_enabled=False))
    with TestClient(app) as client:
        _register_enable_icp(client, A, enable=True)  # fully enabled business...
        resp = client.post(f"/v1/tenants/{A}/discovery/run", headers=AUTH)
        assert resp.status_code == 403  # ...still cannot start work
        assert resp.json()["error"]["message"] == "dca_feature_disabled"
        # No state created by the denial: no runs recorded.
        assert client.get(f"/v1/tenants/{A}/runs", headers=AUTH).json() == []


# --- route: flag on, business narrowing (A on, B off) --------------------------------------------


def test_enabled_feature_narrows_per_business():
    app = create_app(make_settings(feature_enabled=True))
    with TestClient(app) as client:
        _register_enable_icp(client, A, enable=True)  # A narrowed in
        _register_enable_icp(client, B, enable=False)  # B registered but NOT enabled

        run_a = client.post(f"/v1/tenants/{A}/discovery/run", headers=AUTH)
        assert run_a.status_code == 200
        assert run_a.json()["status"] == "completed"

        # Enabling the platform (and A) does not enable B.
        run_b = client.post(f"/v1/tenants/{B}/discovery/run", headers=AUTH)
        assert run_b.status_code == 409
        assert run_b.json()["error"]["message"] == "tenant_not_enabled"
        assert client.get(f"/v1/tenants/{B}/runs", headers=AUTH).json() == []
