"""DCA-115 — track the lifecycle of an external provider call.

Two layers, matching the DCA-112/113/114 suites:

- **Service unit tests** (in-memory SQLite) pin the lifecycle store: authority is checked FIRST; recording
  is idempotent on the call key (a retry replays, never a second row/audit); the safe state machine admits
  only valid edges and refuses an invalid OR duplicate transition with no change; a timeout after possible
  external acceptance (``UNCERTAIN``) is distinguishable from a confirmed no-call failure (``FAILED``);
  an unknown / foreign call key is refused; and the store stages (never commits) into the caller's session.
- **A PostgreSQL concurrency test** (disposable Postgres, same skip guard as the other suites) proves the
  serialization SQLite cannot: several transitions racing on the SAME record serialize on the
  ``FOR UPDATE`` lock, so exactly ONE applies and the rest are refused — a duplicate transition never
  applies twice.

No actual provider/model/network/paid call is ever made — this records lifecycle only; nothing is spent
or sent (the DCA has no such path).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from app.cost.provider_call import (
    AUDIT_ACTION_RECORD,
    AUDIT_ACTION_TRANSITION,
    REASON_INVALID_TRANSITION,
    REASON_MISSING_CAPABILITY,
    REASON_MISSING_KEY,
    REASON_NOT_FOUND,
    ProviderCallActor,
    ProviderCallError,
    ProviderCallIntent,
    ProviderCallAuthorizationError,
    ProviderCallStore,
    ProviderCallTransition,
)
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import AuditEvent, ProviderCallRecord, ProviderCallStatus

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"
ADMIN = ProviderCallActor(subject="00000000-0000-4000-9000-0000000a0115", roles=("admin",))
VIEWER = ProviderCallActor(subject="00000000-0000-4000-9000-0000000v0115", roles=("viewer",))

NOW = datetime(2026, 10, 31, 9, 0, 0, tzinfo=UTC)  # 14:30 IST
LATER = datetime(2026, 10, 31, 9, 5, 0, tzinfo=UTC)


def _intent(key: str, **kw) -> ProviderCallIntent:
    kw.setdefault("capability", "contact_research")
    return ProviderCallIntent(call_key=key, **kw)


def _audit_count(session, business_id, action) -> int:
    return (
        session.query(AuditEvent)
        .filter(AuditEvent.business_id == business_id, AuditEvent.action == action)
        .count()
    )


def _row(session, business_id, key) -> ProviderCallRecord | None:
    return ProviderCallStore(session, business_id).by_key(key)


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


def test_record_stages_a_pending_call_and_an_atomic_audit(session_factory):
    with session_factory() as session:
        store = ProviderCallStore(session, BIZ_A)
        record = store.record(
            _intent(
                "call-1",
                request_id="req-1",
                intent="prospect_enrichment",
                provider="platform",
                model="claude-sonnet-5",
                source_version="v9",
                reservation_id="resv-xyz",
            ),
            ADMIN,
            now=NOW,
        )
        session.commit()
        assert record.status == ProviderCallStatus.PENDING.value
        assert record.dispatched is False and record.cost_possible is False
        assert record.capability == "contact_research" and record.request_id == "req-1"
        assert record.model == "claude-sonnet-5" and record.source_version == "v9"
        assert record.reservation_id == "resv-xyz"
        assert _audit_count(session, BIZ_A, AUDIT_ACTION_RECORD) == 1


def test_record_is_idempotent_on_the_call_key(session_factory):
    with session_factory() as session:
        store = ProviderCallStore(session, BIZ_A)
        first = store.record(_intent("call-1"), ADMIN, now=NOW)
        session.commit()
        first_id = first.id
        # A retry recording the SAME key returns the existing record and stages no second row/audit.
        replay = store.record(_intent("call-1", capability="scoring"), ADMIN, now=LATER)
        session.commit()
        assert replay.id == first_id
        assert replay.capability == "contact_research"  # unchanged by the replay
        assert session.query(ProviderCallRecord).filter_by(business_id=BIZ_A).count() == 1
        assert _audit_count(session, BIZ_A, AUDIT_ACTION_RECORD) == 1


def test_authority_is_checked_first_on_record_and_changes_nothing(session_factory):
    with session_factory() as session:
        store = ProviderCallStore(session, BIZ_A)
        with pytest.raises(ProviderCallAuthorizationError):
            store.record(_intent("call-1"), VIEWER, now=NOW)
        session.rollback()
        assert session.query(ProviderCallRecord).count() == 0
        assert _audit_count(session, BIZ_A, AUDIT_ACTION_RECORD) == 0


def test_missing_call_key_and_capability_are_refused(session_factory):
    with session_factory() as session:
        store = ProviderCallStore(session, BIZ_A)
        with pytest.raises(ProviderCallError) as exc1:
            store.record(_intent("   "), ADMIN, now=NOW)
        assert exc1.value.code == REASON_MISSING_KEY
        with pytest.raises(ProviderCallError) as exc2:
            store.record(ProviderCallIntent(call_key="call-1", capability="  "), ADMIN, now=NOW)
        assert exc2.value.code == REASON_MISSING_CAPABILITY
        session.rollback()
        assert session.query(ProviderCallRecord).count() == 0


def test_happy_path_dispatch_then_succeed_records_usage(session_factory):
    with session_factory() as session:
        store = ProviderCallStore(session, BIZ_A)
        store.record(_intent("call-1"), ADMIN, now=NOW)
        session.commit()

        # PENDING -> IN_FLIGHT marks the call dispatched (external acceptance now possible).
        inflight = store.transition(
            ProviderCallTransition("call-1", ProviderCallStatus.IN_FLIGHT), ADMIN, now=NOW
        )
        session.commit()
        assert inflight.status == ProviderCallStatus.IN_FLIGHT.value
        assert inflight.dispatched is True and inflight.dispatched_at == NOW

        # IN_FLIGHT -> SUCCEEDED records the outcome + numeric token usage, and resolves the call.
        done = store.transition(
            ProviderCallTransition(
                "call-1",
                ProviderCallStatus.SUCCEEDED,
                outcome_code="completed",
                prompt_tokens=120,
                completion_tokens=45,
                total_tokens=165,
            ),
            ADMIN,
            now=LATER,
        )
        session.commit()
        assert done.status == ProviderCallStatus.SUCCEEDED.value
        assert done.outcome_code == "completed" and done.resolved_at == LATER
        assert done.total_tokens == 165 and done.prompt_tokens == 120
        assert done.cost_possible is True
        assert _audit_count(session, BIZ_A, AUDIT_ACTION_TRANSITION) == 2


def test_timeout_after_acceptance_is_distinguishable_from_confirmed_no_call_failure(
    session_factory,
):
    """The load-bearing DCA-115 acceptance: a dispatched call that times out is UNCERTAIN (a cost MAY
    exist) and is kept strictly distinct from a confirmed no-call FAILED (no cost)."""
    with session_factory() as session:
        store = ProviderCallStore(session, BIZ_A)

        # A call dispatched then lost -> UNCERTAIN: dispatched, a cost is possible.
        store.record(_intent("lost"), ADMIN, now=NOW)
        store.transition(
            ProviderCallTransition("lost", ProviderCallStatus.IN_FLIGHT), ADMIN, now=NOW
        )
        uncertain = store.transition(
            ProviderCallTransition(
                "lost", ProviderCallStatus.UNCERTAIN, outcome_code="gateway_timeout"
            ),
            ADMIN,
            now=LATER,
        )
        session.commit()

        # A call that failed before dispatch -> FAILED: a confirmed no-call failure, no cost possible.
        store.record(_intent("prefail"), ADMIN, now=NOW)
        failed = store.transition(
            ProviderCallTransition(
                "prefail", ProviderCallStatus.FAILED, outcome_code="config_invalid"
            ),
            ADMIN,
            now=LATER,
        )
        session.commit()

        assert uncertain.status == ProviderCallStatus.UNCERTAIN.value
        assert uncertain.dispatched is True and uncertain.cost_possible is True
        assert failed.status == ProviderCallStatus.FAILED.value
        assert failed.dispatched is False and failed.cost_possible is False
        # The two terminal states are genuinely different rows/states — never collapsed into one.
        assert uncertain.status != failed.status


def test_invalid_transition_is_refused_and_changes_nothing(session_factory):
    with session_factory() as session:
        store = ProviderCallStore(session, BIZ_A)
        store.record(_intent("call-1"), ADMIN, now=NOW)
        session.commit()

        # PENDING -> SUCCEEDED is not a valid edge (a call cannot succeed without being dispatched).
        with pytest.raises(ProviderCallError) as exc:
            store.transition(
                ProviderCallTransition("call-1", ProviderCallStatus.SUCCEEDED), ADMIN, now=NOW
            )
        assert exc.value.code == REASON_INVALID_TRANSITION
        session.rollback()
        # The record is untouched and no transition was audited.
        assert _row(session, BIZ_A, "call-1").status == ProviderCallStatus.PENDING.value
        assert _audit_count(session, BIZ_A, AUDIT_ACTION_TRANSITION) == 0


def test_pending_cannot_jump_to_uncertain(session_factory):
    """A never-dispatched call can never be UNCERTAIN — an undispatched call is a confirmed no-call."""
    with session_factory() as session:
        store = ProviderCallStore(session, BIZ_A)
        store.record(_intent("call-1"), ADMIN, now=NOW)
        session.commit()
        with pytest.raises(ProviderCallError) as exc:
            store.transition(
                ProviderCallTransition("call-1", ProviderCallStatus.UNCERTAIN), ADMIN, now=NOW
            )
        assert exc.value.code == REASON_INVALID_TRANSITION
        session.rollback()
        assert _row(session, BIZ_A, "call-1").status == ProviderCallStatus.PENDING.value


def test_duplicate_transition_is_refused_and_changes_nothing(session_factory):
    with session_factory() as session:
        store = ProviderCallStore(session, BIZ_A)
        store.record(_intent("call-1"), ADMIN, now=NOW)
        store.transition(
            ProviderCallTransition("call-1", ProviderCallStatus.IN_FLIGHT), ADMIN, now=NOW
        )
        session.commit()
        before = _audit_count(session, BIZ_A, AUDIT_ACTION_TRANSITION)

        # Re-applying the SAME transition (a duplicate / self transition) is refused — no change.
        with pytest.raises(ProviderCallError) as exc:
            store.transition(
                ProviderCallTransition("call-1", ProviderCallStatus.IN_FLIGHT), ADMIN, now=LATER
            )
        assert exc.value.code == REASON_INVALID_TRANSITION
        session.rollback()
        assert _row(session, BIZ_A, "call-1").status == ProviderCallStatus.IN_FLIGHT.value
        assert _audit_count(session, BIZ_A, AUDIT_ACTION_TRANSITION) == before


def test_terminal_state_accepts_no_further_transition(session_factory):
    with session_factory() as session:
        store = ProviderCallStore(session, BIZ_A)
        store.record(_intent("call-1"), ADMIN, now=NOW)
        store.transition(
            ProviderCallTransition("call-1", ProviderCallStatus.FAILED), ADMIN, now=NOW
        )
        session.commit()
        # A terminal FAILED record can never be re-opened or re-resolved.
        for target in (
            ProviderCallStatus.IN_FLIGHT,
            ProviderCallStatus.SUCCEEDED,
            ProviderCallStatus.UNCERTAIN,
        ):
            with pytest.raises(ProviderCallError) as exc:
                store.transition(ProviderCallTransition("call-1", target), ADMIN, now=LATER)
            assert exc.value.code == REASON_INVALID_TRANSITION
            session.rollback()
        assert _row(session, BIZ_A, "call-1").status == ProviderCallStatus.FAILED.value


def test_transition_on_unknown_key_is_refused(session_factory):
    with session_factory() as session:
        store = ProviderCallStore(session, BIZ_A)
        with pytest.raises(ProviderCallError) as exc:
            store.transition(
                ProviderCallTransition("no-such-call", ProviderCallStatus.IN_FLIGHT), ADMIN, now=NOW
            )
        assert exc.value.code == REASON_NOT_FOUND
        session.rollback()


def test_authority_is_checked_first_on_transition(session_factory):
    with session_factory() as session:
        store = ProviderCallStore(session, BIZ_A)
        store.record(_intent("call-1"), ADMIN, now=NOW)
        session.commit()
        with pytest.raises(ProviderCallAuthorizationError):
            store.transition(
                ProviderCallTransition("call-1", ProviderCallStatus.IN_FLIGHT), VIEWER, now=NOW
            )
        session.rollback()
        assert _row(session, BIZ_A, "call-1").status == ProviderCallStatus.PENDING.value


def test_records_are_tenant_scoped(session_factory):
    with session_factory() as session:
        ProviderCallStore(session, BIZ_A).record(_intent("call-1"), ADMIN, now=NOW)
        session.commit()
        # B cannot see or transition A's record: A's key reads exactly like not-found in B's scope.
        b_store = ProviderCallStore(session, BIZ_B)
        assert b_store.by_key("call-1") is None
        with pytest.raises(ProviderCallError) as exc:
            b_store.transition(
                ProviderCallTransition("call-1", ProviderCallStatus.IN_FLIGHT), ADMIN, now=NOW
            )
        assert exc.value.code == REASON_NOT_FOUND
        session.rollback()
        # A's record is intact and still PENDING. B may reuse the same key independently.
        assert _row(session, BIZ_A, "call-1").status == ProviderCallStatus.PENDING.value
        b_record = b_store.record(_intent("call-1"), ADMIN, now=NOW)
        session.commit()
        assert b_record.business_id == BIZ_B


def test_store_never_commits(session_factory):
    with session_factory() as session:
        store = ProviderCallStore(session, BIZ_A)
        store.record(_intent("call-1"), ADMIN, now=NOW)
        session.rollback()  # the store never commits — a rollback undoes the record
    with session_factory() as session:
        assert ProviderCallStore(session, BIZ_A).by_key("call-1") is None
        assert _audit_count(session, BIZ_A, AUDIT_ACTION_RECORD) == 0


# --------------------------------------------------------------------------------------------------
# PostgreSQL concurrency test — a duplicate transition never applies twice, even under a race
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)
_PG_SCHEMA = "dca_provider_call_test"


@pytest.fixture
def pg_factory():
    """A fresh schema with the DCA tables and a session factory whose connections all resolve to that
    schema (so threads share one physical datastore)."""
    from sqlalchemy import create_engine, text

    admin = create_engine(PG_URL)
    with admin.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{_PG_SCHEMA}" CASCADE'))
        conn.execute(text(f'CREATE SCHEMA "{_PG_SCHEMA}"'))
    admin.dispose()

    engine = create_engine(PG_URL, connect_args={"options": f"-csearch_path={_PG_SCHEMA}"})
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    try:
        yield factory
    finally:
        engine.dispose()
        admin = create_engine(PG_URL)
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{_PG_SCHEMA}" CASCADE'))
        admin.dispose()


@requires_pg
def test_concurrent_transitions_apply_exactly_once(pg_factory):
    """Several transitions racing on the SAME dispatched record SERIALIZE on the FOR UPDATE lock, so
    exactly ONE resolves it and the rest are refused as an invalid (out-of-terminal) transition — a
    duplicate transition never applies twice, on real Postgres.

    Eight threads each try to SUCCEED the same IN_FLIGHT record. Exactly one must win; the rest must see
    the committed terminal state and be refused. The record ends resolved exactly once."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    with pg_factory() as session:
        store = ProviderCallStore(session, BIZ_A)
        store.record(_intent("call-1"), ADMIN, now=NOW)
        store.transition(
            ProviderCallTransition("call-1", ProviderCallStatus.IN_FLIGHT), ADMIN, now=NOW
        )
        session.commit()

    n = 8
    barrier = threading.Barrier(n)
    outcomes: list[str] = []
    lock = threading.Lock()

    def _attempt(i: int) -> None:
        barrier.wait()  # release all threads together to maximize contention on the lock
        session = pg_factory()
        try:
            ProviderCallStore(session, BIZ_A).transition(
                ProviderCallTransition(
                    "call-1",
                    ProviderCallStatus.SUCCEEDED,
                    outcome_code="completed",
                    total_tokens=10,
                ),
                ADMIN,
                now=LATER,
            )
            session.commit()
            result = "applied"
        except ProviderCallError as exc:
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
    assert len(applied) == 1, outcomes  # the serializing lock admits exactly one transition
    assert all(o == REASON_INVALID_TRANSITION for o in refused), outcomes

    with pg_factory() as session:
        reloaded = ProviderCallStore(session, BIZ_A).by_key("call-1")
        audits = _audit_count(session, BIZ_A, AUDIT_ACTION_TRANSITION)
    assert reloaded.status == ProviderCallStatus.SUCCEEDED.value
    assert reloaded.total_tokens == 10  # resolved exactly once, never doubled
    # One IN_FLIGHT transition + exactly one SUCCEEDED transition were audited.
    assert audits == 2
