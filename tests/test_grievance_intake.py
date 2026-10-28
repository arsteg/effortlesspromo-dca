"""DCA-082 — record an India privacy grievance (scoped intake/status, proportionate verification).

Two layers, matching the DCA-042/078 suites:

- **Service unit tests** (in-memory SQLite) pin the store: the proportionate verification level is
  derived from the category; every grievance is recorded ``unverified`` / ``received``; the reviewed
  response deadline is pinned and ``respond_by`` computed; the requester's contact is stored only as a
  one-way digest (never raw) and the claim token only as a hash; an unknown category/channel is refused
  before anything is staged; the claim-token lookup returns the row for the right token and ``None`` for
  a wrong/absent one; grievances are scoped per business; each record stages an atomic audit entry; and
  the store stages (never commits) into the caller's session.
- **HTTP tests** (``TestClient`` with the end-user auth contract wired) prove the real API: public
  intake (service-auth only) returns the one-time token; the requester status lookup is safe (valid
  token → status with NO contact/detail, wrong/absent token → 404, cross-tenant id → 404); the operator
  list/by-id require the ``grievance.read`` scope (a viewer / platform operator is denied) and never
  expose contacts; the deadline is configurable; and one business's grievances never leak to another.

The transaction/RLS/migration paths are covered by ``tests/test_rls.py`` and ``tests/test_migrations.py``
(which pin ``privacy_grievances`` into the RLS set and its dual-path migration on disposable Postgres).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, validate_required
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import (
    AuditEvent,
    GrievanceStatus,
    GrievanceVerificationLevel,
    GrievanceVerificationState,
    PrivacyGrievance,
)
from app.identity.fake import BUSINESS_A, BUSINESS_B, USER_ALICE, USER_BOB, USER_CAROL
from app.main import create_app
from app.services.grievance_intake import (
    AUDIT_ACTION_RECORD_GRIEVANCE,
    REASON_UNKNOWN_CATEGORY,
    REASON_UNKNOWN_CHANNEL,
    GrievanceIntakeError,
    GrievanceIntakeInput,
    PrivacyGrievanceStore,
    contact_digest,
    verification_level_for,
)
from tests.conftest import TEST_TOKEN, make_settings

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"
CONTACT = "aggrieved.person@synthetic.example"  # synthetic; stored only as a one-way digest


def resp_text(obj) -> str:
    """Serialize a response body so a test can assert a value is ABSENT from the whole payload."""
    import json

    return json.dumps(obj)


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


def test_record_derives_proportionate_level_and_pins_reviewed_deadline(session_factory):
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        issued = PrivacyGrievanceStore(session, BIZ_A).record(
            GrievanceIntakeInput(category="access", contact=CONTACT, channel="email", detail="x"),
            response_deadline_days=30,
            now=now,
        )
        session.commit()
        g = issued.grievance
        # access is an ELEVATED category (exposes personal data), recorded unverified/received.
        assert g.verification_level_required == GrievanceVerificationLevel.ELEVATED.value
        assert g.verification_state == GrievanceVerificationState.UNVERIFIED.value
        assert g.status == GrievanceStatus.RECEIVED.value
        # The reviewed deadline is pinned onto the row and respond_by computed from it.
        assert g.response_deadline_days == 30
        assert g.respond_by == now + timedelta(days=30)
        # The contact is stored ONLY as a one-way digest; the raw value is nowhere on the row.
        assert g.requester_contact_digest == contact_digest(CONTACT)
        assert CONTACT not in (g.requester_contact_digest, g.detail or "")
        # The claim token is returned once; only its hash is stored (not the plaintext).
        assert issued.access_token and issued.access_token != g.access_token_hash
        assert len(g.access_token_hash) == 64  # sha256 hex


@pytest.mark.parametrize(
    "category,level",
    [
        ("access", GrievanceVerificationLevel.ELEVATED),
        ("correction", GrievanceVerificationLevel.ELEVATED),
        ("erasure", GrievanceVerificationLevel.ELEVATED),
        ("consent_withdrawal", GrievanceVerificationLevel.STANDARD),
        ("nomination", GrievanceVerificationLevel.STANDARD),
        ("processing_complaint", GrievanceVerificationLevel.STANDARD),
        ("other", GrievanceVerificationLevel.STANDARD),
    ],
)
def test_verification_level_is_proportionate_to_category(category, level):
    from app.db.models import GrievanceCategory

    assert verification_level_for(GrievanceCategory(category)) is level


def test_contact_digest_is_one_way_and_stable():
    # Same contact → same digest (so duplicates are recognizable); empty → "". Never the raw value.
    assert contact_digest(CONTACT) == contact_digest(" " + CONTACT.upper() + " ")
    assert contact_digest("") == ""
    assert CONTACT not in contact_digest(CONTACT)


def test_unknown_category_is_refused_and_stages_nothing(session_factory):
    with session_factory() as session:
        with pytest.raises(GrievanceIntakeError) as exc:
            PrivacyGrievanceStore(session, BIZ_A).record(
                GrievanceIntakeInput(category="not-a-category", contact=CONTACT),
                response_deadline_days=30,
            )
        assert exc.value.code == REASON_UNKNOWN_CATEGORY
        session.rollback()
    with session_factory() as session:
        assert session.query(PrivacyGrievance).count() == 0
        assert session.query(AuditEvent).count() == 0


def test_unknown_channel_is_refused_and_stages_nothing(session_factory):
    with session_factory() as session:
        with pytest.raises(GrievanceIntakeError) as exc:
            PrivacyGrievanceStore(session, BIZ_A).record(
                GrievanceIntakeInput(category="access", contact=CONTACT, channel="carrier-pigeon"),
                response_deadline_days=30,
            )
        assert exc.value.code == REASON_UNKNOWN_CHANNEL
        session.rollback()
    with session_factory() as session:
        assert session.query(PrivacyGrievance).count() == 0


def test_claim_token_lookup_matches_only_the_right_token(session_factory):
    with session_factory() as session:
        issued = PrivacyGrievanceStore(session, BIZ_A).record(
            GrievanceIntakeInput(category="access", contact=CONTACT), response_deadline_days=30
        )
        session.commit()
        gid, token = issued.grievance.id, issued.access_token
    with session_factory() as session:
        store = PrivacyGrievanceStore(session, BIZ_A)
        # Right token → the row; wrong/empty token → None (never an existence oracle).
        assert store.get_for_requester(gid, token) is not None
        assert store.get_for_requester(gid, "wrong-token") is None
        assert store.get_for_requester(gid, "") is None
        assert store.get_for_requester("no-such-id", token) is None


def test_grievances_are_scoped_per_business(session_factory):
    with session_factory() as session:
        issued = PrivacyGrievanceStore(session, BIZ_A).record(
            GrievanceIntakeInput(category="access", contact=CONTACT), response_deadline_days=30
        )
        session.commit()
        gid, token = issued.grievance.id, issued.access_token
    with session_factory() as session:
        # B cannot read A's grievance by id (cross-tenant reads as not-found), even with the right token.
        store_b = PrivacyGrievanceStore(session, BIZ_B)
        assert store_b.get(gid) is None
        assert store_b.get_for_requester(gid, token) is None
        assert store_b.grievances() == []


def test_record_stages_opaque_audit_entry(session_factory):
    with session_factory() as session:
        issued = PrivacyGrievanceStore(session, BIZ_A).record(
            GrievanceIntakeInput(category="correction", contact=CONTACT), response_deadline_days=30
        )
        session.commit()
        gid = issued.grievance.id
    with session_factory() as session:
        audits = session.query(AuditEvent).filter(AuditEvent.business_id == BIZ_A).all()
        assert [a.action for a in audits] == [AUDIT_ACTION_RECORD_GRIEVANCE]
        refs = audits[0].references
        assert refs["grievanceId"] == gid
        assert refs["category"] == "correction"
        assert refs["verificationLevelRequired"] == GrievanceVerificationLevel.ELEVATED.value
        # The audit carries opaque references ONLY — never the contact or the claim token.
        assert CONTACT not in str(refs)


def test_record_does_not_commit(session_factory):
    with session_factory() as session:
        PrivacyGrievanceStore(session, BIZ_A).record(
            GrievanceIntakeInput(category="access", contact=CONTACT), response_deadline_days=30
        )
        session.rollback()  # the store never commits — a rollback leaves nothing behind
    with session_factory() as session:
        assert session.query(PrivacyGrievance).count() == 0
        assert session.query(AuditEvent).count() == 0


def test_config_rejects_out_of_range_deadline():
    # The reviewed deadline must be a sane, reviewed window (1..90 days); a bad value fails fast.
    for bad in (0, -1, 91):
        with pytest.raises(RuntimeError):
            validate_required(make_settings(environment="testing", grievance_response_days=bad))
    # A value inside the reviewed window is accepted.
    validate_required(make_settings(environment="testing", grievance_response_days=45))


# --------------------------------------------------------------------------------------------------
# HTTP tests (TestClient) — the real service API with the end-user auth contract wired
# --------------------------------------------------------------------------------------------------

ISSUER = "https://issuer.dca.test"
AUDIENCE = "dca-api"
SECRET = "dca-test-signing-secret-value"  # synthetic HS256 key, not a real credential
OPERATOR = "00000000-0000-4000-9000-00000090e701"  # platform operator; no business membership
SERVICE_AUTH = {"X-Service-Auth": TEST_TOKEN}


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


_INTAKE_BODY = {
    "category": "access",
    "contact": CONTACT,
    "channel": "email",
    "detail": "please help",
}


@pytest.fixture
def client() -> TestClient:
    app = create_app(_auth_settings())
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _audit_count(client: TestClient, business_id: str) -> int:
    with client.app.state.session_factory() as session:
        return (
            session.query(AuditEvent)
            .filter(
                AuditEvent.business_id == business_id,
                AuditEvent.action == AUDIT_ACTION_RECORD_GRIEVANCE,
            )
            .count()
        )


def _intake(client: TestClient, business_id: str, body: dict | None = None) -> dict:
    resp = client.post(
        f"/v1/tenants/{business_id}/grievances",
        json=body or _INTAKE_BODY,
        headers=SERVICE_AUTH,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_http_public_intake_returns_token_and_audits(client):
    body = _intake(client, BUSINESS_A)
    assert body["status"] == "received"
    assert body["verificationState"] == "unverified"
    assert body["verificationLevelRequired"] == "elevated"  # access is elevated
    assert body["responseDeadlineDays"] == 30 and body["respondBy"]
    assert body["statusToken"]  # the one-time claim token
    # The intake response never echoes the contact or any detail back.
    assert CONTACT not in resp_text(body)
    assert "detail" not in body
    assert _audit_count(client, BUSINESS_A) == 1


def test_http_intake_requires_service_auth(client):
    resp = client.post(f"/v1/tenants/{BUSINESS_A}/grievances", json=_INTAKE_BODY)
    assert resp.status_code == 401
    assert _audit_count(client, BUSINESS_A) == 0


def test_http_intake_unknown_category_is_422_and_stages_nothing(client):
    resp = client.post(
        f"/v1/tenants/{BUSINESS_A}/grievances",
        json={**_INTAKE_BODY, "category": "nope"},
        headers=SERVICE_AUTH,
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["message"] == REASON_UNKNOWN_CATEGORY
    assert _audit_count(client, BUSINESS_A) == 0


def test_http_requester_status_with_valid_token_is_safe(client):
    created = _intake(client, BUSINESS_A)
    gid, token = created["grievanceId"], created["statusToken"]
    resp = client.get(
        f"/v1/tenants/{BUSINESS_A}/grievances/{gid}/status",
        headers={**SERVICE_AUTH, "X-Grievance-Token": token},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["grievanceId"] == gid and body["status"] == "received"
    assert body["verificationLevelRequired"] == "elevated"
    # The safe status view exposes NO contact, NO detail and NO token/hash.
    assert "detail" not in body
    assert CONTACT not in resp_text(body)
    assert "statusToken" not in body and "accessTokenHash" not in body


def test_http_requester_status_with_wrong_or_missing_token_is_404(client):
    created = _intake(client, BUSINESS_A)
    gid = created["grievanceId"]
    # Wrong token → 404 (indistinguishable from not-found; never an existence oracle).
    wrong = client.get(
        f"/v1/tenants/{BUSINESS_A}/grievances/{gid}/status",
        headers={**SERVICE_AUTH, "X-Grievance-Token": "not-the-token"},
    )
    assert wrong.status_code == 404
    # Missing token → 404 as well.
    missing = client.get(f"/v1/tenants/{BUSINESS_A}/grievances/{gid}/status", headers=SERVICE_AUTH)
    assert missing.status_code == 404


def test_http_requester_status_cross_tenant_id_is_404(client):
    created = _intake(client, BUSINESS_A)
    gid, token = created["grievanceId"], created["statusToken"]
    # A's grievance id + token presented under B reads as not-found (never a cross-tenant read).
    resp = client.get(
        f"/v1/tenants/{BUSINESS_B}/grievances/{gid}/status",
        headers={**SERVICE_AUTH, "X-Grievance-Token": token},
    )
    assert resp.status_code == 404


def test_http_operator_can_list_and_read_but_not_see_contacts(client):
    created = _intake(client, BUSINESS_A)
    gid = created["grievanceId"]
    alice = _bearer(_mint(subject=USER_ALICE))
    listed = client.get(f"/v1/tenants/{BUSINESS_A}/grievances", headers=alice)
    assert listed.status_code == 200
    rows = listed.json()
    assert [r["grievanceId"] for r in rows] == [gid]
    # The operator list carries no contact digest / token hash.
    assert CONTACT not in resp_text(rows)
    assert "requesterContactDigest" not in rows[0] and "accessTokenHash" not in rows[0]

    by_id = client.get(f"/v1/tenants/{BUSINESS_A}/grievances/{gid}", headers=alice)
    assert by_id.status_code == 200
    # A handler sees the requester's description, but still never the contact digest/token hash.
    assert by_id.json()["detail"] == "please help"
    assert "accessTokenHash" not in by_id.json()


def test_http_viewer_cannot_read_grievances(client):
    _intake(client, BUSINESS_A)
    # Carol is an active viewer in A — a real member, but without grievance.read.
    resp = client.get(
        f"/v1/tenants/{BUSINESS_A}/grievances", headers=_bearer(_mint(subject=USER_CAROL))
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_http_platform_operator_cannot_read_client_grievances(client):
    _intake(client, BUSINESS_A)
    # Global operator rights do NOT imply membership: denied before the scope check even runs.
    resp = client.get(
        f"/v1/tenants/{BUSINESS_A}/grievances",
        headers=_bearer(_mint(subject=OPERATOR, roles=["platform_operator"])),
    )
    assert resp.status_code == 403


def test_http_operator_read_requires_authentication(client):
    _intake(client, BUSINESS_A)
    assert client.get(f"/v1/tenants/{BUSINESS_A}/grievances").status_code == 401


def test_http_grievances_are_isolated_per_business(client):
    created_a = _intake(client, BUSINESS_A)
    # Bob is an admin of B; A's grievance must not appear in B's operator list.
    bob = _bearer(_mint(subject=USER_BOB))
    b_list = client.get(f"/v1/tenants/{BUSINESS_B}/grievances", headers=bob)
    assert b_list.status_code == 200 and b_list.json() == []
    # Bob (no A membership) also cannot read A's grievance by id — denied (403), never a cross read.
    denied = client.get(
        f"/v1/tenants/{BUSINESS_A}/grievances/{created_a['grievanceId']}", headers=bob
    )
    assert denied.status_code == 403


def test_http_deadline_is_configurable():
    # A different reviewed SLA (15 days) flows through to the recorded grievance — configurable deadline.
    app = create_app(_auth_settings(grievance_response_days=15))
    with TestClient(app, raise_server_exceptions=False) as client:
        body = _intake(client, BUSINESS_A)
        assert body["responseDeadlineDays"] == 15


def test_http_intake_is_idempotent_on_retry(client):
    from app.idempotency import IDEMPOTENCY_HEADER

    keyed = {**SERVICE_AUTH, IDEMPOTENCY_HEADER: "grievance-intake-key-1"}
    first = client.post(f"/v1/tenants/{BUSINESS_A}/grievances", json=_INTAKE_BODY, headers=keyed)
    replay = client.post(f"/v1/tenants/{BUSINESS_A}/grievances", json=_INTAKE_BODY, headers=keyed)
    assert first.status_code == 201
    # The replay returns the SAME response (including the same one-time token) and no second row.
    assert replay.json() == first.json()
    assert replay.headers.get("Idempotent-Replay") == "true"
    with client.app.state.session_factory() as session:
        assert (
            session.query(PrivacyGrievance)
            .filter(PrivacyGrievance.business_id == BUSINESS_A)
            .count()
            == 1
        )
