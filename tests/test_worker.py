"""Worker tick: per-tenant isolation of outcomes, disabled tenants skipped, no crash propagation."""

from fastapi.testclient import TestClient

from app.main import create_app
from app.workers.scheduler import tick_all_tenants
from tests.conftest import AUTH, make_settings

READY = "33333333-3333-3333-3333-333333333333"
NO_ICP = "44444444-4444-4444-4444-444444444444"
DISABLED = "55555555-5555-5555-5555-555555555555"


def test_tick_runs_only_enabled_tenants_and_survives_per_tenant_failures():
    settings = make_settings()
    app = create_app(settings)
    with TestClient(app) as client:
        client.post("/v1/tenants", json={"businessId": READY}, headers=AUTH)
        client.post(f"/v1/tenants/{READY}/enable", headers=AUTH)
        client.put(f"/v1/tenants/{READY}/icp", json={"industries": []}, headers=AUTH)

        client.post("/v1/tenants", json={"businessId": NO_ICP}, headers=AUTH)
        client.post(f"/v1/tenants/{NO_ICP}/enable", headers=AUTH)  # enabled but no ICP => skipped

        client.post("/v1/tenants", json={"businessId": DISABLED}, headers=AUTH)  # never enabled

        outcomes = tick_all_tenants(app.state.session_factory, settings)

    assert outcomes[READY] == "completed:3"
    assert outcomes[NO_ICP] == "skipped:icp_missing"
    assert DISABLED not in outcomes

    # The tick's work is visible through the API afterwards.
    with TestClient(app) as client:
        candidates = client.get(f"/v1/tenants/{READY}/candidates", headers=AUTH).json()
        assert len(candidates) == 3
