"""DCA-130 — bound an AI-service (model) invocation's cost and retries.

Pins the load-bearing behaviours of :mod:`app.ai.bound_invocation` — the invoker that wraps ONE model
invocation through the DCA-128 facade so its COST is bounded (DCA-113 reserve → DCA-115 lifecycle →
DCA-114 reconcile) and its RETRIES are not stacked on the gateway's (the gateway is called EXACTLY ONCE;
the outcome is classified with the DCA-104 discipline for a surrounding bounded job retry). **No real/paid
call is ever made** — the only transport is a deterministic scripted fixture that performs no network/model
work and either returns a synthetic envelope or raises the DCA-129 transport errors:

1. **Within-budget call reserves then settles from the returned actual cost.** A completed call reserves
   the conservative estimate, records a SUCCEEDED provider call, and settles the hold at the reported
   actual cost (unused headroom released).
2. **Hard limits deny invocation.** An over-budget reservation (and an oversized payload) is refused BEFORE
   the call, so the gateway is never invoked and nothing is spent or written.
3. **A non-retryable gateway error is NOT retried.** A 4xx rejection / a contract violation releases the
   hold and classifies PERMANENT; a transient (5xx / unreachable) releases the hold and classifies
   TRANSIENT. The pure DCA-104 ``plan_retry`` then retries a transient up to the cap (then dead-letters) and
   dead-letters a permanent immediately — the invoker never loops.
4. **Timeout never implies no spend; the unknown cost stays pending.** A read timeout drives the call
   UNCERTAIN and preserves the full hold (MARK_UNCERTAIN) — never zero.
5. **Duplicated receipts cannot double-settle / idempotent.** Re-invoking the same logical key is a
   deterministic no-op (no second reservation, no second call, no second settlement).

The invoker logic is unit-tested on in-memory SQLite; the cost stores' real transaction / RLS / locking
path is covered on disposable PostgreSQL by ``tests/test_budget_reserve.py`` / ``test_budget_reconcile.py``
/ ``test_provider_call.py``.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime
from typing import Any

import pytest

from app.ai.bound_invocation import (
    OUTCOME_COMPLETED,
    OUTCOME_FAILED,
    OUTCOME_REJECTED,
    OUTCOME_UNAVAILABLE,
    OUTCOME_UNCERTAIN,
    REASON_INVALID_ESTIMATE,
    REASON_MISSING_KEY,
    REASON_PAYLOAD_TOO_LARGE,
    BoundInvocationActor,
    BoundInvocationAuthorizationError,
    BoundInvocationError,
    BoundInvocationRequest,
    BoundModelInvoker,
    _amount_to_minor_ceil,
)
from app.ai.contract import AiCapabilityFacade, AiUntrustedItem
from app.audit.sink import ActorType, AuditActor
from app.clients.ai_client import AiServiceRejected, AiServiceTimeout, AiServiceUnavailable
from app.cost.reserve import REASON_PER_RUN_EXCEEDED
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import (
    AuditEvent,
    BudgetReservation,
    ProviderCallRecord,
    ProviderCallStatus,
    ReservationStatus,
)
from app.jobs.retry import (
    JobFailureClass,
    RetryAction,
    plan_retry,
)
from app.services.budget_store import BudgetLimitInput, BudgetLimitStore

CAPABILITY = "contact_research"  # in the DCA-128 allowlist, so the facade may request it
BIZ_A = "11111111-1111-1111-1111-11111111b130"
BIZ_B = "22222222-2222-2222-2222-22222222b130"
NOW = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)  # 14:30 IST → day 2026-10-31, month 2026-10

ADMIN = BoundInvocationActor(
    subject="00000000-0000-4000-9000-0000000a0130", roles=("owner", "admin")
)
VIEWER = BoundInvocationActor(subject="00000000-0000-4000-9000-0000000v0130", roles=("viewer",))
BUDGET_ACTOR = AuditActor(id="00000000-0000-4000-9000-0000000b0130", type=ActorType.USER)

ESTIMATE_MINOR = 2_000  # ₹20.00 reserved up front; a success reports ₹12.00 actual


class ScriptedGatewayTransport:
    """A deterministic :class:`~app.ai.contract.AiGatewayTransport` for DCA-130 tests. Pure: no network, no
    model, no spend. Echoes the call's ``jobId``/``capability`` (so the facade can parse it) and, by knob,
    either returns a completed/failed envelope with a chosen INR cost or RAISES a DCA-129 transport error.
    Every call is recorded for inspection (never a send)."""

    synthetic = True

    def __init__(
        self,
        *,
        enabled: bool = True,
        cost_major: float = 12.0,
        currency: str = "INR",
        total_tokens: int = 240,
        fail: bool = False,
        error_retryable: bool = False,
        raises: Exception | None = None,
    ) -> None:
        self._enabled = enabled
        self._cost_major = cost_major
        self._currency = currency
        self._total_tokens = total_tokens
        self._fail = fail
        self._error_retryable = error_retryable
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    def invoke(
        self, capability: str, *, business_id: str, job_id: str, request_id: str, **kw: Any
    ) -> dict:
        self.calls.append({"capability": capability, "job_id": job_id, "request_id": request_id})
        if self._raises is not None:
            raise self._raises
        envelope: dict[str, Any] = {
            "jobId": job_id,
            "capability": capability,
            "status": "failed" if self._fail else "completed",
            "structuredOutput": None if self._fail else {"synthetic": True},
            "confidence": None if self._fail else 0.98,
            "modelProvider": "scripted",
            "modelId": "stub-model",
            "promptVersion": kw.get("prompt_version") or "stub-v1",
            "tokenUsage": {
                "promptTokens": 100,
                "completionTokens": max(0, self._total_tokens - 100),
                "totalTokens": self._total_tokens,
            },
            "costEstimate": {
                "amount": self._cost_major,
                "currency": self._currency,
                "estimated": False,
                "pricingUnavailable": False,
            },
            "validationStatus": "failed" if self._fail else "valid",
            "validationReport": {"status": "failed" if self._fail else "passed"},
            "humanReviewRequired": False,
        }
        if self._fail:
            envelope["error"] = {
                "code": "capability_rule_failed",
                "message": "synthetic body-level failure",
                "retryable": self._error_retryable,
            }
        return envelope


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def _seed_budget(
    session, business_id, *, daily=100_000, monthly=1_000_000, per_run=100_000
) -> None:
    BudgetLimitStore(session, business_id).save(
        BudgetLimitInput(
            daily_cap_minor=daily, monthly_cap_minor=monthly, per_run_cap_minor=per_run
        ),
        BUDGET_ACTOR,
    )


def _facade(transport: ScriptedGatewayTransport) -> AiCapabilityFacade:
    return AiCapabilityFacade(transport)


def _request(**overrides) -> BoundInvocationRequest:
    base: dict[str, Any] = {
        "invocation_key": "inv-1",
        "capability": CAPABILITY,
        "reserved_cost_minor": ESTIMATE_MINOR,
        "input_context": {"org_ref": "CIN-U72900MH2026PTC000130"},
    }
    base.update(overrides)
    return BoundInvocationRequest(**base)


# --- pure helper -----------------------------------------------------------------------------------


def test_amount_to_minor_ceil_is_conservative_and_never_negative():
    assert _amount_to_minor_ceil(12.0) == 1_200
    assert _amount_to_minor_ceil("0.01") == 1  # exact paise, no binary-float artefact
    assert _amount_to_minor_ceil(0.001) == 1  # rounds UP (conservative)
    assert _amount_to_minor_ceil(0) == 0
    assert _amount_to_minor_ceil(-5) == 0  # a nonsensical cost is never a negative settle


# --- 1) within-budget call reserves then settles ---------------------------------------------------


def test_within_budget_call_reserves_and_settles(session_factory):
    with session_factory() as session:
        _seed_budget(session, BIZ_A)
        session.commit()

    transport = ScriptedGatewayTransport(cost_major=12.0)
    with session_factory() as session:
        result = BoundModelInvoker(session, BIZ_A, _facade(transport)).invoke(
            _request(), ADMIN, now=NOW
        )
        assert result.authorized is True and result.skipped is False
        assert result.phase == "call" and result.outcome == OUTCOME_COMPLETED
        assert result.call_status == ProviderCallStatus.SUCCEEDED.value
        # Bounded, tracked spend: reserved ₹20.00, settled at the reported ₹12.00, ₹8.00 released.
        assert result.reserved_minor == ESTIMATE_MINOR
        assert result.settled_minor == 1_200 and result.spent_minor == 1_200
        assert result.released_minor == 800
        assert result.failure_class is None and result.retryable is False
        assert result.structured_output == {"synthetic": True}
        session.commit()

    assert len(transport.calls) == 1  # the gateway was called EXACTLY once
    assert transport.calls[0]["request_id"] == "dca:inv-1"  # stable logical invocation key

    with session_factory() as session:
        call = session.query(ProviderCallRecord).one()
        assert call.status == ProviderCallStatus.SUCCEEDED.value
        assert call.dispatched is True and call.total_tokens == 240
        reservation = session.query(BudgetReservation).one()
        assert reservation.id == call.reservation_id
        assert reservation.status == ReservationStatus.SETTLED.value
        assert reservation.settled_amount_minor == 1_200 and reservation.overspend_minor == 0


def test_over_run_cost_is_clamped_and_flagged(session_factory):
    with session_factory() as session:
        _seed_budget(session, BIZ_A)
        session.commit()

    with session_factory() as session:
        # The gateway reports ₹30.00 actual, above the ₹20.00 reserved estimate.
        transport = ScriptedGatewayTransport(cost_major=30.0)
        result = BoundModelInvoker(session, BIZ_A, _facade(transport)).invoke(
            _request(), ADMIN, now=NOW
        )
        assert result.settled_minor == ESTIMATE_MINOR  # clamped to the reserved hold
        session.commit()

    with session_factory() as session:
        reservation = session.query(BudgetReservation).one()
        assert reservation.settled_amount_minor == ESTIMATE_MINOR
        assert reservation.overspend_minor == 1_000  # ₹10.00 over-run, flagged, never charged


# --- 2) hard limits deny invocation (no call) ------------------------------------------------------


def test_over_budget_is_denied_before_the_call(session_factory):
    with session_factory() as session:
        _seed_budget(session, BIZ_A, per_run=500)  # far below the estimate
        session.commit()

    transport = ScriptedGatewayTransport()
    with session_factory() as session:
        result = BoundModelInvoker(session, BIZ_A, _facade(transport)).invoke(
            _request(), ADMIN, now=NOW
        )
        assert result.authorized is False and result.phase == "budget"
        assert result.reason == REASON_PER_RUN_EXCEEDED
        assert result.provider_call_id is None and result.spent_minor == 0
        session.rollback()

    assert transport.calls == []  # the gateway was NEVER invoked
    with session_factory() as session:
        assert session.query(BudgetReservation).count() == 0
        assert session.query(ProviderCallRecord).count() == 0


def test_oversized_payload_is_refused_before_any_state(session_factory):
    with session_factory() as session:
        _seed_budget(session, BIZ_A)
        session.commit()

    transport = ScriptedGatewayTransport()
    with session_factory() as session:
        with pytest.raises(BoundInvocationError) as exc:
            BoundModelInvoker(session, BIZ_A, _facade(transport)).invoke(
                _request(input_context={"blob": "x" * 50}), ADMIN, max_payload_chars=20, now=NOW
            )
        assert exc.value.code == REASON_PAYLOAD_TOO_LARGE
        session.rollback()

    assert transport.calls == []
    with session_factory() as session:
        assert session.query(BudgetReservation).count() == 0


def test_untrusted_frames_count_toward_the_payload_bound(session_factory):
    with session_factory() as session:
        _seed_budget(session, BIZ_A)
        session.commit()

    transport = ScriptedGatewayTransport()
    with session_factory() as session:
        with pytest.raises(BoundInvocationError) as exc:
            BoundModelInvoker(session, BIZ_A, _facade(transport)).invoke(
                _request(
                    input_context={},
                    untrusted=(AiUntrustedItem(source="web", content="y" * 200),),
                ),
                ADMIN,
                max_payload_chars=50,
                now=NOW,
            )
        assert exc.value.code == REASON_PAYLOAD_TOO_LARGE
    assert transport.calls == []


def test_unknown_capability_is_a_reproducible_denial_with_no_call(session_factory):
    with session_factory() as session:
        _seed_budget(session, BIZ_A)
        session.commit()

    transport = ScriptedGatewayTransport()
    with session_factory() as session:
        # "scoring" is not in the DCA-128 allowlist → fail-closed, no reserve, no call.
        result = BoundModelInvoker(session, BIZ_A, _facade(transport)).invoke(
            _request(capability="scoring"), ADMIN, now=NOW
        )
        assert result.authorized is False and result.phase == "capability"
        session.rollback()
    assert transport.calls == []
    with session_factory() as session:
        assert session.query(BudgetReservation).count() == 0


# --- 3) failure classification: non-retryable not retried; transient bounded by the cap ------------


def test_rejected_4xx_releases_hold_and_is_not_retryable(session_factory):
    with session_factory() as session:
        _seed_budget(session, BIZ_A)
        session.commit()

    transport = ScriptedGatewayTransport(raises=AiServiceRejected(400))
    with session_factory() as session:
        result = BoundModelInvoker(session, BIZ_A, _facade(transport)).invoke(
            _request(), ADMIN, now=NOW
        )
        assert result.outcome == OUTCOME_REJECTED
        assert result.call_status == ProviderCallStatus.FAILED.value
        assert result.settled_minor == 0 and result.released_minor == ESTIMATE_MINOR
        assert result.failure_class is JobFailureClass.PERMANENT and result.retryable is False
        session.commit()

    assert len(transport.calls) == 1  # called once, NOT retried by the invoker
    with session_factory() as session:
        assert session.query(BudgetReservation).one().status == ReservationStatus.RELEASED.value

    # A PERMANENT failure is dead-lettered immediately by DCA-104, even with attempts remaining.
    signal = result.as_failure_signal()
    assert signal is not None
    plan = plan_retry(signal, attempt_count=1, max_attempts=5, now=NOW, rng=random.Random(0))
    assert plan.action is RetryAction.DEAD_LETTER_PERMANENT


def test_unavailable_5xx_releases_hold_and_is_a_bounded_transient_retry(session_factory):
    with session_factory() as session:
        _seed_budget(session, BIZ_A)
        session.commit()

    transport = ScriptedGatewayTransport(raises=AiServiceUnavailable("unreachable"))
    with session_factory() as session:
        result = BoundModelInvoker(session, BIZ_A, _facade(transport)).invoke(
            _request(), ADMIN, now=NOW
        )
        assert result.outcome == OUTCOME_UNAVAILABLE
        assert result.call_status == ProviderCallStatus.FAILED.value
        assert result.released_minor == ESTIMATE_MINOR and result.spent_minor == 0
        assert result.failure_class is JobFailureClass.TRANSIENT and result.retryable is True
        session.commit()

    assert len(transport.calls) == 1  # one call; the gateway owns model retry, DCA does not loop

    # DCA-104 retries a transient WHILE attempts remain, then dead-letters at the cap (bounded).
    signal = result.as_failure_signal()
    assert signal is not None
    rng = random.Random(0)
    assert plan_retry(signal, attempt_count=1, max_attempts=3, now=NOW, rng=rng).action is (
        RetryAction.SCHEDULE_RETRY
    )
    assert plan_retry(signal, attempt_count=3, max_attempts=3, now=NOW, rng=rng).action is (
        RetryAction.DEAD_LETTER_EXHAUSTED
    )


def test_body_level_failed_releases_hold(session_factory):
    with session_factory() as session:
        _seed_budget(session, BIZ_A)
        session.commit()

    transport = ScriptedGatewayTransport(fail=True, error_retryable=False)
    with session_factory() as session:
        result = BoundModelInvoker(session, BIZ_A, _facade(transport)).invoke(
            _request(), ADMIN, now=NOW
        )
        assert result.outcome == OUTCOME_FAILED
        assert result.call_status == ProviderCallStatus.FAILED.value
        assert result.released_minor == ESTIMATE_MINOR and result.spent_minor == 0
        assert (
            result.failure_class is JobFailureClass.PERMANENT
        )  # non-retryable gateway rule failure
        session.commit()

    with session_factory() as session:
        assert session.query(BudgetReservation).one().status == ReservationStatus.RELEASED.value


# --- 4) timeout → uncertain → preserved pending (never zero) ---------------------------------------


def test_timeout_preserves_the_hold_as_uncertain(session_factory):
    with session_factory() as session:
        _seed_budget(session, BIZ_A)
        session.commit()

    transport = ScriptedGatewayTransport(raises=AiServiceTimeout("read timeout"))
    with session_factory() as session:
        result = BoundModelInvoker(session, BIZ_A, _facade(transport)).invoke(
            _request(), ADMIN, now=NOW
        )
        assert result.outcome == OUTCOME_UNCERTAIN and result.needs_review is True
        assert result.call_status == ProviderCallStatus.UNCERTAIN.value
        # The full hold is PRESERVED (never zeroed): nothing settled, nothing released.
        assert result.settled_minor is None
        assert result.spent_minor == ESTIMATE_MINOR and result.released_minor == 0
        session.commit()

    assert len(transport.calls) == 1
    with session_factory() as session:
        call = session.query(ProviderCallRecord).one()
        assert call.status == ProviderCallStatus.UNCERTAIN.value and call.cost_possible is True
        reservation = session.query(BudgetReservation).one()
        assert reservation.status == ReservationStatus.UNCERTAIN.value
        assert reservation.settled_amount_minor is None  # unknown cost stays pending, not zero


# --- 5) idempotent re-invocation (duplicated receipts cannot double-settle) ------------------------


def test_idempotent_reinvocation_is_a_no_op(session_factory):
    with session_factory() as session:
        _seed_budget(session, BIZ_A)
        session.commit()

    with session_factory() as session:
        BoundModelInvoker(session, BIZ_A, _facade(ScriptedGatewayTransport())).invoke(
            _request(), ADMIN, now=NOW
        )
        session.commit()

    transport2 = ScriptedGatewayTransport()
    with session_factory() as session:
        again = BoundModelInvoker(session, BIZ_A, _facade(transport2)).invoke(
            _request(), ADMIN, now=NOW
        )
        assert again.skipped is True
        assert again.outcome == OUTCOME_COMPLETED
        assert again.reserved_minor == ESTIMATE_MINOR and again.settled_minor == 1_200
        session.commit()

    assert transport2.calls == []  # the replay made NO second gateway call
    with session_factory() as session:
        # Unchanged: still exactly one reservation and one provider call (no double-settle).
        assert session.query(BudgetReservation).count() == 1
        assert session.query(ProviderCallRecord).count() == 1
        assert session.query(BudgetReservation).one().status == ReservationStatus.SETTLED.value


# --- 6) authority + request-shape refusals change nothing ------------------------------------------


def test_viewer_is_refused_before_any_state(session_factory):
    with session_factory() as session:
        _seed_budget(session, BIZ_A)
        session.commit()

    transport = ScriptedGatewayTransport()
    with session_factory() as session:
        with pytest.raises(BoundInvocationAuthorizationError):
            BoundModelInvoker(session, BIZ_A, _facade(transport)).invoke(
                _request(), VIEWER, now=NOW
            )
        session.rollback()
    assert transport.calls == []
    with session_factory() as session:
        assert session.query(BudgetReservation).count() == 0
        assert session.query(ProviderCallRecord).count() == 0


def test_missing_key_and_invalid_estimate_are_refused(session_factory):
    with session_factory() as session:
        _seed_budget(session, BIZ_A)
        session.commit()

    with session_factory() as session:
        invoker = BoundModelInvoker(session, BIZ_A, _facade(ScriptedGatewayTransport()))
        with pytest.raises(BoundInvocationError) as e1:
            invoker.invoke(_request(invocation_key="  "), ADMIN, now=NOW)
        assert e1.value.code == REASON_MISSING_KEY
        with pytest.raises(BoundInvocationError) as e2:
            invoker.invoke(_request(reserved_cost_minor=0), ADMIN, now=NOW)
        assert e2.value.code == REASON_INVALID_ESTIMATE


# --- 7) tenant isolation + never-commits -----------------------------------------------------------


def test_the_invoker_never_commits(session_factory):
    with session_factory() as session:
        _seed_budget(session, BIZ_A)
        session.commit()

    with session_factory() as session:
        BoundModelInvoker(session, BIZ_A, _facade(ScriptedGatewayTransport())).invoke(
            _request(), ADMIN, now=NOW
        )
        session.rollback()  # the invoker never commits — a rollback undoes the whole run

    with session_factory() as session:
        assert session.query(BudgetReservation).count() == 0
        assert session.query(ProviderCallRecord).count() == 0


def test_two_businesses_invoke_independently(session_factory):
    with session_factory() as session:
        _seed_budget(session, BIZ_A)
        _seed_budget(session, BIZ_B)
        session.commit()

    with session_factory() as session:
        BoundModelInvoker(session, BIZ_A, _facade(ScriptedGatewayTransport())).invoke(
            _request(), ADMIN, now=NOW
        )
        BoundModelInvoker(session, BIZ_B, _facade(ScriptedGatewayTransport())).invoke(
            _request(), ADMIN, now=NOW
        )
        session.commit()

    with session_factory() as session:
        # B_A's invocation record is invisible to B_B (business-scoped lookup).
        assert (
            BoundModelInvoker(session, BIZ_A, _facade(ScriptedGatewayTransport())).call_for("inv-1")
            is not None
        )
        a_rows = (
            session.query(ProviderCallRecord)
            .filter(ProviderCallRecord.business_id == BIZ_A)
            .count()
        )
        b_rows = (
            session.query(ProviderCallRecord)
            .filter(ProviderCallRecord.business_id == BIZ_B)
            .count()
        )
        assert a_rows == 1 and b_rows == 1


def test_audit_entries_are_recorded_for_a_bounded_run(session_factory):
    with session_factory() as session:
        _seed_budget(session, BIZ_A)
        session.commit()

    with session_factory() as session:
        BoundModelInvoker(session, BIZ_A, _facade(ScriptedGatewayTransport())).invoke(
            _request(), ADMIN, now=NOW
        )
        session.commit()

    with session_factory() as session:
        actions = [a.action for a in session.query(AuditEvent).all()]
        # reserve + record + 2 transitions (in_flight, succeeded) + reconcile are all atomically audited.
        assert "budget.reservation.reserve" in actions
        assert "provider.call.record" in actions
        assert "provider.call.transition" in actions
        assert "budget.reservation.reconcile" in actions
