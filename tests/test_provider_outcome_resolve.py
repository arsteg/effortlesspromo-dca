"""DCA-116 — reconcile an ambiguous (UNCERTAIN) provider outcome.

Two layers, matching the DCA-112/113/114/115 suites:

- **Service unit tests** (in-memory SQLite) pin the resolver: authority is checked FIRST; only an
  ``UNCERTAIN`` outcome is resolvable; a SUPPORTED provider with receipt evidence resolves — happened →
  CONFIRMED and the tied hold is SETTLED at the actual cost (unused difference released), not-happened →
  VOID and the tied hold is RELEASED in full; a SUPPORTED provider with NO evidence is refused (certainty
  needs evidence); an UNSUPPORTED provider is routed to NEEDS_REVIEW with nothing settled/released/retried;
  a duplicate resolution under the same evidence identity is an idempotent replay; a conflicting resolution
  under a different identity against a FINAL resolution is refused with no change; an unknown / foreign call
  key is refused; and the store stages (never commits) into the caller's session.
- **A PostgreSQL concurrency test** (disposable Postgres, same skip guard as the other suites) proves the
  serialization SQLite cannot: several resolutions racing on the SAME uncertain outcome serialize on the
  ``FOR UPDATE`` lock, so exactly ONE applies and the rest are refused — a duplicate settlement never
  double-spends, even under a race.

Resolving only adjusts the record/ledger; it performs/retries/spends/sends nothing (there is no external
call to make here).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from app.cost.provider_call import (
    ProviderCallActor,
    ProviderCallIntent,
    ProviderCallStore,
    ProviderCallTransition,
)
from app.cost.provider_reconcile import (
    AUDIT_ACTION_RESOLVE,
    REASON_ALREADY_RESOLVED,
    REASON_INVALID_ACTUAL,
    REASON_MISSING_KEY,
    REASON_MISSING_RESOLUTION_KEY,
    REASON_NO_EVIDENCE,
    REASON_NOT_FOUND,
    REASON_NOT_UNCERTAIN,
    OutcomeEvidence,
    OutcomeResolutionActor,
    OutcomeResolutionAuthorizationError,
    OutcomeResolutionError,
    UncertainOutcomeResolver,
)
from app.cost.reconcile import (
    BudgetReconciliationStore,
    ReconcileOutcome,
    ReconciliationActor,
    ReconciliationRequest,
)
from app.cost.reserve import BudgetReservationStore, ReservationActor, ReservationRequest
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import (
    AuditEvent,
    ProviderCallRecord,
    ProviderCallResolution,
    ProviderCallStatus,
    ReservationStatus,
)
from app.services.budget_store import BudgetLimitInput, BudgetLimitStore
from app.audit.sink import ActorType, AuditActor

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"
ADMIN = OutcomeResolutionActor(subject="00000000-0000-4000-9000-0000000a0116", roles=("admin",))
VIEWER = OutcomeResolutionActor(subject="00000000-0000-4000-9000-0000000v0116", roles=("viewer",))
RES_ADMIN = ReservationActor(subject="00000000-0000-4000-9000-0000000a0116", roles=("admin",))
REC_ADMIN = ReconciliationActor(subject="00000000-0000-4000-9000-0000000a0116", roles=("admin",))
CALL_ADMIN = ProviderCallActor(subject="00000000-0000-4000-9000-0000000a0116", roles=("admin",))
BUDGET_ACTOR = AuditActor(id="00000000-0000-4000-9000-0000000b0116", type=ActorType.USER)

NOW = datetime(2026, 10, 31, 9, 0, 0, tzinfo=UTC)  # 14:30 IST → day 2026-10-31, month 2026-10


def _seed_limit(session, business_id, *, daily=5_000, monthly=1_000_000, per_run=5_000) -> None:
    BudgetLimitStore(session, business_id).save(
        BudgetLimitInput(
            daily_cap_minor=daily, monthly_cap_minor=monthly, per_run_cap_minor=per_run
        ),
        BUDGET_ACTOR,
    )
    session.commit()


def _setup_uncertain(
    session, business_id, *, call_key="call-1", amount=4_000, provider="platform"
) -> str:
    """Build one UNCERTAIN provider call linked to an UNCERTAIN DCA-113 hold via the REAL DCA-113/114/115
    stores, and return the call key. The reservation is reserved, dispatched-then-lost (provider-call
    UNCERTAIN) and marked UNCERTAIN (DCA-114) — exactly the ambiguous state DCA-116 reconciles."""
    resv = BudgetReservationStore(session, business_id).reserve(
        ReservationRequest(
            idempotency_key=f"{call_key}-resv", amount_minor=amount, provider=provider
        ),
        RES_ADMIN,
        now=NOW,
    )
    session.commit()
    BudgetReconciliationStore(session, business_id).reconcile(
        ReconciliationRequest(resv.id, f"{call_key}-mark", ReconcileOutcome.MARK_UNCERTAIN),
        REC_ADMIN,
        now=NOW,
    )
    session.commit()
    store = ProviderCallStore(session, business_id)
    store.record(
        ProviderCallIntent(
            call_key=call_key,
            capability="contact_research",
            provider=provider,
            reservation_id=resv.id,
        ),
        CALL_ADMIN,
        now=NOW,
    )
    store.transition(
        ProviderCallTransition(call_key, ProviderCallStatus.IN_FLIGHT), CALL_ADMIN, now=NOW
    )
    store.transition(
        ProviderCallTransition(
            call_key, ProviderCallStatus.UNCERTAIN, outcome_code="gateway_timeout"
        ),
        CALL_ADMIN,
        now=NOW,
    )
    session.commit()
    return call_key


def _audit_count(session, business_id) -> int:
    return (
        session.query(AuditEvent)
        .filter(AuditEvent.business_id == business_id, AuditEvent.action == AUDIT_ACTION_RESOLVE)
        .count()
    )


def _remaining(session, business_id) -> int:
    return BudgetReservationStore(session, business_id).window(now=NOW).daily_remaining_minor


def _record(session, business_id, call_key) -> ProviderCallRecord:
    r = UncertainOutcomeResolver(session, business_id).by_key(call_key)
    assert r is not None
    return r


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


def test_confirmed_happened_settles_the_tied_hold(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000)
        _setup_uncertain(session, BIZ_A, amount=4_000)
        # The uncertain hold still counts its full 4_000 against the 5_000 cap.
        assert _remaining(session, BIZ_A) == 1_000

        resolver = UncertainOutcomeResolver(session, BIZ_A)
        result = resolver.resolve(
            OutcomeEvidence("call-1", "receipt-1", happened=True, actual_minor=2_500),
            ADMIN,
            now=NOW,
        )
        session.commit()

        assert result.resolution == ProviderCallResolution.CONFIRMED.value
        assert result.supported is True and result.routed_to_review is False
        assert result.call_status == ProviderCallStatus.UNCERTAIN.value  # lifecycle never reopened
        assert result.reservation_status == ReservationStatus.SETTLED.value
        assert result.settled_minor == 2_500 and result.released_minor == 1_500
        assert not result.replayed
        # The unused 1_500 is freed: 5_000 cap - 2_500 settled.
        assert _remaining(session, BIZ_A) == 2_500
        assert _audit_count(session, BIZ_A) == 1
        rec = _record(session, BIZ_A, "call-1")
        assert rec.is_resolved and rec.resolution_outcome_code == "receipt_confirmed"


def test_confirmed_not_happened_releases_the_tied_hold(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000)
        _setup_uncertain(session, BIZ_A, amount=4_000)
        resolver = UncertainOutcomeResolver(session, BIZ_A)
        result = resolver.resolve(
            OutcomeEvidence("call-1", "receipt-1", happened=False), ADMIN, now=NOW
        )
        session.commit()
        assert result.resolution == ProviderCallResolution.VOID.value
        assert result.reservation_status == ReservationStatus.RELEASED.value
        assert result.settled_minor == 0 and result.released_minor == 4_000
        assert _remaining(session, BIZ_A) == 5_000  # the whole preserved hold is freed
        assert _record(session, BIZ_A, "call-1").resolution_outcome_code == "receipt_absent"


def test_unsupported_provider_is_routed_to_review_and_changes_no_ledger(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000)
        _setup_uncertain(session, BIZ_A, amount=4_000, provider="vendor_x")
        before = _remaining(session, BIZ_A)
        resolver = UncertainOutcomeResolver(session, BIZ_A)
        # Even with evidence, an unsupported provider is never auto-confirmed — it is routed to review.
        result = resolver.resolve(
            OutcomeEvidence("call-1", "receipt-1", happened=True, actual_minor=1), ADMIN, now=NOW
        )
        session.commit()
        assert result.resolution == ProviderCallResolution.NEEDS_REVIEW.value
        assert result.supported is False and result.routed_to_review is True
        assert result.reservation_status == ReservationStatus.UNCERTAIN.value  # untouched
        assert result.settled_minor is None and result.released_minor == 0
        # The hold is preserved — no uncontrolled charge, no release, no retry.
        assert _remaining(session, BIZ_A) == before
        rec = _record(session, BIZ_A, "call-1")
        assert not rec.is_resolved  # review is not a FINAL resolution
        assert rec.resolution_outcome_code == "unsupported_provider"


def test_supported_provider_without_evidence_is_refused(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000)
        _setup_uncertain(session, BIZ_A)
        resolver = UncertainOutcomeResolver(session, BIZ_A)
        with pytest.raises(OutcomeResolutionError) as exc:
            resolver.resolve(OutcomeEvidence("call-1", "receipt-1"), ADMIN, now=NOW)
        assert exc.value.code == REASON_NO_EVIDENCE
        session.rollback()
        assert (
            _record(session, BIZ_A, "call-1").resolution == ProviderCallResolution.UNRESOLVED.value
        )
        assert _audit_count(session, BIZ_A) == 0


def test_settle_requires_a_non_negative_actual(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000)
        _setup_uncertain(session, BIZ_A)
        resolver = UncertainOutcomeResolver(session, BIZ_A)
        with pytest.raises(OutcomeResolutionError) as exc1:
            resolver.resolve(OutcomeEvidence("call-1", "receipt-1", happened=True), ADMIN, now=NOW)
        assert exc1.value.code == REASON_INVALID_ACTUAL
        with pytest.raises(OutcomeResolutionError) as exc2:
            resolver.resolve(
                OutcomeEvidence("call-1", "receipt-1", happened=True, actual_minor=-5),
                ADMIN,
                now=NOW,
            )
        assert exc2.value.code == REASON_INVALID_ACTUAL
        session.rollback()
        assert (
            _record(session, BIZ_A, "call-1").resolution == ProviderCallResolution.UNRESOLVED.value
        )


def test_duplicate_resolution_under_same_identity_is_an_idempotent_replay(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000)
        _setup_uncertain(session, BIZ_A, amount=4_000)
        resolver = UncertainOutcomeResolver(session, BIZ_A)
        ev = OutcomeEvidence("call-1", "receipt-1", happened=True, actual_minor=2_500)
        first = resolver.resolve(ev, ADMIN, now=NOW)
        session.commit()
        assert not first.replayed and first.settled_minor == 2_500

        replay = resolver.resolve(ev, ADMIN, now=NOW)
        session.commit()
        assert replay.replayed is True
        assert replay.settled_minor == 2_500 and replay.released_minor == 1_500
        assert _audit_count(session, BIZ_A) == 1  # only the first resolution audited
        assert _remaining(session, BIZ_A) == 2_500  # not double-freed


def test_conflicting_resolution_under_a_different_identity_is_refused(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000)
        _setup_uncertain(session, BIZ_A, amount=4_000)
        resolver = UncertainOutcomeResolver(session, BIZ_A)
        resolver.resolve(
            OutcomeEvidence("call-1", "receipt-1", happened=True, actual_minor=2_500),
            ADMIN,
            now=NOW,
        )
        session.commit()

        with pytest.raises(OutcomeResolutionError) as exc:
            resolver.resolve(OutcomeEvidence("call-1", "receipt-2", happened=False), ADMIN, now=NOW)
        assert exc.value.code == REASON_ALREADY_RESOLVED
        session.rollback()

        # The first resolution is intact; an ambiguity is reconciled only once.
        rec = _record(session, BIZ_A, "call-1")
        assert rec.resolution == ProviderCallResolution.CONFIRMED.value
        assert _audit_count(session, BIZ_A) == 1
        assert _remaining(session, BIZ_A) == 2_500


def test_a_non_uncertain_outcome_cannot_be_resolved(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000)
        # A plain succeeded call (no ambiguity) — never uncertain.
        store = ProviderCallStore(session, BIZ_A)
        store.record(ProviderCallIntent(call_key="done", capability="scoring"), CALL_ADMIN, now=NOW)
        store.transition(
            ProviderCallTransition("done", ProviderCallStatus.IN_FLIGHT), CALL_ADMIN, now=NOW
        )
        store.transition(
            ProviderCallTransition("done", ProviderCallStatus.SUCCEEDED, total_tokens=5),
            CALL_ADMIN,
            now=NOW,
        )
        session.commit()
        resolver = UncertainOutcomeResolver(session, BIZ_A)
        with pytest.raises(OutcomeResolutionError) as exc:
            resolver.resolve(
                OutcomeEvidence("done", "receipt-1", happened=True, actual_minor=1), ADMIN, now=NOW
            )
        assert exc.value.code == REASON_NOT_UNCERTAIN
        session.rollback()
        assert _audit_count(session, BIZ_A) == 0


def test_unknown_call_key_is_refused(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000)
        resolver = UncertainOutcomeResolver(session, BIZ_A)
        with pytest.raises(OutcomeResolutionError) as exc:
            resolver.resolve(
                OutcomeEvidence("no-such-call", "receipt-1", happened=False), ADMIN, now=NOW
            )
        assert exc.value.code == REASON_NOT_FOUND
        session.rollback()


def test_missing_keys_are_refused(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000)
        resolver = UncertainOutcomeResolver(session, BIZ_A)
        with pytest.raises(OutcomeResolutionError) as exc1:
            resolver.resolve(OutcomeEvidence("  ", "receipt-1", happened=False), ADMIN, now=NOW)
        assert exc1.value.code == REASON_MISSING_KEY
        with pytest.raises(OutcomeResolutionError) as exc2:
            resolver.resolve(OutcomeEvidence("call-1", "  ", happened=False), ADMIN, now=NOW)
        assert exc2.value.code == REASON_MISSING_RESOLUTION_KEY
        session.rollback()


def test_resolution_is_tenant_scoped(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000)
        _seed_limit(session, BIZ_B, daily=5_000)
        _setup_uncertain(session, BIZ_A, amount=4_000)
        # B cannot resolve A's ambiguous call: A's key reads exactly like not-found in B's scope.
        b_resolver = UncertainOutcomeResolver(session, BIZ_B)
        with pytest.raises(OutcomeResolutionError) as exc:
            b_resolver.resolve(
                OutcomeEvidence("call-1", "receipt-1", happened=False), ADMIN, now=NOW
            )
        assert exc.value.code == REASON_NOT_FOUND
        session.rollback()
        # A's record is intact and still unresolved.
        assert (
            _record(session, BIZ_A, "call-1").resolution == ProviderCallResolution.UNRESOLVED.value
        )


def test_authority_is_checked_first_and_changes_nothing(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000)
        _setup_uncertain(session, BIZ_A, amount=4_000)
        resolver = UncertainOutcomeResolver(session, BIZ_A)
        with pytest.raises(OutcomeResolutionAuthorizationError):
            resolver.resolve(
                OutcomeEvidence("call-1", "receipt-1", happened=True, actual_minor=1),
                VIEWER,
                now=NOW,
            )
        session.rollback()
        assert (
            _record(session, BIZ_A, "call-1").resolution == ProviderCallResolution.UNRESOLVED.value
        )
        assert _audit_count(session, BIZ_A) == 0


def test_resolver_does_not_commit(session_factory):
    with session_factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000)
        _setup_uncertain(session, BIZ_A, amount=4_000)
        UncertainOutcomeResolver(session, BIZ_A).resolve(
            OutcomeEvidence("call-1", "receipt-1", happened=False), ADMIN, now=NOW
        )
        session.rollback()  # the store never commits — a rollback undoes the resolution
    with session_factory() as session:
        rec = _record(session, BIZ_A, "call-1")
        assert rec.resolution == ProviderCallResolution.UNRESOLVED.value
        # The tied hold is still UNCERTAIN (not released) and no resolution was audited.
        assert _remaining(session, BIZ_A) == 1_000
        assert _audit_count(session, BIZ_A) == 0


# --------------------------------------------------------------------------------------------------
# PostgreSQL concurrency test — a duplicate settlement never applies twice, even under a race
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)
_PG_SCHEMA = "dca_outcome_resolve_test"


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
def test_concurrent_resolutions_apply_exactly_once(pg_factory):
    """Several resolutions racing on the SAME uncertain outcome SERIALIZE on the FOR UPDATE lock, so
    exactly ONE applies and the rest are refused as already-resolved — a duplicate settlement never
    double-spends, on real Postgres.

    Eight threads each try to CONFIRM (settle) the same uncertain outcome under a DISTINCT evidence
    identity. Exactly one must win; the rest must see the committed final resolution and be refused."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    with pg_factory() as session:
        _seed_limit(session, BIZ_A, daily=5_000)
        _setup_uncertain(session, BIZ_A, amount=4_000)

    n = 8
    barrier = threading.Barrier(n)
    outcomes: list[str] = []
    lock = threading.Lock()

    def _attempt(i: int) -> None:
        barrier.wait()  # release all threads together to maximize contention on the lock
        session = pg_factory()
        try:
            UncertainOutcomeResolver(session, BIZ_A).resolve(
                OutcomeEvidence("call-1", f"receipt-{i}", happened=True, actual_minor=2_500),
                ADMIN,
                now=NOW,
            )
            session.commit()
            result = "applied"
        except OutcomeResolutionError as exc:
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
    assert len(applied) == 1, outcomes  # the serializing lock admits exactly one resolution
    assert all(o == REASON_ALREADY_RESOLVED for o in refused), outcomes

    with pg_factory() as session:
        rec = _record(session, BIZ_A, "call-1")
        audits = _audit_count(session, BIZ_A)
        remaining = _remaining(session, BIZ_A)
    assert rec.resolution == ProviderCallResolution.CONFIRMED.value
    assert audits == 1  # a single resolution was audited
    assert remaining == 2_500  # settled exactly once (5_000 cap - 2_500), never doubled
