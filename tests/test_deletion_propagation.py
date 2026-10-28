"""DCA-085 — propagate deletion to external integrations.

Three layers, matching the DCA-081/084 suites:

- **Service unit tests** (in-memory SQLite) pin :class:`~app.services.deletion_propagation.DeletionPropagator`:
  - propagating an erasure sends an idempotent deletion request to each supported destination and
    records the outcome per destination — ``acknowledged`` (with an opaque ack ref), an explicit
    ``unsupported`` state, ``pending`` on a timeout, and ``failed`` on a permanent rejection — plus an
    atomic DCA-026 audit entry carrying only opaque references;
  - **a timeout is ``pending`` (pending reconciliation), NEVER reported as a completed/acknowledged
    deletion** — the headline acceptance criterion;
  - it is idempotent — a re-run never re-sends to a destination already acknowledged / unsupported and
    never rewrites it (no unintended state);
  - reconciliation re-sends the SAME idempotent request for a pending destination; a destination that
    has recovered advances to ``acknowledged``, one still unavailable stays ``pending``;
  - the relevant DENIALS (an ordinary user / a read-only viewer without ``data.erase``) change nothing
    — no request is sent and no row is written.
- **HTTP tests** (``TestClient`` with the end-user auth contract wired) prove the real API: an admin
  propagates and reads the per-destination status; a viewer is denied (403) with nothing sent; an
  unknown/cross-tenant journal is 404; a disabled contract fails closed (503).
- **Postgres-backed test** (``DCA_TEST_DATABASE_URL``) runs the propagator against a disposable
  PostgreSQL (real transactions + the unique constraint, not SQLite): the rows persist, a re-run is a
  no-op, and a pending timeout is reconciled to acknowledged.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import (
    AuditEvent,
    DataErasureJournal,
    ExternalDeletionPropagation,
    ExternalDeletionStatus,
)
from app.identity.fake import BUSINESS_A, BUSINESS_B, USER_ALICE, USER_CAROL
from app.integration.deletion import build_idempotency_key
from app.integration.deletion_fake import FakeDeletionDestinationClient
from app.main import create_app
from app.services.data_erasure import ErasurePrincipal
from app.services.deletion_propagation import (
    AUDIT_ACTION_PROPAGATE,
    AUDIT_ACTION_RECONCILE,
    REASON_NOT_AUTHORIZED,
    DeletionPropagationAuthorizationError,
    DeletionPropagator,
)

BIZ_A = BUSINESS_A
BIZ_B = BUSINESS_B

ADMIN_A = ErasurePrincipal(subject="00000000-0000-4000-9000-0000000a0085a", roles=("owner",))
USER_A = ErasurePrincipal(subject="00000000-0000-4000-9000-0000000a0085b", roles=("user",))
VIEWER_A = ErasurePrincipal(subject="00000000-0000-4000-9000-0000000a0085c", roles=("viewer",))

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)

# Three synthetic destinations, one of each kind, so the success path AND both failure cases are all
# exercised: an ack, an explicit unsupported, and a timeout (pending reconciliation).
BEHAVIOURS = {
    "crm_mirror": "ack",
    "enrichment_cache": "unsupported",
    "outreach_suppression_feed": "timeout",
}
DESTS = ("crm_mirror", "enrichment_cache", "outreach_suppression_feed")


# --------------------------------------------------------------------------------------------------
# Shared builders
# --------------------------------------------------------------------------------------------------


def _seed_journal(session, business_id, *, journal_id, subject_id="person-a") -> None:
    """Seed one DataErasureJournal (the DCA-084 output this story propagates outward)."""
    session.add(
        DataErasureJournal(
            id=journal_id,
            business_id=business_id,
            subject_type="person",
            subject_id=subject_id,
            request_kind="grievance_erasure",
            request_ref="g-1",
            justification="grievance_erasure",
            authority_plane="business",
            authority_scope="data.erase",
            executed_by="op-admin",
            executed_by_type="user",
            observations_deleted=3,
            evidence_deleted=1,
            projections_deleted=1,
            total_deleted=5,
            tombstones={},
        )
    )


def _fake(behaviours=None, *, destinations=DESTS, **kw) -> FakeDeletionDestinationClient:
    return FakeDeletionDestinationClient(behaviours or BEHAVIOURS, destinations=destinations, **kw)


def _status_by_dest(session, business_id, journal_id) -> dict[str, str]:
    rows = session.scalars(
        select(ExternalDeletionPropagation)
        .where(ExternalDeletionPropagation.business_id == business_id)
        .where(ExternalDeletionPropagation.erasure_journal_id == journal_id)
    ).all()
    return {r.destination: r.status for r in rows}


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)


# --------------------------------------------------------------------------------------------------
# Service unit tests
# --------------------------------------------------------------------------------------------------


def test_propagate_records_ack_unsupported_and_pending(session_factory):
    with session_factory() as session:
        _seed_journal(session, BIZ_A, journal_id="j-1")
        session.commit()

    fake = _fake()
    with session_factory() as session:
        result = DeletionPropagator(session, BIZ_A, fake).propagate(
            session.get(DataErasureJournal, "j-1"), ADMIN_A, now=NOW
        )
        session.commit()

    assert result.acknowledged_count == 1
    assert result.unsupported_count == 1
    assert result.pending_count == 1
    assert result.failed_count == 0
    # A timeout destination present → the propagation is NOT complete.
    assert result.is_complete is False

    with session_factory() as session:
        statuses = _status_by_dest(session, BIZ_A, "j-1")
        assert statuses == {
            "crm_mirror": "acknowledged",
            "enrichment_cache": "unsupported",
            "outreach_suppression_feed": "pending",
        }
        # The acknowledged destination carries an opaque ack ref; the pending one carries none.
        rows = {
            r.destination: r for r in session.scalars(select(ExternalDeletionPropagation)).all()
        }
        assert rows["crm_mirror"].acknowledgment_ref != ""
        assert rows["outreach_suppression_feed"].acknowledgment_ref == ""
        # One atomic audit entry per dispatch (3 destinations).
        audits = session.scalars(
            select(AuditEvent).where(AuditEvent.action == AUDIT_ACTION_PROPAGATE)
        ).all()
        assert len(audits) == 3


def test_timeout_is_pending_not_completed(session_factory):
    """The headline acceptance criterion: a timeout is pending reconciliation, not a completed
    deletion — never acknowledged, carries no ack ref, and keeps the propagation incomplete."""
    with session_factory() as session:
        _seed_journal(session, BIZ_A, journal_id="j-t")
        session.commit()

    fake = _fake({"crm_mirror": "timeout"}, destinations=("crm_mirror",))
    with session_factory() as session:
        result = DeletionPropagator(session, BIZ_A, fake).propagate(
            session.get(DataErasureJournal, "j-t"), ADMIN_A, now=NOW
        )
        session.commit()

    assert result.pending_count == 1
    assert result.acknowledged_count == 0
    assert result.is_complete is False
    with session_factory() as session:
        row = session.scalars(select(ExternalDeletionPropagation)).one()
        assert row.status == ExternalDeletionStatus.PENDING.value
        assert row.status != ExternalDeletionStatus.ACKNOWLEDGED.value
        assert row.acknowledgment_ref == ""
        assert row.attempts == 1


def test_propagate_is_idempotent_terminal_not_resent(session_factory):
    with session_factory() as session:
        _seed_journal(session, BIZ_A, journal_id="j-2")
        session.commit()

    fake = _fake()
    with session_factory() as session:
        DeletionPropagator(session, BIZ_A, fake).propagate(
            session.get(DataErasureJournal, "j-2"), ADMIN_A, now=NOW
        )
        session.commit()
    first_sends = len(fake.seen)

    # A second propagate: the two terminal destinations (ack / unsupported) are NOT re-sent; only the
    # pending one is re-attempted.
    with session_factory() as session:
        result = DeletionPropagator(session, BIZ_A, fake).propagate(
            session.get(DataErasureJournal, "j-2"), ADMIN_A, now=NOW
        )
        session.commit()

    reused = {o.destination: o.reused for o in result.outcomes}
    assert reused["crm_mirror"] is True
    assert reused["enrichment_cache"] is True
    assert reused["outreach_suppression_feed"] is False  # pending → re-attempted
    # Only the pending destination was re-sent on the second call.
    assert len(fake.seen) == first_sends + 1
    with session_factory() as session:
        # No duplicate rows — still exactly one row per destination.
        assert session.query(ExternalDeletionPropagation).count() == 3


def test_reconcile_recovers_pending_to_acknowledged(session_factory):
    with session_factory() as session:
        _seed_journal(session, BIZ_A, journal_id="j-3")
        session.commit()

    # The destination times out on the first send, then recovers (acknowledges) on the re-send.
    fake = _fake(
        {"outreach_suppression_feed": "timeout"}, destinations=("outreach_suppression_feed",)
    )
    with session_factory() as session:
        DeletionPropagator(session, BIZ_A, fake).propagate(
            session.get(DataErasureJournal, "j-3"), ADMIN_A, now=NOW
        )
        session.commit()
    with session_factory() as session:
        assert _status_by_dest(session, BIZ_A, "j-3") == {"outreach_suppression_feed": "pending"}

    fake.set_behaviour("outreach_suppression_feed", "ack")
    with session_factory() as session:
        result = DeletionPropagator(session, BIZ_A, fake).reconcile(
            ADMIN_A, journal_id="j-3", now=NOW + timedelta(minutes=5)
        )
        session.commit()

    assert result.acknowledged_count == 1
    with session_factory() as session:
        row = session.scalars(select(ExternalDeletionPropagation)).one()
        assert row.status == ExternalDeletionStatus.ACKNOWLEDGED.value
        assert row.attempts == 2  # first send + the reconcile re-send
        # The re-send used the SAME deterministic idempotency key.
        assert row.idempotency_key == build_idempotency_key("j-3", "outreach_suppression_feed")
        assert len(fake.seen) == 2
        assert fake.seen[0].idempotency_key == fake.seen[1].idempotency_key
        # The reconcile staged its own audit entry.
        assert (
            session.scalars(
                select(AuditEvent).where(AuditEvent.action == AUDIT_ACTION_RECONCILE)
            ).all()
            != []
        )


def test_reconcile_still_unavailable_stays_pending(session_factory):
    with session_factory() as session:
        _seed_journal(session, BIZ_A, journal_id="j-4")
        session.commit()

    fake = _fake(
        {"outreach_suppression_feed": "timeout"}, destinations=("outreach_suppression_feed",)
    )
    with session_factory() as session:
        DeletionPropagator(session, BIZ_A, fake).propagate(
            session.get(DataErasureJournal, "j-4"), ADMIN_A, now=NOW
        )
        session.commit()
    with session_factory() as session:
        result = DeletionPropagator(session, BIZ_A, fake).reconcile(
            ADMIN_A, journal_id="j-4", now=NOW
        )
        session.commit()

    assert result.pending_count == 1
    assert result.acknowledged_count == 0
    with session_factory() as session:
        row = session.scalars(select(ExternalDeletionPropagation)).one()
        assert row.status == ExternalDeletionStatus.PENDING.value
        assert row.attempts == 2  # still pending, but re-attempted


def test_reject_is_failed_not_pending(session_factory):
    with session_factory() as session:
        _seed_journal(session, BIZ_A, journal_id="j-5")
        session.commit()

    fake = _fake({"crm_mirror": "reject"}, destinations=("crm_mirror",))
    with session_factory() as session:
        result = DeletionPropagator(session, BIZ_A, fake).propagate(
            session.get(DataErasureJournal, "j-5"), ADMIN_A, now=NOW
        )
        session.commit()

    assert result.failed_count == 1
    assert result.pending_count == 0
    with session_factory() as session:
        row = session.scalars(select(ExternalDeletionPropagation)).one()
        assert row.status == ExternalDeletionStatus.FAILED.value
        assert row.detail_code == "destination_rejected"


def test_propagate_denied_for_viewer_changes_nothing(session_factory):
    with session_factory() as session:
        _seed_journal(session, BIZ_A, journal_id="j-6")
        session.commit()

    fake = _fake()
    with session_factory() as session:
        with pytest.raises(DeletionPropagationAuthorizationError) as exc:
            DeletionPropagator(session, BIZ_A, fake).propagate(
                session.get(DataErasureJournal, "j-6"), VIEWER_A, now=NOW
            )
        assert exc.value.code == REASON_NOT_AUTHORIZED
        session.commit()

    # The relevant denial: nothing sent, nothing written.
    assert fake.seen == []
    with session_factory() as session:
        assert session.query(ExternalDeletionPropagation).count() == 0
        assert session.query(AuditEvent).count() == 0


def test_propagate_denied_for_ordinary_user(session_factory):
    with session_factory() as session:
        _seed_journal(session, BIZ_A, journal_id="j-7")
        session.commit()
    fake = _fake()
    with session_factory() as session:
        with pytest.raises(DeletionPropagationAuthorizationError):
            DeletionPropagator(session, BIZ_A, fake).propagate(
                session.get(DataErasureJournal, "j-7"), USER_A, now=NOW
            )
    assert fake.seen == []


def test_audit_references_carry_no_personal_data(session_factory):
    with session_factory() as session:
        _seed_journal(session, BIZ_A, journal_id="j-8")
        session.commit()
    fake = _fake()
    with session_factory() as session:
        DeletionPropagator(session, BIZ_A, fake).propagate(
            session.get(DataErasureJournal, "j-8"), ADMIN_A, now=NOW
        )
        session.commit()
    with session_factory() as session:
        import json

        audits = session.scalars(
            select(AuditEvent).where(AuditEvent.action == AUDIT_ACTION_PROPAGATE)
        ).all()
        blob = json.dumps([a.references for a in audits])
        # Opaque references only — the destination keys and ids, never a contact/field value.
        assert "@" not in blob
        assert "crm_mirror" in blob


# --------------------------------------------------------------------------------------------------
# HTTP tests (TestClient) — the real service API with the end-user auth contract wired
# --------------------------------------------------------------------------------------------------

ISSUER = "https://issuer.dca.test"
AUDIENCE = "dca-api"
SECRET = "dca-test-signing-secret-value"  # synthetic HS256 key, not a real credential
TEST_TOKEN = "test-service-token"


def _auth_settings(**overrides) -> Settings:
    base = {
        "environment": "testing",
        "database_url": "sqlite+pysqlite:///:memory:",
        "service_auth_token": TEST_TOKEN,
        "synthetic_sources_enabled": True,
        "feature_enabled": True,
        "worker_enabled": False,
        "auth_token_issuer": ISSUER,
        "auth_token_audience": AUDIENCE,
        "auth_token_signing_secret": SECRET,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _mint(*, subject: str) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": subject,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    return jwt.encode(claims, SECRET, algorithm="HS256")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client() -> TestClient:
    app = create_app(_auth_settings())
    # Use the deterministic three-destination fixture so the HTTP response shows all three outcomes.
    app.state.deletion_destination_client = _fake()
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _seed_journal_via_client(client: TestClient, business_id: str, journal_id: str) -> None:
    with client.app.state.session_factory() as session:
        _seed_journal(session, business_id, journal_id=journal_id)
        session.commit()


def test_http_admin_propagates_and_reads_status(client):
    _seed_journal_via_client(client, BUSINESS_A, "hj-1")
    resp = client.post(
        f"/v1/tenants/{BUSINESS_A}/erasure-journal/hj-1/external-deletions",
        headers=_bearer(_mint(subject=USER_ALICE)),  # Alice: owner of A (administrator)
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["complete"] is False  # one destination timed out → pending
    assert body["counts"] == {"acknowledged": 1, "unsupported": 1, "pending": 1, "failed": 0}
    statuses = {d["destination"]: d["status"] for d in body["destinations"]}
    assert statuses["outreach_suppression_feed"] == "pending"

    read = client.get(
        f"/v1/tenants/{BUSINESS_A}/erasure-journal/hj-1/external-deletions",
        headers=_bearer(_mint(subject=USER_ALICE)),
    )
    assert read.status_code == 200
    assert len(read.json()) == 3


def test_http_reconcile_recovers_pending(client):
    _seed_journal_via_client(client, BUSINESS_A, "hj-2")
    client.app.state.deletion_destination_client = _fake(
        {"outreach_suppression_feed": "timeout"}, destinations=("outreach_suppression_feed",)
    )
    client.post(
        f"/v1/tenants/{BUSINESS_A}/erasure-journal/hj-2/external-deletions",
        headers=_bearer(_mint(subject=USER_ALICE)),
    )
    client.app.state.deletion_destination_client.set_behaviour("outreach_suppression_feed", "ack")
    resp = client.post(
        f"/v1/tenants/{BUSINESS_A}/erasure-journal/hj-2/external-deletions/reconcile",
        json={},
        headers=_bearer(_mint(subject=USER_ALICE)),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["counts"]["acknowledged"] == 1


def test_http_viewer_denied_nothing_sent(client):
    _seed_journal_via_client(client, BUSINESS_A, "hj-3")
    resp = client.post(
        f"/v1/tenants/{BUSINESS_A}/erasure-journal/hj-3/external-deletions",
        headers=_bearer(_mint(subject=USER_CAROL)),  # Carol: active viewer (no data.erase)
    )
    assert resp.status_code == 403
    assert client.app.state.deletion_destination_client.seen == []
    with client.app.state.session_factory() as session:
        assert session.query(ExternalDeletionPropagation).count() == 0


def test_http_unknown_journal_is_404(client):
    resp = client.post(
        f"/v1/tenants/{BUSINESS_A}/erasure-journal/nope/external-deletions",
        headers=_bearer(_mint(subject=USER_ALICE)),
    )
    assert resp.status_code == 404


def test_http_cross_tenant_journal_is_404(client):
    _seed_journal_via_client(client, BUSINESS_B, "hj-b")
    # Alice (owner of A) targets B's journal via the A path → not found in A.
    resp = client.post(
        f"/v1/tenants/{BUSINESS_A}/erasure-journal/hj-b/external-deletions",
        headers=_bearer(_mint(subject=USER_ALICE)),
    )
    assert resp.status_code == 404


def test_http_disabled_contract_is_503(client):
    _seed_journal_via_client(client, BUSINESS_A, "hj-4")
    client.app.state.deletion_destination_client = None  # contract disabled (as in production)
    resp = client.post(
        f"/v1/tenants/{BUSINESS_A}/erasure-journal/hj-4/external-deletions",
        headers=_bearer(_mint(subject=USER_ALICE)),
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["message"] == "external_deletion_contract_disabled"


def test_build_client_disabled_in_production():
    from app.integration.deletion_fake import build_deletion_destination_client

    prod = Settings(
        _env_file=None,
        environment="production",
        database_url="postgresql+psycopg://x/y",
        service_auth_token="t",
    )
    assert build_deletion_destination_client(prod) is None
    dev = Settings(_env_file=None, environment="testing", database_url="sqlite://")
    assert build_deletion_destination_client(dev) is not None


# --------------------------------------------------------------------------------------------------
# Postgres-backed test — disposable PostgreSQL (real transactions + the unique constraint, not SQLite)
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)
_PG_SCHEMA = "dca_085_test"


@pytest.fixture
def pg_session_factory():
    """An ORM session factory over a fresh schema in the disposable Postgres (dropped on teardown)."""
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
def test_pg_propagate_is_idempotent_and_reconciles(pg_session_factory):
    """On real Postgres: propagation persists one row per destination, a re-run is a no-op (the unique
    constraint holds), and a pending timeout is reconciled to acknowledged on a re-send."""
    assert make_url(PG_URL).get_backend_name() == "postgresql"
    with pg_session_factory() as session:
        _seed_journal(session, BIZ_A, journal_id="pg-j")
        session.commit()

    fake = _fake(
        {"outreach_suppression_feed": "timeout"}, destinations=("outreach_suppression_feed",)
    )
    with pg_session_factory() as session:
        DeletionPropagator(session, BIZ_A, fake).propagate(
            session.get(DataErasureJournal, "pg-j"), ADMIN_A, now=NOW
        )
        session.commit()

    # A re-run does not create a duplicate row (unique (business_id, journal, destination)).
    with pg_session_factory() as session:
        DeletionPropagator(session, BIZ_A, fake).propagate(
            session.get(DataErasureJournal, "pg-j"), ADMIN_A, now=NOW
        )
        session.commit()
    with pg_session_factory() as session:
        assert session.query(ExternalDeletionPropagation).count() == 1
        assert session.scalars(select(ExternalDeletionPropagation)).one().status == "pending"

    fake.set_behaviour("outreach_suppression_feed", "ack")
    with pg_session_factory() as session:
        DeletionPropagator(session, BIZ_A, fake).reconcile(ADMIN_A, journal_id="pg-j", now=NOW)
        session.commit()
    with pg_session_factory() as session:
        assert session.scalars(select(ExternalDeletionPropagation)).one().status == "acknowledged"
