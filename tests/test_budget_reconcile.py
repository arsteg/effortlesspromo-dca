"""DCA-114 — reconcile a reserved hold against the actual cost.

Two layers, matching the DCA-112/113 suites:

- **Service unit tests** (in-memory SQLite) pin the reconciler: authority is checked FIRST; settling an
  under-spend releases the unused difference back to the budget; settling exactly uses it all; an
  over-run is CLAMPED to the reserved amount and the excess flagged (never charged past the cap);
  releasing frees the whole hold; marking uncertain PRESERVES the full hold (no refund); a reservation is
  reconciled only once (a duplicate under the same provider-call identity is an idempotent replay, a
  different identity is refused); an unknown / foreign reservation id is refused with no change; and the
  store stages (never commits) into the caller's session. The freed headroom is observable as restored
  remaining budget via the DCA-113 window.
- **A PostgreSQL concurrency test** (disposable Postgres, same skip guard as the other suites) proves the
  serialization SQLite cannot: several reconciliations racing on the SAME hold serialize on the
  ``FOR UPDATE`` lock, so exactly ONE applies and the rest are refused — a duplicate settlement never
  double-spends, even under a race.

Reconciling only adjusts the ledger; it spends/sends nothing (there is no external call to make here).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from app.audit.sink import ActorType, AuditActor
from app.cost.reconcile import (
    AUDIT_ACTION_RECONCILE,
    REASON_ALREADY_RECONCILED,
    REASON_INVALID_ACTUAL,
    REASON_MISSING_OUTCOME_KEY,
    REASON_NOT_FOUND,
    BudgetReconciliationStore,
    ReconcileOutcome,
    ReconciliationActor,
    ReconciliationAuthorizationError,
    ReconciliationError,
    ReconciliationRequest,
)
from app.cost.reserve import BudgetReservationStore, ReservationActor, ReservationRequest
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import AuditEvent, BudgetReservation, ReservationStatus
from app.services.budget_store import BudgetLimitInput, BudgetLimitStore

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"
ADMIN = ReconciliationActor(subject="00000000-0000-4000-9000-0000000a0114", roles=("admin",))
VIEWER = ReconciliationActor(subject="00000000-0000-4000-9000-0000000v0114", roles=("viewer",))
RES_ADMIN = ReservationActor(subject="00000000-0000-4000-9000-0000000a0114", roles=("admin",))
BUDGET_ACTOR = AuditActor(id="00000000-0000-4000-9000-0000000b0114", type=ActorType.USER)

NOW = datetime(2026, 10, 31, 9, 0, 0, tzinfo=UTC)  # 14:30 IST → day 2026-10-31, month 2026-10


def _seed_limit(session, business_id, *, daily=5_000, monthly=1_000_000, per_run=5_000) -> None:
    BudgetLimitStore(session, business_id).save(
        BudgetLimitInput(
            daily_cap_minor=daily, monthly_cap_minor=monthly, per_run_cap_minor=per_run
        ),
        BUDGET_ACTOR,
    )
    session.commit()


def _reserve(session, business_id, key, amount, **kw) -> BudgetReservation:
    """Create and commit one ACTIVE hold via the real DCA-113 store, returning the reservation."""
    resv = BudgetReservationStore(session, business_id).reserve(
        ReservationRequest(idempotency_key=key, amount_minor=amount, **kw), RES_ADMIN, now=NOW
    )
    session.commit()
    return resv


def _reconcile_audit_count(session, business_id) -> int:
    return (
        session.query(AuditEvent)
        .filter(AuditEvent.business_id == business_id, AuditEvent.action == AUDIT_ACTION_RECONCILE)
        .count()
    )


def _remaining(session, business_id) -> int:
    return BudgetReservationStore(session, business_id).window(now=NOW).daily_remaining_minor


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


# --------------------------------------------------------------------------------------------------
# Service unit tests (in-memory SQLite)
# --------------------------------------------------------------------------------------------------


def test_under_spend_settle_releases_the_unused_difference(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000)
        resv = _reserve(session, BIZ_A, "run-1", 4_000)
        assert _remaining(session, BIZ_A) == 1_000  # 5_000 cap - 4_000 hold

        store = BudgetReconciliationStore(session, BIZ_A)
        result = store.reconcile(
            ReconciliationRequest(resv.id, "call-1", ReconcileOutcome.SETTLE, actual_minor=2_500),
            ADMIN,
            now=NOW,
        )
        session.commit()

        assert result.status == ReservationStatus.SETTLED.value
        assert result.settled_minor == 2_500
        assert result.released_minor == 1_500  # 4_000 held - 2_500 actual, freed back to budget
        assert result.overspend_minor == 0
        assert not result.replayed
        # The freed 1_500 is observable as restored remaining budget: 5_000 - 2_500 settled = 2_500.
        assert _remaining(session, BIZ_A) == 2_500
        assert _reconcile_audit_count(session, BIZ_A) == 1


def test_exact_settle_releases_nothing(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000)
        resv = _reserve(session, BIZ_A, "run-1", 3_000)
        store = BudgetReconciliationStore(session, BIZ_A)
        result = store.reconcile(
            ReconciliationRequest(resv.id, "call-1", ReconcileOutcome.SETTLE, actual_minor=3_000),
            ADMIN,
            now=NOW,
        )
        session.commit()
        assert result.settled_minor == 3_000 and result.released_minor == 0
        assert result.overspend_minor == 0
        assert _remaining(session, BIZ_A) == 2_000  # unchanged: the whole hold was spent


def test_over_spend_is_clamped_and_flagged_never_charged_past_the_cap(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000)
        resv = _reserve(session, BIZ_A, "run-1", 3_000)
        store = BudgetReconciliationStore(session, BIZ_A)
        # Actual 4_000 > the 3_000 hold: the excess is CLAMPED (settled at 3_000) and FLAGGED, never
        # retroactively charged past the cap the reserve path already enforced.
        result = store.reconcile(
            ReconciliationRequest(resv.id, "call-1", ReconcileOutcome.SETTLE, actual_minor=4_000),
            ADMIN,
            now=NOW,
        )
        session.commit()
        assert result.status == ReservationStatus.SETTLED.value
        assert result.actual_minor == 4_000  # the reported actual is preserved verbatim
        assert result.settled_minor == 3_000  # clamped to the reserved hold
        assert result.overspend_minor == 1_000  # flagged for review
        assert result.released_minor == 0
        # The hold still counts only its reserved 3_000 — the cap was never retroactively overshot.
        assert _remaining(session, BIZ_A) == 2_000


def test_release_frees_the_whole_hold(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000)
        resv = _reserve(session, BIZ_A, "run-1", 4_000)
        store = BudgetReconciliationStore(session, BIZ_A)
        result = store.reconcile(
            ReconciliationRequest(resv.id, "call-1", ReconcileOutcome.RELEASE), ADMIN, now=NOW
        )
        session.commit()
        assert result.status == ReservationStatus.RELEASED.value
        assert result.settled_minor == 0 and result.released_minor == 4_000
        assert _remaining(session, BIZ_A) == 5_000  # the whole hold is freed


def test_mark_uncertain_preserves_the_full_hold_and_does_not_refund(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000)
        resv = _reserve(session, BIZ_A, "run-1", 4_000)
        store = BudgetReconciliationStore(session, BIZ_A)
        result = store.reconcile(
            ReconciliationRequest(resv.id, "call-1", ReconcileOutcome.MARK_UNCERTAIN),
            ADMIN,
            now=NOW,
        )
        session.commit()
        assert result.status == ReservationStatus.UNCERTAIN.value
        assert result.actual_minor is None  # an unknown result is never coerced to a number
        assert result.settled_minor is None
        assert result.released_minor == 0  # NOT refunded
        # The full hold is PRESERVED against the caps — an unknown result frees nothing.
        assert _remaining(session, BIZ_A) == 1_000


def test_duplicate_settlement_under_same_identity_is_an_idempotent_replay(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000)
        resv = _reserve(session, BIZ_A, "run-1", 4_000)
        store = BudgetReconciliationStore(session, BIZ_A)
        req = ReconciliationRequest(resv.id, "call-1", ReconcileOutcome.SETTLE, actual_minor=2_500)
        first = store.reconcile(req, ADMIN, now=NOW)
        session.commit()
        assert not first.replayed and first.settled_minor == 2_500

        # A duplicate settlement with the SAME provider-call identity returns the existing result and
        # stages no second adjustment / no second audit — a duplicate never double-spends.
        replay = store.reconcile(req, ADMIN, now=NOW)
        session.commit()
        assert replay.replayed is True
        assert replay.settled_minor == 2_500 and replay.released_minor == 1_500
        assert _reconcile_audit_count(session, BIZ_A) == 1  # only the first reconciliation audited
        assert _remaining(session, BIZ_A) == 2_500  # not double-released


def test_second_reconciliation_under_a_different_identity_is_refused(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000)
        resv = _reserve(session, BIZ_A, "run-1", 4_000)
        store = BudgetReconciliationStore(session, BIZ_A)
        store.reconcile(
            ReconciliationRequest(resv.id, "call-1", ReconcileOutcome.SETTLE, actual_minor=2_500),
            ADMIN,
            now=NOW,
        )
        session.commit()

        with pytest.raises(ReconciliationError) as exc:
            store.reconcile(
                ReconciliationRequest(resv.id, "call-2", ReconcileOutcome.RELEASE), ADMIN, now=NOW
            )
        assert exc.value.code == REASON_ALREADY_RECONCILED
        session.rollback()

        # The first settlement is intact; a reservation is reconciled only once.
        reloaded = store._get(resv.id)
        assert reloaded.status == ReservationStatus.SETTLED.value
        assert reloaded.settled_amount_minor == 2_500
        assert _reconcile_audit_count(session, BIZ_A) == 1
        assert _remaining(session, BIZ_A) == 2_500


def test_unknown_reservation_is_refused_and_changes_nothing(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000)
        _reserve(session, BIZ_A, "run-1", 4_000)
        store = BudgetReconciliationStore(session, BIZ_A)
        with pytest.raises(ReconciliationError) as exc:
            store.reconcile(
                ReconciliationRequest(
                    "no-such-id", "call-1", ReconcileOutcome.SETTLE, actual_minor=1
                ),
                ADMIN,
                now=NOW,
            )
        assert exc.value.code == REASON_NOT_FOUND
        session.rollback()
        assert _reconcile_audit_count(session, BIZ_A) == 0
        assert _remaining(session, BIZ_A) == 1_000  # the existing hold is untouched


def test_reconciliation_is_tenant_scoped(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000)
        _seed_limit(session, BIZ_B, daily=5_000)
        resv = _reserve(session, BIZ_A, "run-1", 4_000)
        # B cannot reconcile A's hold: A's id reads exactly like not-found in B's scope.
        b_store = BudgetReconciliationStore(session, BIZ_B)
        with pytest.raises(ReconciliationError) as exc:
            b_store.reconcile(
                ReconciliationRequest(resv.id, "call-1", ReconcileOutcome.RELEASE), ADMIN, now=NOW
            )
        assert exc.value.code == REASON_NOT_FOUND
        session.rollback()
        # A's hold is intact and still ACTIVE.
        assert BudgetReconciliationStore(session, BIZ_A)._get(resv.id).status == (
            ReservationStatus.ACTIVE.value
        )


def test_authority_is_checked_first_and_changes_nothing(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000)
        resv = _reserve(session, BIZ_A, "run-1", 4_000)
        store = BudgetReconciliationStore(session, BIZ_A)
        with pytest.raises(ReconciliationAuthorizationError):
            store.reconcile(
                ReconciliationRequest(resv.id, "call-1", ReconcileOutcome.RELEASE), VIEWER, now=NOW
            )
        session.rollback()
        assert store._get(resv.id).status == ReservationStatus.ACTIVE.value
        assert _reconcile_audit_count(session, BIZ_A) == 0


def test_invalid_actual_is_refused(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000)
        resv = _reserve(session, BIZ_A, "run-1", 4_000)
        store = BudgetReconciliationStore(session, BIZ_A)
        # A settle with no actual cost, and a negative actual, are both refused.
        with pytest.raises(ReconciliationError) as exc1:
            store.reconcile(
                ReconciliationRequest(resv.id, "call-1", ReconcileOutcome.SETTLE), ADMIN, now=NOW
            )
        assert exc1.value.code == REASON_INVALID_ACTUAL
        with pytest.raises(ReconciliationError) as exc2:
            store.reconcile(
                ReconciliationRequest(resv.id, "call-1", ReconcileOutcome.SETTLE, actual_minor=-5),
                ADMIN,
                now=NOW,
            )
        assert exc2.value.code == REASON_INVALID_ACTUAL
        session.rollback()
        assert store._get(resv.id).status == ReservationStatus.ACTIVE.value


def test_missing_outcome_key_is_refused(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000)
        resv = _reserve(session, BIZ_A, "run-1", 4_000)
        store = BudgetReconciliationStore(session, BIZ_A)
        with pytest.raises(ReconciliationError) as exc:
            store.reconcile(
                ReconciliationRequest(resv.id, "  ", ReconcileOutcome.RELEASE), ADMIN, now=NOW
            )
        assert exc.value.code == REASON_MISSING_OUTCOME_KEY
        session.rollback()


def test_released_headroom_is_reservable_again(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000)
        resv = _reserve(session, BIZ_A, "run-1", 4_000)  # 1_000 remaining
        BudgetReconciliationStore(session, BIZ_A).reconcile(
            ReconciliationRequest(resv.id, "call-1", ReconcileOutcome.RELEASE), ADMIN, now=NOW
        )
        session.commit()
        assert _remaining(session, BIZ_A) == 5_000
        # The freed headroom is genuinely reservable again: a fresh 5_000 hold now fits.
        again = _reserve(session, BIZ_A, "run-2", 5_000)
        assert again.status == ReservationStatus.ACTIVE.value
        assert _remaining(session, BIZ_A) == 0


def test_reconcile_does_not_commit(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000)
        resv = _reserve(session, BIZ_A, "run-1", 4_000)
        resv_id = resv.id
        BudgetReconciliationStore(session, BIZ_A).reconcile(
            ReconciliationRequest(resv_id, "call-1", ReconcileOutcome.RELEASE), ADMIN, now=NOW
        )
        session.rollback()  # the store never commits — a rollback undoes the reconciliation
    with session_factory() as session:
        reloaded = BudgetReconciliationStore(session, BIZ_A)._get(resv_id)
        assert reloaded.status == ReservationStatus.ACTIVE.value  # still a live hold
        assert _reconcile_audit_count(session, BIZ_A) == 0


# --------------------------------------------------------------------------------------------------
# PostgreSQL concurrency test — a duplicate settlement never applies twice, even under a race
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)
_PG_SCHEMA = "dca_reconcile_test"


@pytest.fixture
def pg_factory():
    """A fresh schema with the DCA tables, a seeded in-force BudgetLimit, and a session factory whose
    connections all resolve to that schema (so threads share one physical datastore)."""
    from sqlalchemy import create_engine, text

    admin = create_engine(PG_URL)
    with admin.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{_PG_SCHEMA}" CASCADE'))
        conn.execute(text(f'CREATE SCHEMA "{_PG_SCHEMA}"'))
    admin.dispose()

    engine = create_engine(PG_URL, connect_args={"options": f"-csearch_path={_PG_SCHEMA}"})
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000, monthly=1_000_000, per_run=5_000)
    try:
        yield factory
    finally:
        engine.dispose()
        admin = create_engine(PG_URL)
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{_PG_SCHEMA}" CASCADE'))
        admin.dispose()


@requires_pg
def test_concurrent_reconciliations_apply_exactly_once(pg_factory):
    """Several reconciliations racing on the SAME ACTIVE hold SERIALIZE on the FOR UPDATE lock, so exactly
    ONE applies and the rest are refused as already-reconciled — a duplicate settlement never
    double-spends, on real Postgres.

    Eight threads each try to SETTLE the same hold under a DISTINCT provider-call identity. Exactly one
    must win; the rest must see the committed terminal state and be refused with the already-reconciled
    code. The hold ends up reconciled exactly once, settled at the single winner's actual cost.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    with pg_factory() as session:
        resv = _reserve(session, BIZ_A, "run-1", 4_000)
    reservation_id = resv.id

    n = 8
    barrier = threading.Barrier(n)
    outcomes: list[str] = []
    lock = threading.Lock()

    def _attempt(i: int) -> None:
        barrier.wait()  # release all threads together to maximize contention on the lock
        session = pg_factory()
        try:
            BudgetReconciliationStore(session, BIZ_A).reconcile(
                ReconciliationRequest(
                    reservation_id, f"call-{i}", ReconcileOutcome.SETTLE, actual_minor=2_500
                ),
                ADMIN,
                now=NOW,
            )
            session.commit()
            result = "applied"
        except ReconciliationError as exc:
            session.rollback()
            result = exc.code
        except Exception:  # pragma: no cover - defensive; surfaced via the outcomes assertion
            session.rollback()
            raise
        finally:
            session.close()
        with lock:
            outcomes.append(result)

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(_attempt, range(n)))

    applied = [o for o in outcomes if o == "applied"]
    refused = [o for o in outcomes if o != "applied"]
    assert len(applied) == 1, outcomes  # the serializing lock admits exactly one reconciliation
    assert all(o == REASON_ALREADY_RECONCILED for o in refused), outcomes

    with pg_factory() as session:
        reloaded = BudgetReconciliationStore(session, BIZ_A)._get(reservation_id)
        audits = _reconcile_audit_count(session, BIZ_A)
    assert reloaded.status == ReservationStatus.SETTLED.value
    assert reloaded.settled_amount_minor == 2_500  # settled exactly once, never doubled
    assert audits == 1  # a single reconciliation was audited
