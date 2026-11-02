"""DCA-152 — preview an audience selection (a tenant-scoped, read-only, non-mutating projection).

Two layers, matching the DCA-150 candidate-list suite:

- **Service unit tests** (in-memory SQLite) pin
  :meth:`~app.services.audience_selection_preview.AudienceSelectionPreviewer.preview`: every selected
  suggestion is classified into exactly one bucket; research-only organizations are separated from the
  (currently empty) activatable set; exclusions are tallied by reason (freshness / dismissed / rights /
  not-found); an unknown / cross-tenant id is a ``not_found`` line (never another tenant's row); the
  selection is de-duplicated and bounded; and the preview writes NOTHING (no rows, no audit).
- **HTTP tests** (``TestClient`` with the end-user auth contract wired, like
  ``tests/test_prospect_suggestions_api.py``) prove the authorization gate and tenant isolation over the
  real API: an ordinary viewer (``candidate.read``) can preview; a non-member (platform operator) cannot
  (403); an unauthenticated request is 401; and one business's member can never preview another's
  candidates — a cross-tenant id reads as not-found, never leaked.

SQLite is sufficient — the behaviour is a pure read over tenant-scoped rows; the RLS / migration paths
for ``prospect_suggestions`` are covered by ``tests/test_rls.py`` / ``tests/test_migrations.py``. A
PG-gated test proves tenant isolation over real Postgres RLS when ``DCA_TEST_DATABASE_URL`` is set.
Previewing writes nothing, spends nothing and sends nothing — and creates NO audience membership.
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
from app.services.audience_selection_preview import (
    EXCLUSION_DISMISSED,
    EXCLUSION_EXPIRED,
    EXCLUSION_NOT_FOUND,
    EXCLUSION_NOT_REVIEWABLE,
    EXCLUSION_RESEARCH_ONLY,
    MAX_SELECTION_SIZE,
    AudienceSelectionPreviewer,
)
from tests.conftest import make_settings

BIZ_A = "11111111-1111-1111-1111-111111111152"
BIZ_B = "22222222-2222-2222-2222-222222222252"


def _suggestion(
    business_id: str,
    subject_id: str,
    *,
    run_id: str = "run-152",
    status: str = "suggested",
    visibility: str = SuggestionVisibility.RESEARCH_ONLY.value,
    match_score: float = 0.5,
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
        provenance={"source": "prospect_suggestion", "matchedDimensions": ["industry"]},
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


def test_separates_research_only_from_activatable_and_tallies_exclusions(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        rows = [
            _suggestion(BIZ_A, "research", now=now),  # research_only → excluded
            _suggestion(BIZ_A, "expired", expires_in_days=-1, now=now),  # freshness → excluded
            _suggestion(BIZ_A, "dismissed", status="dismissed", now=now),  # dismissed → excluded
            _suggestion(BIZ_A, "accepted", status="accepted", now=now),  # not_reviewable → excluded
            # a visible, unexpired, reviewable SENDABLE row → the (disabled future) activatable bucket
            _suggestion(BIZ_A, "sendable", visibility=SuggestionVisibility.SENDABLE.value, now=now),
        ]
        session.add_all(rows)
        session.commit()
        ids = [r.id for r in rows]
        sendable_id = rows[-1].id

        preview = AudienceSelectionPreviewer(session, BIZ_A).preview(
            ids, intended_use="Q4 outreach", channel="email", now=now
        )

    assert preview.selected == 5
    assert preview.found == 5
    assert preview.not_found == 0
    # Research-only organizations are SEPARATED from the activatable set.
    assert preview.research_only == 1
    assert preview.activatable == 1
    assert preview.excluded == {
        EXCLUSION_RESEARCH_ONLY: 1,
        EXCLUSION_EXPIRED: 1,
        EXCLUSION_DISMISSED: 1,
        EXCLUSION_NOT_REVIEWABLE: 1,
    }
    # The one activatable line is exactly the sendable row; everything else is excluded with a reason.
    activatable = [line for line in preview.lines if line.activatable]
    assert [line.suggestion_id for line in activatable] == [sendable_id]
    assert activatable[0].exclusion_reason is None and activatable[0].sendable is True
    # The cost projection is a would-be figure only; no billing/membership is committed.
    assert preview.estimated_chargeable_contacts == 1
    assert preview.billing_committed is False
    assert preview.membership_created is False


def test_default_research_only_selection_has_empty_activatable_audience(session_factory):
    # The real-world case: every suggestion the pipeline produces is research-only → NOTHING activatable.
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        rows = [_suggestion(BIZ_A, f"org-{i}", now=now) for i in range(3)]
        session.add_all(rows)
        session.commit()
        ids = [r.id for r in rows]
        preview = AudienceSelectionPreviewer(session, BIZ_A).preview(ids, now=now)

    assert preview.found == 3
    assert preview.activatable == 0
    assert preview.research_only == 3
    assert preview.excluded == {EXCLUSION_RESEARCH_ONLY: 3}
    assert preview.estimated_chargeable_contacts == 0
    assert all(line.activatable is False for line in preview.lines)


def test_unknown_and_cross_tenant_ids_read_as_not_found(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        a_row = _suggestion(BIZ_A, "a-own", now=now)
        b_row = _suggestion(BIZ_B, "b-own", now=now)
        session.add_all([a_row, b_row])
        session.commit()
        b_id = b_row.id

        # A previews: its own id (found), a nonexistent id, and B's id — the last two are not-found and
        # never leak B's row.
        preview = AudienceSelectionPreviewer(session, BIZ_A).preview(
            [a_row.id, "does-not-exist", b_id], now=now
        )

    by_id = {line.suggestion_id: line for line in preview.lines}
    assert by_id[a_row.id].found is True
    assert by_id["does-not-exist"].found is False
    assert by_id["does-not-exist"].exclusion_reason == EXCLUSION_NOT_FOUND
    # B's id is invisible to A — reported as not-found, with no subject/visibility leaked.
    assert by_id[b_id].found is False
    assert by_id[b_id].exclusion_reason == EXCLUSION_NOT_FOUND
    assert by_id[b_id].subject_id is None and by_id[b_id].visibility is None
    assert preview.not_found == 2 and preview.found == 1


def test_selection_is_deduplicated_and_bounded(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        row = _suggestion(BIZ_A, "org", now=now)
        session.add(row)
        session.commit()
        # Duplicate ids collapse to one line; blanks are ignored.
        deduped = AudienceSelectionPreviewer(session, BIZ_A).preview(
            [row.id, row.id, "", "  ", row.id], now=now
        )
        # A selection beyond the cap is bounded.
        bounded = AudienceSelectionPreviewer(session, BIZ_A).preview(
            [f"id-{i}" for i in range(MAX_SELECTION_SIZE + 50)], now=now
        )

    assert deduped.selected == 1
    assert [line.suggestion_id for line in deduped.lines] == [row.id]
    assert bounded.selected == MAX_SELECTION_SIZE


def test_preview_writes_nothing(session_factory):
    from app.db.models import AuditEvent

    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        row = _suggestion(BIZ_A, "org", now=now)
        session.add(row)
        session.commit()
        row_id = row.id
    with session_factory() as session:
        audits_before = session.query(AuditEvent).count()
        rows_before = session.query(ProspectSuggestion).count()
        AudienceSelectionPreviewer(session, BIZ_A).preview([row_id], now=now)
        session.commit()
    with session_factory() as session:
        # No audience/membership table exists AND nothing was written to the rows we have.
        assert session.query(AuditEvent).count() == audits_before
        assert session.query(ProspectSuggestion).count() == rows_before


# --------------------------------------------------------------------------------------------------
# HTTP tests (TestClient) — the real service API with the end-user auth contract wired
# --------------------------------------------------------------------------------------------------

ISSUER = "https://issuer.dca.test"
AUDIENCE = "dca-api"
SECRET = "dca-test-signing-secret-value"  # synthetic HS256 key, not a real credential
OPERATOR = "00000000-0000-4000-9000-00000090e752"  # platform operator; no business membership


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


def _seed(client: TestClient, business_id: str, *suggestions: ProspectSuggestion) -> list[str]:
    factory = client.app.state.session_factory
    ids: list[str] = []
    with factory() as session:
        for s in suggestions:
            s.business_id = business_id
            session.add(s)
        session.commit()
        ids = [s.id for s in suggestions]
    return ids


def test_http_viewer_can_preview_selection(client):
    ids = _seed(
        client,
        BUSINESS_A,
        _suggestion(BUSINESS_A, "research", match_score=0.7),
        _suggestion(BUSINESS_A, "expired", expires_in_days=-1),
    )
    resp = client.post(
        f"/v1/tenants/{BUSINESS_A}/audience-selection/preview",
        headers=_bearer(_mint(subject=USER_CAROL)),
        json={"suggestionIds": ids, "intendedUse": "Q4 outreach", "channel": "email"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["selected"] == 2 and body["found"] == 2 and body["notFound"] == 0
    assert body["activatable"] == 0
    assert body["researchOnly"] == 1
    assert body["excluded"] == {EXCLUSION_RESEARCH_ONLY: 1, EXCLUSION_EXPIRED: 1}
    # Non-mutating projection: no billing committed, no membership created.
    assert body["billingCommitted"] is False
    assert body["membershipCreated"] is False
    assert body["estimatedChargeableContacts"] == 0
    # Rights-aware: no contact value is exposed on any line.
    for line in body["lines"]:
        assert not any(k.lower() in {"email", "phone", "contact"} for k in line)


def test_http_platform_operator_cannot_preview(client):
    ids = _seed(client, BUSINESS_A, _suggestion(BUSINESS_A, "research"))
    resp = client.post(
        f"/v1/tenants/{BUSINESS_A}/audience-selection/preview",
        headers=_bearer(_mint(subject=OPERATOR, roles=["platform_operator"])),
        json={"suggestionIds": ids},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_http_unauthenticated_request_is_401(client):
    resp = client.post(
        f"/v1/tenants/{BUSINESS_A}/audience-selection/preview", json={"suggestionIds": []}
    )
    assert resp.status_code == 401


def test_http_unknown_body_field_is_422(client):
    resp = client.post(
        f"/v1/tenants/{BUSINESS_A}/audience-selection/preview",
        headers=_bearer(_mint(subject=USER_ALICE)),
        json={"suggestionIds": [], "unexpected": "x"},
    )
    assert resp.status_code == 422


def test_http_tenant_cannot_preview_another_businesss_candidates(client):
    a_ids = _seed(client, BUSINESS_A, _suggestion(BUSINESS_A, "a-only", match_score=0.9))
    b_ids = _seed(client, BUSINESS_B, _suggestion(BUSINESS_B, "b-only", match_score=0.8))

    # Alice (member of A) previews A's own id plus B's id — B's reads as not-found, never leaked.
    resp = client.post(
        f"/v1/tenants/{BUSINESS_A}/audience-selection/preview",
        headers=_bearer(_mint(subject=USER_ALICE)),
        json={"suggestionIds": a_ids + b_ids},
    )
    assert resp.status_code == 200
    body = resp.json()
    by_id = {line["suggestionId"]: line for line in body["lines"]}
    assert by_id[a_ids[0]]["found"] is True
    assert by_id[b_ids[0]]["found"] is False
    assert by_id[b_ids[0]]["exclusionReason"] == EXCLUSION_NOT_FOUND
    assert by_id[b_ids[0]]["subjectId"] is None

    # Bob's A membership was revoked → he cannot preview A's candidates (403), never another tenant's.
    cross = client.post(
        f"/v1/tenants/{BUSINESS_A}/audience-selection/preview",
        headers=_bearer(_mint(subject=USER_BOB)),
        json={"suggestionIds": a_ids},
    )
    assert cross.status_code == 403


# --------------------------------------------------------------------------------------------------
# Postgres-backed test — disposable PostgreSQL (real RLS + transactions, not SQLite)
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)
_PG_SCHEMA = "dca_152_test"


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
def test_pg_preview_is_tenant_scoped(pg_session_factory):
    """On real Postgres: a business previews only its OWN suggestions — another tenant's id reads as
    not-found, never returned, and the preview writes nothing."""
    assert make_url(PG_URL).get_backend_name() == "postgresql"
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with pg_session_factory() as session:
        a_row = _suggestion(BIZ_A, "a-own", now=now)
        b_row = _suggestion(BIZ_B, "b-own", now=now)
        session.add_all([a_row, b_row])
        session.commit()
        a_id, b_id = a_row.id, b_row.id

        preview = AudienceSelectionPreviewer(session, BIZ_A).preview([a_id, b_id], now=now)

    by_id = {line.suggestion_id: line for line in preview.lines}
    assert by_id[a_id].found is True
    assert by_id[b_id].found is False
    assert by_id[b_id].exclusion_reason == EXCLUSION_NOT_FOUND
