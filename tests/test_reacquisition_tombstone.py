"""DCA-086 — prevent reacquisition of restricted identities (protected minimal tombstones).

Four layers, matching the DCA-078/084/085 suites:

- **Service unit tests** (in-memory SQLite) pin :class:`~app.services.reacquisition.RestrictedIdentityTombstoneStore`
  and the scope-aware :func:`~app.services.reacquisition.reacquisition_decision`:
  - recording a tombstone stores only the PSEUDONYMOUS fingerprint (never the raw identifier) + the
    declared purpose/retention + opaque provenance, and an atomic DCA-026 audit entry WITHOUT the
    fingerprint; re-restricting the same identity is idempotent (reuses the row);
  - the relevant DENIALS (an ordinary user / a read-only viewer without ``data.erase``) change nothing;
  - the comparison is SCOPE-AWARE — it blocks only the SAME business + subject kind + origin +
    fingerprint; a different identity, a different origin, a lapsed ``until_date`` tombstone and another
    tenant are all NOT blocked (unrelated lawful scoped data preserved).
- **Discovery integration** (the acceptance headline): a reimport of a restricted platform-sourced
  identity is screened out ``restricted_identity`` (not resurrected) while unrelated candidates are
  preserved, and another tenant's discovery is unaffected.
- **HTTP tests** (``TestClient`` with the end-user auth contract wired): an admin records a tombstone and
  reads it back (no raw identifier in the projection); a viewer is denied (403); a cross-tenant path is
  denied; a bad body is 422.
- **Postgres-backed test** (``DCA_TEST_DATABASE_URL``) runs the store + unique constraint on real
  transactions.
"""

from __future__ import annotations

import json
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
    IcpProfile,
    ProspectCandidate,
    RestrictedIdentityOrigin,
    RestrictedIdentityTombstone,
    RestrictionRetentionMode,
    TenantAgent,
)
from app.identity.fake import BUSINESS_A, BUSINESS_B, USER_ALICE, USER_CAROL
from app.main import create_app
from app.services.discovery import run_discovery
from app.services.reacquisition import (
    AUDIT_ACTION_RECORD_TOMBSTONE,
    REASON_MISSING_IDENTIFIER,
    REASON_NOT_AUTHORIZED,
    REASON_RESTRICTED_IDENTITY,
    REASON_UNKNOWN_ORIGIN,
    REASON_UNKNOWN_SUBJECT_TYPE,
    ReacquisitionAuthorizationError,
    ReacquisitionError,
    ReacquisitionPrincipal,
    RestrictedIdentityTombstoneStore,
    fingerprint_identity,
    reacquisition_decision,
)

BIZ_A = BUSINESS_A
BIZ_B = BUSINESS_B

ADMIN_A = ReacquisitionPrincipal(subject="00000000-0000-4000-9000-0000000a0086a", roles=("owner",))
USER_A = ReacquisitionPrincipal(subject="00000000-0000-4000-9000-0000000a0086b", roles=("user",))
VIEWER_A = ReacquisitionPrincipal(
    subject="00000000-0000-4000-9000-0000000a0086c", roles=("viewer",)
)

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)

# A synthetic org registry identifier — the raw identity that must NEVER be stored in the tombstone.
RESTRICTED_ID = "U17120MH2026PTC900001"
# A second synthetic org id, left lawful — its unrelated scope must be preserved.
UNRELATED_ID = "U15400GJ2026PTC900002"
PLATFORM = RestrictedIdentityOrigin.PLATFORM.value
TENANT = RestrictedIdentityOrigin.TENANT.value


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


