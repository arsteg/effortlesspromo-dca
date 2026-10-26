"""DCA-024 — scoped business agent configuration (enabled state + data mode).

The config is per-business: changing A never affects B. Data mode is my-data-only or
suggest-platform-data; suggestion mode grants no send permission; and auto-accept (and any
unsupported mode) is refused with no state change.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import AUTH, make_settings

A = "11111111-1111-1111-1111-111111111111"
B = "22222222-2222-2222-2222-222222222222"


def _client() -> TestClient:
    return TestClient(create_app(make_settings()))


def _register(client: TestClient, business_id: str) -> None:
    assert (
        client.post("/v1/tenants", json={"businessId": business_id}, headers=AUTH).status_code
        == 201
    )


# --- defaults + round-trip ------------------------------------------------------------------------


def test_default_config_is_my_data_only_with_auto_accept_off():
    with _client() as client:
        _register(client, A)
        cfg = client.get(f"/v1/tenants/{A}/config", headers=AUTH).json()
        assert cfg["dataMode"] == "my_data_only"
        assert cfg["enabled"] is False
        assert cfg["autoAccept"] is False
        assert cfg["suggestionModeGrantsSend"] is False


def test_set_suggest_platform_data_mode():
    with _client() as client:
        _register(client, A)
        resp = client.put(
            f"/v1/tenants/{A}/config",
            json={"dataMode": "suggest_platform_data", "enabled": True},
            headers=AUTH,
        )
        assert resp.status_code == 200
        cfg = resp.json()
        assert cfg["dataMode"] == "suggest_platform_data"
        assert cfg["enabled"] is True
        # Suggestion mode must not grant any sending right.
        assert cfg["suggestionModeGrantsSend"] is False


# --- scoping: A's change does not affect B --------------------------------------------------------


def test_config_change_is_scoped_to_one_business():
    with _client() as client:
        _register(client, A)
        _register(client, B)
        client.put(
            f"/v1/tenants/{A}/config", json={"dataMode": "suggest_platform_data"}, headers=AUTH
        )
        assert client.get(f"/v1/tenants/{A}/config", headers=AUTH).json()["dataMode"] == (
            "suggest_platform_data"
        )
        # B is untouched — still the default.
        assert client.get(f"/v1/tenants/{B}/config", headers=AUTH).json()["dataMode"] == (
            "my_data_only"
        )


# --- auto-accept + unsupported modes are refused, with no state change ---------------------------


def test_auto_accept_is_refused():
    with _client() as client:
        _register(client, A)
        resp = client.put(
            f"/v1/tenants/{A}/config",
            json={"dataMode": "my_data_only", "autoAccept": True},
            headers=AUTH,
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["message"] == "auto_accept_disabled"


def test_unsupported_mode_is_refused_without_state_change():
    with _client() as client:
        _register(client, A)
        # Put a known-good mode first so we can prove the rejected call changed nothing.
        client.put(
            f"/v1/tenants/{A}/config", json={"dataMode": "suggest_platform_data"}, headers=AUTH
        )
        resp = client.put(f"/v1/tenants/{A}/config", json={"dataMode": "auto_accept"}, headers=AUTH)
        assert resp.status_code == 422
        assert resp.json()["error"]["message"] == "unsupported_data_mode"
        # Unchanged by the rejected call.
        assert client.get(f"/v1/tenants/{A}/config", headers=AUTH).json()["dataMode"] == (
            "suggest_platform_data"
        )


def test_config_for_unregistered_business_is_404():
    with _client() as client:
        assert client.get(f"/v1/tenants/{A}/config", headers=AUTH).status_code == 404
