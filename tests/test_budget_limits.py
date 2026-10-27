"""DCA-042 — store per-business spending limits (validated, versioned INR caps + timezone).

Two layers, matching the DCA-039/040 suites:

- **Service unit tests** (in-memory SQLite) pin the store logic: validation (INR only, non-negative +
  coherent caps, India timezone normalized) runs before anything is staged; a save always inserts a
  NEW version and preserves prior ones; each save stages an atomic audit entry; limits are scoped per
  business; and the store stages (never commits) into the caller's session.
- **HTTP tests** (``TestClient`` with the end-user auth contract wired, like
  ``tests/test_operation_permissions.py``) prove the authorization gate and the developer manual-test
  flow over the real API: an administrator (owner/admin) can set/raise caps; an ordinary user and a
  non-member (platform operator) cannot (403, nothing staged); a negative/invalid value is a 422 with
  nothing staged; and one business's change never touches another's configuration.

SQLite is sufficient here — the behaviour is app-level (per-tenant versioning + validation + audit +
authorization); the transaction/RLS/migration paths are covered by ``tests/test_rls.py`` and
``tests/test_migrations.py`` (which pin ``budget_limits`` into the RLS set and its dual-path migration
on disposable Postgres).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient

from app.audit.sink import ActorType, AuditActor
from app.core.config import Settings
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import AuditEvent, BudgetLimit
from app.identity.fake import BUSINESS_A, BUSINESS_B, USER_ALICE, USER_BOB, USER_CAROL
from app.main import create_app
from app.services.budget_store import (
    AUDIT_ACTION_SET_LIMITS,
    CANONICAL_TIMEZONE,
    REASON_DAILY_EXCEEDS_MONTHLY,
    REASON_NEGATIVE_CAP,
    REASON_PER_RUN_EXCEEDS_DAILY,
    REASON_UNSUPPORTED_CURRENCY,
    REASON_UNSUPPORTED_TIMEZONE,
    BudgetLimitInput,
    BudgetLimitStore,
    BudgetValidationError,
)
from tests.conftest import make_settings

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"
ACTOR = AuditActor(id="00000000-0000-4000-9000-0000000a0042", type=ActorType.USER)

# A coherent example limit set: per-run ₹500 <= daily ₹2,000 <= monthly ₹50,000 (in paise).
VALID = BudgetLimitInput(
    daily_cap_minor=200_000, monthly_cap_minor=5_000_000, per_run_cap_minor=50_000
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


def test_save_stores_precise_minor_units_and_audits(session_factory):
    with session_factory() as session:
        limit = BudgetLimitStore(session, BIZ_A).save(VALID, ACTOR)
        session.commit()
        assert limit.version == 1
        assert limit.currency == "INR"
        assert (limit.daily_cap_minor, limit.monthly_cap_minor, limit.per_run_cap_minor) == (
            200_000,
            5_000_000,
            50_000,
        )
        assert limit.timezone == CANONICAL_TIMEZONE
        assert limit.actor_id == ACTOR.id

    with session_factory() as session:
        audits = session.query(AuditEvent).filter(AuditEvent.business_id == BIZ_A).all()
        assert [a.action for a in audits] == [AUDIT_ACTION_SET_LIMITS]
        assert audits[0].references["version"] == 1
        assert audits[0].references["dailyCapMinor"] == 200_000
        assert audits[0].references["currency"] == "INR"


def test_each_save_inserts_a_new_version_and_preserves_prior(session_factory):
    with session_factory() as session:
        BudgetLimitStore(session, BIZ_A).save(VALID, ACTOR)
        session.commit()
    # A second save = a new version; the first is retained, never mutated.
    with session_factory() as session:
        raised = BudgetLimitInput(
            daily_cap_minor=400_000, monthly_cap_minor=8_000_000, per_run_cap_minor=100_000
        )
        v2 = BudgetLimitStore(session, BIZ_A).save(raised, ACTOR)
        session.commit()
        assert v2.version == 2

    with session_factory() as session:
        store = BudgetLimitStore(session, BIZ_A)
        versions = store.versions()
        assert [v.version for v in versions] == [1, 2]
        assert versions[0].daily_cap_minor == 200_000  # v1 unchanged
        assert store.latest().version == 2 and store.latest().daily_cap_minor == 400_000


@pytest.mark.parametrize(
    "kwargs,code",
    [
        ({"currency": "USD"}, REASON_UNSUPPORTED_CURRENCY),
        ({"daily_cap_minor": -1}, REASON_NEGATIVE_CAP),
        ({"per_run_cap_minor": -5}, REASON_NEGATIVE_CAP),
        # per-run (300_000) > daily (200_000)
        ({"per_run_cap_minor": 300_000}, REASON_PER_RUN_EXCEEDS_DAILY),
        # daily (6_000_000) > monthly (5_000_000)
        ({"daily_cap_minor": 6_000_000}, REASON_DAILY_EXCEEDS_MONTHLY),
        ({"timezone": "America/New_York"}, REASON_UNSUPPORTED_TIMEZONE),
        ({"timezone": "not-a-zone"}, REASON_UNSUPPORTED_TIMEZONE),
    ],
)
def test_invalid_limits_are_refused_and_stage_nothing(session_factory, kwargs, code):
    base = {
        "daily_cap_minor": VALID.daily_cap_minor,
        "monthly_cap_minor": VALID.monthly_cap_minor,
        "per_run_cap_minor": VALID.per_run_cap_minor,
    }
    base.update(kwargs)
    with session_factory() as session:
        with pytest.raises(BudgetValidationError) as exc:
            BudgetLimitStore(session, BIZ_A).save(BudgetLimitInput(**base), ACTOR)
        assert exc.value.code == code
        session.rollback()
    # Refusal left no row and no audit.
    with session_factory() as session:
        assert session.query(BudgetLimit).count() == 0
        assert session.query(AuditEvent).count() == 0


def test_timezone_alias_is_normalized(session_factory):
    with session_factory() as session:
        data = BudgetLimitInput(
            daily_cap_minor=1, monthly_cap_minor=1, per_run_cap_minor=1, timezone="Asia/Calcutta"
        )
        limit = BudgetLimitStore(session, BIZ_A).save(data, ACTOR)
        assert limit.timezone == CANONICAL_TIMEZONE  # legacy alias -> canonical


def test_zero_caps_are_allowed(session_factory):
    # A zero ceiling is a valid (effectively no-spend) configuration; only negatives are rejected.
    with session_factory() as session:
        limit = BudgetLimitStore(session, BIZ_A).save(
            BudgetLimitInput(daily_cap_minor=0, monthly_cap_minor=0, per_run_cap_minor=0), ACTOR
        )
        assert limit.daily_cap_minor == 0


def test_limits_are_scoped_per_business(session_factory):
    with session_factory() as session:
        BudgetLimitStore(session, BIZ_A).save(VALID, ACTOR)
        session.commit()
    with session_factory() as session:
        # B has its own independent version numbering and never sees A's rows.
        assert BudgetLimitStore(session, BIZ_B).latest() is None
        assert BudgetLimitStore(session, BIZ_B).versions() == []
        b = BudgetLimitStore(session, BIZ_B).save(VALID, ACTOR)
        assert b.version == 1  # independent of A's version 1


def test_save_does_not_commit(session_factory):
    with session_factory() as session:
        BudgetLimitStore(session, BIZ_A).save(VALID, ACTOR)
        session.rollback()  # the store never commits — a rollback leaves nothing behind
    with session_factory() as session:
        assert session.query(BudgetLimit).count() == 0
        assert session.query(AuditEvent).count() == 0


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


_LIMIT_BODY = {
    "dailyCapMinor": 200_000,
    "monthlyCapMinor": 5_000_000,
    "perRunCapMinor": 50_000,
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
                AuditEvent.action == AUDIT_ACTION_SET_LIMITS,
            )
            .count()
        )


def test_http_admin_can_set_limits_and_change_is_audited(client):
    resp = client.post(
        f"/v1/tenants/{BUSINESS_A}/budget/limits",
        json=_LIMIT_BODY,
        headers=_bearer(_mint(subject=USER_ALICE)),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["version"] == 1
    assert body["currency"] == "INR"
    assert body["dailyCapMinor"] == 200_000 and body["dailyCapMajor"] == 2000.0
    assert body["timezone"] == CANONICAL_TIMEZONE
    assert _audit_count(client, BUSINESS_A) == 1  # the change was audited


def test_http_ordinary_user_cannot_increase_caps(client):
    # Carol is an active viewer in A — a real member, but without the budget.manage scope.
    resp = client.post(
        f"/v1/tenants/{BUSINESS_A}/budget/limits",
        json=_LIMIT_BODY,
        headers=_bearer(_mint(subject=USER_CAROL)),
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    # Nothing staged: no limit version, no audit.
    assert _audit_count(client, BUSINESS_A) == 0
    with client.app.state.session_factory() as session:
        assert session.query(BudgetLimit).filter(BudgetLimit.business_id == BUSINESS_A).count() == 0


def test_http_platform_operator_cannot_set_client_caps(client):
    # Global operator rights do NOT imply membership: no active membership in A => denied before the
    # scope check even runs. No universal bypass, no impersonation.
    resp = client.post(
        f"/v1/tenants/{BUSINESS_A}/budget/limits",
        json=_LIMIT_BODY,
        headers=_bearer(_mint(subject=OPERATOR, roles=["platform_operator"])),
    )
    assert resp.status_code == 403
    assert _audit_count(client, BUSINESS_A) == 0


def test_http_unauthenticated_request_is_401(client):
    resp = client.post(f"/v1/tenants/{BUSINESS_A}/budget/limits", json=_LIMIT_BODY)
    assert resp.status_code == 401


@pytest.mark.parametrize(
    "overrides,code",
    [
        ({"dailyCapMinor": -1}, REASON_NEGATIVE_CAP),
        ({"currency": "USD"}, REASON_UNSUPPORTED_CURRENCY),
        ({"timezone": "America/New_York"}, REASON_UNSUPPORTED_TIMEZONE),
        ({"perRunCapMinor": 999_999_999}, REASON_PER_RUN_EXCEEDS_DAILY),
    ],
)
def test_http_invalid_values_are_422_and_stage_nothing(client, overrides, code):
    body = {**_LIMIT_BODY, **overrides}
    resp = client.post(
        f"/v1/tenants/{BUSINESS_A}/budget/limits",
        json=body,
        headers=_bearer(_mint(subject=USER_ALICE)),
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["message"] == code
    # A rejected write stages no version and no audit.
    assert _audit_count(client, BUSINESS_A) == 0
    with client.app.state.session_factory() as session:
        assert session.query(BudgetLimit).filter(BudgetLimit.business_id == BUSINESS_A).count() == 0


def test_http_unsupported_field_is_422(client):
    body = {**_LIMIT_BODY, "autoApprove": True}
    resp = client.post(
        f"/v1/tenants/{BUSINESS_A}/budget/limits",
        json=body,
        headers=_bearer(_mint(subject=USER_ALICE)),
    )
    assert resp.status_code == 422


def test_http_versions_are_preserved_and_listed(client):
    alice = _bearer(_mint(subject=USER_ALICE))
    client.post(f"/v1/tenants/{BUSINESS_A}/budget/limits", json=_LIMIT_BODY, headers=alice)
    raised = {"dailyCapMinor": 400_000, "monthlyCapMinor": 8_000_000, "perRunCapMinor": 100_000}
    client.post(f"/v1/tenants/{BUSINESS_A}/budget/limits", json=raised, headers=alice)

    listed = client.get(f"/v1/tenants/{BUSINESS_A}/budget/limits/versions", headers=alice).json()
    assert [row["version"] for row in listed] == [1, 2]
    assert listed[0]["dailyCapMinor"] == 200_000  # v1 preserved

    latest = client.get(f"/v1/tenants/{BUSINESS_A}/budget/limits", headers=alice).json()
    assert latest["configured"] is True and latest["version"] == 2
    assert latest["dailyCapMinor"] == 400_000


def test_http_viewer_can_read_but_not_write(client):
    # An admin sets limits; a viewer in the same business can READ them (candidate.read) but not write.
    client.post(
        f"/v1/tenants/{BUSINESS_A}/budget/limits",
        json=_LIMIT_BODY,
        headers=_bearer(_mint(subject=USER_ALICE)),
    )
    carol = _bearer(_mint(subject=USER_CAROL))
    read = client.get(f"/v1/tenants/{BUSINESS_A}/budget/limits", headers=carol)
    assert read.status_code == 200 and read.json()["configured"] is True


def test_http_limits_are_isolated_per_business(client):
    # Alice sets A's limits; B's admin (Bob) sets different limits for B. Neither change touches the
    # other, and before B is set, B reads as unconfigured — the manual-test isolation check.
    client.post(
        f"/v1/tenants/{BUSINESS_A}/budget/limits",
        json=_LIMIT_BODY,
        headers=_bearer(_mint(subject=USER_ALICE)),
    )
    bob = _bearer(_mint(subject=USER_BOB))
    before = client.get(f"/v1/tenants/{BUSINESS_B}/budget/limits", headers=bob).json()
    assert before["configured"] is False  # setting A did not configure B

    b_body = {"dailyCapMinor": 10_000, "monthlyCapMinor": 100_000, "perRunCapMinor": 5_000}
    assert (
        client.post(f"/v1/tenants/{BUSINESS_B}/budget/limits", json=b_body, headers=bob).status_code
        == 201
    )
    # A's config is still its own values, independent of B.
    a_latest = client.get(
        f"/v1/tenants/{BUSINESS_A}/budget/limits", headers=_bearer(_mint(subject=USER_ALICE))
    ).json()
    assert a_latest["dailyCapMinor"] == 200_000
    b_latest = client.get(f"/v1/tenants/{BUSINESS_B}/budget/limits", headers=bob).json()
    assert b_latest["dailyCapMinor"] == 10_000


def test_http_admin_only_in_own_business(client):
    # Bob is an admin in B but his A membership was revoked => setting caps in A is denied.
    resp = client.post(
        f"/v1/tenants/{BUSINESS_A}/budget/limits",
        json=_LIMIT_BODY,
        headers=_bearer(_mint(subject=USER_BOB)),
    )
    assert resp.status_code == 403


def test_http_unknown_version_is_404(client):
    alice = _bearer(_mint(subject=USER_ALICE))
    client.post(f"/v1/tenants/{BUSINESS_A}/budget/limits", json=_LIMIT_BODY, headers=alice)
    resp = client.get(f"/v1/tenants/{BUSINESS_A}/budget/limits/versions/99", headers=alice)
    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == "budget_limit_version_not_found"


def test_http_set_is_idempotent_on_retry(client):
    from app.idempotency import IDEMPOTENCY_HEADER

    keyed = {**_bearer(_mint(subject=USER_ALICE)), IDEMPOTENCY_HEADER: "budget-set-key-1"}
    first = client.post(f"/v1/tenants/{BUSINESS_A}/budget/limits", json=_LIMIT_BODY, headers=keyed)
    replay = client.post(f"/v1/tenants/{BUSINESS_A}/budget/limits", json=_LIMIT_BODY, headers=keyed)
    assert first.status_code == 201
    assert replay.json() == first.json()
    assert replay.headers.get("Idempotent-Replay") == "true"
    # The retry produced NO second version.
    with client.app.state.session_factory() as session:
        assert session.query(BudgetLimit).filter(BudgetLimit.business_id == BUSINESS_A).count() == 1