def test_record_stores_only_the_pseudonymous_fingerprint(session_factory):
    with session_factory() as session:
        tombstone = RestrictedIdentityTombstoneStore(session, BIZ_A).record(
            "organization",
            RESTRICTED_ID,
            ADMIN_A,
            origin=PLATFORM,
            identity_scheme="org_identifier",
            origin_kind="erasure_journal",
            origin_ref="journal-1",
            now=NOW,
        )
        session.commit()
        tid = tombstone.id

    with session_factory() as session:
        row = session.get(RestrictedIdentityTombstone, tid)
        assert row is not None
        # The raw identifier is NEVER stored — only its fingerprint, explicitly labelled (not anonymous).
        assert row.identity_hash == fingerprint_identity(
            "organization", "org_identifier", RESTRICTED_ID
        )
        assert row.hash_algorithm == "sha256"
        assert RESTRICTED_ID not in json.dumps(
            {k: str(v) for k, v in row.__dict__.items() if not k.startswith("_")}
        )
        assert row.origin == PLATFORM
        assert row.purpose == "prevent_reacquisition"
        assert row.retention_mode == RestrictionRetentionMode.UNTIL_LIFTED.value
        assert row.authority_scope == "data.erase"
        assert row.origin_ref == "journal-1"


def test_record_audit_omits_the_fingerprint_and_raw_identifier(session_factory):
    with session_factory() as session:
        RestrictedIdentityTombstoneStore(session, BIZ_A).record(
            "organization",
            RESTRICTED_ID,
            ADMIN_A,
            origin=PLATFORM,
            identity_scheme="org_identifier",
        )
        session.commit()

    with session_factory() as session:
        events = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.business_id == BIZ_A)
            .where(AuditEvent.action == AUDIT_ACTION_RECORD_TOMBSTONE)
        ).all()
        assert len(events) == 1
        blob = json.dumps({"reason": events[0].reason, "references": events[0].references})
        # The audit trail keeps only the minimal provenance — never the raw identifier NOR the
        # pseudonymous fingerprint (a pseudonym is still personal data).
        assert RESTRICTED_ID not in blob
        assert fingerprint_identity("organization", "org_identifier", RESTRICTED_ID) not in blob
        assert events[0].references["origin"] == PLATFORM
        assert events[0].references["authorityScope"] == "data.erase"


def test_record_is_idempotent(session_factory):
    with session_factory() as session:
        first = RestrictedIdentityTombstoneStore(session, BIZ_A).record(
            "organization",
            RESTRICTED_ID,
            ADMIN_A,
            origin=PLATFORM,
            identity_scheme="org_identifier",
        )
        session.commit()
        first_id = first.id

    with session_factory() as session:
        # Casing/whitespace differences fold to the same identity → the same tombstone is reused.
        second = RestrictedIdentityTombstoneStore(session, BIZ_A).record(
            "organization",
            f"  {RESTRICTED_ID.lower()}  ",
            ADMIN_A,
            origin=PLATFORM,
            identity_scheme="org_identifier",
        )
        assert second.id == first_id
        session.commit()

    with session_factory() as session:
        assert session.query(RestrictedIdentityTombstone).count() == 1
        # Only the first record audited — the idempotent reuse writes no second audit entry.
        assert (
            session.query(AuditEvent).filter_by(action=AUDIT_ACTION_RECORD_TOMBSTONE).count() == 1
        )


def test_record_denied_for_viewer_changes_nothing(session_factory):
    with session_factory() as session:
        with pytest.raises(ReacquisitionAuthorizationError) as exc:
            RestrictedIdentityTombstoneStore(session, BIZ_A).record(
                "organization", RESTRICTED_ID, VIEWER_A, origin=PLATFORM
            )
        assert exc.value.code == REASON_NOT_AUTHORIZED
        session.commit()

    with session_factory() as session:
        assert session.query(RestrictedIdentityTombstone).count() == 0


def test_record_denied_for_ordinary_user(session_factory):
    with session_factory() as session:
        with pytest.raises(ReacquisitionAuthorizationError) as exc:
            RestrictedIdentityTombstoneStore(session, BIZ_A).record(
                "organization", RESTRICTED_ID, USER_A, origin=PLATFORM
            )
        assert exc.value.code == REASON_NOT_AUTHORIZED


