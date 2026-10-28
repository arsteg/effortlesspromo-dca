"""DCA-063 — filter fields by display entitlement: the record & evidence API.

Two layers, matching the DCA-047/062 suites:

- **Always-on route tests** (in-memory SQLite) drive the real HTTP contract against the real DCA-047
  ``permits`` read. Two isolated synthetic businesses observe the SAME source-licensed field; A holds
  a DISPLAY entitlement for it and B does not. Fetching each business's record and comparing the RAW
  JSON proves the headline: A's payload CARRIES the licensed value, B's payload OMITS it (the value is
  absent, not nulled) and marks it restricted. Each business's private overlay (its own un-sourced
  field) is visible to it regardless. Evidence raw payloads are gated the same way. Tenant isolation
  is enforced (B cannot read A's subject or evidence — 404). The routes are read-only, so a result and
  its denial reproduce with no state change.
- **The same A-vs-B projection on a disposable PostgreSQL** (``DCA_TEST_DATABASE_URL``) exercises the
  real transaction / RLS engine rather than SQLite; it skips when no URL is supplied.

Spin up a throwaway Postgres, e.g.:
    docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55490:5432 postgres:16-alpine
    export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55490/dca"
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.audit.sink import ActorType, AuditActor
from app.db.base import Base, make_engine, make_session_factory
from app.main import create_app
from app.services.field_observation import FieldObservationInput, FieldObservationStore
from app.services.org_observation import ObservationInput, OrgObservationStore
from app.services.source_approval import (
    ReviewerPrincipal,
    SourceApprovalReview,
    SourceApprovalStore,
)
from app.services.source_catalog import SourceCatalogStore, SourceDraftInput
from app.services.source_entitlement import (
    BusinessEntitler,
    BusinessSourceEntitlementInput,
    BusinessSourceEntitlementStore,
)
from app.services.source_terms import SourceTermsInput, SourceTermsStore
from tests.conftest import AUTH, make_settings

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"
SOURCE_KEY = "mca_master_data"
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
ACTOR = AuditActor(id="00000000-0000-4000-9000-0000000a0063", type=ActorType.USER)
ADMIN = BusinessEntitler(subject="00000000-0000-4000-9000-0000000a0001", roles=("owner", "admin"))
OPERATOR = ReviewerPrincipal(subject="op-001", roles=("platform_operator",))

REVENUE = "120000000"
SEGMENT = "mid-market textiles"  # a private overlay the business inferred itself


# --------------------------------------------------------------------------------------------------
# Seed helpers (via the sanctioned stores — there is no write route for observations/evidence)
# --------------------------------------------------------------------------------------------------


def _seed_shared_source(session) -> None:
    """Catalogue a source, record a terms version that documents DISPLAY, and approve it in-force."""
    SourceCatalogStore(session).create_draft(
        SourceDraftInput(
            key=SOURCE_KEY,
            kind="official",
            access_method="api",
            name="Synthetic MCA",
            india_coverage={"nationwide": True},
        )
    )
    SourceTermsStore(session).add_version(
        SourceTermsInput(
            source_key=SOURCE_KEY,
            operations={"collect": True, "store": True, "display": True},
            purposes=["prospecting"],
            contract_evidence={"reference": "MCA-TOS-2026", "kind": "terms_of_use"},
        )
    )
    SourceApprovalStore(session).approve(
        SOURCE_KEY,
        1,
        SourceApprovalReview(
            reviewer_id="op-001",
            evidence={"reference": "RIGHTS-REVIEW-2026", "kind": "legal_review"},
            reviewed_at=NOW - timedelta(days=1),
            expires_at=NOW + timedelta(days=365),
        ),
        OPERATOR,
    )


def _entitle_display(session, business_id: str, *, display: bool) -> None:
    """Record the business's DCA-047 entitlement: display granted (or not) source-wide for the source."""
    BusinessSourceEntitlementStore(session, business_id).set_entitlement(
        BusinessSourceEntitlementInput(
            source_key=SOURCE_KEY,
            enabled=True,
            operations={"display": display},
            purposes=["prospecting"],
        ),
        ADMIN,
        now=NOW,
    )


