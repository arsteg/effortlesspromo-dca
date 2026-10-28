"""DCA-083 — resolve a privacy request's affected scope.

Three layers, matching the DCA-081/082 suites:

- **Service unit tests** (in-memory SQLite) pin
  :class:`~app.services.privacy_scope.PrivacyScopeResolver`:
  - a LOCAL grievance resolution identifies ONLY the filing business's records that pertain to the
    requester (matched by the one-way contact digest), names that single business as the data fiduciary,
    derives the permissible action from the category, and NEVER selects another tenant's records even
    when that tenant holds a record for the same subject — a request for one tenant does not
    automatically select every tenant's relationship;
  - platform-controlled source scope a local operator cannot resolve is surfaced as ``needs_review``
    (``unresolved_authority``), and a grievance with no subject contact flags it too;
  - a PLATFORM-SOURCE removal resolution identifies EVERY tenant's records from the removed source and
    names the platform source controller + each affected tenant as a fiduciary (nothing unresolved);
  - resolution EXECUTES nothing (``actions_executable`` is always False; no candidate is deleted or
    changed), so even an erasure request only NAMES ``erase_personal_data``;
  - a DENIED resolution (a read-only viewer locally, a non-operator for a platform-wide removal) is
    refused BEFORE any state is read, and so is a plane mismatch.
- **HTTP tests** (``TestClient`` with the end-user auth contract wired) prove the real API: the operator
  local-scope read requires ``grievance.read`` (viewer / platform operator denied), a cross-tenant id is
  404; the platform-source read requires the platform scope (tenant admin denied) and spans tenants.
- **Postgres-backed test** (``DCA_TEST_DATABASE_URL``) runs the resolver against a disposable PostgreSQL
  (real transactions, not SQLite): with the SAME subject present in Business A AND B, a local resolution
  for A selects only A's record while the platform-source resolution spans both.

The resolver writes nothing, so there is no new table / migration / RLS surface (``privacy_grievances``
and the restriction/candidate tables are already covered in ``tests/test_rls.py``).
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
from app.db.models import (
    DiscoveryRun,
    PlatformSourceRestriction,
    ProspectCandidate,
    RestrictionAuthorityPlane,
)
from app.identity.fake import BUSINESS_A, BUSINESS_B, USER_ALICE, USER_CAROL
from app.main import create_app
from app.services.grievance_intake import (
    GrievanceIntakeInput,
    PrivacyGrievanceStore,
    contact_digest,
)
from app.services.privacy_scope import (
    CONTROLLER_DATA_FIDUCIARY,
    CONTROLLER_SOURCE_CONTROLLER,
    Controller,
    REASON_NOT_AUTHORIZED,
    REASON_NOT_PLATFORM_OPERATOR,
    REASON_PLANE_MISMATCH,
    REQUEST_KIND_LOCAL,
    REQUEST_KIND_PLATFORM_SOURCE,
    REVIEW_PLATFORM_SOURCE_SCOPE,
    REVIEW_SUBJECT_CONTACT_NOT_PROVIDED,
    GrievanceScopePrincipal,
    PlatformScopePrincipal,
    PrivacyScopeAuthorizationError,
    PrivacyScopeError,
    PrivacyScopeResolver,
)
from tests.conftest import TEST_TOKEN, make_settings

BIZ_A = BUSINESS_A
BIZ_B = BUSINESS_B
SUBJECT = (
    "aggrieved.person@synthetic.example"  # synthetic; stored/compared only as a one-way digest
)
OTHER = "someone.else@synthetic.example"

OWNER_A = GrievanceScopePrincipal(subject="00000000-0000-4000-9000-0000000a0083a", roles=("owner",))
VIEWER_A = GrievanceScopePrincipal(
    subject="00000000-0000-4000-9000-0000000a0083b", roles=("viewer",)
)
OPERATOR = PlatformScopePrincipal(
    subject="00000000-0000-4000-9000-0000000a0083c", roles=("platform_operator",)
)
TENANT_ADMIN = PlatformScopePrincipal(
    subject="00000000-0000-4000-9000-0000000a0083d", roles=("admin",)
)


# --------------------------------------------------------------------------------------------------
# Shared builders
# --------------------------------------------------------------------------------------------------


def _add_candidate(
    session,
    business_id,
    *,
    org,
    status="suggested",
    source_key="synthetic.mca",
    contact=None,
) -> ProspectCandidate:
    candidate = ProspectCandidate(
        business_id=business_id,
        run_id="run-synthetic",
        org_name="Synthetic Co",
        org_identifier=org,
        contact=contact if contact is not None else {"email": f"{org}@synthetic.example"},
        source_key=source_key,
        source_ref=f"synthetic://mca/{org}",
        trust_tier="official_registry",
        match_score=1.0,
        status=status,
    )
    session.add(candidate)
    return candidate


def _record_grievance(session, business_id, *, category="access", contact=SUBJECT):
    return PrivacyGrievanceStore(session, business_id).record(
        GrievanceIntakeInput(category=category, contact=contact, channel="email"),
        response_deadline_days=30,
    )


def _platform_restriction(source_key, *, version=1) -> PlatformSourceRestriction:
    return PlatformSourceRestriction(
        id=f"plat-{source_key}-{version}",
        source_key=source_key,
        version=version,
        authority_plane=RestrictionAuthorityPlane.PLATFORM.value,
        authority_scope="platform.source.restrict",
        authorized_by=OPERATOR.subject,
        justification="synthetic removal",
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


def test_local_resolution_selects_only_the_filing_tenants_records(session_factory):
    with session_factory() as session:
        # A holds a matching record for the subject AND an unrelated one; B holds a record for the SAME
        # subject — a one-tenant request must never select B's relationship.
        _add_candidate(session, BIZ_A, org="a-subject", contact={"email": SUBJECT})
        _add_candidate(session, BIZ_A, org="a-other", contact={"email": OTHER})
        _add_candidate(session, BIZ_B, org="b-subject", contact={"email": SUBJECT})
        issued = _record_grievance(session, BIZ_A)
        session.commit()
        gid = issued.grievance.id
        matched_id = next(
            c.id for c in session.query(ProspectCandidate).filter_by(org_identifier="a-subject")
        )

    with session_factory() as session:
        grievance = PrivacyGrievanceStore(session, BIZ_A).get(gid)
        resolution = PrivacyScopeResolver(session).resolve_local(grievance, OWNER_A)

    assert resolution.request_kind == REQUEST_KIND_LOCAL
    assert resolution.authority_plane == "business"
    # Only A's matching record — never A's unrelated record, never B's same-subject record.
    assert resolution.record_count == 1
    assert resolution.affected_records[0].record_id == matched_id
    assert resolution.affected_business_ids == [BIZ_A]
    # The one business is named the data fiduciary.
    assert resolution.controllers == [Controller(role=CONTROLLER_DATA_FIDUCIARY, business_id=BIZ_A)]
    # access → provide_access_summary, identified but NOT executable.
    assert resolution.permissible_actions == ["provide_access_summary"]
    assert resolution.actions_executable is False
    # The matched record came from a platform-controlled source → that platform-wide scope needs review.
    assert resolution.needs_review is True
    assert resolution.unresolved_authority == [f"{REVIEW_PLATFORM_SOURCE_SCOPE}:synthetic.mca"]


def test_local_resolution_matches_by_digest_never_raw_contact(session_factory):
    with session_factory() as session:
        _add_candidate(session, BIZ_A, org="a-subject", contact={"email": SUBJECT})
        issued = _record_grievance(session, BIZ_A)
        session.commit()
        gid = issued.grievance.id
        # The grievance stores only the one-way digest (never the raw contact).
        assert issued.grievance.requester_contact_digest == contact_digest(SUBJECT)
        assert SUBJECT not in issued.grievance.requester_contact_digest

    with session_factory() as session:
        grievance = PrivacyGrievanceStore(session, BIZ_A).get(gid)
        resolution = PrivacyScopeResolver(session).resolve_local(grievance, OWNER_A)
    assert resolution.subject_digest == contact_digest(SUBJECT)
    assert resolution.record_count == 1


def test_local_resolution_erasure_identifies_action_but_deletes_nothing(session_factory):
    with session_factory() as session:
        _add_candidate(session, BIZ_A, org="a-subject", contact={"email": SUBJECT})
        issued = _record_grievance(session, BIZ_A, category="erasure")
        session.commit()
        gid = issued.grievance.id

    with session_factory() as session:
        grievance = PrivacyGrievanceStore(session, BIZ_A).get(gid)
        resolution = PrivacyScopeResolver(session).resolve_local(grievance, OWNER_A)
        # Erasure is NAMED as a permissible action, but execution is disabled — nothing is deleted.
        assert resolution.permissible_actions == ["erase_personal_data"]
        assert resolution.actions_executable is False

    with session_factory() as session:
        # No unintended state change: the record still exists, untouched (no deletion executed).
        remaining = session.query(ProspectCandidate).filter_by(org_identifier="a-subject").all()
        assert len(remaining) == 1
        assert remaining[0].status == "suggested"


def test_local_resolution_without_subject_contact_flags_review(session_factory):
    with session_factory() as session:
        _add_candidate(session, BIZ_A, org="a-subject", contact={"email": SUBJECT})
        # A grievance with no contact supplied has an empty subject digest — the scope cannot be resolved.
        issued = _record_grievance(session, BIZ_A, contact="")
        session.commit()
        gid = issued.grievance.id

    with session_factory() as session:
        grievance = PrivacyGrievanceStore(session, BIZ_A).get(gid)
        resolution = PrivacyScopeResolver(session).resolve_local(grievance, OWNER_A)
    # No records selected (never over-selected), and the gap is flagged for review rather than guessed.
    assert resolution.record_count == 0
    assert resolution.needs_review is True
    assert resolution.unresolved_authority == [REVIEW_SUBJECT_CONTACT_NOT_PROVIDED]


def test_local_resolution_own_data_source_needs_no_platform_review(session_factory):
    # A tenant's OWN-data record (no source key) carries no platform-controlled scope — nothing unresolved.
    with session_factory() as session:
        _add_candidate(session, BIZ_A, org="a-subject", source_key="", contact={"email": SUBJECT})
        issued = _record_grievance(session, BIZ_A)
        session.commit()
        gid = issued.grievance.id

    with session_factory() as session:
        grievance = PrivacyGrievanceStore(session, BIZ_A).get(gid)
        resolution = PrivacyScopeResolver(session).resolve_local(grievance, OWNER_A)
    assert resolution.record_count == 1
    assert resolution.needs_review is False
    assert resolution.unresolved_authority == []


def test_local_resolution_denied_for_viewer_reads_nothing(session_factory):
    with session_factory() as session:
        _add_candidate(session, BIZ_A, org="a-subject", contact={"email": SUBJECT})
        issued = _record_grievance(session, BIZ_A)
        session.commit()
        gid = issued.grievance.id

    with session_factory() as session:
        grievance = PrivacyGrievanceStore(session, BIZ_A).get(gid)
        with pytest.raises(PrivacyScopeAuthorizationError) as exc:
            PrivacyScopeResolver(session).resolve_local(grievance, VIEWER_A)
        assert exc.value.code == REASON_NOT_AUTHORIZED

    with session_factory() as session:
        # The relevant denial changed nothing: the record is still present and unchanged.
        assert session.query(ProspectCandidate).filter_by(org_identifier="a-subject").count() == 1


def test_platform_source_resolution_spans_every_affected_tenant(session_factory):
    with session_factory() as session:
        _add_candidate(session, BIZ_A, org="a-removed", source_key="src.gone")
        _add_candidate(session, BIZ_B, org="b-removed", source_key="src.gone")
        _add_candidate(session, BIZ_A, org="a-kept", source_key="src.other")
        session.add(_platform_restriction("src.gone"))
        session.commit()

    with session_factory() as session:
        restriction = session.get(PlatformSourceRestriction, _platform_restriction("src.gone").id)
        resolution = PrivacyScopeResolver(session).resolve_platform_source(restriction, OPERATOR)

    assert resolution.request_kind == REQUEST_KIND_PLATFORM_SOURCE
    assert resolution.authority_plane == "platform"
    assert resolution.source_key == "src.gone"
    # Every tenant's copy from the removed source — spanning tenants (a copy from another source excluded).
    assert resolution.record_count == 2
    assert resolution.affected_business_ids == sorted([BIZ_A, BIZ_B])
    # The platform is the source controller; each affected tenant is a data fiduciary.
    roles = [(c.role, c.source_key, c.business_id) for c in resolution.controllers]
    assert (CONTROLLER_SOURCE_CONTROLLER, "src.gone", "") in roles
    assert (CONTROLLER_DATA_FIDUCIARY, "", BIZ_A) in roles
    assert (CONTROLLER_DATA_FIDUCIARY, "", BIZ_B) in roles
    assert resolution.permissible_actions == ["remove_platform_source_copies"]
    assert resolution.actions_executable is False
    # The operator holds authority over the full platform-wide scope — nothing unresolved.
    assert resolution.needs_review is False


def test_platform_source_resolution_denied_for_non_operator(session_factory):
    with session_factory() as session:
        _add_candidate(session, BIZ_A, org="a-removed", source_key="src.gone")
        session.add(_platform_restriction("src.gone"))
        session.commit()

    with session_factory() as session:
        restriction = session.get(PlatformSourceRestriction, _platform_restriction("src.gone").id)
        with pytest.raises(PrivacyScopeAuthorizationError) as exc:
            PrivacyScopeResolver(session).resolve_platform_source(restriction, TENANT_ADMIN)
        assert exc.value.code == REASON_NOT_PLATFORM_OPERATOR

    with session_factory() as session:
        assert session.query(ProspectCandidate).filter_by(org_identifier="a-removed").count() == 1


def test_platform_source_resolution_rejects_business_plane_row(session_factory):
    with session_factory() as session:
        restriction = _platform_restriction("src.gone")
        restriction.authority_plane = RestrictionAuthorityPlane.BUSINESS.value  # wrong plane
        session.add(restriction)
        session.commit()

    with session_factory() as session:
        restriction = session.get(PlatformSourceRestriction, _platform_restriction("src.gone").id)
        with pytest.raises(PrivacyScopeError) as exc:
            PrivacyScopeResolver(session).resolve_platform_source(restriction, OPERATOR)
        assert exc.value.code == REASON_PLANE_MISMATCH


# --------------------------------------------------------------------------------------------------
# HTTP tests (TestClient) — the real service API with the end-user auth contract wired
# --------------------------------------------------------------------------------------------------

ISSUER = "https://issuer.dca.test"
AUDIENCE = "dca-api"
SECRET = "dca-test-signing-secret-value"  # synthetic HS256 key, not a real credential
PLATFORM_OP = "00000000-0000-4000-9000-00000090e783"  # platform operator; no business membership
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


@pytest.fixture
def client() -> TestClient:
    app = create_app(_auth_settings())
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _seed_local(client: TestClient) -> str:
    """Record an A grievance (public intake) + seed a matching A candidate; return the grievance id."""
    created = client.post(
        f"/v1/tenants/{BUSINESS_A}/grievances",
        json={"category": "access", "contact": SUBJECT, "channel": "email"},
        headers=SERVICE_AUTH,
    )
    assert created.status_code == 201, created.text
    with client.app.state.session_factory() as session:
        _add_candidate(session, BUSINESS_A, org="a-subject", contact={"email": SUBJECT})
        _add_candidate(session, BUSINESS_B, org="b-subject", contact={"email": SUBJECT})
        session.commit()
    return created.json()["grievanceId"]


def test_http_operator_resolves_local_scope(client):
    gid = _seed_local(client)
    resp = client.get(
        f"/v1/tenants/{BUSINESS_A}/grievances/{gid}/affected-scope",
        headers=_bearer(_mint(subject=USER_ALICE)),  # Alice: owner of A
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["requestKind"] == REQUEST_KIND_LOCAL
    assert body["recordCount"] == 1  # only A's record, never B's same-subject record
    assert body["affectedBusinessIds"] == [BUSINESS_A]
    assert body["permissibleActions"] == ["provide_access_summary"]
    assert body["actionsExecutable"] is False
    assert body["needsReview"] is True  # platform-controlled source scope flagged
    # No raw contact / org name leaks into the resolution projection.
    import json

    assert SUBJECT not in json.dumps(body)


def test_http_local_scope_requires_grievance_read(client):
    gid = _seed_local(client)
    # Carol is an active viewer in A — a member, but without grievance.read.
    resp = client.get(
        f"/v1/tenants/{BUSINESS_A}/grievances/{gid}/affected-scope",
        headers=_bearer(_mint(subject=USER_CAROL)),
    )
    assert resp.status_code == 403


def test_http_local_scope_platform_operator_denied(client):
    gid = _seed_local(client)
    # Global operator rights do NOT imply membership: denied before the scope check.
    resp = client.get(
        f"/v1/tenants/{BUSINESS_A}/grievances/{gid}/affected-scope",
        headers=_bearer(_mint(subject=PLATFORM_OP, roles=["platform_operator"])),
    )
    assert resp.status_code == 403


def test_http_local_scope_unknown_id_is_404(client):
    _seed_local(client)
    resp = client.get(
        f"/v1/tenants/{BUSINESS_A}/grievances/no-such-id/affected-scope",
        headers=_bearer(_mint(subject=USER_ALICE)),
    )
    assert resp.status_code == 404


def test_http_platform_source_scope_for_operator(client):
    with client.app.state.session_factory() as session:
        _add_candidate(session, BUSINESS_A, org="a-removed", source_key="src.gone")
        _add_candidate(session, BUSINESS_B, org="b-removed", source_key="src.gone")
        session.add(_platform_restriction("src.gone"))
        session.commit()
    resp = client.get(
        "/v1/platform/source-removals/src.gone/affected-scope",
        headers=_bearer(_mint(subject=PLATFORM_OP, roles=["platform_operator"])),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["requestKind"] == REQUEST_KIND_PLATFORM_SOURCE
    assert body["sourceKey"] == "src.gone"
    assert body["recordCount"] == 2
    assert body["affectedBusinessIds"] == sorted([BUSINESS_A, BUSINESS_B])
    assert body["actionsExecutable"] is False


def test_http_platform_source_scope_tenant_admin_denied(client):
    with client.app.state.session_factory() as session:
        session.add(_platform_restriction("src.gone"))
        session.commit()
    # Bob is an admin of B (business plane), not a platform operator.
    from app.identity.fake import USER_BOB

    resp = client.get(
        "/v1/platform/source-removals/src.gone/affected-scope",
        headers=_bearer(_mint(subject=USER_BOB)),
    )
    assert resp.status_code == 403


def test_http_platform_source_scope_unknown_source_is_404(client):
    resp = client.get(
        "/v1/platform/source-removals/never.catalogued/affected-scope",
        headers=_bearer(_mint(subject=PLATFORM_OP, roles=["platform_operator"])),
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------------------------------
# Postgres-backed test — disposable PostgreSQL (real transactions, not SQLite)
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)
_PG_SCHEMA = "dca_083_test"


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
def test_pg_local_scope_excludes_other_tenant_platform_spans_both(pg_session_factory):
    """On real Postgres: the SAME subject sits in A and B; a LOCAL resolution for A selects only A's
    record (a one-tenant request never selects B's relationship), while the PLATFORM-SOURCE resolution
    spans both tenants — the two proposed affected sets differ exactly as the acceptance requires."""
    assert make_url(PG_URL).get_backend_name() == "postgresql"
    with pg_session_factory() as session:
        # Postgres enforces the candidate→discovery_runs FK, so seed the referenced run first.
        session.add(DiscoveryRun(id="run-synthetic", business_id=BIZ_A, status="completed"))
        session.flush()
        _add_candidate(
            session, BIZ_A, org="a-subject", source_key="src.gone", contact={"email": SUBJECT}
        )
        _add_candidate(
            session, BIZ_B, org="b-subject", source_key="src.gone", contact={"email": SUBJECT}
        )
        issued = _record_grievance(session, BIZ_A)
        session.add(_platform_restriction("src.gone"))
        session.commit()
        gid = issued.grievance.id

    with pg_session_factory() as session:
        grievance = PrivacyGrievanceStore(session, BIZ_A).get(gid)
        local = PrivacyScopeResolver(session).resolve_local(grievance, OWNER_A)
        restriction = session.get(PlatformSourceRestriction, _platform_restriction("src.gone").id)
        platform = PrivacyScopeResolver(session).resolve_platform_source(restriction, OPERATOR)

    # Local: only A's record; never B's same-subject relationship.
    assert local.affected_business_ids == [BIZ_A]
    assert local.record_count == 1
    # Platform-source: every affected tenant.
    assert platform.affected_business_ids == sorted([BIZ_A, BIZ_B])
    assert platform.record_count == 2

    with pg_session_factory() as session:
        # Read-only: both resolutions changed nothing (no deletion executed).
        assert session.query(ProspectCandidate).count() == 2
