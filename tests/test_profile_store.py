"""DCA-036 — versioned business-profile drafts.

Two layers, matching the DCA-031/035 suites:

- **Service unit tests** (in-memory SQLite) pin the store logic: a new draft always inserts the next
  version and preserves every prior one; unknown aspects are retained explicitly (empty aspects kept
  in ``unknowns``, populated ones never there); confirming is a disabled future capability that
  refuses and stages nothing, so no confirmed profile is ever created; and the store stages (never
  commits) into the caller's session.
- **HTTP tests** (``TestClient``) prove the same over the real service API, following the developer
  manual-test steps: save two drafts, list/retrieve their versions, confirm no confirmed profile was
  created automatically, and reproduce the confirm denial with no state change. Tenant isolation: a
  version id from another business reads as not-found.

SQLite is sufficient — the behaviour is app-level (per-tenant versioning + the unique constraint);
the transaction/RLS paths are covered by ``tests/test_rls.py`` and ``tests/test_migrations.py``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db.base import Base, make_engine, make_session_factory
from app.db.models import ProfileStatus
from app.main import create_app
from app.services.profile_store import (
    REASON_CONFIRM_DISABLED,
    ProfileDraftInput,
    ProfileError,
    ProfileStore,
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


def _save(factory, business_id, data: ProfileDraftInput) -> int:
    with factory() as session:
        draft = ProfileStore(session, business_id).save_draft(data)
        session.commit()
        return draft.version


def test_first_draft_is_version_one(session_factory):
    v = _save(
        session_factory, BIZ_A, ProfileDraftInput(products=["widget"], business_type="retail")
    )
    assert v == 1


def test_a_new_draft_preserves_prior_versions(session_factory):
    v1 = _save(session_factory, BIZ_A, ProfileDraftInput(business_type="retail"))
    v2 = _save(session_factory, BIZ_A, ProfileDraftInput(business_type="wholesale"))
    assert (v1, v2) == (1, 2)

    with session_factory() as session:
        store = ProfileStore(session, BIZ_A)
        versions = store.versions()
        assert [d.version for d in versions] == [1, 2]
        # The prior version is unchanged, not overwritten by the new draft.
        assert store.get_version(1).business_type == "retail"
        assert store.get_version(2).business_type == "wholesale"
        assert store.latest_draft().version == 2


def test_unknown_aspects_are_retained_explicitly(session_factory):
    # value_proposition / languages / confirmed_facts left empty => retained as unknown; products and
    # business_type populated => never unknown.
    _save(
        session_factory,
        BIZ_A,
        ProfileDraftInput(products=["widget"], business_type="retail"),
    )
    with session_factory() as session:
        draft = ProfileStore(session, BIZ_A).get_version(1)
        assert set(draft.unknowns) == {"value_proposition", "languages", "confirmed_facts"}


def test_populated_aspect_is_never_unknown_even_if_declared(session_factory):
    # The caller wrongly declares 'products' unknown while also populating it: the store drops it from
    # unknowns (a populated aspect is known), keeping unknowns and knowns disjoint and honest.
    _save(
        session_factory,
        BIZ_A,
        ProfileDraftInput(
            products=["widget"],
            business_type="retail",
            value_proposition="we do X",
            languages=["en"],
            confirmed_facts=["gst-registered"],
            unknowns=["products"],
        ),
    )
    with session_factory() as session:
        draft = ProfileStore(session, BIZ_A).get_version(1)
        assert draft.unknowns == []  # everything populated => nothing unknown


def test_versions_are_scoped_per_business(session_factory):
    _save(session_factory, BIZ_A, ProfileDraftInput(business_type="a1"))
    _save(session_factory, BIZ_A, ProfileDraftInput(business_type="a2"))
    _save(session_factory, BIZ_B, ProfileDraftInput(business_type="b1"))
    with session_factory() as session:
        # B's own versioning starts at 1 independently; A's rows are not visible to B's store.
        assert [d.version for d in ProfileStore(session, BIZ_B).versions()] == [1]
        assert ProfileStore(session, BIZ_B).get_version(2) is None  # A's v2 is not B's


def test_save_draft_only_stages_draft_status(session_factory):
    with session_factory() as session:
        draft = ProfileStore(session, BIZ_A).save_draft(ProfileDraftInput(business_type="x"))
        session.commit()
        assert draft.status == ProfileStatus.DRAFT.value


def test_confirm_is_disabled_and_stages_nothing(session_factory):
    _save(session_factory, BIZ_A, ProfileDraftInput(business_type="retail"))
    with session_factory() as session:
        store = ProfileStore(session, BIZ_A)
        with pytest.raises(ProfileError) as excinfo:
            store.confirm(1)
        assert excinfo.value.code == REASON_CONFIRM_DISABLED
        session.rollback()
    # No confirmed profile was ever created — saving/confirming cannot replace the confirmed profile.
    with session_factory() as session:
        assert ProfileStore(session, BIZ_A).confirmed() is None


def test_save_draft_does_not_commit(session_factory):
    # The store stages into the caller's session; a rollback leaves nothing behind.
    with session_factory() as session:
        ProfileStore(session, BIZ_A).save_draft(ProfileDraftInput(business_type="x"))
        session.rollback()
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


def _draft_body(**overrides) -> dict:
    body = {
        "products": [],
        "businessType": "",
        "valueProposition": "",
        "languages": [],
        "confirmedFacts": [],
        "unknowns": [],
    }
    body.update(overrides)
    return body


def test_http_save_two_drafts_and_retrieve_versions(client):
    first = client.post(
        f"/v1/tenants/{BIZ_A}/profile/drafts",
        json=_draft_body(products=["widget"], businessType="retail"),
        headers=AUTH,
    )
    second = client.post(
        f"/v1/tenants/{BIZ_A}/profile/drafts",
        json=_draft_body(products=["widget", "gadget"], businessType="wholesale"),
        headers=AUTH,
    )
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["version"] == 1
    assert second.json()["version"] == 2
    assert first.json()["status"] == "draft"

    listed = client.get(f"/v1/tenants/{BIZ_A}/profile/drafts", headers=AUTH).json()
    assert [row["version"] for row in listed] == [1, 2]

    # The first version is still retrievable and unchanged by the second save.
    v1 = client.get(f"/v1/tenants/{BIZ_A}/profile/drafts/1", headers=AUTH).json()
    assert v1["businessType"] == "retail"
    assert set(v1["unknowns"]) == {"value_proposition", "languages", "confirmed_facts"}


def test_http_saving_drafts_creates_no_confirmed_profile(client):
    client.post(
        f"/v1/tenants/{BIZ_A}/profile/drafts",
        json=_draft_body(businessType="retail"),
        headers=AUTH,
    )
    confirmed = client.get(f"/v1/tenants/{BIZ_A}/profile/confirmed", headers=AUTH).json()
    assert confirmed["confirmed"] is False
    assert confirmed["profile"] is None


def test_http_confirm_is_denied_and_changes_nothing(client):
    client.post(
        f"/v1/tenants/{BIZ_A}/profile/drafts",
        json=_draft_body(businessType="retail"),
        headers=AUTH,
    )
    before = client.get(f"/v1/tenants/{BIZ_A}/profile/drafts", headers=AUTH).json()

    denied = client.post(f"/v1/tenants/{BIZ_A}/profile/drafts/1/confirm", headers=AUTH)
    assert denied.status_code == 403
    assert denied.json()["error"]["message"] == REASON_CONFIRM_DISABLED

    # No confirmed profile appeared and the draft list is untouched.
    after = client.get(f"/v1/tenants/{BIZ_A}/profile/drafts", headers=AUTH).json()
    assert after == before
    confirmed = client.get(f"/v1/tenants/{BIZ_A}/profile/confirmed", headers=AUTH).json()
    assert confirmed["confirmed"] is False


def test_http_unknown_version_is_404(client):
    resp = client.get(f"/v1/tenants/{BIZ_A}/profile/drafts/99", headers=AUTH)
    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == "profile_draft_version_not_found"


def test_http_cross_tenant_version_is_not_visible(client):
    # A saves version 1; B (its own store) never sees it, and B's version 1 is independent.
    client.post(
        f"/v1/tenants/{BIZ_A}/profile/drafts", json=_draft_body(businessType="a"), headers=AUTH
    )
    # B has saved nothing yet, so B's version 1 is a 404 (A's row does not leak across the boundary).
    resp = client.get(f"/v1/tenants/{BIZ_B}/profile/drafts/1", headers=AUTH)
    assert resp.status_code == 404
    assert client.get(f"/v1/tenants/{BIZ_B}/profile/drafts", headers=AUTH).json() == []


def test_http_unregistered_tenant_is_404(client):
    resp = client.post(
        "/v1/tenants/99999999-9999-9999-9999-999999999999/profile/drafts",
        json=_draft_body(businessType="x"),
        headers=AUTH,
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == "tenant agent not registered"


def test_http_save_is_idempotent_on_retry(client):
    from app.idempotency import IDEMPOTENCY_HEADER

    body = _draft_body(businessType="retail")
    keyed = {**AUTH, IDEMPOTENCY_HEADER: "draft-key-1"}
    first = client.post(f"/v1/tenants/{BIZ_A}/profile/drafts", json=body, headers=keyed)
    replay = client.post(f"/v1/tenants/{BIZ_A}/profile/drafts", json=body, headers=keyed)

    assert first.status_code == 201
    assert replay.json() == first.json()  # replayed verbatim
    assert replay.headers.get("Idempotent-Replay") == "true"
    # The retry created no second version.
    listed = client.get(f"/v1/tenants/{BIZ_A}/profile/drafts", headers=AUTH).json()
    assert [row["version"] for row in listed] == [1]
