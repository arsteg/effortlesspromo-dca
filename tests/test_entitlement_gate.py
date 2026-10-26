"""DCA-023 — entitlement freshness enforced before a chargeable operation.

A fresh active snapshot authorizes work and is cached; once the subscription expires or is revoked
the gate fails closed; and during an entitlement-host outage the cached projection authorizes only
while fresh — a prior-active subscription cannot keep authorizing paid work indefinitely.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import app.db.models  # noqa: F401 — register tables on Base.metadata
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import EntitlementProjection
from app.entitlements import EntitlementRequest, EntitlementSnapshot, EntitlementStatus
from app.identity.fake import BUSINESS_A, BUSINESS_B
from app.main import create_app
from app.services.entitlement_gate import (
    EntitlementGateError,
    ensure_entitled,
)
from tests.conftest import AUTH, make_settings

T0 = datetime(2026, 6, 1, tzinfo=UTC)
FUTURE = datetime(2999, 1, 1, tzinfo=UTC)


@pytest.fixture
def session():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = make_session_factory(engine)()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


class _StubClient:
    """Returns a fixed snapshot (or raises, to simulate a host outage)."""

    def __init__(
        self, snapshot: EntitlementSnapshot | None = None, *, raises: bool = False
    ) -> None:
        self._snapshot = snapshot
        self._raises = raises

    def resolve(self, request: EntitlementRequest) -> EntitlementSnapshot:
        if self._raises:
            raise RuntimeError("entitlement host unreachable")
        assert self._snapshot is not None
        return self._snapshot


def _active(business_id: str, *, as_of: datetime) -> EntitlementSnapshot:
    return EntitlementSnapshot(
        business_id=business_id,
        status=EntitlementStatus.ACTIVE,
        plan="synthetic-growth",
        capabilities=("discovery",),
        limits={"monthly_discovery_runs": 100},
        expires_at=FUTURE,
        as_of=as_of,
        version="fake-entitlements-v1",
        synthetic=True,
    )


def _none(business_id: str, *, as_of: datetime) -> EntitlementSnapshot:
    return EntitlementSnapshot(
        business_id=business_id,
        status=EntitlementStatus.NONE,
        as_of=as_of,
        version="fake-entitlements-v1",
        synthetic=True,
    )


def _settings():
    return make_settings(entitlement_enforcement_enabled=True)


# --- gate unit: caching, expiry, revocation, outage ----------------------------------------------


def test_fresh_active_snapshot_authorizes_and_is_cached(session):
    client = _StubClient(_active(BUSINESS_A, as_of=T0))
    snap = ensure_entitled(session, _settings(), client, BUSINESS_A, "discovery", now=T0)
    assert snap.status is EntitlementStatus.ACTIVE
    row = session.query(EntitlementProjection).filter_by(business_id=BUSINESS_A).one()
    assert row.status == "active"  # a permitted projection was cached


def test_capability_outside_plan_is_refused(session):
    client = _StubClient(_active(BUSINESS_A, as_of=T0))
    with pytest.raises(EntitlementGateError) as exc:
        ensure_entitled(session, _settings(), client, BUSINESS_A, "contact_research", now=T0)
    assert exc.value.reason == "capability_not_included"


def test_outage_with_fresh_cache_still_authorizes(session):
    # 1) fresh active resolve caches a projection...
    ensure_entitled(
        session,
        _settings(),
        _StubClient(_active(BUSINESS_A, as_of=T0)),
        BUSINESS_A,
        "discovery",
        now=T0,
    )
    # 2) host outage 1h later: the cached projection is still fresh => allowed.
    outage = _StubClient(raises=True)
    snap = ensure_entitled(
        session, _settings(), outage, BUSINESS_A, "discovery", now=T0 + timedelta(hours=1)
    )
    assert snap.status is EntitlementStatus.ACTIVE


def test_outage_with_stale_cache_fails_closed(session):
    ensure_entitled(
        session,
        _settings(),
        _StubClient(_active(BUSINESS_A, as_of=T0)),
        BUSINESS_A,
        "discovery",
        now=T0,
    )
    outage = _StubClient(raises=True)
    # 25h later (> 24h max age): the cached projection is stale => a prior-active subscription can
    # NOT keep authorizing indefinitely during the outage.
    with pytest.raises(EntitlementGateError) as exc:
        ensure_entitled(
            session, _settings(), outage, BUSINESS_A, "discovery", now=T0 + timedelta(hours=25)
        )
    assert exc.value.reason == "stale"


def test_outage_with_no_cache_fails_closed(session):
    outage = _StubClient(raises=True)
    with pytest.raises(EntitlementGateError) as exc:
        ensure_entitled(session, _settings(), outage, BUSINESS_A, "discovery", now=T0)
    assert exc.value.reason == "no_entitlement"


def test_revocation_clears_cache_so_later_outage_fails_closed(session):
    # 1) active -> cached
    ensure_entitled(
        session,
        _settings(),
        _StubClient(_active(BUSINESS_A, as_of=T0)),
        BUSINESS_A,
        "discovery",
        now=T0,
    )
    assert session.query(EntitlementProjection).filter_by(business_id=BUSINESS_A).count() == 1
    # 2) a live resolve now reports NONE (revoked) -> denied AND the cache is cleared
    with pytest.raises(EntitlementGateError) as exc:
        ensure_entitled(
            session,
            _settings(),
            _StubClient(_none(BUSINESS_A, as_of=T0 + timedelta(hours=1))),
            BUSINESS_A,
            "discovery",
            now=T0 + timedelta(hours=1),
        )
    assert exc.value.reason == "no_entitlement"
    assert session.query(EntitlementProjection).filter_by(business_id=BUSINESS_A).count() == 0
    # 3) a subsequent outage has no cache to fall back on -> still fails closed
    with pytest.raises(EntitlementGateError):
        ensure_entitled(
            session,
            _settings(),
            _StubClient(raises=True),
            BUSINESS_A,
            "discovery",
            now=T0 + timedelta(hours=2),
        )


def test_disabled_contract_fails_closed(session):
    with pytest.raises(EntitlementGateError) as exc:
        ensure_entitled(session, _settings(), None, BUSINESS_A, "discovery", now=T0)
    assert exc.value.reason == "no_entitlement"


# --- route: enforcement applied to the chargeable discovery run ----------------------------------


def _setup_tenant(client: TestClient, business_id: str) -> None:
    client.post("/v1/tenants", json={"businessId": business_id}, headers=AUTH)
    client.post(f"/v1/tenants/{business_id}/enable", headers=AUTH)
    client.put(
        f"/v1/tenants/{business_id}/icp",
        json={"industries": [], "regions": [], "roles": []},
        headers=AUTH,
    )


def test_route_allows_work_for_an_actively_entitled_business():
    # Feature on (conftest) + enforcement on; BUSINESS_A has an active synthetic entitlement.
    app = create_app(make_settings(entitlement_enforcement_enabled=True))
    with TestClient(app) as client:
        _setup_tenant(client, BUSINESS_A)
        resp = client.post(f"/v1/tenants/{BUSINESS_A}/discovery/run", headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"


def test_route_blocks_work_for_an_expired_entitlement():
    # BUSINESS_B's synthetic entitlement is expired => the chargeable run is refused (403), no run.
    app = create_app(make_settings(entitlement_enforcement_enabled=True))
    with TestClient(app) as client:
        _setup_tenant(client, BUSINESS_B)
        resp = client.post(f"/v1/tenants/{BUSINESS_B}/discovery/run", headers=AUTH)
        assert resp.status_code == 403
        assert resp.json()["error"]["message"] == "entitlement_expired"
        assert client.get(f"/v1/tenants/{BUSINESS_B}/runs", headers=AUTH).json() == []
