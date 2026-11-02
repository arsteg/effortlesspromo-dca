"""DCA-153 — record ONE audience-acceptance intent over a prospect suggestion.

Two layers, matching the DCA-151 dismiss suite (``tests/test_prospect_suggestion_dismiss.py``):

- **Service unit tests** (in-memory SQLite) pin
  :meth:`~app.services.acceptance_intent.AcceptanceIntentRecorder.accept`: accepting a reviewable
  suggestion stages exactly ONE ``pending_sync`` intent, ONE atomic audit entry AND ONE durable
  outbox sync REQUEST — with the CAPTURED visibility + selection versions and the load-bearing
  invariants (sendable / synced / sent / spent / consentConferred / membershipCreated = False);
  accepting is idempotent (a re-accept stages no duplicate / no second audit / no second outbox); a
  transaction failure commits NEITHER the intent NOR its audit/outbox (the atomic-unit rule); an
  unauthorized caller (a read-only viewer) is refused with NO state change; an unknown / cross-tenant
  id reads as not-found; a dismissed / expired suggestion is not acceptable; and an acceptance is
  tenant-private — it never touches another business's independent suggestion for the same subject.
- **HTTP tests** (``TestClient`` with the end-user auth contract wired) prove the authorization gate and
  tenant isolation over the real API: an acting owner can accept (200); a read-only viewer cannot
  (403); a non-member / platform operator cannot (403); an unauthenticated request is 401; an unknown
  id is 404; the accept is idempotent (and replays verbatim with an ``Idempotency-Key``); and one
  business accepting its suggestion leaves another business's state untouched.

SQLite is sufficient here — the behaviour is a tenant-scoped multi-row insert; the RLS / migration
paths are covered by ``tests/test_rls.py`` / ``tests/test_migrations.py`` (which pin the
``acceptance_intents`` table + its 0068 create branch), and a PG-gated test below proves the acceptance
is tenant-scoped and atomic over real Postgres when ``DCA_TEST_DATABASE_URL`` is set. Recording an
acceptance writes only the intent + its atomic audit + its pending outbox request; it syncs nothing,
sends nothing, spends nothing and confers no consent.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import (
    AcceptanceIntent,
    AcceptanceIntentStatus,
    AuditEvent,
    OutboxMessage,
    OutboxStatus,
    ProspectSuggestion,
    SuggestionVisibility,
)
from app.identity.fake import BUSINESS_A, BUSINESS_B, USER_ALICE, USER_CAROL
from app.main import create_app
from app.services.acceptance_intent import (
    AUDIT_ACTION_ACCEPT,
    OUTBOX_EVENT_ACCEPTANCE,
    REASON_SUGGESTION_NOT_ACCEPTABLE,
    REASON_SUGGESTION_NOT_FOUND,
    AcceptanceIntentActor,
    AcceptanceIntentAuthorizationError,
    AcceptanceIntentError,
    AcceptanceIntentRecorder,
)
from tests.conftest import make_settings

BIZ_A = "11111111-1111-1111-1111-111111111153"
BIZ_B = "22222222-2222-2222-2222-222222222253"

ADMIN_ROLES = ("owner",)
VIEWER_ROLES = ("viewer",)


def _suggestion(
    business_id: str,
    subject_id: str,
    *,
    run_id: str = "run-153",
    status: str = "suggested",
    visibility: str | None = None,
    match_score: float = 0.6,
    scored_icp_version: int | None = 2,
    schema_version: int = 1,
    expires_in_days: int | None = 30,
    now: datetime | None = None,
) -> ProspectSuggestion:
    moment = now or datetime.now(UTC)
    expires = None if expires_in_days is None else moment + timedelta(days=expires_in_days)
    return ProspectSuggestion(
        business_id=business_id,
        run_id=run_id,
        subject_type="organization",
        subject_id=subject_id,
        visibility=visibility or SuggestionVisibility.RESEARCH_ONLY.value,
        match_score=match_score,
        rationale=f"ICP fit {match_score:.2f}",
        scored_icp_version=scored_icp_version,
        schema_version=schema_version,
        status=status,
        provenance={"source": "prospect_suggestion", "matchedDimensions": ["industry"]},
        created_at=moment,
        expires_at=expires,
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


def _admin(subject: str = "00000000-0000-4000-9000-0000000a0153a") -> AcceptanceIntentActor:
    return AcceptanceIntentActor(subject=subject, roles=ADMIN_ROLES)


def _viewer(subject: str = "00000000-0000-4000-9000-0000000a0153b") -> AcceptanceIntentActor:
    return AcceptanceIntentActor(subject=subject, roles=VIEWER_ROLES)


def _counts(session) -> tuple[int, int, int]:
    intents = session.query(AcceptanceIntent).count()
    audits = session.query(AuditEvent).filter_by(action=AUDIT_ACTION_ACCEPT).count()
    outbox = session.query(OutboxMessage).filter_by(event_type=OUTBOX_EVENT_ACCEPTANCE).count()
    return intents, audits, outbox


def test_accept_creates_pending_sync_intent_audit_and_outbox(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        s = _suggestion(BIZ_A, "subject-1", scored_icp_version=7, schema_version=1, now=now)
        session.add(s)
        session.commit()
        sid = s.id

        result = AcceptanceIntentRecorder(session, BIZ_A).accept(sid, _admin(), now=now)
        session.commit()

        intent = session.query(AcceptanceIntent).one()
        audit = session.query(AuditEvent).filter_by(action=AUDIT_ACTION_ACCEPT).one()
        outbox = session.query(OutboxMessage).filter_by(event_type=OUTBOX_EVENT_ACCEPTANCE).one()

    assert result.already_accepted is False
    # One pending_sync intent, with CAPTURED visibility + selection versions.
    assert intent.status == AcceptanceIntentStatus.PENDING_SYNC.value
    assert intent.suggestion_id == sid
    assert intent.visibility == SuggestionVisibility.RESEARCH_ONLY.value
    assert intent.scored_icp_version == 7  # captured targeting version
    assert intent.suggestion_schema_version == 1  # captured suggestion contract version
    assert intent.accepted_by == _admin().subject
    assert result.sendable is False
    # The load-bearing invariants are pinned on the provenance.
    prov = intent.provenance
    assert prov["accepted"] is True
    assert prov["sendable"] is False
    assert prov["synced"] is False
    assert prov["sent"] is False
    assert prov["spent"] is False
    assert prov["consentConferred"] is False
    assert prov["membershipCreated"] is False
    # Exactly one atomic audit entry, carrying only opaque references (no contact value).
    refs = audit.references
    assert refs["prospectSuggestionId"] == sid
    assert refs["acceptanceIntentId"] == intent.id
    assert refs["consentConferred"] is False
    assert not any(k.lower() in {"email", "phone", "contact"} for k in refs)
    # One durable outbox sync REQUEST — PENDING, never delivered here; joined to the intent by event id.
    assert outbox.status == OutboxStatus.PENDING.value
    assert outbox.event_id == intent.outbox_event_id
    assert outbox.delivered_at is None


def test_accept_is_idempotent(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        s = _suggestion(BIZ_A, "subject-1", now=now)
        session.add(s)
        session.commit()
        sid = s.id
        recorder = AcceptanceIntentRecorder(session, BIZ_A)

        first = recorder.accept(sid, _admin(), now=now)
        session.commit()
        # A re-accept is a no-op: no duplicate intent, no second audit, no second outbox request.
        second = recorder.accept(sid, _admin(), now=now + timedelta(days=1))
        session.commit()

        counts = _counts(session)
        intent_id_after = session.query(AcceptanceIntent).one().id

    assert first.already_accepted is False
    assert second.already_accepted is True
    assert second.intent.id == first.intent.id == intent_id_after
    assert counts == (1, 1, 1)  # exactly one intent, one audit, one outbox after the replay


def test_transaction_failure_commits_neither_intent_nor_audit_nor_outbox(session_factory):
    """A genuine commit failure (a duplicate (business_id, suggestion_id) staged alongside the accept)
    rolls the WHOLE unit of work back — the intent, its audit entry and its outbox request all vanish."""
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        s = _suggestion(BIZ_A, "subject-1", now=now)
        session.add(s)
        session.commit()
        sid = s.id

        # Stage the accept (intent + audit + outbox), then stage a CONFLICTING duplicate intent for the
        # same suggestion so the commit violates the uq_acceptance_intent_suggestion constraint.
        AcceptanceIntentRecorder(session, BIZ_A).accept(sid, _admin(), now=now)
        session.add(
            AcceptanceIntent(
                business_id=BIZ_A,
                suggestion_id=sid,
                run_id="run-153",
                subject_type="organization",
                subject_id="subject-1",
                visibility=SuggestionVisibility.RESEARCH_ONLY.value,
                status=AcceptanceIntentStatus.PENDING_SYNC.value,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        counts = _counts(session)
        # The suggestion (committed earlier) survives; the failed transaction left nothing else.
        suggestion_still = session.query(ProspectSuggestion).count()

    assert counts == (0, 0, 0)  # neither intent, nor audit, nor outbox committed
    assert suggestion_still == 1


def test_unauthorized_viewer_refused_with_no_state_change(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        s = _suggestion(BIZ_A, "subject-1", now=now)
        session.add(s)
        session.commit()
        sid = s.id

        with pytest.raises(AcceptanceIntentAuthorizationError):
            AcceptanceIntentRecorder(session, BIZ_A).accept(sid, _viewer(), now=now)
        session.rollback()

        counts = _counts(session)

    assert counts == (0, 0, 0)  # the denial staged nothing


def test_unknown_or_cross_tenant_id_is_not_found(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        s_b = _suggestion(BIZ_B, "subject-b", now=now)
        session.add(s_b)
        session.commit()
        b_id = s_b.id

        with pytest.raises(AcceptanceIntentError) as unknown:
            AcceptanceIntentRecorder(session, BIZ_A).accept("no-such-id", _admin(), now=now)
        assert unknown.value.code == REASON_SUGGESTION_NOT_FOUND

        with pytest.raises(AcceptanceIntentError) as cross:
            AcceptanceIntentRecorder(session, BIZ_A).accept(b_id, _admin(), now=now)
        assert cross.value.code == REASON_SUGGESTION_NOT_FOUND

        counts = _counts(session)
    assert counts == (0, 0, 0)  # nothing accepted, no leak


def test_dismissed_or_expired_suggestion_is_not_acceptable(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        dismissed = _suggestion(BIZ_A, "dismissed", status="dismissed", now=now)
        expired = _suggestion(BIZ_A, "expired", expires_in_days=-1, now=now)
        session.add_all([dismissed, expired])
        session.commit()
        recorder = AcceptanceIntentRecorder(session, BIZ_A)

        with pytest.raises(AcceptanceIntentError) as d:
            recorder.accept(dismissed.id, _admin(), now=now)
        with pytest.raises(AcceptanceIntentError) as e:
            recorder.accept(expired.id, _admin(), now=now)

        counts = _counts(session)

    assert d.value.code == REASON_SUGGESTION_NOT_ACCEPTABLE
    assert e.value.code == REASON_SUGGESTION_NOT_ACCEPTABLE
    assert counts == (0, 0, 0)


def test_acceptance_is_private_and_does_not_affect_another_business(session_factory):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        a = _suggestion(BIZ_A, "shared-subject", now=now)
        b = _suggestion(BIZ_B, "shared-subject", now=now)
        session.add_all([a, b])
        session.commit()
        a_id, b_id = a.id, b.id

        AcceptanceIntentRecorder(session, BIZ_A).accept(a_id, _admin(), now=now)
        session.commit()

        # A has one intent; B (same subject, independent suggestion) has none and is untouched.
        a_intents = session.query(AcceptanceIntent).filter_by(business_id=BIZ_A).count()
        b_intents = session.query(AcceptanceIntent).filter_by(business_id=BIZ_B).count()
        row_b = session.get(ProspectSuggestion, b_id)

    assert a_intents == 1
    assert b_intents == 0
    assert row_b.status == "suggested"  # B's suggestion wholly unaffected


# --------------------------------------------------------------------------------------------------
# HTTP tests (TestClient) — the real service API with the end-user auth contract wired
# --------------------------------------------------------------------------------------------------

ISSUER = "https://issuer.dca.test"
AUDIENCE = "dca-api"
SECRET = "dca-test-signing-secret-value"  # synthetic HS256 key, not a real credential
OPERATOR = "00000000-0000-4000-9000-00000090e753"  # platform operator; no business membership


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


def _intent_count(client: TestClient, business_id: str) -> int:
    factory = client.app.state.session_factory
    with factory() as session:
        return session.query(AcceptanceIntent).filter_by(business_id=business_id).count()


def test_http_owner_can_accept(client):
    sid = _seed(client, BUSINESS_A, _suggestion(BUSINESS_A, "visible", match_score=0.7))
    resp = client.post(
        f"/v1/tenants/{BUSINESS_A}/prospect-suggestions/{sid}/accept",
        json={},
        headers=_bearer(_mint(subject=USER_ALICE)),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "pending_sync"
    assert body["alreadyAccepted"] is False
    assert body["sendable"] is False
    assert body["syncPerformed"] is False
    assert body["consentConferred"] is False
    assert body["membershipCreated"] is False
    assert body["outboxEventId"]
    # No contact value leaks into the response.
    assert not any(k.lower() in {"email", "phone", "contact"} for k in body)
    assert _intent_count(client, BUSINESS_A) == 1


def test_http_viewer_cannot_accept(client):
    # Carol is an active viewer in A — she holds candidate.read but NOT candidate.accept.
    sid = _seed(client, BUSINESS_A, _suggestion(BUSINESS_A, "visible"))
    resp = client.post(
        f"/v1/tenants/{BUSINESS_A}/prospect-suggestions/{sid}/accept",
        json={},
        headers=_bearer(_mint(subject=USER_CAROL)),
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    assert _intent_count(client, BUSINESS_A) == 0  # denial changed nothing


def test_http_platform_operator_cannot_accept(client):
    sid = _seed(client, BUSINESS_A, _suggestion(BUSINESS_A, "visible"))
    resp = client.post(
        f"/v1/tenants/{BUSINESS_A}/prospect-suggestions/{sid}/accept",
        json={},
        headers=_bearer(_mint(subject=OPERATOR, roles=["platform_operator"])),
    )
    assert resp.status_code == 403
    assert _intent_count(client, BUSINESS_A) == 0


def test_http_unauthenticated_request_is_401(client):
    sid = _seed(client, BUSINESS_A, _suggestion(BUSINESS_A, "visible"))
    resp = client.post(f"/v1/tenants/{BUSINESS_A}/prospect-suggestions/{sid}/accept", json={})
    assert resp.status_code == 401


def test_http_unknown_suggestion_is_404(client):
    resp = client.post(
        f"/v1/tenants/{BUSINESS_A}/prospect-suggestions/no-such-id/accept",
        json={},
        headers=_bearer(_mint(subject=USER_ALICE)),
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    assert REASON_SUGGESTION_NOT_FOUND in resp.text


def test_http_cross_tenant_suggestion_is_not_found(client):
    # An A-admin cannot reach B's suggestion by id via A's path (resolver scopes to the path business).
    sid_b = _seed(client, BUSINESS_B, _suggestion(BUSINESS_B, "b-only"))
    resp = client.post(
        f"/v1/tenants/{BUSINESS_A}/prospect-suggestions/{sid_b}/accept",
        json={},
        headers=_bearer(_mint(subject=USER_ALICE)),
    )
    assert resp.status_code == 404
    assert _intent_count(client, BUSINESS_B) == 0  # B's row untouched


def test_http_accept_is_idempotent(client):
    sid = _seed(client, BUSINESS_A, _suggestion(BUSINESS_A, "visible"))
    headers = _bearer(_mint(subject=USER_ALICE))
    first = client.post(
        f"/v1/tenants/{BUSINESS_A}/prospect-suggestions/{sid}/accept", json={}, headers=headers
    )
    second = client.post(
        f"/v1/tenants/{BUSINESS_A}/prospect-suggestions/{sid}/accept", json={}, headers=headers
    )
    assert first.status_code == 200 and first.json()["alreadyAccepted"] is False
    assert second.status_code == 200 and second.json()["alreadyAccepted"] is True
    assert first.json()["id"] == second.json()["id"]
    assert _intent_count(client, BUSINESS_A) == 1  # still exactly one intent


def test_http_idempotency_key_replays_verbatim(client):
    sid = _seed(client, BUSINESS_A, _suggestion(BUSINESS_A, "visible"))
    headers = {**_bearer(_mint(subject=USER_ALICE)), "Idempotency-Key": "dca-153-key-1"}
    first = client.post(
        f"/v1/tenants/{BUSINESS_A}/prospect-suggestions/{sid}/accept", json={}, headers=headers
    )
    replay = client.post(
        f"/v1/tenants/{BUSINESS_A}/prospect-suggestions/{sid}/accept", json={}, headers=headers
    )
    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.headers.get("Idempotent-Replay") == "true"
    assert replay.json() == first.json()


def test_http_acceptance_does_not_affect_another_business(client):
    sid_a = _seed(client, BUSINESS_A, _suggestion(BUSINESS_A, "shared"))
    _seed(client, BUSINESS_B, _suggestion(BUSINESS_B, "shared"))

    accepted = client.post(
        f"/v1/tenants/{BUSINESS_A}/prospect-suggestions/{sid_a}/accept",
        json={},
        headers=_bearer(_mint(subject=USER_ALICE)),
    )
    assert accepted.status_code == 200
    assert _intent_count(client, BUSINESS_A) == 1
    assert _intent_count(client, BUSINESS_B) == 0  # B is untouched (Bob is an active admin in B)


# --------------------------------------------------------------------------------------------------
# Postgres-backed test — disposable PostgreSQL (real RLS + transactions, not SQLite)
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)
_PG_SCHEMA = "dca_153_test"


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
def test_pg_accept_is_tenant_scoped_and_atomic(pg_session_factory):
    """On real Postgres: accepting A's suggestion persists one intent + audit + outbox atomically, while
    B's independent suggestion for the same subject gets no intent."""
    assert make_url(PG_URL).get_backend_name() == "postgresql"
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    with pg_session_factory() as session:
        a = _suggestion(BIZ_A, "shared-subject", now=now)
        b = _suggestion(BIZ_B, "shared-subject", now=now)
        session.add_all([a, b])
        session.commit()
        a_id = a.id

        AcceptanceIntentRecorder(session, BIZ_A).accept(a_id, _admin(), now=now)
        session.commit()

        a_intents = session.query(AcceptanceIntent).filter_by(business_id=BIZ_A).count()
        b_intents = session.query(AcceptanceIntent).filter_by(business_id=BIZ_B).count()
        audits = session.query(AuditEvent).filter_by(action=AUDIT_ACTION_ACCEPT).count()
        outbox = session.query(OutboxMessage).filter_by(event_type=OUTBOX_EVENT_ACCEPTANCE).count()

    assert a_intents == 1 and b_intents == 0
    assert audits == 1 and outbox == 1