def _record_org_with_fields(session, business_id: str) -> str:
    """Observe one org in this business: a licensed shared fact (revenue, from the source) + a private
    overlay (segment, inferred by the business itself, no source). Returns the org observation id."""
    org = OrgObservationStore(session, business_id).create(
        ObservationInput(entity_type="legal_entity", display_name="Synthetic Textiles Pvt Ltd"),
        actor=ACTOR,
    )
    fields = FieldObservationStore(session, business_id)
    fields.record(
        FieldObservationInput(
            subject_type="organization",
            subject_id=org.id,
            field_key="revenue_inr",
            field_value=REVENUE,
            inference_label="observed",
            source_key=SOURCE_KEY,
            source_version=1,
            provenance_ref="synthetic://mca/record/42",
            observed_at=NOW,
            scope="nationwide",
            rights_basis="source_terms:display;purpose=prospecting",
        ),
        actor=ACTOR,
    )
    fields.record(
        FieldObservationInput(
            subject_type="organization",
            subject_id=org.id,
            field_key="segment",
            field_value=SEGMENT,
            inference_label="inferred",  # the business's own derivation — a private overlay
            provenance_ref="derived:size+industry",
            observed_at=NOW,
            scope="nationwide",
            rights_basis="own_analysis",
        ),
        actor=ACTOR,
    )
    return org.id


def _register_tenant(client: TestClient, business_id: str) -> None:
    assert (
        client.post("/v1/tenants", json={"businessId": business_id}, headers=AUTH).status_code
        == 201
    )


def _field(payload: dict, key: str) -> dict:
    return next(f for f in payload["fields"] if f["fieldKey"] == key)


# --------------------------------------------------------------------------------------------------
# Always-on route tests (in-memory SQLite)
# --------------------------------------------------------------------------------------------------


@pytest.fixture
def seeded_client():
    """A client whose DB has the shared source approved, A display-entitled, B not, each observing the
    same licensed field + its own private overlay. Returns (client, a_org_id, b_org_id)."""
    app = create_app(make_settings())
    with TestClient(app) as client:
        _register_tenant(client, BIZ_A)
        _register_tenant(client, BIZ_B)
        factory = app.state.session_factory
        with factory() as session:
            _seed_shared_source(session)
            _entitle_display(session, BIZ_A, display=True)
            _entitle_display(session, BIZ_B, display=False)
            a_org = _record_org_with_fields(session, BIZ_A)
            b_org = _record_org_with_fields(session, BIZ_B)
            session.commit()
        yield client, a_org, b_org


