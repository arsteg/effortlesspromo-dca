"""DCA-140 — create a bounded discovery run (the orchestration envelope).

Two layers, matching the DCA-135 / DCA-139 suites:

- **Service unit tests** (in-memory SQLite) pin the creation logic: a bounded run is CREATED in the
  not-yet-executing PENDING state, grounded in the confirmed ICP and bounded to the approved+entitled
  source scope within the (clamped) scope / budget caps; over-scope and over-budget requests are clamped
  (recorded) rather than widened; an unconfirmed profile, a disabled feature, an out-of-country / not-
  enabled tenant, a failed entitlement check and a duplicate active scope each refuse and stage NOTHING;
  creating a run executes nothing (no job, no candidate, no send, no spend); and everything is
  tenant-scoped and idempotent.
- **HTTP tests** (``TestClient``) prove the same over the real service API, plus the fail-closed 403/409.

No AI, no network, no model, no spend — a run is only a durable, bounded record of intent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.db.base import Base, make_engine, make_session_factory
from app.db.models import (
    BudgetLimit,
    BusinessSourceEntitlement,
    DiscoveryRunRequest,
    DiscoveryRunStatus,
    DurableJobRequest,
    IcpConfirmation,
    IcpDraft,
    ProspectCandidate,
    Source,
    SourceAccessMethod,
    SourceApprovalStatus,
    SourceKind,
    SourceStatus,
    SourceTerms,
    SourceTermsApproval,
    SourceTermsStatus,
    TenantAgent,
)
from app.entitlements import EntitlementRequest, EntitlementSnapshot, EntitlementStatus
from app.main import create_app
from app.services.discovery_planner import PLAN_PURPOSE
from app.services.discovery_run import (
    CLAMP_BUDGET,
    CLAMP_TARGET_CAP,
    MAX_QUERY_CAP,
    MAX_TARGET_CAP,
    MAX_TASK_CAP,
    REASON_DUPLICATE_ACTIVE_RUN,
    REASON_FEATURE_DISABLED,
    REASON_NO_CONFIRMED_ICP,
    REASON_TENANT_NOT_ENABLED,
    DiscoveryRunStore,
    create_discovery_run,
)
from tests.conftest import AUTH, make_settings

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"

ALLOWED_KEY = "mca_master_data"
ALLOWED_FIELDS = ["org_name", "org_identifier"]


# --- seed helpers ---------------------------------------------------------------------------------


def _seed_tenant(session, business_id: str, *, enabled: bool = True, country: str = "IN") -> None:
    session.add(TenantAgent(business_id=business_id, country=country, enabled=enabled))


def _seed_confirmed_icp(session, business_id: str, *, version: int = 1) -> None:
    """A confirmed ICP version for the business (the targeting the run is grounded in)."""
    session.add(
        IcpDraft(
            business_id=business_id,
            version=version,
            industries=["information technology"],
            size_bands=["small", "medium"],
            roles=["owner"],
            geography=["KA", "MH"],
            status="draft",
        )
    )
    session.add(
        IcpConfirmation(
            business_id=business_id, version=version, actor_id="seed", actor_type="user"
        )
    )


def _seed_budget(session, business_id: str, *, per_run: int, daily: int, monthly: int) -> None:
    session.add(
        BudgetLimit(
            business_id=business_id,
            version=1,
            currency="INR",
            daily_cap_minor=daily,
            monthly_cap_minor=monthly,
            per_run_cap_minor=per_run,
            timezone="Asia/Kolkata",
            actor_id="seed",
            actor_type="user",
        )
    )


def _seed_entitled_source(session, business_id: str, key: str, fields: list[str]) -> None:
    """Seed the full rights chain so ``key`` is approved+entitled for COLLECT on ``fields`` (DCA-048)."""
    now = datetime.now(UTC)
    session.add(
        Source(
            key=key,
            name=key,
            kind=SourceKind.OFFICIAL.value,
            trust_tier="official_registry",
            capabilities=["org_discovery"],
            fields=list(fields),
            access_method=SourceAccessMethod.API.value,
            india_coverage={"nationwide": True, "states": []},
            status=SourceStatus.ENABLED.value,
            enabled=True,
            permissions=[],
        )
    )
    session.add(
        SourceTerms(
            source_key=key,
            version=1,
            operation_rights={"collect": True},
            field_rights={},
            purposes=[PLAN_PURPOSE],
            contract_evidence={"reference": "lic-1", "kind": "licence", "uri": ""},
            status=SourceTermsStatus.ACTIVE.value,
        )
    )
    session.add(
        SourceTermsApproval(
            source_key=key,
            terms_version=1,
            reviewer_id="op-1",
            reviewer_type="platform_operator",
            review_evidence={"reference": "rev-1", "kind": "review", "uri": ""},
            reviewed_at=now - timedelta(days=1),
            effective_from=now - timedelta(days=1),
            expires_at=now + timedelta(days=365),
            approved_operation_rights={"collect": True},
            approved_field_rights={},
            status=SourceApprovalStatus.ACTIVE.value,
        )
    )
    session.add(
        BusinessSourceEntitlement(
            business_id=business_id,
            source_key=key,
            version=1,
            enabled=True,
            operation_rights={"collect": True},
            field_rights={},
            purposes=[PLAN_PURPOSE],
            approved_terms_version=1,
            actor_id="seed",
            actor_type="user",
        )
    )


class _StubEntitlementClient:
    """Returns a fixed entitlement snapshot (the DCA-023 contract seam)."""

    def __init__(self, snapshot: EntitlementSnapshot) -> None:
        self._snapshot = snapshot

    def resolve(self, request: EntitlementRequest) -> EntitlementSnapshot:
        return self._snapshot


def _snapshot(
    business_id: str, status: EntitlementStatus, *, as_of: datetime
) -> EntitlementSnapshot:
    return EntitlementSnapshot(
        business_id=business_id,
        status=status,
        plan="synthetic-growth",
        capabilities=("discovery",),
        limits={},
        expires_at=datetime(2999, 1, 1, tzinfo=UTC),
        as_of=as_of,
        version="fake-entitlements-v1",
        synthetic=True,
    )


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


# --- a bounded run created in PENDING, nothing executed -------------------------------------------


def test_create_bounded_run_in_pending_state(session_factory):
    settings = make_settings()
    with session_factory() as session:
        _seed_tenant(session, BIZ_A)
        _seed_confirmed_icp(session, BIZ_A)
        session.flush()

        result = create_discovery_run(session, settings, BIZ_A)
        session.flush()

        assert result.run is not None
        assert result.refused_reason is None
        run = result.run
        assert run.status == DiscoveryRunStatus.PENDING.value
        assert run.status in DiscoveryRunStatus.active_values()
        assert run.icp_version == 1
        assert run.clamped is False and run.clamp_notes == []
        # A run is CREATED only — nothing was executed, sent or spent.
        assert run.provenance["executed"] is False
        assert run.provenance["sent"] is False
        assert run.provenance["spent"] is False
        assert run.provenance["autoApproved"] is False
        assert session.query(DiscoveryRunRequest).count() == 1
        assert session.query(ProspectCandidate).count() == 0
        assert session.query(DurableJobRequest).count() == 0


def test_source_scope_lists_only_entitled_sources(session_factory):
    settings = make_settings()
    with session_factory() as session:
        _seed_tenant(session, BIZ_A)
        _seed_confirmed_icp(session, BIZ_A)
        _seed_entitled_source(session, BIZ_A, ALLOWED_KEY, ALLOWED_FIELDS)
        session.flush()

        result = create_discovery_run(session, settings, BIZ_A)
        assert result.run is not None
        scope = {s["sourceKey"]: set(s["permittedFields"]) for s in result.run.source_scope}
        assert scope == {ALLOWED_KEY: set(ALLOWED_FIELDS)}


# --- over-scope / over-budget are clamped, never widened ------------------------------------------


def test_over_scope_caps_are_clamped(session_factory):
    settings = make_settings()
    with session_factory() as session:
        _seed_tenant(session, BIZ_A)
        _seed_confirmed_icp(session, BIZ_A)
        session.flush()

        result = create_discovery_run(
            session,
            settings,
            BIZ_A,
            target_cap=999_999,
            task_cap=999,
            query_cap=999,
        )
        run = result.run
        assert run is not None
        assert run.target_cap == MAX_TARGET_CAP
        assert run.task_cap == MAX_TASK_CAP
        assert run.query_cap == MAX_QUERY_CAP
        assert run.clamped is True
        reasons = {n["reason"] for n in run.clamp_notes}
        assert CLAMP_TARGET_CAP in reasons
        # the honest clamp record keeps both the requested and the effective value
        target_note = next(n for n in run.clamp_notes if n["reason"] == CLAMP_TARGET_CAP)
        assert target_note["requested"] == 999_999
        assert target_note["effective"] == MAX_TARGET_CAP


def test_over_budget_is_clamped_to_per_run_cap(session_factory):
    settings = make_settings()
    with session_factory() as session:
        _seed_tenant(session, BIZ_A)
        _seed_confirmed_icp(session, BIZ_A)
        _seed_budget(session, BIZ_A, per_run=50_000, daily=100_000, monthly=1_000_000)
        session.flush()

        result = create_discovery_run(session, settings, BIZ_A, requested_budget_minor=999_999)
        run = result.run
        assert run is not None
        assert run.per_run_budget_minor == 50_000  # clamped down to the business's per-run cap
        assert run.budget_limit_version == 1
        assert run.clamped is True
        budget_note = next(n for n in run.clamp_notes if n["reason"] == CLAMP_BUDGET)
        assert budget_note["requested"] == 999_999
        assert budget_note["effective"] == 50_000


# --- refusals (fail-closed, no state change) ------------------------------------------------------


def test_unconfirmed_profile_cannot_start_a_run(session_factory):
    settings = make_settings()
    with session_factory() as session:
        _seed_tenant(session, BIZ_A)  # enabled, but NO confirmed ICP
        session.flush()

        result = create_discovery_run(session, settings, BIZ_A)
        assert result.run is None
        assert result.refused_reason == REASON_NO_CONFIRMED_ICP
        assert session.query(DiscoveryRunRequest).count() == 0


def test_duplicate_active_scope_refused(session_factory):
    settings = make_settings()
    with session_factory() as session:
        _seed_tenant(session, BIZ_A)
        _seed_confirmed_icp(session, BIZ_A)
        session.flush()

        first = create_discovery_run(session, settings, BIZ_A)
        session.flush()
        assert first.run is not None

        # A second run for the SAME confirmed-ICP scope while the first is still active is redundant.
        second = create_discovery_run(session, settings, BIZ_A)
        assert second.run is None
        assert second.refused_reason == REASON_DUPLICATE_ACTIVE_RUN
        assert session.query(DiscoveryRunRequest).count() == 1

        # A terminal run frees the scope: a fresh run can then be created.
        first.run.status = DiscoveryRunStatus.COMPLETED.value
        session.flush()
        third = create_discovery_run(session, settings, BIZ_A)
        session.flush()
        assert third.run is not None
        assert session.query(DiscoveryRunRequest).count() == 2


def test_feature_disabled_refused(session_factory):
    settings = make_settings(feature_enabled=False)
    with session_factory() as session:
        _seed_tenant(session, BIZ_A)
        _seed_confirmed_icp(session, BIZ_A)
        session.flush()

        result = create_discovery_run(session, settings, BIZ_A)
        assert result.run is None
        assert result.refused_reason == REASON_FEATURE_DISABLED
        assert session.query(DiscoveryRunRequest).count() == 0


def test_not_enabled_tenant_refused(session_factory):
    settings = make_settings()
    with session_factory() as session:
        _seed_tenant(session, BIZ_A, enabled=False)
        _seed_confirmed_icp(session, BIZ_A)
        session.flush()

        result = create_discovery_run(session, settings, BIZ_A)
        assert result.run is None
        assert result.refused_reason == REASON_TENANT_NOT_ENABLED
        assert session.query(DiscoveryRunRequest).count() == 0


def test_unentitled_refused_when_enforcement_on(session_factory):
    settings = make_settings(entitlement_enforcement_enabled=True)
    now = datetime.now(UTC)
    client = _StubEntitlementClient(_snapshot(BIZ_A, EntitlementStatus.NONE, as_of=now))
    with session_factory() as session:
        _seed_tenant(session, BIZ_A)
        _seed_confirmed_icp(session, BIZ_A)
        session.commit()

        result = create_discovery_run(session, settings, BIZ_A, entitlement_client=client, now=now)
        assert result.run is None
        assert result.refused_reason is not None
        assert result.refused_reason.startswith("entitlement_")
        assert session.query(DiscoveryRunRequest).count() == 0


def test_entitled_allows_run_when_enforcement_on(session_factory):
    settings = make_settings(entitlement_enforcement_enabled=True)
    now = datetime.now(UTC)
    client = _StubEntitlementClient(_snapshot(BIZ_A, EntitlementStatus.ACTIVE, as_of=now))
    with session_factory() as session:
        _seed_tenant(session, BIZ_A)
        _seed_confirmed_icp(session, BIZ_A)
        session.commit()

        result = create_discovery_run(session, settings, BIZ_A, entitlement_client=client, now=now)
        session.flush()
        assert result.run is not None
        assert result.run.status == DiscoveryRunStatus.PENDING.value


# --- tenant isolation -----------------------------------------------------------------------------


def test_tenant_scoped(session_factory):
    settings = make_settings()
    with session_factory() as session:
        _seed_tenant(session, BIZ_A)
        _seed_confirmed_icp(session, BIZ_A)
        _seed_tenant(session, BIZ_B)
        _seed_confirmed_icp(session, BIZ_B)
        session.flush()

        create_discovery_run(session, settings, BIZ_A)
        session.flush()

        assert len(DiscoveryRunStore(session, BIZ_A).runs()) == 1
        assert len(DiscoveryRunStore(session, BIZ_B).runs()) == 0


# --- HTTP surface ---------------------------------------------------------------------------------


def _http_app(*, seed_icp: bool = True, feature: bool = True):
    settings = make_settings(feature_enabled=feature)
    app = create_app(settings)
    client = TestClient(app, raise_server_exceptions=False)
    client.post("/v1/tenants", json={"businessId": BIZ_A}, headers=AUTH)
    client.post(f"/v1/tenants/{BIZ_A}/enable", headers=AUTH)
    with app.state.session_factory() as session:
        if seed_icp:
            _seed_confirmed_icp(session, BIZ_A)
        session.commit()
    return app, client


def test_http_create_list_and_get_run():
    app, client = _http_app()
    try:
        resp = client.post(
            f"/v1/tenants/{BIZ_A}/discovery/runs",
            json={"maxTargets": 10, "maxQueriesPerTask": 3},
            headers=AUTH,
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["status"] == "pending"
        assert body["executed"] is False
        assert body["icpVersion"] == 1
        run_id = body["runId"]

        got = client.get(f"/v1/tenants/{BIZ_A}/discovery/runs/{run_id}", headers=AUTH)
        assert got.status_code == 200
        assert got.json()["runId"] == run_id

        listed = client.get(f"/v1/tenants/{BIZ_A}/discovery/runs", headers=AUTH).json()
        assert [r["runId"] for r in listed] == [run_id]
    finally:
        app.state.engine.dispose()


def test_http_idempotent_replay_creates_one_run():
    app, client = _http_app()
    try:
        headers = {**AUTH, "Idempotency-Key": "run-key-1"}
        first = client.post(f"/v1/tenants/{BIZ_A}/discovery/runs", json={}, headers=headers)
        assert first.status_code == 201
        second = client.post(f"/v1/tenants/{BIZ_A}/discovery/runs", json={}, headers=headers)
        assert second.status_code == 201
        assert second.json()["runId"] == first.json()["runId"]
        listed = client.get(f"/v1/tenants/{BIZ_A}/discovery/runs", headers=AUTH).json()
        assert len(listed) == 1
    finally:
        app.state.engine.dispose()


def test_http_duplicate_active_run_is_409():
    app, client = _http_app()
    try:
        assert (
            client.post(f"/v1/tenants/{BIZ_A}/discovery/runs", json={}, headers=AUTH).status_code
            == 201
        )
        dup = client.post(f"/v1/tenants/{BIZ_A}/discovery/runs", json={}, headers=AUTH)
        assert dup.status_code == 409
        assert dup.json()["error"]["message"] == REASON_DUPLICATE_ACTIVE_RUN
    finally:
        app.state.engine.dispose()


def test_http_no_confirmed_icp_is_409():
    app, client = _http_app(seed_icp=False)
    try:
        resp = client.post(f"/v1/tenants/{BIZ_A}/discovery/runs", json={}, headers=AUTH)
        assert resp.status_code == 409
        assert resp.json()["error"]["message"] == REASON_NO_CONFIRMED_ICP
    finally:
        app.state.engine.dispose()


def test_http_feature_disabled_is_403():
    app, client = _http_app(feature=False)
    try:
        resp = client.post(f"/v1/tenants/{BIZ_A}/discovery/runs", json={}, headers=AUTH)
        assert resp.status_code == 403
        assert resp.json()["error"]["message"] == REASON_FEATURE_DISABLED
    finally:
        app.state.engine.dispose()
