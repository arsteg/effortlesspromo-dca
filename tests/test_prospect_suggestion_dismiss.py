"""DCA-151 — dismiss a prospect suggestion PRIVATELY (a per-business relevance-feedback mutation).

Two layers, matching the DCA-150 suite (``tests/test_prospect_suggestions_api.py``):

- **Service unit tests** (in-memory SQLite) pin
  :meth:`~app.services.prospect_suggestion.ProspectSuggestionDismisser.dismiss`: a dismissal flips the
  row to the ``dismissed`` status (so it drops out of the DCA-150 VISIBLE list), stamps the feedback
  reason/actor and stages exactly one atomic audit entry; dismissing is idempotent (a re-dismiss is a
  no-op with no second audit); an unauthorized caller (a read-only viewer) is refused with NO state
  change; an unknown / cross-tenant id reads as not-found; and crucially a dismissal is PRIVATE — it
  never touches another business's independent suggestion for the same subject.
- **HTTP tests** (``TestClient`` with the end-user auth contract wired) prove the authorization gate and
  tenant isolation over the real API: an acting owner can dismiss (200); a read-only viewer cannot
  (403); a non-member / platform operator cannot (403); an unauthenticated request is 401; an unknown
  id is 404; the dismissal is idempotent (and replays verbatim with an ``Idempotency-Key``); and one
  business dismissing its suggestion leaves another business's identical suggestion untouched.

SQLite is sufficient — the behaviour is a tenant-scoped status mutation; the RLS / migration paths are
covered by ``tests/test_rls.py`` / ``tests/test_migrations.py`` (which pin the ``prospect_suggestions``
table + its DCA-151 dismissal columns), and a PG-gated test below proves the dismissal is tenant-scoped
over real Postgres RLS when ``DCA_TEST_DATABASE_URL`` is set. A dismissal writes only this one row + its
atomic audit entry; it spends nothing and sends nothing.
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
from app.db.models import AuditEvent, ProspectSuggestion, SuggestionVisibility
from app.identity.fake import BUSINESS_A, BUSINESS_B, USER_ALICE, USER_BOB, USER_CAROL
from app.main import create_app
from app.services.prospect_suggestion import (
    AUDIT_ACTION_DISMISS,
    DISMISSED_STATUS,
    REASON_SUGGESTION_NOT_FOUND,
    ProspectSuggestionDismissAuthorizationError,
    ProspectSuggestionDismissError,
    ProspectSuggestionDismisser,
    ProspectSuggestionReader,
    SuggestionDismisser,
)
from tests.conftest import make_settings

BIZ_A = "11111111-1111-1111-1111-111111111151"
BIZ_B = "22222222-2222-2222-2222-222222222251"

# Role tuples the fake identity would resolve for these synthetic subjects.
ADMIN_ROLES = ("owner",)
VIEWER_ROLES = ("viewer",)


def _suggestion(
    business_id: str,
    subject_id: str,
    *,
    run_id: str = "run-151",
    status: str = "suggested",
    match_score: float = 0.5,
    now: datetime | None = None,
) -> ProspectSuggestion:
    moment = now or datetime.now(UTC)
    return ProspectSuggestion(
        business_id=business_id,
        run_id=run_id,
        subject_type="organization",
        subject_id=subject_id,
        visibility=SuggestionVisibility.RESEARCH_ONLY.value,
        match_score=match_score,
        rationale=f"ICP fit {match_score:.2f}",
        scored_icp_version=1,
        schema_version=1,
        status=status,
        provenance={"source": "prospect_suggestion", "matchedDimensions": ["industry"]},
        created_at=moment,
        expires_at=moment + timedelta(days=30),
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


def _admin(subject: str = "00000000-0000-4000-9000-0000000a0151a") -> SuggestionDismisser:
    return SuggestionDismisser(subject=subject, roles=ADMIN_ROLES)


def _viewer(subject: str = "00000000-0000-4000-9000-0000000a0151b") -> SuggestionDismisser:
    return SuggestionDismisser(subject=subject, roles=VIEWER_ROLES)


def test_dismiss_removes_from_visible_list_and_audits(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        s = _suggestion(BIZ_A, "subject-1", now=now)
        session.add(s)
        session.commit()
        sid = s.id

        # Visible before the dismissal.
        before = ProspectSuggestionReader(session, BIZ_A).list_visible(now=now)
        assert [i.subject_id for i in before.items] == ["subject-1"]

        result = ProspectSuggestionDismisser(session, BIZ_A).dismiss(
            sid, _admin(), reason="not a fit for our ICP", now=now
        )
        session.commit()

        after = ProspectSuggestionReader(session, BIZ_A).list_visible(now=now)
        row = session.get(ProspectSuggestion, sid)
        audits = session.query(AuditEvent).filter_by(action=AUDIT_ACTION_DISMISS).all()

    assert result.already_dismissed is False
    assert row.status == DISMISSED_STATUS
    assert row.dismissed_at == now
    assert row.dismissal_reason == "not a fit for our ICP"
    assert row.dismissed_by == _admin().subject
    # It is no longer VISIBLE to the DCA-150 reader.
    assert after.items == [] and after.total == 0
    # Exactly one atomic audit entry, carrying only opaque references (no contact value).
    assert len(audits) == 1
    refs = audits[0].references
    assert refs["prospectSuggestionId"] == sid
    assert refs["subjectId"] == "subject-1"
    assert refs["hasReason"] is True


def test_dismiss_is_idempotent(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        s = _suggestion(BIZ_A, "subject-1", now=now)
        session.add(s)
        session.commit()
        sid = s.id
        dismisser = ProspectSuggestionDismisser(session, BIZ_A)

        first = dismisser.dismiss(sid, _admin(), reason="first reason", now=now)
        session.commit()
        # A re-dismiss (even with a different reason) is a no-op: no field re-written, no second audit.
        second = dismisser.dismiss(
            sid, _admin(), reason="different reason", now=now + timedelta(days=1)
        )
        session.commit()

        audits = session.query(AuditEvent).filter_by(action=AUDIT_ACTION_DISMISS).count()
        row = session.get(ProspectSuggestion, sid)

    assert first.already_dismissed is False
    assert second.already_dismissed is True
    assert row.dismissal_reason == "first reason"  # unchanged by the replay
    assert row.dismissed_at == now  # unchanged by the replay
    assert audits == 1  # only the first dismissal audited


def test_unauthorized_viewer_refused_with_no_state_change(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        s = _suggestion(BIZ_A, "subject-1", now=now)
        session.add(s)
        session.commit()
        sid = s.id

        with pytest.raises(ProspectSuggestionDismissAuthorizationError):
            ProspectSuggestionDismisser(session, BIZ_A).dismiss(sid, _viewer(), now=now)

        row = session.get(ProspectSuggestion, sid)
        audits = session.query(AuditEvent).filter_by(action=AUDIT_ACTION_DISMISS).count()

    assert row.status == "suggested"  # untouched
    assert row.dismissed_at is None
    assert audits == 0  # the denial staged nothing


def test_unknown_or_cross_tenant_id_is_not_found(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        # A suggestion owned by B — A must NOT be able to dismiss it by id.
        s_b = _suggestion(BIZ_B, "subject-b", now=now)
        session.add(s_b)
        session.commit()
        b_id = s_b.id

        with pytest.raises(ProspectSuggestionDismissError) as unknown:
            ProspectSuggestionDismisser(session, BIZ_A).dismiss("no-such-id", _admin(), now=now)
        assert unknown.value.code == REASON_SUGGESTION_NOT_FOUND

        with pytest.raises(ProspectSuggestionDismissError) as cross:
            ProspectSuggestionDismisser(session, BIZ_A).dismiss(b_id, _admin(), now=now)
        assert cross.value.code == REASON_SUGGESTION_NOT_FOUND

        # B's row is untouched by A's failed cross-tenant attempt.
        row_b = session.get(ProspectSuggestion, b_id)
    assert row_b.status == "suggested"


def test_dismissal_is_private_and_does_not_affect_another_business(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        # A and B each have an INDEPENDENT suggestion about the SAME subject.
        a = _suggestion(BIZ_A, "shared-subject", now=now)
        b = _suggestion(BIZ_B, "shared-subject", now=now)
        session.add_all([a, b])
        session.commit()
        a_id, b_id = a.id, b.id

        ProspectSuggestionDismisser(session, BIZ_A).dismiss(a_id, _admin(), now=now)
        session.commit()

        a_visible = ProspectSuggestionReader(session, BIZ_A).list_visible(now=now)
        b_visible = ProspectSuggestionReader(session, BIZ_B).list_visible(now=now)
        row_b = session.get(ProspectSuggestion, b_id)

    # A's list dropped it; B's identical suggestion is wholly unaffected.
    assert a_visible.items == []
    assert [i.subject_id for i in b_visible.items] == ["shared-subject"]
    assert row_b.status == "suggested" and row_b.dismissed_at is None


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


def _seed(client: TestClient, business_id: str, suggestion: ProspectSuggestion) -> str:
    factory = client.app.state.session_factory
    with factory() as session:
        suggestion.business_id = business_id
        session.add(suggestion)
        session.commit()
        return suggestion.id


def _status_of(client: TestClient, suggestion_id: str) -> str:
    factory = client.app.state.session_factory
    with factory() as session:
        return session.get(ProspectSuggestion, suggestion_id).status


def test_http_owner_can_dismiss_and_it_leaves_the_visible_list(client):
    sid = _seed(client, BUSINESS_A, _suggestion(BUSINESS_A, "visible", match_score=0.7))
    resp = client.post(
        f"/v1/tenants/{BUSINESS_A}/prospect-suggestions/{sid}/dismiss",
        json={"reason": "already a customer"},
        headers=_bearer(_mint(subject=USER_ALICE)),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dismissed"] is True
    assert body["status"] == DISMISSED_STATUS
    assert body["alreadyDismissed"] is False
    assert body["dismissalReason"] == "already a customer"
    # No contact value leaks into the response.
    assert not any(k.lower() in {"email", "phone", "contact"} for k in body)

    # It is gone from the DCA-150 visible list.
    listed = client.get(
        f"/v1/tenants/{BUSINESS_A}/prospect-suggestions",
        headers=_bearer(_mint(subject=USER_ALICE)),
    ).json()
    assert listed["total"] == 0


def test_http_viewer_cannot_dismiss(client):
    # Carol is an active viewer in A — she holds candidate.read but NOT candidate.dismiss.
    sid = _seed(client, BUSINESS_A, _suggestion(BUSINESS_A, "visible"))
    resp = client.post(
        f"/v1/tenants/{BUSINESS_A}/prospect-suggestions/{sid}/dismiss",
        json={},
        headers=_bearer(_mint(subject=USER_CAROL)),
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    assert _status_of(client, sid) == "suggested"  # denial changed nothing


def test_http_platform_operator_cannot_dismiss(client):
    sid = _seed(client, BUSINESS_A, _suggestion(BUSINESS_A, "visible"))
    resp = client.post(
        f"/v1/tenants/{BUSINESS_A}/prospect-suggestions/{sid}/dismiss",
        json={},
        headers=_bearer(_mint(subject=OPERATOR, roles=["platform_operator"])),
    )
    assert resp.status_code == 403
    assert _status_of(client, sid) == "suggested"


def test_http_unauthenticated_request_is_401(client):
    sid = _seed(client, BUSINESS_A, _suggestion(BUSINESS_A, "visible"))
    resp = client.post(f"/v1/tenants/{BUSINESS_A}/prospect-suggestions/{sid}/dismiss", json={})
    assert resp.status_code == 401


def test_http_unknown_suggestion_is_404(client):
    resp = client.post(
        f"/v1/tenants/{BUSINESS_A}/prospect-suggestions/no-such-id/dismiss",
        json={},
        headers=_bearer(_mint(subject=USER_ALICE)),
    )
    assert resp.status_code == 404
    # The envelope `code` is status-derived (`not_found`); the stable reason travels in the message.
    assert resp.json()["error"]["code"] == "not_found"
    assert REASON_SUGGESTION_NOT_FOUND in resp.text


def test_http_cross_tenant_suggestion_is_not_found(client):
    # A suggestion owned by B cannot be dismissed by a B-member calling A's path (no A membership → 403),
    # nor can an A-admin reach B's row by id (resolver scopes to the path business → 404).
    sid_b = _seed(client, BUSINESS_B, _suggestion(BUSINESS_B, "b-only"))
    # Alice (A owner) tries to dismiss B's suggestion id via A's path: bound to A → not found.
    resp = client.post(
        f"/v1/tenants/{BUSINESS_A}/prospect-suggestions/{sid_b}/dismiss",
        json={},
        headers=_bearer(_mint(subject=USER_ALICE)),
    )
    assert resp.status_code == 404
    assert _status_of(client, sid_b) == "suggested"  # B's row untouched


def test_http_dismiss_is_idempotent(client):
    sid = _seed(client, BUSINESS_A, _suggestion(BUSINESS_A, "visible"))
    headers = _bearer(_mint(subject=USER_ALICE))
    first = client.post(
        f"/v1/tenants/{BUSINESS_A}/prospect-suggestions/{sid}/dismiss",
        json={"reason": "first"},
        headers=headers,
    )
    second = client.post(
        f"/v1/tenants/{BUSINESS_A}/prospect-suggestions/{sid}/dismiss",
        json={"reason": "second"},
        headers=headers,
    )
    assert first.status_code == 200 and first.json()["alreadyDismissed"] is False
    assert second.status_code == 200 and second.json()["alreadyDismissed"] is True
    # The reason from the first dismissal stands; the replay re-wrote nothing.
    assert second.json()["dismissalReason"] == "first"


def test_http_idempotency_key_replays_verbatim(client):
    sid = _seed(client, BUSINESS_A, _suggestion(BUSINESS_A, "visible"))
    headers = {**_bearer(_mint(subject=USER_ALICE)), "Idempotency-Key": "dca-151-key-1"}
    first = client.post(
        f"/v1/tenants/{BUSINESS_A}/prospect-suggestions/{sid}/dismiss",
        json={"reason": "dupe-safe"},
        headers=headers,
    )
    replay = client.post(
        f"/v1/tenants/{BUSINESS_A}/prospect-suggestions/{sid}/dismiss",
        json={"reason": "dupe-safe"},
        headers=headers,
    )
    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.headers.get("Idempotent-Replay") == "true"
    assert replay.json() == first.json()


def test_http_dismissal_does_not_affect_another_business(client):
    # A and B each have an independent suggestion for the SAME subject.
    sid_a = _seed(client, BUSINESS_A, _suggestion(BUSINESS_A, "shared"))
    sid_b = _seed(client, BUSINESS_B, _suggestion(BUSINESS_B, "shared"))

    dismissed = client.post(
        f"/v1/tenants/{BUSINESS_A}/prospect-suggestions/{sid_a}/dismiss",
        json={},
        headers=_bearer(_mint(subject=USER_ALICE)),
    )
    assert dismissed.status_code == 200

    # B's list still shows its identical suggestion (Bob is an active admin in B).
    b_list = client.get(
        f"/v1/tenants/{BUSINESS_B}/prospect-suggestions",
        headers=_bearer(_mint(subject=USER_BOB)),
    ).json()
    assert [i["subjectId"] for i in b_list["items"]] == ["shared"]
    assert _status_of(client, sid_b) == "suggested"


# --------------------------------------------------------------------------------------------------
# Postgres-backed test — disposable PostgreSQL (real RLS + transactions, not SQLite)
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)
_PG_SCHEMA = "dca_151_test"


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
def test_pg_dismissal_is_tenant_scoped_and_private(pg_session_factory):
    """On real Postgres: dismissing A's suggestion drops it from A's visible list while B's independent
    suggestion for the same subject is wholly unaffected."""
    assert make_url(PG_URL).get_backend_name() == "postgresql"
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with pg_session_factory() as session:
        a = _suggestion(BIZ_A, "shared-subject", now=now)
        b = _suggestion(BIZ_B, "shared-subject", now=now)
        session.add_all([a, b])
        session.commit()
        a_id, b_id = a.id, b.id

        ProspectSuggestionDismisser(session, BIZ_A).dismiss(a_id, _admin(), reason="pg", now=now)
        session.commit()

        a_visible = ProspectSuggestionReader(session, BIZ_A).list_visible(now=now)
        b_visible = ProspectSuggestionReader(session, BIZ_B).list_visible(now=now)
        row_b = session.get(ProspectSuggestion, b_id)

    assert a_visible.items == [] and a_visible.total == 0
    assert [i.subject_id for i in b_visible.items] == ["shared-subject"]
    assert row_b.status == "suggested"