def test_display_entitled_business_sees_the_licensed_value(seeded_client):
    client, a_org, _ = seeded_client
    resp = client.get(f"/v1/tenants/{BIZ_A}/records/organization/{a_org}", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    revenue = _field(body, "revenue_inr")
    # A holds the DISPLAY entitlement -> the licensed value IS in the payload.
    assert revenue["visible"] is True
    assert revenue["overlay"] is False
    assert revenue["value"] == REVENUE
    assert "restricted" not in revenue
    assert body["visibleFieldCount"] == 2
    assert body["restrictedFieldCount"] == 0


def test_unentitled_business_the_value_is_absent_from_the_payload(seeded_client):
    client, _, b_org = seeded_client
    resp = client.get(f"/v1/tenants/{BIZ_B}/records/organization/{b_org}", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    revenue = _field(body, "revenue_inr")
    # B has NO display entitlement -> the value is ABSENT from the payload, not merely hidden.
    assert revenue["visible"] is False
    assert "value" not in revenue
    assert revenue["restricted"] is True
    # B's entitlement is enabled but withholds the 'display' right -> operation-level denial.
    assert revenue["restrictedReason"] == "operation_not_entitled"
    # The restricted value must not leak anywhere in the raw response body.
    assert REVENUE not in resp.text
    assert body["restrictedFieldCount"] == 1


def test_private_overlay_is_visible_to_both_regardless_of_entitlement(seeded_client):
    client, a_org, b_org = seeded_client
    a = client.get(f"/v1/tenants/{BIZ_A}/records/organization/{a_org}", headers=AUTH).json()
    b = client.get(f"/v1/tenants/{BIZ_B}/records/organization/{b_org}", headers=AUTH).json()
    # The segment overlay is each business's own un-sourced data — always shown to its owner.
    for body in (a, b):
        seg = _field(body, "segment")
        assert seg["overlay"] is True
        assert seg["visible"] is True
        assert seg["value"] == SEGMENT


def test_raw_json_differs_only_by_the_restricted_value(seeded_client):
    """The acceptance comparison: same licensed field, different display rights -> different raw JSON."""
    client, a_org, b_org = seeded_client
    a_rev = _field(
        client.get(f"/v1/tenants/{BIZ_A}/records/organization/{a_org}", headers=AUTH).json(),
        "revenue_inr",
    )
    b_rev = _field(
        client.get(f"/v1/tenants/{BIZ_B}/records/organization/{b_org}", headers=AUTH).json(),
        "revenue_inr",
    )
    assert "value" in a_rev and "value" not in b_rev
    assert b_rev.get("restricted") is True
    # Provenance (source/key/scope) is identical — only the value's presence differs.
    assert a_rev["source"] == b_rev["source"] == SOURCE_KEY
    assert a_rev["fieldKey"] == b_rev["fieldKey"]


def test_tenant_isolation_cross_business_subject_is_404(seeded_client):
    client, a_org, _ = seeded_client
    # B asking for A's subject reads exactly like not-found.
    resp = client.get(f"/v1/tenants/{BIZ_B}/records/organization/{a_org}", headers=AUTH)
    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == "subject_not_found"


def test_unknown_subject_type_is_404(seeded_client):
    client, a_org, _ = seeded_client
    resp = client.get(f"/v1/tenants/{BIZ_A}/records/widget/{a_org}", headers=AUTH)
    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == "unknown_subject_type"


def test_record_route_requires_service_auth(seeded_client):
    client, a_org, _ = seeded_client
    assert client.get(f"/v1/tenants/{BIZ_A}/records/organization/{a_org}").status_code == 401


# --- evidence projection ---------------------------------------------------------------------------


def _seed_evidence(session, business_id: str) -> str:
    from app.services.evidence_store import EvidenceInput, EvidenceObjectStore

    ev = EvidenceObjectStore(session, business_id).store(
        EvidenceInput(
            source_key=SOURCE_KEY,
            source_version=1,
            content_kind="registry_record",
            content_ref="synthetic://mca/record/42",
            raw_content='{"cin": "U17110KA1990PTC011089"}',
            captured_at=NOW,
            rights_basis="source_terms:store;purpose=prospecting",
            content_media_type="application/json",
        ),
        actor=ACTOR,
    )
    return ev.id


def test_evidence_raw_payload_is_gated_by_display_entitlement():
    app = create_app(make_settings())
    with TestClient(app) as client:
        _register_tenant(client, BIZ_A)
        _register_tenant(client, BIZ_B)
        with app.state.session_factory() as session:
            _seed_shared_source(session)
            # The terms grant 'store', so the raw payload is retained (DCA-062 raw_retained).
            _entitle_display(session, BIZ_A, display=True)
            _entitle_display(session, BIZ_B, display=False)
            a_ev = _seed_evidence(session, BIZ_A)
            b_ev = _seed_evidence(session, BIZ_B)
            session.commit()

        a = client.get(f"/v1/tenants/{BIZ_A}/evidence/{a_ev}", headers=AUTH)
        assert a.status_code == 200
        a_body = a.json()
        assert a_body["rawVisible"] is True
        assert a_body["rawContent"] == '{"cin": "U17110KA1990PTC011089"}'
        assert "rawRestricted" not in a_body

        b = client.get(f"/v1/tenants/{BIZ_B}/evidence/{b_ev}", headers=AUTH)
        assert b.status_code == 200
        b_body = b.json()
        # B is not display-entitled -> the raw payload is ABSENT from the payload.
        assert b_body["rawVisible"] is False
        assert "rawContent" not in b_body
        assert b_body["rawRestricted"] is True
        assert "U17110KA1990PTC011089" not in b.text
        # Traceability metadata still flows.
        assert b_body["contentDigest"] == a_body["contentDigest"]
        assert b_body["retentionMode"] == "raw_retained"

        # Tenant isolation: B cannot read A's evidence object by id.
        assert client.get(f"/v1/tenants/{BIZ_B}/evidence/{a_ev}", headers=AUTH).status_code == 404


# --------------------------------------------------------------------------------------------------
# The same A-vs-B projection on a disposable PostgreSQL (real transaction/RLS engine)
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)


@requires_pg
def test_projection_on_real_postgres_withholds_the_restricted_value():
    engine = make_engine(PG_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        app = create_app(make_settings(database_url=PG_URL, auto_create_schema=False))
        with TestClient(app) as client:
            _register_tenant(client, BIZ_A)
            _register_tenant(client, BIZ_B)
            factory = make_session_factory(engine)
            with factory() as session:
                _seed_shared_source(session)
                _entitle_display(session, BIZ_A, display=True)
                _entitle_display(session, BIZ_B, display=False)
                a_org = _record_org_with_fields(session, BIZ_A)
                b_org = _record_org_with_fields(session, BIZ_B)
                session.commit()

            a_rev = _field(
                client.get(
                    f"/v1/tenants/{BIZ_A}/records/organization/{a_org}", headers=AUTH
                ).json(),
                "revenue_inr",
            )
            b_resp = client.get(f"/v1/tenants/{BIZ_B}/records/organization/{b_org}", headers=AUTH)
            b_rev = _field(b_resp.json(), "revenue_inr")
            assert a_rev["value"] == REVENUE
            assert "value" not in b_rev
            assert b_rev["restricted"] is True
            assert REVENUE not in b_resp.text
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
