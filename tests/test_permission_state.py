"""DCA-075 — derive the current permission state from the versioned receipt/withdrawal history.

Pins the load-bearing behaviours of :mod:`app.consent.permission_state` — the pure, deterministic
conclusion layer that projects the permission in force *now* for a scope
(``brand_key, contact_point_id, channel, purpose``) over the append-only DCA-074
:class:`~app.db.models.ConsentReceipt` timeline:

1. **Out-of-order and duplicate events cannot revive a withdrawn permission.** The latest event *by the
   subject's action time* (``collected_at``) decides the state, not the recording order — so a delayed
   old grant recorded AFTER a withdrawal is superseded (``late_grant_superseded``), a duplicate of an
   earlier event collapses to the same state (``duplicate_ignored``), and at an equal ``collected_at`` a
   revocation beats a grant (``revocation_wins_tie``).
2. **Channel and purpose scopes stay distinct.** A grant for one ``(channel, purpose)`` never satisfies
   another; :func:`project_permission_states` keeps each scope independent.
3. **Reproducible, pure, no state change.** The same events at the same ``now`` always yield the same
   state and the same denial; the projection mutates / records / deletes nothing, and the full event
   history is preserved.

The projection is pure (no DB), so these run network-free. The read-only receipt adapter is exercised
on in-memory SQLite; the real transaction / RLS path for the underlying ``consent_receipts`` table is
covered on a disposable PostgreSQL (``DCA_TEST_DATABASE_URL``) by the test at the end and by
``tests/test_consent_receipt.py`` / ``tests/test_rls.py`` — never relying on SQLite for the durable
path.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine

from app.audit.sink import ActorType, AuditActor
from app.consent import (
    ACTION_CONFIRM,
    ACTION_DECLINE,
    ACTION_GRANT,
    ACTION_WITHDRAW,
    REASON_CONFIRMED_IN_FORCE,
    REASON_DECLINED,
    REASON_DUPLICATE_IGNORED,
    REASON_GRANTED_IN_FORCE,
    REASON_LATE_GRANT_SUPERSEDED,
    REASON_NO_EVENTS,
    REASON_REVOCATION_WINS_TIE,
    REASON_WITHDRAWN,
    PermissionEvent,
    PermissionState,
    ScopeKey,
    derive_permission_state,
    events_from_receipts,
    project_permission_states,
)
from app.db.base import Base, make_engine, make_session_factory

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"
ACTOR = AuditActor(id="00000000-0000-4000-9000-0000000a0075", type=ActorType.USER)
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
CP = "cp-email-1"


def _event(
    receipt_id: str,
    action: str,
    collected_at: datetime,
    *,
    brand: str = "acmenewsletter",
    contact_point: str = CP,
    channel: str = "email",
    purpose: str = "marketing_email",
    recorded_at: datetime | None = None,
) -> PermissionEvent:
    return PermissionEvent(
        receipt_id=receipt_id,
        brand_key=brand,
        contact_point_id=contact_point,
        channel=channel,
        purpose=purpose,
        action=action,
        collected_at=collected_at,
        recorded_at=recorded_at if recorded_at is not None else collected_at,
    )


# --- the headline: out-of-order / duplicate events cannot revive permission -----------------------


def test_latest_grant_is_in_force():
    proj = derive_permission_state([_event("r1", ACTION_GRANT, T0)])
    assert proj.state is PermissionState.GRANTED
    assert proj.granted is True
    assert proj.effective_event is not None and proj.effective_event.receipt_id == "r1"
    assert REASON_GRANTED_IN_FORCE in proj.reasons
    assert proj.mutated_state is False


def test_withdrawal_after_grant_revokes_permission():
    events = [
        _event("r1", ACTION_GRANT, T0),
        _event("r2", ACTION_WITHDRAW, T0 + timedelta(days=5)),
    ]
    proj = derive_permission_state(events)
    assert proj.state is PermissionState.WITHDRAWN
    assert proj.granted is False
    assert proj.effective_event is not None and proj.effective_event.receipt_id == "r2"
    assert REASON_WITHDRAWN in proj.reasons
    assert REASON_LATE_GRANT_SUPERSEDED in proj.reasons  # the grant exists but was superseded
    assert "r1" in proj.superseded_event_ids


def test_delayed_old_grant_recorded_after_withdrawal_does_not_revive():
    """The acceptance criterion: an out-of-order (late-recorded) OLD grant cannot revive a withdrawal.

    The grant was COLLECTED before the withdrawal but RECORDED after it (recorded_at later). Ordering
    by the subject's action time keeps the withdrawal effective — permission stays revoked."""
    withdraw = _event(
        "r2", ACTION_WITHDRAW, T0 + timedelta(days=5), recorded_at=T0 + timedelta(days=5)
    )
    # arrives last, but it was collected on day 0 — an out-of-order replay of the original opt-in
    late_grant = _event("r1", ACTION_GRANT, T0, recorded_at=T0 + timedelta(days=9))
    proj = derive_permission_state([withdraw, late_grant])
    assert proj.state is PermissionState.WITHDRAWN
    assert proj.granted is False
    assert proj.effective_event is not None and proj.effective_event.receipt_id == "r2"
    assert REASON_LATE_GRANT_SUPERSEDED in proj.reasons
    # the late grant is preserved in the timeline, just not effective
    assert "r1" in proj.applied_event_ids
    assert "r1" in proj.superseded_event_ids


