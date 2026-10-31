"""DCA-117 — expose business spend summaries (read-only aggregation of the DCA-113/114 ledger).

Two layers, matching the DCA-042/113/114 suites:

- **Service unit tests** (in-memory SQLite) pin the aggregation: the per-status / per-day / per-month /
  per-run roll-ups reconcile with the committed reservation ledger; an UNCERTAIN hold is reported in its
  own bucket AND counted toward ``committed`` (never folded into ``released`` / free budget); the
  remaining headroom matches the DCA-113 reserve window; and summaries are scoped per business (B never
  sees A's spend).
- **HTTP tests** (``TestClient`` with the end-user auth contract wired, like ``tests/test_budget_limits.py``)
  prove the authorization gate and tenant isolation over the real API: an administrator can read the
  summary; an ordinary viewer and a non-member (platform operator) cannot (403); an unauthenticated
  request is 401; and one business's admin can never read another business's cost details.

SQLite is sufficient — the behaviour is pure aggregation over committed rows; the transaction / RLS /
migration paths are covered by ``tests/test_rls.py`` / ``tests/test_migrations.py`` (which pin the
reservation tables) and the concurrency guarantees by ``tests/test_budget_reserve.py`` /
``tests/test_budget_reconcile.py``. Reading summaries writes nothing, spends nothing and sends nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient

from app.audit.sink import ActorType, AuditActor
from app.core.config import Settings
from app.cost.reconcile import (
    BudgetReconciliationStore,
    ReconcileOutcome,
    ReconciliationActor,
    ReconciliationRequest,
)
from app.cost.reserve import BudgetReservationStore, ReservationActor, ReservationRequest
from app.cost.summary import BudgetSpendSummaryStore
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import AuditEvent
from app.identity.fake import BUSINESS_A, BUSINESS_B, USER_ALICE, USER_BOB, USER_CAROL
from app.main import create_app
from app.services.budget_store import BudgetLimitInput, BudgetLimitStore
from tests.conftest import make_settings

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"

RES_ADMIN = ReservationActor(subject="00000000-0000-4000-9000-0000000a0117", roles=("admin",))
REC_ADMIN = ReconciliationActor(subject="00000000-0000-4000-9000-0000000a0117", roles=("admin",))
BUDGET_ACTOR = AuditActor(id="00000000-0000-4000-9000-0000000b0117", type=ActorType.USER)

# 2026-10-31 14:30 IST → civil day 2026-10-31, month 2026-10 (in Asia/Kolkata).
NOW = datetime(2026, 10, 31, 9, 0, 0, tzinfo=UTC)
# A different civil month: 2026-09-30 05:00 UTC → 2026-09-30 10:30 IST → day 2026-09-30, month 2026-09.
LAST_MONTH = datetime(2026, 9, 30, 5, 0, 0, tzinfo=UTC)


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


def _seed_limit(session_factory, business_id, *, daily=100_000, monthly=2_000_000, per_run=50_000):
    with session_factory() as session:
        BudgetLimitStore(session, business_id).save(
            BudgetLimitInput(
                daily_cap_minor=daily, monthly_cap_minor=monthly, per_run_cap_minor=per_run
            ),
            BUDGET_ACTOR,
        )
        session.commit()


def _reserve(session_factory, business_id, key, amount, *, now=NOW) -> str:
    with session_factory() as session:
        r = BudgetReservationStore(session, business_id).reserve(
            ReservationRequest(idempotency_key=key, amount_minor=amount), RES_ADMIN, now=now
        )
        session.commit()
        return r.id


def _reconcile(
    session_factory, business_id, resv_id, outcome, key, *, actual=None, now=NOW
) -> None:
    with session_factory() as session:
        BudgetReconciliationStore(session, business_id).reconcile(
            ReconciliationRequest(resv_id, key, outcome, actual_minor=actual), REC_ADMIN, now=now
        )
        session.commit()


def _summary(session_factory, business_id, *, now=NOW):
    with session_factory() as session:
        return BudgetSpendSummaryStore(session, business_id).summarize(now=now)


def test_empty_summary_when_no_budget_or_reservations(session_factory):
    s = _summary(session_factory, BIZ_A)
    assert s.configured is False
    assert s.daily_cap_minor is None and s.daily_remaining_minor is None
    assert s.total.committed_minor == 0 and s.total.reservation_count == 0
    assert s.by_run == () and s.by_day == () and s.by_month == ()


def test_totals_reconcile_with_the_ledger(session_factory):
    _seed_limit(session_factory, BIZ_A)
    # ACTIVE 4000; SETTLED actual 2500 (unused 1500 freed); UNCERTAIN 1000 (preserved); RELEASED 800.
    _reserve(session_factory, BIZ_A, "run-active", 4_000)
    s_id = _reserve(session_factory, BIZ_A, "run-settle", 6_000)
    _reconcile(session_factory, BIZ_A, s_id, ReconcileOutcome.SETTLE, "call-settle", actual=2_500)
    u_id = _reserve(session_factory, BIZ_A, "run-uncertain", 1_000)
    _reconcile(session_factory, BIZ_A, u_id, ReconcileOutcome.MARK_UNCERTAIN, "call-uncertain")
    rel_id = _reserve(session_factory, BIZ_A, "run-release", 800)
    _reconcile(session_factory, BIZ_A, rel_id, ReconcileOutcome.RELEASE, "call-release")

    s = _summary(session_factory, BIZ_A)
    t = s.total
    assert t.reserved_minor == 4_000  # ACTIVE
    assert t.incurred_minor == 2_500  # SETTLED actual
    assert t.uncertain_minor == 1_000  # UNCERTAIN preserved
    assert t.released_minor == 800  # RELEASED freed
    # committed = reserved + incurred + uncertain (NOT released) = 4000 + 2500 + 1000
    assert t.committed_minor == 7_500
    assert t.reservation_count == 4


def test_uncertain_is_not_shown_as_free_budget(session_factory):
    # The headline acceptance: an UNCERTAIN hold keeps counting against the budget; it is reported in the
    # uncertain bucket AND in committed, and never as released/free. The remaining headroom reflects it.
    _seed_limit(session_factory, BIZ_A, daily=10_000, monthly=2_000_000, per_run=10_000)
    u_id = _reserve(session_factory, BIZ_A, "run-u", 3_000)
    _reconcile(session_factory, BIZ_A, u_id, ReconcileOutcome.MARK_UNCERTAIN, "call-u")

    s = _summary(session_factory, BIZ_A)
    assert s.total.uncertain_minor == 3_000
    assert s.total.released_minor == 0  # NOT freed
    assert s.total.committed_minor == 3_000  # still counts
    # Daily remaining = 10_000 cap - 3_000 uncertain = 7_000 (uncertain did NOT free budget).
    assert s.daily_remaining_minor == 7_000

    # Contrast: releasing the same amount DOES free it.
    r_id = _reserve(session_factory, BIZ_A, "run-r", 3_000)
    _reconcile(session_factory, BIZ_A, r_id, ReconcileOutcome.RELEASE, "call-r")
    s2 = _summary(session_factory, BIZ_A)
    assert s2.total.released_minor == 3_000
    # Released budget is reclaimed: remaining is back to 10_000 - 3_000(uncertain) = 7_000, not 4_000.
    assert s2.daily_remaining_minor == 7_000


def test_remaining_matches_reserve_window(session_factory):
    _seed_limit(session_factory, BIZ_A, daily=20_000, monthly=500_000, per_run=20_000)
    _reserve(session_factory, BIZ_A, "run-1", 5_000)
    _reserve(session_factory, BIZ_A, "run-2", 2_000)
    s = _summary(session_factory, BIZ_A)
    with session_factory() as session:
        w = BudgetReservationStore(session, BIZ_A).window(now=NOW)
    assert s.daily_remaining_minor == w.daily_remaining_minor == 13_000
    assert s.monthly_remaining_minor == w.monthly_remaining_minor


def test_by_day_and_by_month_breakdown(session_factory):
    _seed_limit(session_factory, BIZ_A, daily=1_000_000, monthly=5_000_000, per_run=1_000_000)
    _reserve(session_factory, BIZ_A, "sep", 1_000, now=LAST_MONTH)  # 2026-09-30 / 2026-09
    _reserve(session_factory, BIZ_A, "oct-a", 2_000, now=NOW)  # 2026-10-31 / 2026-10
    _reserve(session_factory, BIZ_A, "oct-b", 3_000, now=NOW)  # 2026-10-31 / 2026-10

    s = _summary(session_factory, BIZ_A)
    by_day = {b.key: b for b in s.by_day}
    assert by_day["2026-09-30"].reserved_minor == 1_000
    assert by_day["2026-10-31"].reserved_minor == 5_000  # the two October holds together
    assert by_day["2026-10-31"].reservation_count == 2

    by_month = {b.key: b for b in s.by_month}
    assert by_month["2026-09"].committed_minor == 1_000
    assert by_month["2026-10"].committed_minor == 5_000


def test_by_run_lines_reflect_each_reservation(session_factory):
    _seed_limit(session_factory, BIZ_A)
    a = _reserve(session_factory, BIZ_A, "run-a", 4_000)
    _reconcile(session_factory, BIZ_A, a, ReconcileOutcome.SETTLE, "call-a", actual=2_500)
    _reserve(session_factory, BIZ_A, "run-b", 1_000)

    s = _summary(session_factory, BIZ_A)
    lines = {line.run_key: line for line in s.by_run}
    assert lines["run-a"].status == "settled"
    assert lines["run-a"].reserved_minor == 4_000
    assert lines["run-a"].incurred_minor == 2_500
    assert lines["run-a"].committed_minor == 2_500  # only the settled actual still counts
    assert lines["run-b"].status == "active"
    assert lines["run-b"].incurred_minor is None
    assert lines["run-b"].committed_minor == 1_000


def test_summaries_are_scoped_per_business(session_factory):
    _seed_limit(session_factory, BIZ_A)
    _seed_limit(session_factory, BIZ_B)
    _reserve(session_factory, BIZ_A, "a-run", 4_000)
    _reserve(session_factory, BIZ_B, "b-run", 9_000)

    a = _summary(session_factory, BIZ_A)
    b = _summary(session_factory, BIZ_B)
    assert a.total.reserved_minor == 4_000 and a.total.reservation_count == 1
    assert b.total.reserved_minor == 9_000 and b.total.reservation_count == 1
    # A never sees B's run and vice-versa.
    assert {line.run_key for line in a.by_run} == {"a-run"}
    assert {line.run_key for line in b.by_run} == {"b-run"}


def test_summarize_does_not_write(session_factory):
    from app.db.models import BudgetReservation

    _seed_limit(session_factory, BIZ_A)
    _reserve(session_factory, BIZ_A, "run-1", 4_000)
    with session_factory() as session:
        audits_before = session.query(AuditEvent).count()
        reservations_before = session.query(BudgetReservation).count()
        BudgetSpendSummaryStore(session, BIZ_A).summarize(now=NOW)
        session.commit()
    with session_factory() as session:
        assert session.query(AuditEvent).count() == audits_before  # no new audit
        assert session.query(BudgetReservation).count() == reservations_before


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


def _seed_business_spend(client: TestClient, business_id: str, *, amount: int) -> None:
    """Seed one in-force budget + a reserved hold and an uncertain hold for a business over the app's DB."""
    factory = client.app.state.session_factory
    with factory() as session:
        BudgetLimitStore(session, business_id).save(
            BudgetLimitInput(
                daily_cap_minor=100_000, monthly_cap_minor=2_000_000, per_run_cap_minor=50_000
            ),
            BUDGET_ACTOR,
        )
        session.commit()
    with factory() as session:
        BudgetReservationStore(session, business_id).reserve(
            ReservationRequest(idempotency_key="http-active", amount_minor=amount),
            RES_ADMIN,
            now=NOW,
        )
        session.commit()
    with factory() as session:
        u = BudgetReservationStore(session, business_id).reserve(
            ReservationRequest(idempotency_key="http-uncertain", amount_minor=1_000),
            RES_ADMIN,
            now=NOW,
        )
        session.commit()
        BudgetReconciliationStore(session, business_id).reconcile(
            ReconciliationRequest(u.id, "http-call-u", ReconcileOutcome.MARK_UNCERTAIN),
            REC_ADMIN,
            now=NOW,
        )
        session.commit()


