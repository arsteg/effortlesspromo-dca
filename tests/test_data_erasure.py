"""DCA-084 — erase permitted DCA operational data.

Three layers, matching the DCA-081/082/083 suites:

- **Service unit tests** (in-memory SQLite) pin :class:`~app.services.data_erasure.OperationalDataEraser`:
  - a successful erasure deletes the subject's observations (identity node + contact points + field
    observations), evidence and projections (field corrections) and records a
    :class:`~app.db.models.DataErasureJournal` with the OPAQUE tombstone references + counts, plus an
    atomic DCA-026 audit entry carrying only permitted minimal metadata (no contact value / field value);
  - the erasure is SCOPED — another tenant's SAME-shaped subject is left entirely untouched;
  - the retained records (audit trail, consent receipts, restriction ledgers) are NOT erased;
  - the relevant DENIALS (an ordinary user / a read-only viewer without ``data.erase``; an unknown /
    cross-tenant subject; a missing justification) each change nothing — no row is deleted and no journal
    is written — and a re-run of an already-erased subject reads as not-found (idempotent).
- **HTTP tests** (``TestClient`` with the end-user auth contract wired) prove the real API: an admin of
  the business erases and gets the journal; a viewer is denied (403); a cross-tenant subject is 404; a
  justification carrying a raw contact is refused (422) and deletes nothing.
- **Postgres-backed test** (``DCA_TEST_DATABASE_URL``) runs the executor against a disposable PostgreSQL
  (real transactions, not SQLite): the subject's rows are gone while the journal + audit remain.
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
    ConsentAction,
    ConsentReceipt,
    ContactPoint,
    DataErasureJournal,
    EvidenceObject,
    FieldCorrection,
    FieldObservation,
    PersonObservation,
)
from app.identity.fake import BUSINESS_A, BUSINESS_B, USER_ALICE, USER_CAROL
from app.main import create_app
from app.services.data_erasure import (
    AUDIT_ACTION_ERASE,
    REASON_MISSING_JUSTIFICATION,
    REASON_NOT_AUTHORIZED,
    REASON_SUBJECT_NOT_FOUND,
    REASON_UNKNOWN_SUBJECT_TYPE,
    DataErasureAuthorizationError,
    DataErasureError,
    ErasurePrincipal,
    OperationalDataEraser,
)

BIZ_A = BUSINESS_A
BIZ_B = BUSINESS_B

ADMIN_A = ErasurePrincipal(subject="00000000-0000-4000-9000-0000000a0084a", roles=("owner",))
USER_A = ErasurePrincipal(subject="00000000-0000-4000-9000-0000000a0084b", roles=("user",))
VIEWER_A = ErasurePrincipal(subject="00000000-0000-4000-9000-0000000a0084c", roles=("viewer",))

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)

# A synthetic contact value + field value — personal data that must be GONE after erasure and never
# land in the journal or the audit trail.
SECRET_EMAIL = "erase.me@synthetic.example"
SECRET_FIELD_VALUE = "+91 90000 00001"


# --------------------------------------------------------------------------------------------------
# Shared builders — seed a person subject with observations / evidence / projections, in one business.
# --------------------------------------------------------------------------------------------------


def _seed_person_subject(session, business_id, *, person_id, contact=SECRET_EMAIL) -> None:
    session.add(
        PersonObservation(
            id=person_id,
            business_id=business_id,
            display_name="Synthetic Person",
            role_title="Head",
        )
    )
    # observation: a contact point
    session.add(
        ContactPoint(
            business_id=business_id,
            person_observation_id=person_id,
            channel="email",
            value=contact,
            evidence="self_reported",
        )
    )
    # observation: a field observation (value is potentially personal data)
    session.add(
        FieldObservation(
            business_id=business_id,
            subject_type="person",
            subject_id=person_id,
            field_key="phone",
            field_value=SECRET_FIELD_VALUE,
            inference_label="observed",
            source_key="synthetic.mca",
            source_version=1,
            provenance_ref="synthetic://fetch/1",
            observed_at=NOW,
            scope="nationwide",
            rights_basis="synthetic_basis",
        )
    )
    # evidence: an evidence object
    session.add(
        EvidenceObject(
            business_id=business_id,
            source_key="synthetic.mca",
            source_version=1,
            subject_type="person",
            subject_id=person_id,
            content_kind="registry_record",
            content_ref="synthetic://evidence/1",
            content_digest="0" * 64,
            retention_mode="reference_only",
            rights_basis="synthetic_basis",
            captured_at=NOW,
        )
    )
    # projection: a field correction (the tenant's canonical overlay)
    session.add(
        FieldCorrection(
            business_id=business_id,
            subject_type="person",
            subject_id=person_id,
            field_key="phone",
            corrected_value=SECRET_FIELD_VALUE,
            version=1,
            actor_id="00000000-0000-4000-9000-0000000a0084d",
        )
    )


def _counts(session, business_id, person_id) -> dict[str, int]:
    return {
        "person": session.query(PersonObservation)
        .filter_by(business_id=business_id, id=person_id)
        .count(),
        "contact_points": session.query(ContactPoint)
        .filter_by(business_id=business_id, person_observation_id=person_id)
        .count(),
        "field_observations": session.query(FieldObservation)
        .filter_by(business_id=business_id, subject_type="person", subject_id=person_id)
        .count(),
        "evidence": session.query(EvidenceObject)
        .filter_by(business_id=business_id, subject_type="person", subject_id=person_id)
        .count(),
        "corrections": session.query(FieldCorrection)
        .filter_by(business_id=business_id, subject_type="person", subject_id=person_id)
        .count(),
    }


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


def test_erase_deletes_all_operational_data_and_journals_it(session_factory):
    with session_factory() as session:
        _seed_person_subject(session, BIZ_A, person_id="person-a")
        session.commit()

    with session_factory() as session:
        result = OperationalDataEraser(session, BIZ_A).erase_subject(
            "person", "person-a", ADMIN_A, justification="grievance_erasure", request_ref="g-1"
        )
        session.commit()
        journal_id = result.journal_id

    # Counts match: 1 identity + 1 contact point + 1 field observation = 3 observations; 1 evidence;
    # 1 projection.
    assert result.observations_deleted == 3
    assert result.evidence_deleted == 1
    assert result.projections_deleted == 1
    assert result.total_deleted == 5

    with session_factory() as session:
        # The operational data is GONE — no tenant-scoped read returns it anymore.
        assert _counts(session, BIZ_A, "person-a") == {
            "person": 0,
            "contact_points": 0,
            "field_observations": 0,
            "evidence": 0,
            "corrections": 0,
        }
        # The journal survives, with the justified tombstone references + counts.
        journal = session.get(DataErasureJournal, journal_id)
        assert journal is not None
        assert journal.subject_id == "person-a"
        assert journal.request_ref == "g-1"
        assert journal.total_deleted == 5
        assert set(journal.tombstones["observations"]) == {
            "person_observations",
            "contact_points",
            "field_observations",
        }
        assert len(journal.tombstones["evidence"]) == 1
        assert len(journal.tombstones["projections"]) == 1


def test_erase_audit_retains_only_opaque_minimal_metadata(session_factory):
    with session_factory() as session:
        _seed_person_subject(session, BIZ_A, person_id="person-a")
        session.commit()

    with session_factory() as session:
        OperationalDataEraser(session, BIZ_A).erase_subject(
            "person", "person-a", ADMIN_A, justification="grievance_erasure"
        )
        session.commit()

    with session_factory() as session:
        events = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.business_id == BIZ_A)
            .where(AuditEvent.action == AUDIT_ACTION_ERASE)
        ).all()
        assert len(events) == 1
        event = events[0]
        # The audit holds only opaque metadata — never the erased contact value / field value.
        import json

        blob = json.dumps({"reason": event.reason, "references": event.references})
        assert SECRET_EMAIL not in blob
        assert SECRET_FIELD_VALUE not in blob
        assert event.references["totalDeleted"] == 5
        assert event.references["subjectId"] == "person-a"


def test_erase_is_scoped_and_leaves_other_tenant_untouched(session_factory):
    with session_factory() as session:
        _seed_person_subject(session, BIZ_A, person_id="person-a")
        # B holds a SAME-shaped subject — it must be left entirely untouched by A's erasure.
        _seed_person_subject(session, BIZ_B, person_id="person-b")
        session.commit()

    with session_factory() as session:
        OperationalDataEraser(session, BIZ_A).erase_subject(
            "person", "person-a", ADMIN_A, justification="grievance_erasure"
        )
        session.commit()

    with session_factory() as session:
        assert _counts(session, BIZ_A, "person-a")["person"] == 0  # A erased
        # B's subject is intact — a one-tenant erasure never reaches another tenant.
        assert _counts(session, BIZ_B, "person-b") == {
            "person": 1,
            "contact_points": 1,
            "field_observations": 1,
            "evidence": 1,
            "corrections": 1,
        }


def test_erase_does_not_touch_retained_records(session_factory):
    with session_factory() as session:
        _seed_person_subject(session, BIZ_A, person_id="person-a")
        # A retained record a privacy obligation requires be KEPT (a consent receipt).
        session.add(
            ConsentReceipt(
                business_id=BIZ_A,
                brand_key="default",
                subject_type="person",
                subject_id="person-a",
                contact_point_id="cp-synthetic",
                channel="email",
                purpose="marketing_email",
                action=ConsentAction.WITHDRAW.value,
                notice_version="v1",
                form_version="v1",
                language="en",
                evidence_id="ev-synthetic",
                collected_at=NOW,
            )
        )
        session.commit()

    with session_factory() as session:
        OperationalDataEraser(session, BIZ_A).erase_subject(
            "person", "person-a", ADMIN_A, justification="grievance_erasure"
        )
        session.commit()

    with session_factory() as session:
        # The consent receipt (retained record) survives; only operational data was erased.
        assert session.query(ConsentReceipt).filter_by(business_id=BIZ_A).count() == 1


def test_erase_denied_for_viewer_changes_nothing(session_factory):
    with session_factory() as session:
        _seed_person_subject(session, BIZ_A, person_id="person-a")
        session.commit()

    with session_factory() as session:
        with pytest.raises(DataErasureAuthorizationError) as exc:
            OperationalDataEraser(session, BIZ_A).erase_subject(
                "person", "person-a", VIEWER_A, justification="grievance_erasure"
            )
        assert exc.value.code == REASON_NOT_AUTHORIZED
        session.commit()

    with session_factory() as session:
        # The relevant denial changed nothing: every row is still present, and no journal was written.
        assert _counts(session, BIZ_A, "person-a")["person"] == 1
        assert session.query(DataErasureJournal).count() == 0


def test_erase_denied_for_ordinary_user(session_factory):
    with session_factory() as session:
        _seed_person_subject(session, BIZ_A, person_id="person-a")
        session.commit()
    with session_factory() as session:
        with pytest.raises(DataErasureAuthorizationError) as exc:
            OperationalDataEraser(session, BIZ_A).erase_subject(
                "person", "person-a", USER_A, justification="grievance_erasure"
            )
        assert exc.value.code == REASON_NOT_AUTHORIZED


def test_erase_unknown_subject_is_not_found_and_changes_nothing(session_factory):
    with session_factory() as session:
        _seed_person_subject(session, BIZ_A, person_id="person-a")
        session.commit()

    with session_factory() as session:
        with pytest.raises(DataErasureError) as exc:
            OperationalDataEraser(session, BIZ_A).erase_subject(
                "person", "no-such-person", ADMIN_A, justification="grievance_erasure"
            )
        assert exc.value.code == REASON_SUBJECT_NOT_FOUND

    with session_factory() as session:
        assert session.query(DataErasureJournal).count() == 0
        assert _counts(session, BIZ_A, "person-a")["person"] == 1


def test_erase_cross_tenant_subject_is_not_found(session_factory):
    with session_factory() as session:
        _seed_person_subject(session, BIZ_B, person_id="person-b")
        session.commit()

    with session_factory() as session:
        # An A-scoped eraser cannot reach B's subject — it reads exactly like not-found.
        with pytest.raises(DataErasureError) as exc:
            OperationalDataEraser(session, BIZ_A).erase_subject(
                "person", "person-b", ADMIN_A, justification="grievance_erasure"
            )
        assert exc.value.code == REASON_SUBJECT_NOT_FOUND

    with session_factory() as session:
        assert _counts(session, BIZ_B, "person-b")["person"] == 1  # B untouched


def test_erase_missing_justification_is_refused(session_factory):
    with session_factory() as session:
        _seed_person_subject(session, BIZ_A, person_id="person-a")
        session.commit()

    with session_factory() as session:
        with pytest.raises(DataErasureError) as exc:
            OperationalDataEraser(session, BIZ_A).erase_subject(
                "person", "person-a", ADMIN_A, justification="  "
            )
        assert exc.value.code == REASON_MISSING_JUSTIFICATION

    with session_factory() as session:
        assert _counts(session, BIZ_A, "person-a")["person"] == 1


def test_erase_unknown_subject_type_is_refused(session_factory):
    with session_factory() as session:
        with pytest.raises(DataErasureError) as exc:
            OperationalDataEraser(session, BIZ_A).erase_subject(
                "spaceship", "x", ADMIN_A, justification="grievance_erasure"
            )
        assert exc.value.code == REASON_UNKNOWN_SUBJECT_TYPE


def test_second_erase_is_idempotent_not_found(session_factory):
    with session_factory() as session:
        _seed_person_subject(session, BIZ_A, person_id="person-a")
        session.commit()

    with session_factory() as session:
        OperationalDataEraser(session, BIZ_A).erase_subject(
            "person", "person-a", ADMIN_A, justification="grievance_erasure"
        )
        session.commit()

    with session_factory() as session:
        # The subject is gone, so a re-run reads as not-found and changes nothing (no second journal).
        with pytest.raises(DataErasureError) as exc:
            OperationalDataEraser(session, BIZ_A).erase_subject(
                "person", "person-a", ADMIN_A, justification="grievance_erasure"
            )
        assert exc.value.code == REASON_SUBJECT_NOT_FOUND

    with session_factory() as session:
        assert session.query(DataErasureJournal).count() == 1  # only the first erasure journaled


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


def _seed_via_client(client: TestClient, business_id: str, person_id: str) -> None:
    with client.app.state.session_factory() as session:
        _seed_person_subject(session, business_id, person_id=person_id)
        session.commit()


def test_http_admin_executes_erasure(client):
    _seed_via_client(client, BUSINESS_A, "person-a")
    resp = client.post(
        f"/v1/tenants/{BUSINESS_A}/subjects/person/person-a/erasure",
        json={"justification": "grievance_erasure", "requestRef": "g-1"},
        headers=_bearer(_mint(subject=USER_ALICE)),  # Alice: owner of A (administrator)
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["totalDeleted"] == 5
    assert body["subjectId"] == "person-a"
    # No raw contact / field value leaks into the journal projection.
    import json

    assert SECRET_EMAIL not in json.dumps(body)
    assert SECRET_FIELD_VALUE not in json.dumps(body)

    # The journal is readable afterwards; the subject's operational data is gone.
    read = client.get(
        f"/v1/tenants/{BUSINESS_A}/erasure-journal/{body['erasureJournalId']}",
        headers=_bearer(_mint(subject=USER_ALICE)),
    )
    assert read.status_code == 200
    with client.app.state.session_factory() as session:
        assert _counts(session, BUSINESS_A, "person-a")["person"] == 0


def test_http_viewer_denied(client):
    _seed_via_client(client, BUSINESS_A, "person-a")
    # Carol is an active viewer in A — a member, but without data.erase.
    resp = client.post(
        f"/v1/tenants/{BUSINESS_A}/subjects/person/person-a/erasure",
        json={"justification": "grievance_erasure"},
        headers=_bearer(_mint(subject=USER_CAROL)),
    )
    assert resp.status_code == 403
    with client.app.state.session_factory() as session:
        assert _counts(session, BUSINESS_A, "person-a")["person"] == 1  # nothing deleted


def test_http_cross_tenant_subject_is_404(client):
    _seed_via_client(client, BUSINESS_B, "person-b")
    # Alice (owner of A) targets B's subject via the A path → not found in A.
    resp = client.post(
        f"/v1/tenants/{BUSINESS_A}/subjects/person/person-b/erasure",
        json={"justification": "grievance_erasure"},
        headers=_bearer(_mint(subject=USER_ALICE)),
    )
    assert resp.status_code == 404
    with client.app.state.session_factory() as session:
        assert _counts(session, BUSINESS_B, "person-b")["person"] == 1  # B untouched


def test_http_justification_with_contact_is_refused(client):
    _seed_via_client(client, BUSINESS_A, "person-a")
    resp = client.post(
        f"/v1/tenants/{BUSINESS_A}/subjects/person/person-a/erasure",
        json={"justification": f"requested by {SECRET_EMAIL}"},
        headers=_bearer(_mint(subject=USER_ALICE)),
    )
    assert resp.status_code == 422
    with client.app.state.session_factory() as session:
        assert _counts(session, BUSINESS_A, "person-a")["person"] == 1  # nothing deleted


# --------------------------------------------------------------------------------------------------
# Postgres-backed test — disposable PostgreSQL (real transactions, not SQLite)
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)
_PG_SCHEMA = "dca_084_test"


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
def test_pg_erase_removes_rows_and_keeps_journal(pg_session_factory):
    """On real Postgres: a resolved erasure deletes the subject's operational rows while the journal +
    audit survive — the erased data is no longer accessible; only permitted minimal metadata remains."""
    assert make_url(PG_URL).get_backend_name() == "postgresql"
    with pg_session_factory() as session:
        _seed_person_subject(session, BIZ_A, person_id="person-a")
        session.commit()

    with pg_session_factory() as session:
        result = OperationalDataEraser(session, BIZ_A).erase_subject(
            "person", "person-a", ADMIN_A, justification="grievance_erasure", request_ref="g-1"
        )
        session.commit()
        journal_id = result.journal_id

    with pg_session_factory() as session:
        assert _counts(session, BIZ_A, "person-a") == {
            "person": 0,
            "contact_points": 0,
            "field_observations": 0,
            "evidence": 0,
            "corrections": 0,
        }
        assert session.get(DataErasureJournal, journal_id) is not None
        assert (
            session.scalars(
                select(AuditEvent)
                .where(AuditEvent.business_id == BIZ_A)
                .where(AuditEvent.action == AUDIT_ACTION_ERASE)
            ).all()
            != []
        )