def test_duplicate_grant_after_withdrawal_does_not_revive():
    """A duplicate of the ORIGINAL grant (same action + same collected_at) replayed after a withdrawal
    collapses to the same state — it cannot re-grant."""
    events = [
        _event("r1", ACTION_GRANT, T0),
        _event("r2", ACTION_WITHDRAW, T0 + timedelta(days=5)),
        # a duplicate of r1: same action at the same instant, recorded much later
        _event("r1-dup", ACTION_GRANT, T0, recorded_at=T0 + timedelta(days=9)),
    ]
    proj = derive_permission_state(events)
    assert proj.state is PermissionState.WITHDRAWN
    assert proj.granted is False
    assert "r1-dup" in proj.duplicate_event_ids  # recognised as a duplicate, preserved in history
    assert REASON_DUPLICATE_IGNORED in proj.reasons
    assert REASON_LATE_GRANT_SUPERSEDED in proj.reasons


def test_revocation_wins_at_the_same_collected_at():
    """A grant and a withdrawal recorded at the SAME action instant: the revocation wins, so a
    same-instant replay can never re-grant."""
    same = T0 + timedelta(days=5)
    # order the positive first on input to prove the ordering, not the input order, decides it
    events = [
        _event("rg", ACTION_GRANT, same, recorded_at=same + timedelta(hours=1)),
        _event("rw", ACTION_WITHDRAW, same, recorded_at=same),
    ]
    proj = derive_permission_state(events)
    assert proj.state is PermissionState.WITHDRAWN
    assert proj.effective_event is not None and proj.effective_event.receipt_id == "rw"
    assert REASON_REVOCATION_WINS_TIE in proj.reasons


def test_reconfirmation_after_withdrawal_regrants_with_later_timestamp():
    """A genuine re-opt-in (a NEW event with a later collected_at) legitimately re-grants — the
    cannot-revive guard only blocks OUT-OF-ORDER stale events, not a real later action."""
    events = [
        _event("r1", ACTION_GRANT, T0),
        _event("r2", ACTION_WITHDRAW, T0 + timedelta(days=5)),
        _event("r3", ACTION_CONFIRM, T0 + timedelta(days=10)),
    ]
    proj = derive_permission_state(events)
    assert proj.state is PermissionState.GRANTED
    assert proj.granted is True
    assert proj.effective_event is not None and proj.effective_event.receipt_id == "r3"
    assert REASON_CONFIRMED_IN_FORCE in proj.reasons


def test_no_events_is_fail_closed_none():
    proj = derive_permission_state([])
    assert proj.state is PermissionState.NONE
    assert proj.granted is False
    assert proj.effective_event is None
    assert proj.reasons == (REASON_NO_EVENTS,)


def test_decline_is_distinct_from_withdraw():
    proj = derive_permission_state([_event("r1", ACTION_DECLINE, T0)])
    assert proj.state is PermissionState.DECLINED
    assert proj.granted is False
    assert REASON_DECLINED in proj.reasons


# --- channel / purpose scopes remain distinct -----------------------------------------------------


def test_channel_and_purpose_scopes_are_independent():
    """A grant for one (channel, purpose) does NOT satisfy another channel or another purpose, and a
    withdrawal in one scope leaves the others untouched."""
    events = [
        # email / marketing — granted then withdrawn
        _event("e1", ACTION_GRANT, T0, channel="email", purpose="marketing_email"),
        _event(
            "e2",
            ACTION_WITHDRAW,
            T0 + timedelta(days=2),
            channel="email",
            purpose="marketing_email",
        ),
        # whatsapp / marketing — granted, still in force (different channel)
        _event("w1", ACTION_GRANT, T0, channel="whatsapp", purpose="marketing_whatsapp"),
        # email / transactional — granted, still in force (different purpose)
        _event("t1", ACTION_GRANT, T0, channel="email", purpose="transactional"),
    ]
    states = project_permission_states(events)

    email_marketing = states[ScopeKey("acmenewsletter", CP, "email", "marketing_email")]
    whatsapp_marketing = states[ScopeKey("acmenewsletter", CP, "whatsapp", "marketing_whatsapp")]
    email_txn = states[ScopeKey("acmenewsletter", CP, "email", "transactional")]

    assert email_marketing.state is PermissionState.WITHDRAWN  # withdrawn in its own scope
    assert whatsapp_marketing.state is PermissionState.GRANTED  # different channel unaffected
    assert email_txn.state is PermissionState.GRANTED  # different purpose unaffected