def test_http_admin_can_read_summary(client):
    _seed_business_spend(client, BUSINESS_A, amount=4_000)
    resp = client.get(
        f"/v1/tenants/{BUSINESS_A}/spend/summary",
        headers=_bearer(_mint(subject=USER_ALICE)),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["configured"] is True
    assert body["total"]["reservedMinor"] == 4_000
    assert body["total"]["uncertainMinor"] == 1_000
    assert body["total"]["committedMinor"] == 5_000  # reserved + uncertain, uncertain is NOT free
    assert body["total"]["releasedMinor"] == 0
    # Remaining reflects the uncertain hold (never shown as free budget).
    assert body["dailyRemainingMinor"] == 100_000 - 5_000
    assert {line["runKey"] for line in body["byRun"]} == {"http-active", "http-uncertain"}


def test_http_ordinary_viewer_cannot_read_summary(client):
    # Carol is an active viewer in A — a real member, but without the budget.manage scope.
    _seed_business_spend(client, BUSINESS_A, amount=4_000)
    resp = client.get(
        f"/v1/tenants/{BUSINESS_A}/spend/summary",
        headers=_bearer(_mint(subject=USER_CAROL)),
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_http_platform_operator_cannot_read_client_summary(client):
    # Global operator rights do NOT imply membership: no active membership in A => denied.
    _seed_business_spend(client, BUSINESS_A, amount=4_000)
    resp = client.get(
        f"/v1/tenants/{BUSINESS_A}/spend/summary",
        headers=_bearer(_mint(subject=OPERATOR, roles=["platform_operator"])),
    )
    assert resp.status_code == 403


def test_http_unauthenticated_request_is_401(client):
    resp = client.get(f"/v1/tenants/{BUSINESS_A}/spend/summary")
    assert resp.status_code == 401


def test_http_tenant_cannot_read_another_businesss_spend(client):
    # Alice (admin of A) sees A's spend; Bob (admin of B, revoked in A) can read B but NEVER A.
    _seed_business_spend(client, BUSINESS_A, amount=4_000)
    _seed_business_spend(client, BUSINESS_B, amount=9_000)

    a = client.get(
        f"/v1/tenants/{BUSINESS_A}/spend/summary", headers=_bearer(_mint(subject=USER_ALICE))
    )
    assert a.status_code == 200 and a.json()["total"]["reservedMinor"] == 4_000

    b = client.get(
        f"/v1/tenants/{BUSINESS_B}/spend/summary", headers=_bearer(_mint(subject=USER_BOB))
    )
    assert b.status_code == 200 and b.json()["total"]["reservedMinor"] == 9_000

    # Bob's A membership was revoked => he cannot read A's cost details (403, not another tenant's rows).
    cross = client.get(
        f"/v1/tenants/{BUSINESS_A}/spend/summary", headers=_bearer(_mint(subject=USER_BOB))
    )
    assert cross.status_code == 403