def test_record_bad_body_is_refused(session_factory):
    with session_factory() as session:
        store = RestrictedIdentityTombstoneStore(session, BIZ_A)
        with pytest.raises(ReacquisitionError) as exc:
            store.record("spaceship", RESTRICTED_ID, ADMIN_A, origin=PLATFORM)
        assert exc.value.code == REASON_UNKNOWN_SUBJECT_TYPE
        with pytest.raises(ReacquisitionError) as exc:
            store.record("organization", RESTRICTED_ID, ADMIN_A, origin="satellite")
        assert exc.value.code == REASON_UNKNOWN_ORIGIN
        with pytest.raises(ReacquisitionError) as exc:
            store.record("organization", "   ", ADMIN_A, origin=PLATFORM)
        assert exc.value.code == REASON_MISSING_IDENTIFIER
        assert session.query(RestrictedIdentityTombstone).count() == 0


# -- the scope-aware comparison --------------------------------------------------------------------


def _record(session, business_id, identifier, **kw):
    return RestrictedIdentityTombstoneStore(session, business_id).record(
        kw.pop("subject_type", "organization"),
        identifier,
        ADMIN_A,
        origin=kw.pop("origin", PLATFORM),
        identity_scheme=kw.pop("identity_scheme", "org_identifier"),
        **kw,
    )


def test_decision_blocks_the_restricted_identity(session_factory):
    with session_factory() as session:
        _record(session, BIZ_A, RESTRICTED_ID)
        session.commit()
    with session_factory() as session:
        decision = reacquisition_decision(
            session,
            BIZ_A,
            "organization",
            RESTRICTED_ID,
            PLATFORM,
            identity_scheme="org_identifier",
        )
        assert decision.blocked is True
        assert decision.reason == REASON_RESTRICTED_IDENTITY
        assert decision.tombstone_id is not None


def test_decision_preserves_unrelated_identity_origin_and_tenant(session_factory):
    with session_factory() as session:
        _record(session, BIZ_A, RESTRICTED_ID, origin=PLATFORM)
        session.commit()
    with session_factory() as session:
        # A DIFFERENT identity is not blocked.
        assert not reacquisition_decision(
            session, BIZ_A, "organization", UNRELATED_ID, PLATFORM, identity_scheme="org_identifier"
        ).blocked
        # The SAME identity from a DIFFERENT origin (tenant-supplied) is not blocked — scope-aware.
        assert not reacquisition_decision(
            session, BIZ_A, "organization", RESTRICTED_ID, TENANT, identity_scheme="org_identifier"
        ).blocked
        # The SAME identity in ANOTHER tenant is not blocked — tenant isolation.
        assert not reacquisition_decision(
            session,
            BIZ_B,
            "organization",
            RESTRICTED_ID,
            PLATFORM,
            identity_scheme="org_identifier",
        ).blocked


def test_decision_until_date_tombstone_lapses(session_factory):
    expiry = NOW + timedelta(days=30)
    with session_factory() as session:
        _record(
            session,
            BIZ_A,
            RESTRICTED_ID,
            retention_mode=RestrictionRetentionMode.UNTIL_DATE.value,
            retain_until=expiry,
        )
        session.commit()
    with session_factory() as session:
        # In force up to the retain_until moment.
        assert reacquisition_decision(
            session,
            BIZ_A,
            "organization",
            RESTRICTED_ID,
            PLATFORM,
            identity_scheme="org_identifier",
            now=NOW,
        ).blocked
        # Lapsed afterwards — no longer blocks (unrelated lawful acquisition may resume).
        assert not reacquisition_decision(
            session,
            BIZ_A,
            "organization",
            RESTRICTED_ID,
            PLATFORM,
            identity_scheme="org_identifier",
            now=expiry + timedelta(seconds=1),
        ).blocked