def test_mixed_scopes_to_single_scope_derive_raises():
    events = [
        _event("a", ACTION_GRANT, T0, purpose="marketing_email"),
        _event("b", ACTION_GRANT, T0, purpose="transactional"),
    ]
    with pytest.raises(ValueError):
        derive_permission_state(events)


# --- reproducible & order-independent -------------------------------------------------------------


def test_result_is_independent_of_input_order_and_reproducible():
    base = [
        _event("r1", ACTION_GRANT, T0),
        _event("r2", ACTION_WITHDRAW, T0 + timedelta(days=5)),
        _event("r3", ACTION_GRANT, T0 + timedelta(days=1)),  # out of order on input
    ]
    forward = derive_permission_state(base)
    reversed_ = derive_permission_state(list(reversed(base)))
    again = derive_permission_state(base)
    assert forward.state is reversed_.state is again.state is PermissionState.WITHDRAWN
    assert forward.effective_event is not None and forward.effective_event.receipt_id == "r2"
    assert forward.applied_event_ids == reversed_.applied_event_ids == again.applied_event_ids


# --- read-only adapter over stored receipts (SQLite ok for the pure-logic adapter) -----------------


def _session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _seed_receipts_for_contact_point(session, business_id: str, cp_id: str):
    """Write a grant then a withdrawal directly as rows (the writer's own tests cover the write gate);
    here we only need durable rows to exercise the read-only adapter + projection."""
    from app.db.models import ConsentReceipt

    def _row(rid, action, collected, recorded):
        return ConsentReceipt(
            id=rid,
            business_id=business_id,
            brand_key="acmenewsletter",
            subject_type="person",
            subject_id="person-1",
            contact_point_id=cp_id,
            channel="email",
            purpose="marketing_email",
            action=action,
            notice_version="notice-2026.3",
            form_version="newsletter_optin_v3",
            language="en",
            evidence_id="ev-1",
            collected_at=collected,
            created_at=recorded,
        )

    session.add(_row("g1", ACTION_GRANT, T0, T0))
    session.add(_row("w1", ACTION_WITHDRAW, T0 + timedelta(days=5), T0 + timedelta(days=5)))
    # a delayed OLD grant: collected on day 0, recorded on day 9 (out of order)
    session.add(_row("g2", ACTION_GRANT, T0, T0 + timedelta(days=9)))
    session.commit()


def test_adapter_projects_stored_receipts_without_mutating_history():
    with _session_factory()() as session:
        _seed_receipts_for_contact_point(session, BIZ_A, CP)

        from app.db.models import ConsentReceipt

        rows = (
            session.query(ConsentReceipt)
            .filter(ConsentReceipt.business_id == BIZ_A)
            .order_by(ConsentReceipt.created_at)
            .all()
        )
        events = events_from_receipts(rows)
        proj = derive_permission_state(events)

        # out-of-order old grant does not revive the withdrawal
        assert proj.state is PermissionState.WITHDRAWN
        assert REASON_LATE_GRANT_SUPERSEDED in proj.reasons
        # the full history is preserved — projecting read nothing away
        assert (
            session.query(ConsentReceipt).filter(ConsentReceipt.business_id == BIZ_A).count() == 3
        )
        assert {"g1", "g2"}.issubset(set(proj.applied_event_ids) | set(proj.duplicate_event_ids))


# --- Postgres-backed: the real transaction / RLS path for the underlying table --------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")


@pytest.mark.skipif(not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres")
def test_projection_over_postgres_receipts():
    """Exercise the read-only adapter + projection over receipts stored in a disposable PostgreSQL with
    real transactions — never SQLite for the durable path. The out-of-order old grant must not revive a
    withdrawal, and the event history is unchanged."""
    engine = create_engine(PG_URL, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        factory = make_session_factory(engine)
        with factory() as session:
            _seed_receipts_for_contact_point(session, BIZ_A, CP)

        with factory() as session:
            from app.db.models import ConsentReceipt

            rows = (
                session.query(ConsentReceipt)
                .filter(ConsentReceipt.business_id == BIZ_A)
                .order_by(ConsentReceipt.created_at)
                .all()
            )
            proj = derive_permission_state(events_from_receipts(rows))
            assert proj.state is PermissionState.WITHDRAWN
            assert REASON_LATE_GRANT_SUPERSEDED in proj.reasons
            assert (
                session.query(ConsentReceipt).filter(ConsentReceipt.business_id == BIZ_A).count()
                == 3
            )
    finally:
        Base.metadata.drop_all(engine)
