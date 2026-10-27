"""DCA-035 — request idempotency over the real HTTP surface.

Drives the service's write routes through ``TestClient`` to prove the headline acceptance criteria
end to end: a byte-identical retry carrying the same ``Idempotency-Key`` replays the first result with
NO second state change; the same key with a different body is rejected (``409``) and changes nothing;
and the same key used by two tenants never collides. SQLite is sufficient — the behaviour is
app-level (store scoping + the unique constraint), independent of the backend; the transaction/RLS
paths are covered by ``tests/test_idempotency_store.py`` and ``tests/test_rls.py``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.idempotency import IDEMPOTENCY_HEADER
from app.main import create_app
from tests.conftest import AUTH, make_settings

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def client() -> TestClient:
    app = create_app(make_settings())
    with TestClient(app, raise_server_exceptions=False) as test_client:
        # Register both synthetic tenants (so config writes have a tenant to act on).
        for biz in (BIZ_A, BIZ_B):
            test_client.post("/v1/tenants", json={"businessId": biz}, headers=AUTH)
        yield test_client


def _key(value: str) -> dict:
    return {**AUTH, IDEMPOTENCY_HEADER: value}


def _config(client: TestClient, biz: str) -> dict:
    return client.get(f"/v1/tenants/{biz}/config", headers=AUTH).json()


def test_same_key_same_body_returns_the_same_result(client):
    body = {"dataMode": "suggest_platform_data", "enabled": True}
    first = client.put(f"/v1/tenants/{BIZ_A}/config", json=body, headers=_key("cfg-1"))
    second = client.put(f"/v1/tenants/{BIZ_A}/config", json=body, headers=_key("cfg-1"))

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()  # replayed verbatim
    assert second.headers.get("Idempotent-Replay") == "true"  # served from the store, not re-run


def test_replay_causes_no_second_state_change(client):
    # First call flips data mode AND enables; a change to the stored result would show on a replay.
    client.put(
        f"/v1/tenants/{BIZ_A}/config",
        json={"dataMode": "suggest_platform_data", "enabled": True},
        headers=_key("cfg-1"),
    )
    after_first = _config(client, BIZ_A)

    # Out of band, move the config somewhere else (no idempotency key — a normal write).
    client.put(
        f"/v1/tenants/{BIZ_A}/config",
        json={"dataMode": "my_data_only", "enabled": False},
        headers=AUTH,
    )
    moved = _config(client, BIZ_A)
    assert moved["dataMode"] == "my_data_only" and moved["enabled"] is False

    # Replaying the first request returns the first result but must NOT re-apply its state change.
    replay = client.put(
        f"/v1/tenants/{BIZ_A}/config",
        json={"dataMode": "suggest_platform_data", "enabled": True},
        headers=_key("cfg-1"),
    )
    assert replay.json() == after_first  # same response as the first call
    # State is still where the out-of-band write left it — the replay did not run the handler.
    assert _config(client, BIZ_A)["dataMode"] == "my_data_only"
    assert _config(client, BIZ_A)["enabled"] is False


def test_same_key_different_body_is_rejected_and_changes_nothing(client):
    client.put(
        f"/v1/tenants/{BIZ_A}/config",
        json={"dataMode": "suggest_platform_data"},
        headers=_key("cfg-1"),
    )
    before = _config(client, BIZ_A)

    reused = client.put(
        f"/v1/tenants/{BIZ_A}/config",
        json={"dataMode": "my_data_only"},  # SAME key, DIFFERENT body
        headers=_key("cfg-1"),
    )
    assert reused.status_code == 409
    assert reused.json()["error"]["message"] == "idempotency_key_reuse"
    assert _config(client, BIZ_A) == before  # the rejected reuse touched no state


def test_same_key_does_not_collide_across_tenants(client):
    body = {"dataMode": "suggest_platform_data", "enabled": True}
    a = client.put(f"/v1/tenants/{BIZ_A}/config", json=body, headers=_key("shared-key"))
    b = client.put(f"/v1/tenants/{BIZ_B}/config", json=body, headers=_key("shared-key"))

    assert a.status_code == 200 and b.status_code == 200
    assert a.json()["businessId"] == BIZ_A
    assert b.json()["businessId"] == BIZ_B  # B's own result, not A's replayed
    assert b.headers.get("Idempotent-Replay") is None  # B's key is independent of A's


def test_register_is_idempotent_by_body_scoped_key(client):
    # register carries businessId in the BODY (no path scope); the key still scopes to that business.
    biz = "33333333-3333-3333-3333-333333333333"
    first = client.post("/v1/tenants", json={"businessId": biz}, headers=_key("reg-1"))
    second = client.post("/v1/tenants", json={"businessId": biz}, headers=_key("reg-1"))
    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json() == first.json()
    assert second.headers.get("Idempotent-Replay") == "true"


def test_no_key_is_served_normally(client):
    # No Idempotency-Key => the write runs every time (opt-in protection), recording nothing.
    r1 = client.post(f"/v1/tenants/{BIZ_A}/enable", headers=AUTH)
    r2 = client.post(f"/v1/tenants/{BIZ_A}/enable", headers=AUTH)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.headers.get("Idempotent-Replay") is None
    assert r2.headers.get("Idempotent-Replay") is None