# --------------------------------------------------------------------------------------------------
# Discovery integration — the acceptance headline (reimport cannot resurrect; unrelated preserved)
# --------------------------------------------------------------------------------------------------


def _discovery_settings(**overrides) -> Settings:
    base = {
        "environment": "testing",
        "database_url": "sqlite+pysqlite:///:memory:",
        "service_auth_token": "test-service-token",
        "synthetic_sources_enabled": True,
        "feature_enabled": True,
        "worker_enabled": False,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _enable_tenant(session, business_id) -> None:
    session.add(TenantAgent(business_id=business_id, country="IN", enabled=True))
    # Empty ICP industries/regions → the synthetic registry returns every fixture.
    session.add(
        IcpProfile(business_id=business_id, industries=[], regions=[], roles=[], status="active")
    )


def _decisions(session, business_id) -> dict[str, str]:
    from app.db.models import AcquisitionDecision

    rows = session.scalars(
        select(AcquisitionDecision).where(AcquisitionDecision.business_id == business_id)
    ).all()
    # Last decision per identifier (a later reimport supersedes an earlier one for inspection).
    out: dict[str, str] = {}
    for r in sorted(rows, key=lambda r: r.created_at):
        out[r.org_identifier] = r.decision + ":" + r.reason_code
    return out


def test_reimport_cannot_resurrect_restricted_identity(session_factory):
    settings = _discovery_settings()
    with session_factory() as session:
        _enable_tenant(session, BIZ_A)
        session.commit()

    # 1) First discovery suggests the restricted identity (no tombstone yet).
    with session_factory() as session:
        run_discovery(session, settings, BIZ_A, now=NOW)
    with session_factory() as session:
        first = _decisions(session, BIZ_A)
        assert first[RESTRICTED_ID] == "suggested:ok"
        assert first[UNRELATED_ID] == "suggested:ok"

    # 2) The identity is restricted (a tombstone), then its candidate row is deleted (simulating the
    #    DCA-084 erasure of the resurrected record).
    with session_factory() as session:
        _record(session, BIZ_A, RESTRICTED_ID, origin=PLATFORM, origin_kind="erasure")
        session.query(ProspectCandidate).filter_by(
            business_id=BIZ_A, org_identifier=RESTRICTED_ID
        ).delete()
        session.commit()

    # 3) A reimport from the SAME source must NOT resurrect it, while the unrelated candidate is kept.
    with session_factory() as session:
        run_discovery(session, settings, BIZ_A, now=NOW)
    with session_factory() as session:
        again = _decisions(session, BIZ_A)
        # Restricted → screened out as restricted_identity (reacquisition prevented, not resurrected).
        assert again[RESTRICTED_ID] == "screened_out:" + REASON_RESTRICTED_IDENTITY
        # The restricted candidate was NOT recreated.
        assert (
            session.query(ProspectCandidate)
            .filter_by(business_id=BIZ_A, org_identifier=RESTRICTED_ID)
            .count()
            == 0
        )
        # The unrelated lawful candidate is preserved (still present from the first run → duplicate).
        assert again[UNRELATED_ID] == "screened_out:duplicate"
        assert (
            session.query(ProspectCandidate)
            .filter_by(business_id=BIZ_A, org_identifier=UNRELATED_ID)
            .count()
            == 1
        )


def test_reimport_block_is_tenant_scoped(session_factory):
    settings = _discovery_settings()
    with session_factory() as session:
        _enable_tenant(session, BIZ_A)
        _enable_tenant(session, BIZ_B)
        # Restrict the identity in A only.
        _record(session, BIZ_A, RESTRICTED_ID, origin=PLATFORM)
        session.commit()

    with session_factory() as session:
        run_discovery(session, settings, BIZ_B, now=NOW)
    with session_factory() as session:
        b = _decisions(session, BIZ_B)
        # B is unaffected by A's tombstone — it may lawfully acquire the same identity.
        assert b[RESTRICTED_ID] == "suggested:ok"
        assert (
            session.query(ProspectCandidate)
            .filter_by(business_id=BIZ_B, org_identifier=RESTRICTED_ID)
            .count()
            == 1
        )


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
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def test_http_admin_records_and_reads_tombstone(client):
    resp = client.post(
        f"/v1/tenants/{BUSINESS_A}/restricted-identities",
        json={
            "subjectType": "organization",
            "identifier": RESTRICTED_ID,
            "origin": PLATFORM,
            "identityScheme": "org_identifier",
            "originRef": "journal-1",
        },
        headers=_bearer(_mint(subject=USER_ALICE)),  # Alice: owner of A (administrator)
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # No raw identifier in the projection — only the pseudonymous fingerprint, labelled with its algo.
    assert RESTRICTED_ID not in json.dumps(body)
    assert body["identityHash"] == fingerprint_identity(
        "organization", "org_identifier", RESTRICTED_ID
    )
    assert body["hashAlgorithm"] == "sha256"

    read = client.get(
        f"/v1/tenants/{BUSINESS_A}/restricted-identities",
        headers=_bearer(_mint(subject=USER_ALICE)),
    )
    assert read.status_code == 200
    rows = read.json()
    assert len(rows) == 1
    assert RESTRICTED_ID not in json.dumps(rows)


def test_http_viewer_denied(client):
    # Carol is an active viewer in A — a member, but without data.erase.
    resp = client.post(
        f"/v1/tenants/{BUSINESS_A}/restricted-identities",
        json={"subjectType": "organization", "identifier": RESTRICTED_ID},
        headers=_bearer(_mint(subject=USER_CAROL)),
    )
    assert resp.status_code == 403
    with client.app.state.session_factory() as session:
        assert session.query(RestrictedIdentityTombstone).count() == 0  # nothing written


def test_http_cross_tenant_is_denied(client):
    # Carol holds no membership in B → denied before anything is written.
    resp = client.post(
        f"/v1/tenants/{BUSINESS_B}/restricted-identities",
        json={"subjectType": "organization", "identifier": RESTRICTED_ID},
        headers=_bearer(_mint(subject=USER_CAROL)),
    )
    assert resp.status_code == 403


def test_http_bad_body_is_422(client):
    resp = client.post(
        f"/v1/tenants/{BUSINESS_A}/restricted-identities",
        json={"subjectType": "organization", "identifier": RESTRICTED_ID, "origin": "satellite"},
        headers=_bearer(_mint(subject=USER_ALICE)),
    )
    assert resp.status_code == 422
    with client.app.state.session_factory() as session:
        assert session.query(RestrictedIdentityTombstone).count() == 0


# --------------------------------------------------------------------------------------------------
# Postgres-backed test — disposable PostgreSQL (real transactions + the unique constraint)
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)
_PG_SCHEMA = "dca_086_test"


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
def test_pg_record_is_idempotent_under_unique_constraint(pg_session_factory):
    """On real Postgres: re-restricting the same identity reuses the one row (unique within business)."""
    assert make_url(PG_URL).get_backend_name() == "postgresql"
    with pg_session_factory() as session:
        _record(session, BIZ_A, RESTRICTED_ID, origin=PLATFORM)
        session.commit()
    with pg_session_factory() as session:
        _record(session, BIZ_A, RESTRICTED_ID, origin=PLATFORM)
        session.commit()
    with pg_session_factory() as session:
        assert session.query(RestrictedIdentityTombstone).filter_by(business_id=BIZ_A).count() == 1
        # The scope-aware comparison blocks the restricted identity on real transactions.
        assert reacquisition_decision(
            session,
            BIZ_A,
            "organization",
            RESTRICTED_ID,
            PLATFORM,
            identity_scheme="org_identifier",
        ).blocked
