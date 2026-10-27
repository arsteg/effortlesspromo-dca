"""DCA-037 — import an authorized platform profile snapshot into a provenance-bearing draft.

Two layers, matching the DCA-036 suite:

- **Service unit tests** (in-memory SQLite) pin the importer: a fetched snapshot is staged as a new
  draft version that retains the source version + business scope and stays a ``draft``; its facts
  (products / Brand KB facts) are carried through; A and B import distinct profiles; a second import
  is a NEW version (DCA-036 preservation holds); the contract-disabled and nothing-to-import denials
  both refuse with a stable code and stage nothing; and the importer stages (never commits).
- **HTTP tests** (``TestClient``) prove the same over the real service API, following the developer
  manual-test steps: import A and B and inspect the distinct facts/provenance/draft status, confirm
  the import created no confirmed profile, and reproduce the import's idempotent retry and the 404
  for an unregistered tenant.

SQLite is sufficient for the importer logic (app-level); the transaction/RLS/migration paths are
covered on disposable PostgreSQL by ``tests/test_rls.py`` / ``tests/test_migrations.py``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db.base import Base, make_engine, make_session_factory
from app.db.models import ProfileStatus
from app.identity.fake import BUSINESS_A, BUSINESS_B
from app.main import create_app
from app.profiles import (
    SYNTHETIC_SOURCE,
    SYNTHETIC_SOURCE_VERSION,
    FakePlatformProfileClient,
)
from app.services.profile_importer import (
    REASON_IMPORT_CONTRACT_DISABLED,
    REASON_PROFILE_NOT_FOUND,
    ProfileImportError,
    import_profile_snapshot,
)
from app.services.profile_store import ProfileStore
from tests.conftest import AUTH, make_settings

BIZ_A = BUSINESS_A
BIZ_B = BUSINESS_B


# --------------------------------------------------------------------------------------------------
# Service unit tests (in-memory SQLite)
# --------------------------------------------------------------------------------------------------


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def test_import_stages_a_provenance_bearing_draft(session_factory):
    client = FakePlatformProfileClient()
    with session_factory() as session:
        draft = import_profile_snapshot(session, client, BIZ_A)
        session.commit()
        version = draft.version

    with session_factory() as session:
        stored = ProfileStore(session, BIZ_A).get_version(version)
        # Retains source version + business scope, stays a draft (the acceptance criterion).
        assert stored.business_id == BIZ_A
        assert stored.status == ProfileStatus.DRAFT.value
        assert stored.source == SYNTHETIC_SOURCE
        assert stored.source_version == SYNTHETIC_SOURCE_VERSION
        assert stored.provenance["source_version"] == SYNTHETIC_SOURCE_VERSION
        assert stored.provenance["business_id"] == BIZ_A
        assert stored.provenance["imported"] is True
        assert stored.provenance["synthetic"] is True
        # The platform's facts (products + Brand KB facts) were carried into the draft.
        assert stored.products
        assert stored.confirmed_facts


def test_import_carries_distinct_facts_per_business(session_factory):
    client = FakePlatformProfileClient()
    with session_factory() as session:
        a = import_profile_snapshot(session, client, BIZ_A)
        b = import_profile_snapshot(session, client, BIZ_B)
        session.commit()
        assert a.business_id == BIZ_A and b.business_id == BIZ_B
        assert a.products != b.products
        assert a.business_type != b.business_type


def test_second_import_is_a_new_version_preserving_the_first(session_factory):
    client = FakePlatformProfileClient()
    with session_factory() as session:
        v1 = import_profile_snapshot(session, client, BIZ_A).version
        v2 = import_profile_snapshot(session, client, BIZ_A).version
        session.commit()
    assert (v1, v2) == (1, 2)
    with session_factory() as session:
        assert [d.version for d in ProfileStore(session, BIZ_A).versions()] == [1, 2]


def test_contract_disabled_refuses_and_stages_nothing(session_factory):
    with session_factory() as session:
        with pytest.raises(ProfileImportError) as excinfo:
            import_profile_snapshot(session, None, BIZ_A)  # client None => contract disabled
        assert excinfo.value.code == REASON_IMPORT_CONTRACT_DISABLED
        session.rollback()
    with session_factory() as session:
        assert ProfileStore(session, BIZ_A).versions() == []  # no draft was staged


def test_nothing_to_import_refuses_and_stages_nothing(session_factory):
    client = FakePlatformProfileClient()
    with session_factory() as session:
        with pytest.raises(ProfileImportError) as excinfo:
            import_profile_snapshot(session, client, "business-with-no-profile")
        assert excinfo.value.code == REASON_PROFILE_NOT_FOUND
        session.rollback()
    with session_factory() as session:
        assert ProfileStore(session, "business-with-no-profile").versions() == []


def test_import_does_not_commit(session_factory):
    client = FakePlatformProfileClient()
    with session_factory() as session:
        import_profile_snapshot(session, client, BIZ_A)
        session.rollback()  # the importer only stages; a rollback leaves nothing behind
    with session_factory() as session:
        assert ProfileStore(session, BIZ_A).versions() == []


# --------------------------------------------------------------------------------------------------
# HTTP tests (TestClient) — the real service API
# --------------------------------------------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    app = create_app(make_settings())
    with TestClient(app, raise_server_exceptions=False) as test_client:
        for biz in (BIZ_A, BIZ_B):
            test_client.post("/v1/tenants", json={"businessId": biz}, headers=AUTH)
        yield test_client


def test_http_import_a_and_b_distinct_profiles(client):
    a = client.post(f"/v1/tenants/{BIZ_A}/profile/import", headers=AUTH)
    b = client.post(f"/v1/tenants/{BIZ_B}/profile/import", headers=AUTH)
    assert a.status_code == 201 and b.status_code == 201
    a_body, b_body = a.json(), b.json()

    # Each import is version 1 of its own business, a draft, provenance-bearing.
    assert a_body["version"] == 1 and b_body["version"] == 1
    assert a_body["status"] == "draft"
    assert a_body["businessId"] == BIZ_A
    assert a_body["sourceVersion"] == SYNTHETIC_SOURCE_VERSION
    assert a_body["source"] == SYNTHETIC_SOURCE
    assert a_body["provenance"]["imported"] is True
    # Distinct facts for A vs B.
    assert a_body["products"] != b_body["products"]
    assert a_body["confirmedFacts"] and b_body["confirmedFacts"]

    # Listed/retrievable afterwards, with provenance visible on the summary + version view.
    listed = client.get(f"/v1/tenants/{BIZ_A}/profile/drafts", headers=AUTH).json()
    assert [row["version"] for row in listed] == [1]
    assert listed[0]["sourceVersion"] == SYNTHETIC_SOURCE_VERSION
    v1 = client.get(f"/v1/tenants/{BIZ_A}/profile/drafts/1", headers=AUTH).json()
    assert v1["provenance"]["source_version"] == SYNTHETIC_SOURCE_VERSION


def test_http_import_creates_no_confirmed_profile(client):
    client.post(f"/v1/tenants/{BIZ_A}/profile/import", headers=AUTH)
    confirmed = client.get(f"/v1/tenants/{BIZ_A}/profile/confirmed", headers=AUTH).json()
    assert confirmed["confirmed"] is False
    assert confirmed["profile"] is None


def test_http_import_is_idempotent_on_retry(client):
    from app.idempotency import IDEMPOTENCY_HEADER

    keyed = {**AUTH, IDEMPOTENCY_HEADER: "import-key-1"}
    first = client.post(f"/v1/tenants/{BIZ_A}/profile/import", headers=keyed)
    replay = client.post(f"/v1/tenants/{BIZ_A}/profile/import", headers=keyed)
    assert first.status_code == 201
    assert replay.json() == first.json()  # replayed verbatim
    assert replay.headers.get("Idempotent-Replay") == "true"
    # The retry created no second version.
    listed = client.get(f"/v1/tenants/{BIZ_A}/profile/drafts", headers=AUTH).json()
    assert [row["version"] for row in listed] == [1]


def test_http_import_unregistered_tenant_is_404(client):
    resp = client.post(
        "/v1/tenants/99999999-9999-9999-9999-999999999999/profile/import", headers=AUTH
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == "tenant agent not registered"


def test_http_import_fails_closed_when_contract_disabled(client):
    # Simulate production (the live adapter is unbuilt, so the contract is disabled => client None).
    client.app.state.platform_profile_client = None

    before = client.get(f"/v1/tenants/{BIZ_A}/profile/drafts", headers=AUTH).json()
    denied = client.post(f"/v1/tenants/{BIZ_A}/profile/import", headers=AUTH)
    after = client.get(f"/v1/tenants/{BIZ_A}/profile/drafts", headers=AUTH).json()

    assert denied.status_code == 503
    assert denied.json()["error"]["message"] == REASON_IMPORT_CONTRACT_DISABLED
    assert after == before  # the denial staged no draft


def test_http_import_nothing_to_import_is_404(client):
    # Point the running app at a fake with NO fixtures => every business is "not found".
    client.app.state.platform_profile_client = FakePlatformProfileClient(fixtures={})

    before = client.get(f"/v1/tenants/{BIZ_A}/profile/drafts", headers=AUTH).json()
    denied = client.post(f"/v1/tenants/{BIZ_A}/profile/import", headers=AUTH)
    after = client.get(f"/v1/tenants/{BIZ_A}/profile/drafts", headers=AUTH).json()

    assert denied.status_code == 404
    assert denied.json()["error"]["message"] == REASON_PROFILE_NOT_FOUND
    assert after == before  # the denial staged no draft
