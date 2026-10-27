"""DCA-039 — versioned ideal-customer-profile (ICP) drafts.

Two layers, matching the DCA-036 profile-store suite:

- **Service unit tests** (in-memory SQLite) pin the store logic: a new draft always inserts the next
  version and preserves every prior one; versions are scoped per business; size bands are validated
  against the controlled vocabulary and non-India geography is rejected (each before anything is
  staged); a linked profile version must exist; and the store stages (never commits) into the
  caller's session.
- **HTTP tests** (``TestClient``) prove the same over the real service API, following the developer
  manual-test steps: save valid drafts, list/retrieve their versions, reproduce the invalid-band /
  unsupported-field / bad-geography denials with no state change, and confirm one business cannot
  retrieve another's version (tenant isolation — a foreign version reads as not-found).

SQLite is sufficient — the behaviour is app-level (per-tenant versioning + validation); the
transaction/RLS paths are covered by ``tests/test_rls.py`` and ``tests/test_migrations.py``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db.base import Base, make_engine, make_session_factory
from app.db.models import BusinessProfileDraft
from app.main import create_app
from app.services.icp_store import (
    REASON_INVALID_SIZE_BAND,
    REASON_PROFILE_VERSION_NOT_FOUND,
    REASON_UNSUPPORTED_GEOGRAPHY,
    IcpDraftInput,
    IcpStore,
    IcpValidationError,
)
from tests.conftest import AUTH, make_settings

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"


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


def _save(factory, business_id, data: IcpDraftInput) -> int:
    with factory() as session:
        draft = IcpStore(session, business_id).save_draft(data)
        session.commit()
        return draft.version


def test_first_draft_is_version_one(session_factory):
    v = _save(session_factory, BIZ_A, IcpDraftInput(industries=["retail"], size_bands=["small"]))
    assert v == 1


def test_a_new_draft_preserves_prior_versions(session_factory):
    v1 = _save(session_factory, BIZ_A, IcpDraftInput(industries=["retail"]))
    v2 = _save(session_factory, BIZ_A, IcpDraftInput(industries=["wholesale"]))
    assert (v1, v2) == (1, 2)

    with session_factory() as session:
        store = IcpStore(session, BIZ_A)
        assert [d.version for d in store.versions()] == [1, 2]
        # The prior version is unchanged, not overwritten by the new draft.
        assert store.get_version(1).industries == ["retail"]
        assert store.get_version(2).industries == ["wholesale"]
        assert store.latest_draft().version == 2


def test_versions_are_scoped_per_business(session_factory):
    _save(session_factory, BIZ_A, IcpDraftInput(industries=["a1"]))
    _save(session_factory, BIZ_A, IcpDraftInput(industries=["a2"]))
    _save(session_factory, BIZ_B, IcpDraftInput(industries=["b1"]))
    with session_factory() as session:
        # B's own versioning starts at 1 independently; A's rows are not visible to B's store.
        assert [d.version for d in IcpStore(session, BIZ_B).versions()] == [1]
        assert IcpStore(session, BIZ_B).get_version(2) is None  # A's v2 is not B's


def test_size_bands_are_normalized_and_deduped(session_factory):
    _save(
        session_factory,
        BIZ_A,
        IcpDraftInput(size_bands=["Small", "SMALL", "medium"]),
    )
    with session_factory() as session:
        assert IcpStore(session, BIZ_A).get_version(1).size_bands == ["small", "medium"]


def test_invalid_size_band_is_refused_and_stages_nothing(session_factory):
    with session_factory() as session:
        store = IcpStore(session, BIZ_A)
        with pytest.raises(IcpValidationError) as excinfo:
            store.save_draft(IcpDraftInput(size_bands=["humongous"]))
        assert excinfo.value.code == REASON_INVALID_SIZE_BAND
        assert excinfo.value.detail == "humongous"
        session.rollback()
    # Nothing was staged — the refusal left no version.
    with session_factory() as session:
        assert IcpStore(session, BIZ_A).versions() == []


def test_geography_is_normalized_to_india_codes(session_factory):
    _save(
        session_factory,
        BIZ_A,
        IcpDraftInput(geography=["Karnataka", "MH", "maharashtra"]),
    )
    with session_factory() as session:
        # Full name and code both resolve to the ISO code; duplicates collapse.
        assert IcpStore(session, BIZ_A).get_version(1).geography == ["KA", "MH"]


def test_non_india_geography_is_refused_and_stages_nothing(session_factory):
    with session_factory() as session:
        store = IcpStore(session, BIZ_A)
        with pytest.raises(IcpValidationError) as excinfo:
            store.save_draft(IcpDraftInput(geography=["California"]))
        assert excinfo.value.code == REASON_UNSUPPORTED_GEOGRAPHY
        assert excinfo.value.detail == "California"
        session.rollback()
    with session_factory() as session:
        assert IcpStore(session, BIZ_A).versions() == []


def test_linked_profile_version_must_exist(session_factory):
    # No business-profile draft exists, so a link to version 1 is rejected.
    with session_factory() as session:
        store = IcpStore(session, BIZ_A)
        with pytest.raises(IcpValidationError) as excinfo:
            store.save_draft(IcpDraftInput(profile_version=1))
        assert excinfo.value.code == REASON_PROFILE_VERSION_NOT_FOUND
        session.rollback()

    # Seed a profile draft for A; now the link resolves.
    with session_factory() as session:
        session.add(BusinessProfileDraft(business_id=BIZ_A, version=1, status="draft"))
        session.commit()
    v = _save(session_factory, BIZ_A, IcpDraftInput(profile_version=1))
    assert v == 1
    with session_factory() as session:
        assert IcpStore(session, BIZ_A).get_version(1).profile_version == 1


def test_linked_profile_version_is_scoped_per_business(session_factory):
    # A profile draft for B does not satisfy a link requested under A's scope.
    with session_factory() as session:
        session.add(BusinessProfileDraft(business_id=BIZ_B, version=1, status="draft"))
        session.commit()
    with session_factory() as session:
        with pytest.raises(IcpValidationError) as excinfo:
            IcpStore(session, BIZ_A).save_draft(IcpDraftInput(profile_version=1))
        assert excinfo.value.code == REASON_PROFILE_VERSION_NOT_FOUND
        session.rollback()


def test_save_draft_only_stages_draft_status(session_factory):
    with session_factory() as session:
        draft = IcpStore(session, BIZ_A).save_draft(IcpDraftInput(industries=["x"]))
        session.commit()
        assert draft.status == "draft"


def test_save_draft_does_not_commit(session_factory):
    # The store stages into the caller's session; a rollback leaves nothing behind.
    with session_factory() as session:
        IcpStore(session, BIZ_A).save_draft(IcpDraftInput(industries=["x"]))
        session.rollback()
    with session_factory() as session:
        assert IcpStore(session, BIZ_A).versions() == []


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


def _icp_body(**overrides) -> dict:
    body = {
        "industries": [],
        "sizeBands": [],
        "roles": [],
        "geography": [],
        "exclusions": [],
    }
    body.update(overrides)
    return body


def test_http_save_two_drafts_and_retrieve_versions(client):
    first = client.post(
        f"/v1/tenants/{BIZ_A}/icp/drafts",
        json=_icp_body(industries=["retail"], sizeBands=["small"], geography=["KA"]),
        headers=AUTH,
    )
    second = client.post(
        f"/v1/tenants/{BIZ_A}/icp/drafts",
        json=_icp_body(industries=["retail", "wholesale"], sizeBands=["medium"], roles=["owner"]),
        headers=AUTH,
    )
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["version"] == 1
    assert second.json()["version"] == 2
    assert first.json()["status"] == "draft"
    assert first.json()["sizeBands"] == ["small"]
    assert first.json()["geography"] == ["KA"]

    listed = client.get(f"/v1/tenants/{BIZ_A}/icp/drafts", headers=AUTH).json()
    assert [row["version"] for row in listed] == [1, 2]

    # The first version is still retrievable and unchanged by the second save.
    v1 = client.get(f"/v1/tenants/{BIZ_A}/icp/drafts/1", headers=AUTH).json()
    assert v1["industries"] == ["retail"]
    assert v1["roles"] == []


def test_http_invalid_size_band_is_422_and_changes_nothing(client):
    before = client.get(f"/v1/tenants/{BIZ_A}/icp/drafts", headers=AUTH).json()
    resp = client.post(
        f"/v1/tenants/{BIZ_A}/icp/drafts",
        json=_icp_body(sizeBands=["ginormous"]),
        headers=AUTH,
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["message"] == REASON_INVALID_SIZE_BAND
    after = client.get(f"/v1/tenants/{BIZ_A}/icp/drafts", headers=AUTH).json()
    assert after == before  # the rejected draft created no version


def test_http_unsupported_field_is_422(client):
    # extra='forbid' on the body: a field this capability does not support is rejected (422), not
    # silently ignored.
    resp = client.post(
        f"/v1/tenants/{BIZ_A}/icp/drafts",
        json=_icp_body(industries=["retail"], autoAccept=True),
        headers=AUTH,
    )
    assert resp.status_code == 422
    assert client.get(f"/v1/tenants/{BIZ_A}/icp/drafts", headers=AUTH).json() == []


def test_http_non_india_geography_is_422_and_changes_nothing(client):
    resp = client.post(
        f"/v1/tenants/{BIZ_A}/icp/drafts",
        json=_icp_body(geography=["Texas"]),
        headers=AUTH,
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["message"] == REASON_UNSUPPORTED_GEOGRAPHY
    assert client.get(f"/v1/tenants/{BIZ_A}/icp/drafts", headers=AUTH).json() == []


def test_http_link_to_business_profile_version(client):
    # Create a business-profile draft first, then link the ICP draft to its version.
    client.post(
        f"/v1/tenants/{BIZ_A}/profile/drafts",
        json={
            "products": [],
            "businessType": "retail",
            "valueProposition": "",
            "languages": [],
            "confirmedFacts": [],
            "unknowns": [],
        },
        headers=AUTH,
    )
    linked = client.post(
        f"/v1/tenants/{BIZ_A}/icp/drafts",
        json=_icp_body(industries=["retail"], profileVersion=1),
        headers=AUTH,
    )
    assert linked.status_code == 201
    assert linked.json()["profileVersion"] == 1

    # A link to a non-existent profile version is a clear 422, stages nothing.
    bad = client.post(
        f"/v1/tenants/{BIZ_A}/icp/drafts",
        json=_icp_body(industries=["retail"], profileVersion=99),
        headers=AUTH,
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["message"] == REASON_PROFILE_VERSION_NOT_FOUND
    # Only the first (valid) draft exists.
    listed = client.get(f"/v1/tenants/{BIZ_A}/icp/drafts", headers=AUTH).json()
    assert [row["version"] for row in listed] == [1]


def test_http_unknown_version_is_404(client):
    resp = client.get(f"/v1/tenants/{BIZ_A}/icp/drafts/99", headers=AUTH)
    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == "icp_draft_version_not_found"


def test_http_cross_tenant_version_is_not_visible(client):
    # A saves version 1; B (its own store) never sees it, and B has saved nothing.
    client.post(
        f"/v1/tenants/{BIZ_A}/icp/drafts",
        json=_icp_body(industries=["a"]),
        headers=AUTH,
    )
    resp = client.get(f"/v1/tenants/{BIZ_B}/icp/drafts/1", headers=AUTH)
    assert resp.status_code == 404
    assert client.get(f"/v1/tenants/{BIZ_B}/icp/drafts", headers=AUTH).json() == []


def test_http_unregistered_tenant_is_404(client):
    resp = client.post(
        "/v1/tenants/99999999-9999-9999-9999-999999999999/icp/drafts",
        json=_icp_body(industries=["x"]),
        headers=AUTH,
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == "tenant agent not registered"


def test_http_save_is_idempotent_on_retry(client):
    from app.idempotency import IDEMPOTENCY_HEADER

    body = _icp_body(industries=["retail"], sizeBands=["small"])
    keyed = {**AUTH, IDEMPOTENCY_HEADER: "icp-key-1"}
    first = client.post(f"/v1/tenants/{BIZ_A}/icp/drafts", json=body, headers=keyed)
    replay = client.post(f"/v1/tenants/{BIZ_A}/icp/drafts", json=body, headers=keyed)

    assert first.status_code == 201
    assert replay.json() == first.json()  # replayed verbatim
    assert replay.headers.get("Idempotent-Replay") == "true"
    # The retry created no second version.
    listed = client.get(f"/v1/tenants/{BIZ_A}/icp/drafts", headers=AUTH).json()
    assert [row["version"] for row in listed] == [1]
