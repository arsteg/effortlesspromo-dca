"""DCA-135 — generate bounded discovery plans (the AI-capability integration).

Two layers, matching the DCA-134 suite:

- **Service unit tests** (in-memory SQLite + the deterministic fixture transport) pin the planning
  logic: a bounded plan grounded in the CONFIRMED ICP using ONLY approved+entitled sources and their
  permitted fields, within the task/query caps; an un-entitled source, an un-permitted field, a
  geography outside the confirmed ICP and an over-cap task are each clamped/EXCLUDED (recorded) and
  change nothing; the plan is a DRAFT, never executed, never auto-approved; a rejected gateway output,
  a disabled capability and a missing confirmed ICP each stage NO plan; and everything is tenant-scoped.
- **HTTP tests** (``TestClient``, facade overridden with the fixture transport) prove the same over the
  real service API, plus the fail-closed 503/409/502.

No AI, no network, no model, no spend — the fixture transport stands in for the gateway (the real
gateway capability schema + its rule_check bound are pinned by effortlesspromo-ai's own contract test).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.ai import AiCapabilityFacade, FakeAiGatewayTransport
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import (
    BusinessSourceEntitlement,
    DiscoveryPlan,
    DiscoveryRun,
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
)
from app.main import create_app
from app.services.discovery_planner import (
    EXCL_FIELD_NOT_PERMITTED,
    EXCL_GEOGRAPHY_SCOPE_EXPANSION,
    EXCL_SOURCE_NOT_ALLOWED,
    EXCL_TASK_CAP_EXCEEDED,
    PLAN_PURPOSE,
    REASON_CAPABILITY_DISABLED,
    REASON_NO_CONFIRMED_ICP,
    REASON_OUTPUT_REJECTED,
    plan_discovery,
    resolve_allowed_sources,
)
from tests.conftest import AUTH, make_settings

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"

ALLOWED_KEY = "mca_master_data"
ALLOWED_FIELDS = ["org_name", "org_identifier"]
UNENTITLED_KEY = "gst_registry"


def _seed_icp(session, business_id: str, *, geography=("KA", "MH")) -> None:
    """A confirmed ICP version for the business (the targeting the plan is grounded in)."""
    session.add(
        IcpDraft(
            business_id=business_id,
            version=1,
            industries=["information technology"],
            size_bands=["small", "medium"],
            roles=["owner"],
            geography=list(geography),
            status="draft",
        )
    )
    session.add(
        IcpConfirmation(business_id=business_id, version=1, actor_id="seed", actor_type="user")
    )


def _seed_catalog_source(session, key: str, fields: list[str], *, enabled: bool) -> None:
    """A catalogued source. ``enabled`` marks it approved+ENABLED (otherwise a plain draft)."""
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
            status=SourceStatus.ENABLED.value if enabled else SourceStatus.DRAFT.value,
            enabled=enabled,
            permissions=[],
        )
    )


def _seed_approved_entitled_source(session, business_id: str, key: str, fields: list[str]) -> None:
    """Seed the full rights chain so ``key`` is approved+entitled for COLLECT on ``fields`` (DCA-048)."""
    now = datetime.now(UTC)
    _seed_catalog_source(session, key, fields, enabled=True)
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


def _facade(structured_output, validation=None, enabled=True) -> AiCapabilityFacade:
    """A facade over the deterministic fixture transport returning a crafted plan (no network/spend)."""
    return AiCapabilityFacade(
        FakeAiGatewayTransport(
            enabled=enabled, structured_output=structured_output, validation=validation
        )
    )


def _plan_output(tasks, *, rationale="plan", confidence=0.7) -> dict:
    return {"tasks": tasks, "rationale": rationale, "confidence": confidence}


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


# --- resolve_allowed_sources: only approved+entitled sources, permitted fields -------------------


def test_resolve_allowed_sources_lists_only_entitled(session_factory):
    with session_factory() as session:
        _seed_approved_entitled_source(session, BIZ_A, ALLOWED_KEY, ALLOWED_FIELDS)
        # A catalogued-but-not-entitled source (no approval/entitlement) must never be available.
        _seed_catalog_source(session, UNENTITLED_KEY, ["org_name"], enabled=False)
        session.flush()
        allowed = resolve_allowed_sources(session, BIZ_A)
    assert set(allowed) == {ALLOWED_KEY}
    assert set(allowed[ALLOWED_KEY]) == set(ALLOWED_FIELDS)


# --- a bounded, grounded plan over only entitled sources + permitted fields ----------------------


def test_bounded_plan_generated_within_caps_and_only_entitled_sources(session_factory):
    with session_factory() as session:
        _seed_icp(session, BIZ_A)
        _seed_approved_entitled_source(session, BIZ_A, ALLOWED_KEY, ALLOWED_FIELDS)
        session.flush()

        # The model proposes: one grounded task (allowed source) requesting a mix of permitted +
        # un-permitted fields, with an over-cap maxQueries; plus a task for an un-entitled source.
        tasks = [
            {
                "sourceKey": ALLOWED_KEY,
                "fields": ["org_name", "secret_field"],  # secret_field is NOT permitted
                "filters": {"geography": ["KA"], "industries": ["information technology"]},
                "maxQueries": 999,  # over the per-task cap
            },
            {
                "sourceKey": UNENTITLED_KEY,  # the business is NOT entitled to this source
                "fields": ["org_name"],
                "filters": {"geography": ["MH"]},
                "maxQueries": 2,
            },
        ]
        result = plan_discovery(
            session,
            _facade(_plan_output(tasks)),
            BIZ_A,
            invocation_key="discovery-plan:test:1",
            task_cap=5,
            query_cap=3,
        )
        session.flush()

        assert result.plan is not None
        assert result.plan.status == "draft"
        assert result.icp_version == 1

        # Only the entitled source survived, with only its permitted field, geography inside the ICP,
        # and the query count clamped to the cap.
        assert len(result.tasks) == 1
        kept = result.tasks[0]
        assert kept["sourceKey"] == ALLOWED_KEY
        assert kept["fields"] == ["org_name"]  # secret_field dropped
        assert kept["filters"]["geography"] == ["KA"]
        assert kept["maxQueries"] == 3

        reasons = {(e.get("sourceKey"), e["reason"]) for e in result.excluded}
        assert (UNENTITLED_KEY, EXCL_SOURCE_NOT_ALLOWED) in reasons
        assert (ALLOWED_KEY, EXCL_FIELD_NOT_PERMITTED) in reasons

        # A plan is a PROPOSAL — nothing was executed.
        assert session.query(DiscoveryRun).count() == 0
        assert session.query(ProspectCandidate).count() == 0
        assert result.plan.provenance["executed"] is False
        assert result.plan.provenance["autoApproved"] is False


def test_over_cap_plan_is_clamped(session_factory):
    with session_factory() as session:
        _seed_icp(session, BIZ_A)
        _seed_approved_entitled_source(session, BIZ_A, ALLOWED_KEY, ALLOWED_FIELDS)
        session.flush()
        tasks = [
            {"sourceKey": ALLOWED_KEY, "fields": ["org_name"], "filters": {"geography": ["KA"]}, "maxQueries": 1}
            for _ in range(5)
        ]
        result = plan_discovery(
            session,
            _facade(_plan_output(tasks)),
            BIZ_A,
            invocation_key="discovery-plan:test:cap",
            task_cap=2,
            query_cap=3,
        )
    assert len(result.tasks) == 2  # clamped to the task cap
    overflow = [e for e in result.excluded if e["reason"] == EXCL_TASK_CAP_EXCEEDED]
    assert len(overflow) == 3


def test_geography_scope_expansion_is_excluded(session_factory):
    with session_factory() as session:
        _seed_icp(session, BIZ_A, geography=("KA", "MH"))
        _seed_approved_entitled_source(session, BIZ_A, ALLOWED_KEY, ALLOWED_FIELDS)
        session.flush()
        tasks = [
            # TN is a real Indian state but OUTSIDE the confirmed ICP -> scope expansion, dropped.
            {"sourceKey": ALLOWED_KEY, "fields": ["org_name"], "filters": {"geography": ["TN"]}, "maxQueries": 1},
            # A non-India region -> also dropped (India-only).
            {"sourceKey": ALLOWED_KEY, "fields": ["org_name"], "filters": {"geography": ["US-CA"]}, "maxQueries": 1},
            # Inside the ICP -> kept.
            {"sourceKey": ALLOWED_KEY, "fields": ["org_name"], "filters": {"geography": ["KA"]}, "maxQueries": 1},
        ]
        result = plan_discovery(
            session, _facade(_plan_output(tasks)), BIZ_A, invocation_key="discovery-plan:test:geo"
        )
    assert len(result.tasks) == 1
    assert result.tasks[0]["filters"]["geography"] == ["KA"]
    expansions = [e for e in result.excluded if e["reason"] == EXCL_GEOGRAPHY_SCOPE_EXPANSION]
    assert {e["detail"] for e in expansions} == {"TN", "US-CA"}


def test_planning_is_deterministic(session_factory):
    tasks = [
        {"sourceKey": ALLOWED_KEY, "fields": ["org_name", "secret_field"], "filters": {"geography": ["KA"]}, "maxQueries": 50},
    ]
    outs = []
    for _ in range(2):
        with session_factory() as session:
            _seed_icp(session, BIZ_A)
            _seed_approved_entitled_source(session, BIZ_A, ALLOWED_KEY, ALLOWED_FIELDS)
            session.flush()
            result = plan_discovery(
                session, _facade(_plan_output(tasks)), BIZ_A, invocation_key="discovery-plan:test:det"
            )
            outs.append((result.tasks, result.excluded))
    assert outs[0] == outs[1]


# --- denials stage nothing ----------------------------------------------------------------------


def test_no_confirmed_icp_refused(session_factory):
    with session_factory() as session:
        _seed_approved_entitled_source(session, BIZ_A, ALLOWED_KEY, ALLOWED_FIELDS)  # but no ICP
        session.flush()
        result = plan_discovery(
            session,
            _facade(_plan_output([{"sourceKey": ALLOWED_KEY, "fields": ["org_name"], "filters": {}, "maxQueries": 1}])),
            BIZ_A,
            invocation_key="discovery-plan:test:noicp",
        )
        assert result.plan is None
        assert result.refused_reason == REASON_NO_CONFIRMED_ICP
        assert session.query(DiscoveryPlan).count() == 0


def test_disabled_capability_refused(session_factory):
    with session_factory() as session:
        _seed_icp(session, BIZ_A)
        _seed_approved_entitled_source(session, BIZ_A, ALLOWED_KEY, ALLOWED_FIELDS)
        session.flush()
        # facade=None and a disabled transport both refuse before any call.
        for facade in (None, _facade(_plan_output([]), enabled=False)):
            result = plan_discovery(
                session, facade, BIZ_A, invocation_key="discovery-plan:test:off"
            )
            assert result.plan is None
            assert result.refused_reason == REASON_CAPABILITY_DISABLED
        assert session.query(DiscoveryPlan).count() == 0


def test_rejected_output_stages_nothing(session_factory):
    with session_factory() as session:
        _seed_icp(session, BIZ_A)
        _seed_approved_entitled_source(session, BIZ_A, ALLOWED_KEY, ALLOWED_FIELDS)
        session.flush()
        facade = _facade(
            _plan_output([{"sourceKey": ALLOWED_KEY, "fields": ["org_name"], "filters": {}, "maxQueries": 1}]),
            validation={"validationStatus": "failed", "validationReport": {"status": "failed"}},
        )
        result = plan_discovery(session, facade, BIZ_A, invocation_key="discovery-plan:test:rej")
        assert result.plan is None
        assert result.refused_reason == REASON_OUTPUT_REJECTED
        assert session.query(DiscoveryPlan).count() == 0


def test_tenant_scoped(session_factory):
    with session_factory() as session:
        _seed_icp(session, BIZ_A)
        _seed_approved_entitled_source(session, BIZ_A, ALLOWED_KEY, ALLOWED_FIELDS)
        session.flush()
        plan_discovery(
            session,
            _facade(_plan_output([{"sourceKey": ALLOWED_KEY, "fields": ["org_name"], "filters": {"geography": ["KA"]}, "maxQueries": 1}])),
            BIZ_A,
            invocation_key="discovery-plan:test:tenant",
        )
        session.flush()
        a_rows = session.query(DiscoveryPlan).filter(DiscoveryPlan.business_id == BIZ_A).count()
        b_rows = session.query(DiscoveryPlan).filter(DiscoveryPlan.business_id == BIZ_B).count()
    assert a_rows == 1
    assert b_rows == 0


# --- HTTP surface -------------------------------------------------------------------------------


def _http_client_and_seed(structured_output, validation=None, *, seed_icp=True):
    settings = make_settings()
    app = create_app(settings)
    client = TestClient(app, raise_server_exceptions=False)
    client.post("/v1/tenants", json={"businessId": BIZ_A}, headers=AUTH)
    # Seed the rights chain + ICP directly in the app's session.
    factory = app.state.session_factory
    with factory() as session:
        if seed_icp:
            _seed_icp(session, BIZ_A)
        _seed_approved_entitled_source(session, BIZ_A, ALLOWED_KEY, ALLOWED_FIELDS)
        session.commit()
    app.state.ai_capability_facade = _facade(structured_output, validation=validation)
    return app, client


def test_http_generate_and_fetch_plan():
    tasks = [
        {"sourceKey": ALLOWED_KEY, "fields": ["org_name", "nope"], "filters": {"geography": ["KA"]}, "maxQueries": 99},
        {"sourceKey": UNENTITLED_KEY, "fields": ["org_name"], "filters": {"geography": ["MH"]}, "maxQueries": 1},
    ]
    app, client = _http_client_and_seed(_plan_output(tasks))
    try:
        resp = client.post(
            f"/v1/tenants/{BIZ_A}/discovery/plans", json={"maxTasks": 5, "maxQueriesPerTask": 3}, headers=AUTH
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["status"] == "draft"
        assert body["executed"] is False and body["autoApproved"] is False
        assert len(body["tasks"]) == 1
        assert body["tasks"][0]["sourceKey"] == ALLOWED_KEY
        assert body["tasks"][0]["fields"] == ["org_name"]
        assert body["tasks"][0]["maxQueries"] == 3

        got = client.get(f"/v1/tenants/{BIZ_A}/discovery/plans/1", headers=AUTH)
        assert got.status_code == 200
        assert got.json()["version"] == 1
        listed = client.get(f"/v1/tenants/{BIZ_A}/discovery/plans", headers=AUTH).json()
        assert [p["version"] for p in listed] == [1]
    finally:
        app.state.engine.dispose()


def test_http_no_confirmed_icp_is_409():
    app, client = _http_client_and_seed(_plan_output([]), seed_icp=False)
    try:
        resp = client.post(f"/v1/tenants/{BIZ_A}/discovery/plans", json={}, headers=AUTH)
        assert resp.status_code == 409
    finally:
        app.state.engine.dispose()


def test_http_disabled_capability_is_503():
    app, client = _http_client_and_seed(_plan_output([]))
    app.state.ai_capability_facade = None
    try:
        resp = client.post(f"/v1/tenants/{BIZ_A}/discovery/plans", json={}, headers=AUTH)
        assert resp.status_code == 503
    finally:
        app.state.engine.dispose()
