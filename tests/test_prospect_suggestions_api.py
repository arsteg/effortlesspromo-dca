"""DCA-150 — list a business's VISIBLE prospect suggestions (a tenant-scoped, read-only candidate list).

Two layers, matching the DCA-117 spend-summary suite:

- **Service unit tests** (in-memory SQLite) pin
  :meth:`~app.services.prospect_suggestion.ProspectSuggestionReader.list_visible`: only VISIBLE
  suggestions are returned — a dismissed / expired / sendable suggestion is omitted; the safe filters
  (one run, a minimum ICP fit) only ever narrow the page; pagination is deterministic and bounded
  (limit clamped, total/has_more reported); and the list is scoped per business (B never sees A's rows).
- **HTTP tests** (``TestClient`` with the end-user auth contract wired, like
  ``tests/test_spend_summary.py``) prove the authorization gate and tenant isolation over the real API:
  an ordinary viewer (``candidate.read``) can read; a non-member (platform operator) cannot (403); an
  unauthenticated request is 401; and one business's member can never page into another's candidates.

SQLite is sufficient — the behaviour is a pure read over tenant-scoped rows; the RLS / migration paths
are covered by ``tests/test_rls.py`` / ``tests/test_migrations.py`` (which pin the
``prospect_suggestions`` table), and a PG-gated test below proves tenant isolation over real Postgres
RLS when ``DCA_TEST_DATABASE_URL`` is set. Listing writes nothing, spends nothing and sends nothing.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import ProspectSuggestion, SuggestionVisibility
from app.identity.fake import BUSINESS_A, BUSINESS_B, USER_ALICE, USER_BOB, USER_CAROL
from app.main import create_app
from app.services.prospect_suggestion import (
    MAX_PAGE_SIZE,
    ProspectSuggestionReader,
    SuggestionListFilters,
)
from tests.conftest import make_settings

BIZ_A = "11111111-1111-1111-1111-111111111150"
BIZ_B = "22222222-2222-2222-2222-222222222250"


def _suggestion(
    business_id: str,
    subject_id: str,
    *,
    run_id: str = "run-150",
    status: str = "suggested",
    visibility: str = SuggestionVisibility.RESEARCH_ONLY.value,
    match_score: float = 0.5,
    matched_dimensions: list[str] | None = None,
    now: datetime | None = None,
    expires_in_days: int | None = 30,
) -> ProspectSuggestion:
    moment = now or datetime.now(UTC)
    expires_at = None if expires_in_days is None else moment + timedelta(days=expires_in_days)
    return ProspectSuggestion(
        business_id=business_id,
        run_id=run_id,
        subject_type="organization",
        subject_id=subject_id,
        visibility=visibility,
        match_score=match_score,
        rationale=f"ICP fit {match_score:.2f}",
        scored_icp_version=1,
        schema_version=1,
        status=status,
        provenance={
            "source": "prospect_suggestion",
            "matchedDimensions": matched_dimensions or ["industry"],
            "sendable": False,
            "addedToAudience": False,
        },
        created_at=moment,
        expires_at=expires_at,
    )


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


def test_lists_only_visible_suggestions(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        session.add_all(
            [
                _suggestion(BIZ_A, "visible-1", match_score=0.9, now=now),
                _suggestion(
                    BIZ_A, "visible-no-expiry", match_score=0.3, expires_in_days=None, now=now
                ),
                # expired → not visible
                _suggestion(BIZ_A, "expired", match_score=0.8, expires_in_days=-1, now=now),
                # dismissed status → not visible
                _suggestion(BIZ_A, "dismissed", status="dismissed", match_score=0.95, now=now),
                # sendable visibility is a disabled future capability → never surfaced
                _suggestion(
                    BIZ_A,
                    "sendable",
                    visibility=SuggestionVisibility.SENDABLE.value,
                    match_score=0.99,
                    now=now,
                ),
            ]
        )
        session.commit()

        page = ProspectSuggestionReader(session, BIZ_A).list_visible(now=now)

    subjects = [item.subject_id for item in page.items]
    # Only the two visible, research-only, unexpired, suggested rows — ordered by score desc.
    assert subjects == ["visible-1", "visible-no-expiry"]
    assert page.total == 2
    assert page.has_more is False
    assert all(item.sendable is False for item in page.items)
    assert all(item.visibility == SuggestionVisibility.RESEARCH_ONLY.value for item in page.items)


def test_expiry_drops_a_suggestion_from_the_list(session_factory):
    # A suggestion visible BEFORE its expiry becomes invisible AFTER it (invalidation affects reads).
    created = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        session.add(_suggestion(BIZ_A, "s1", expires_in_days=10, now=created))
        session.commit()

        before = ProspectSuggestionReader(session, BIZ_A).list_visible(
            now=created + timedelta(days=5)
        )
        after = ProspectSuggestionReader(session, BIZ_A).list_visible(
            now=created + timedelta(days=15)
        )

    assert [i.subject_id for i in before.items] == ["s1"] and before.total == 1
    assert after.items == [] and after.total == 0


def test_filters_only_narrow(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        session.add_all(
            [
                _suggestion(BIZ_A, "run-a-high", run_id="run-a", match_score=0.9, now=now),
                _suggestion(BIZ_A, "run-a-low", run_id="run-a", match_score=0.2, now=now),
                _suggestion(BIZ_A, "run-b-high", run_id="run-b", match_score=0.8, now=now),
            ]
        )
        session.commit()
        reader = ProspectSuggestionReader(session, BIZ_A)

        by_run = reader.list_visible(filters=SuggestionListFilters(run_id="run-a"), now=now)
        by_score = reader.list_visible(filters=SuggestionListFilters(min_score=0.5), now=now)
        both = reader.list_visible(
            filters=SuggestionListFilters(run_id="run-a", min_score=0.5), now=now
        )

    assert {i.subject_id for i in by_run.items} == {"run-a-high", "run-a-low"}
    assert {i.subject_id for i in by_score.items} == {"run-a-high", "run-b-high"}
    assert [i.subject_id for i in both.items] == ["run-a-high"]


def test_pagination_is_bounded_and_deterministic(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        # Five distinct scores so the deterministic score-desc ordering is unambiguous.
        for i, score in enumerate([0.9, 0.8, 0.7, 0.6, 0.5]):
            session.add(_suggestion(BIZ_A, f"s{i}", match_score=score, now=now))
        session.commit()
        reader = ProspectSuggestionReader(session, BIZ_A)

        page1 = reader.list_visible(limit=2, offset=0, now=now)
        page2 = reader.list_visible(limit=2, offset=2, now=now)
        page3 = reader.list_visible(limit=2, offset=4, now=now)
        # A limit beyond the max is clamped, never served unbounded.
        clamped = reader.list_visible(limit=10_000, now=now)

    assert [i.subject_id for i in page1.items] == ["s0", "s1"]
    assert page1.total == 5 and page1.has_more is True
    assert [i.subject_id for i in page2.items] == ["s2", "s3"]
    assert page2.has_more is True
    assert [i.subject_id for i in page3.items] == ["s4"]
    assert page3.has_more is False
    assert clamped.limit == MAX_PAGE_SIZE


def test_list_is_scoped_per_business(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        session.add_all(
            [
                _suggestion(BIZ_A, "a-only", now=now),
                _suggestion(BIZ_B, "b-only", now=now),
            ]
        )
        session.commit()

        a = ProspectSuggestionReader(session, BIZ_A).list_visible(now=now)
        b = ProspectSuggestionReader(session, BIZ_B).list_visible(now=now)

    assert [i.subject_id for i in a.items] == ["a-only"]
    assert [i.subject_id for i in b.items] == ["b-only"]


def test_list_writes_nothing(session_factory):
    from app.db.models import AuditEvent

    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        session.add(_suggestion(BIZ_A, "s1", now=now))
        session.commit()
    with session_factory() as session:
        audits_before = session.query(AuditEvent).count()
        rows_before = session.query(ProspectSuggestion).count()
        ProspectSuggestionReader(session, BIZ_A).list_visible(now=now)
        session.commit()
    with session_factory() as session:
        assert session.query(AuditEvent).count() == audits_before
        assert session.query(ProspectSuggestion).count() == rows_before


# --------------------------------------------------------------------------------------------------
# HTTP tests (TestClient) — the real service API with the end-user auth contract wired
# --------------------------------------------------------------------------------------------------

ISSUER = "https://issuer.dca.test"
AUDIENCE = "dca-api"
SECRET = "dca-test-signing-secret-value"  # synthetic HS256 key, not a real credential
OPERATOR = "00000000-0000-4000-9000-00000090e701"  # platform operator; no business membership


def _auth_settings(**overrides) -> Settings:
    base = {
        "auth_token_issuer": ISSUER,
        "auth_token_audience": AUDIENCE,
        "auth_token_signing_secret": SECRET,
    }
    base.update(overrides)
    return make_settings(**base)


def _mint(*, subject: str, roles: list[str] | None = None) -> str:
    now = datetime.now(UTC)
    claims: dict = {
        "sub": subject,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    if roles is not None:
        claims["roles"] = roles
    return jwt.encode(claims, SECRET, algorithm="HS256")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client() -> TestClient:
    app = create_app(_auth_settings())
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _seed(client: TestClient, business_id: str, *suggestions: ProspectSuggestion) -> None:
    factory = client.app.state.session_factory
    with factory() as session:
        for s in suggestions:
            s.business_id = business_id
            session.add(s)
        session.commit()


def test_http_viewer_can_list_visible_suggestions(client):
    # Carol is an active viewer in A — she holds candidate.read, so she may view candidates.
    _seed(
        client,
        BUSINESS_A,
        _suggestion(BUSINESS_A, "visible", match_score=0.7),
        _suggestion(BUSINESS_A, "expired", expires_in_days=-1),
        _suggestion(BUSINESS_A, "sendable", visibility=SuggestionVisibility.SENDABLE.value),
    )
    resp = client.get(
        f"/v1/tenants/{BUSINESS_A}/prospect-suggestions",
        headers=_bearer(_mint(subject=USER_CAROL)),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert body["hasMore"] is False
    assert [i["subjectId"] for i in body["items"]] == ["visible"]
    item = body["items"][0]
    assert item["visibility"] == SuggestionVisibility.RESEARCH_ONLY.value
    assert item["sendable"] is False
    # Rights-aware fields only — no contact value is exposed.
    assert "subjectId" in item and "matchScore" in item
    assert not any(k.lower() in {"email", "phone", "contact"} for k in item)


def test_http_pagination_and_filters(client):
    _seed(
        client,
        BUSINESS_A,
        _suggestion(BUSINESS_A, "high", run_id="run-x", match_score=0.9),
        _suggestion(BUSINESS_A, "mid", run_id="run-x", match_score=0.6),
        _suggestion(BUSINESS_A, "low", run_id="run-y", match_score=0.2),
    )
    token = _mint(subject=USER_ALICE)
    page1 = client.get(
        f"/v1/tenants/{BUSINESS_A}/prospect-suggestions?limit=1&offset=0",
        headers=_bearer(token),
    ).json()
    assert [i["subjectId"] for i in page1["items"]] == ["high"]
    assert page1["total"] == 3 and page1["hasMore"] is True

    filtered = client.get(
        f"/v1/tenants/{BUSINESS_A}/prospect-suggestions?runId=run-x&minScore=0.5",
        headers=_bearer(token),
    ).json()
    assert {i["subjectId"] for i in filtered["items"]} == {"high", "mid"}
    assert filtered["total"] == 2


def test_http_platform_operator_cannot_list(client):
    _seed(client, BUSINESS_A, _suggestion(BUSINESS_A, "visible"))
    resp = client.get(
        f"/v1/tenants/{BUSINESS_A}/prospect-suggestions",
        headers=_bearer(_mint(subject=OPERATOR, roles=["platform_operator"])),
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_http_unauthenticated_request_is_401(client):
    resp = client.get(f"/v1/tenants/{BUSINESS_A}/prospect-suggestions")
    assert resp.status_code == 401


def test_http_out_of_range_pagination_is_422(client):
    # A negative offset / an over-max limit is rejected at the boundary — never served unbounded.
    token = _mint(subject=USER_ALICE)
    assert (
        client.get(
            f"/v1/tenants/{BUSINESS_A}/prospect-suggestions?offset=-1", headers=_bearer(token)
        ).status_code
        == 422
    )
    assert (
        client.get(
            f"/v1/tenants/{BUSINESS_A}/prospect-suggestions?limit=99999", headers=_bearer(token)
        ).status_code
        == 422
    )


def test_http_tenant_cannot_page_into_another_businesss_candidates(client):
    _seed(client, BUSINESS_A, _suggestion(BUSINESS_A, "a-only", match_score=0.9))
    _seed(client, BUSINESS_B, _suggestion(BUSINESS_B, "b-only", match_score=0.8))

    a = client.get(
        f"/v1/tenants/{BUSINESS_A}/prospect-suggestions", headers=_bearer(_mint(subject=USER_ALICE))
    )
    assert a.status_code == 200
    assert [i["subjectId"] for i in a.json()["items"]] == ["a-only"]

    b = client.get(
        f"/v1/tenants/{BUSINESS_B}/prospect-suggestions", headers=_bearer(_mint(subject=USER_BOB))
    )
    assert b.status_code == 200
    assert [i["subjectId"] for i in b.json()["items"]] == ["b-only"]

    # Bob's A membership was revoked → he cannot list A's candidates (403, never another tenant's rows).
    cross = client.get(
        f"/v1/tenants/{BUSINESS_A}/prospect-suggestions", headers=_bearer(_mint(subject=USER_BOB))
    )
    assert cross.status_code == 403


# --------------------------------------------------------------------------------------------------
# Postgres-backed test — disposable PostgreSQL (real RLS + transactions, not SQLite)
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)
_PG_SCHEMA = "dca_150_test"


@pytest.fixture
def pg_session_factory():
    engine = make_engine(PG_URL)

    @event.listens_for(engine, "connect")
    def _set_search_path(dbapi_conn, _record):  # pragma: no cover - exercised only with a PG URL
        cur = dbapi_conn.cursor()
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{_PG_SCHEMA}"')
        cur.execute(f'SET search_path TO "{_PG_SCHEMA}"')
        cur.close()

    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        with engine.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{_PG_SCHEMA}" CASCADE')
        engine.dispose()


@requires_pg
def test_pg_list_is_tenant_scoped_and_visible_only(pg_session_factory):
    """On real Postgres: a business sees only its OWN visible suggestions — an expired / sendable row is
    omitted, and another tenant's rows are never returned."""
    assert make_url(PG_URL).get_backend_name() == "postgresql"
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with pg_session_factory() as session:
        session.add_all(
            [
                _suggestion(BIZ_A, "a-visible", match_score=0.9, now=now),
                _suggestion(BIZ_A, "a-expired", expires_in_days=-1, now=now),
                _suggestion(
                    BIZ_A, "a-sendable", visibility=SuggestionVisibility.SENDABLE.value, now=now
                ),
                _suggestion(BIZ_B, "b-visible", now=now),
            ]
        )
        session.commit()

        a = ProspectSuggestionReader(session, BIZ_A).list_visible(now=now)
        b = ProspectSuggestionReader(session, BIZ_B).list_visible(now=now)

    assert [i.subject_id for i in a.items] == ["a-visible"] and a.total == 1
    assert [i.subject_id for i in b.items] == ["b-visible"] and b.total == 1
