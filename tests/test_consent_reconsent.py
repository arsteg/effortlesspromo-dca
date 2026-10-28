"""DCA-077 — require FRESH evidence for re-consent.

Pins the load-bearing behaviour of the re-consent gate (:mod:`app.consent.reconsent`) and the command
that applies it (:mod:`app.services.consent_reconsent`): after a withdrawal, a permission can be
restored ONLY by a new, valid, affirmative receipt citing FRESH evidence collected AFTER the
withdrawal. The three ways a withdrawn permission could be made to *look* re-granted without one are
each refused and change nothing (the DCA-077 acceptance criteria):

1. **Replayed original receipt** — re-presenting the original opt-in's captured evidence cannot restore
   consent (``reconsent_stale_evidence`` / ``reconsent_replayed_receipt``).
2. **Stale import** — a grant collected at/before the in-force withdrawal cannot restore consent
   (``reconsent_not_after_withdrawal``); the DCA-075 projection independently still reports WITHDRAWN.
3. **Provider refresh** — an assertion carrying no NEW captured evidence cannot restore consent
   (``reconsent_missing_evidence`` / ``reconsent_stale_evidence``).

The gate is pure (no DB) and tested directly. The command logic is unit-tested on in-memory SQLite; the
real transaction / RLS path is exercised on a disposable PostgreSQL (``DCA_TEST_DATABASE_URL``), never
relying on SQLite for the durable path.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine

from app.audit.sink import ActorType, AuditActor
from app.consent import (
    PermissionEvent,
    PermissionState,
    ScopeKey,
    derive_permission_state,
    events_from_receipts,
    evaluate_reconsent,
    project_after_reconsent,
)
from app.consent.reconsent import (
    REASON_MISSING_EVIDENCE,
    REASON_NOT_AFFIRMATIVE,
    REASON_NOT_AFTER_WITHDRAWAL,
    REASON_NOT_WITHDRAWN,
    REASON_RECONSENTED,
    REASON_REPLAYED_RECEIPT,
    REASON_STALE_EVIDENCE,
)
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import ConsentReceipt, OutboxMessage
from app.services.consent_receipt import ConsentReceiptInput, ConsentReceiptStore
from app.services.consent_reconsent import (
    OUTBOX_EVENT_SCOPE_RECONSENTED,
    ConsentReconsentError,
    ConsentReconsentService,
    ReconsentInput,
)
from app.services.consent_withdrawal import (
    AuthenticatedWithdrawal,
    ConsentWithdrawalService,
    ScopeRef,
)
from app.services.contact_point import ContactPointInput, ContactPointStore, PersonInput
from app.services.evidence_store import EvidenceInput, EvidenceObjectStore
from app.services.source_catalog import SourceCatalogStore, SourceDraftInput
from app.services.source_terms import SourceTermsInput, SourceTermsStore

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"
ACTOR = AuditActor(id="00000000-0000-4000-9000-0000000a0077", type=ActorType.USER)
GRANT_AT = datetime(2026, 10, 25, 14, 30, tzinfo=UTC)
WITHDRAW_AT = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
RECONSENT_AT = datetime(2026, 10, 27, 10, 0, tzinfo=UTC)
EMAIL = "priya@synthetic.example"
BRAND = "AcmeNewsletter"
PURPOSE = "marketing_email"


# ---------------------------------------------------------------------------------------------------
# the pure gate — no DB
# ---------------------------------------------------------------------------------------------------

SCOPE = ScopeKey("acmenewsletter", "cp-1", "email", "marketing_email")


def _ev(
    receipt_id: str,
    action: str,
    collected_at: datetime,
    *,
    evidence_id: str = "",
) -> PermissionEvent:
    return PermissionEvent(
        receipt_id=receipt_id,
        brand_key=SCOPE.brand_key,
        contact_point_id=SCOPE.contact_point_id,
        channel=SCOPE.channel,
        purpose=SCOPE.purpose,
        action=action,
        collected_at=collected_at,
        recorded_at=collected_at,
        evidence_id=evidence_id,
    )


def _withdrawn_history() -> list[PermissionEvent]:
    """A scope that is currently WITHDRAWN: granted (evidence ev-orig), then withdrawn."""
    return [
        _ev("g1", "grant", GRANT_AT, evidence_id="ev-orig"),
        _ev("w1", "withdraw", WITHDRAW_AT),
    ]


def test_valid_reconsent_requires_fresh_evidence_after_withdrawal():
    prior = _withdrawn_history()
    fresh = _ev("g2", "grant", RECONSENT_AT, evidence_id="ev-fresh")
    decision = evaluate_reconsent(prior, fresh)
    assert decision.restored is True
    assert decision.reason == REASON_RECONSENTED
    assert decision.prior_state is PermissionState.WITHDRAWN
    assert decision.resulting_state is PermissionState.GRANTED
    assert decision.mutated_state is False
    # the DCA-075 projection independently agrees the scope would be GRANTED
    assert project_after_reconsent(prior, fresh).state is PermissionState.GRANTED


def test_replayed_original_receipt_cannot_restore_consent():
    prior = _withdrawn_history()
    # Same captured evidence as the original grant, re-presented AFTER the withdrawal.
    replay = _ev("g-replay", "grant", RECONSENT_AT, evidence_id="ev-orig")
    decision = evaluate_reconsent(prior, replay)
    assert decision.restored is False
    assert decision.reason == REASON_STALE_EVIDENCE
    assert decision.resulting_state is PermissionState.WITHDRAWN


def test_literal_receipt_replay_is_refused():
    prior = _withdrawn_history()
    # The exact original grant row re-submitted (same receipt id).
    replay = _ev("g1", "grant", RECONSENT_AT, evidence_id="ev-another")
    decision = evaluate_reconsent(prior, replay)
    assert decision.restored is False
    assert decision.reason == REASON_REPLAYED_RECEIPT


def test_stale_import_before_withdrawal_cannot_restore_consent():
    prior = _withdrawn_history()
    # A fresh-looking evidence id, but collected BEFORE the withdrawal (a late-imported old grant).
    stale = _ev("g-stale", "grant", GRANT_AT - timedelta(days=1), evidence_id="ev-fresh")
    decision = evaluate_reconsent(prior, stale)
    assert decision.restored is False
    assert decision.reason == REASON_NOT_AFTER_WITHDRAWAL
    # DCA-075 ordering agrees: the older grant is superseded, the scope stays WITHDRAWN.
    assert project_after_reconsent(prior, stale).state is PermissionState.WITHDRAWN


def test_same_instant_replay_cannot_restore_consent():
    prior = _withdrawn_history()
    same_instant = _ev("g-same", "grant", WITHDRAW_AT, evidence_id="ev-fresh")
    decision = evaluate_reconsent(prior, same_instant)
    assert decision.restored is False
    assert decision.reason == REASON_NOT_AFTER_WITHDRAWAL
    assert project_after_reconsent(prior, same_instant).state is PermissionState.WITHDRAWN


def test_provider_refresh_without_evidence_cannot_restore_consent():
    prior = _withdrawn_history()
    # A provider re-assertion carrying NO captured evidence of a fresh opt-in.
    refresh = _ev("g-refresh", "grant", RECONSENT_AT, evidence_id="")
    decision = evaluate_reconsent(prior, refresh)
    assert decision.restored is False
    assert decision.reason == REASON_MISSING_EVIDENCE


def test_reconsent_requires_an_affirmative_action():
    prior = _withdrawn_history()
    negative = _ev("w2", "withdraw", RECONSENT_AT, evidence_id="ev-fresh")
    decision = evaluate_reconsent(prior, negative)
    assert decision.restored is False
    assert decision.reason == REASON_NOT_AFFIRMATIVE


def test_reconsent_only_applies_to_a_withdrawn_scope():
    # No prior withdrawal: a grant onto NONE is a first grant, not a re-consent.
    fresh = _ev("g1", "grant", RECONSENT_AT, evidence_id="ev-fresh")
    decision = evaluate_reconsent([], fresh)
    assert decision.restored is False
    assert decision.reason == REASON_NOT_WITHDRAWN


def test_gate_rejects_mixed_scopes():
    prior = _withdrawn_history()
    other = PermissionEvent(
        receipt_id="x",
        brand_key="acmenewsletter",
        contact_point_id="cp-2",  # different scope
        channel="email",
        purpose="marketing_email",
        action="grant",
        collected_at=RECONSENT_AT,
        evidence_id="ev-fresh",
    )
    with pytest.raises(ValueError):
        evaluate_reconsent(prior, other)


# ---------------------------------------------------------------------------------------------------
# the command — in-memory SQLite (logic) + disposable PostgreSQL (durable path)
# ---------------------------------------------------------------------------------------------------


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _seed_source(session, key: str = "optin_form") -> int:
    catalog = SourceCatalogStore(session)
    if catalog.get(key) is None:
        catalog.create_draft(
            SourceDraftInput(
                key=key,
                kind="official",
                access_method="api",
                name="Synthetic Opt-in Form",
                india_coverage={"nationwide": True},
            )
        )
        return (
            SourceTermsStore(session)
            .add_version(
                SourceTermsInput(
                    source_key=key,
                    operations={"collect": True, "store": True},
                    purposes=["prospecting"],
                    retention={"max_days": 3650, "policy": "retain"},
                    contract_evidence={
                        "reference": "LIC-1",
                        "kind": "licence",
                        "uri": "file://lic",
                    },
                )
            )
            .version
        )
    return 1


def _seed_contact(session, business_id: str, *, value: str = EMAIL):
    """Create a person + an email contact point. Returns (person_id, contact_point_id)."""
    cp_store = ContactPointStore(session, business_id)
    person = cp_store.create_person(PersonInput(display_name="Priya Synthetic"), actor=ACTOR)
    cp = cp_store.add_contact_point(
        ContactPointInput(
            person_observation_id=person.id, channel="email", value=value, evidence="self_reported"
        ),
        actor=ACTOR,
    )
    return person.id, cp.id


def _capture(session, business_id, person_id, *, ref: str, at: datetime) -> str:
    """Capture one evidence object about ``person_id`` and return its id."""
    ver = _seed_source(session)
    evidence = EvidenceObjectStore(session, business_id).store(
        EvidenceInput(
            source_key="optin_form",
            source_version=ver,
            content_kind="optin_record",
            content_ref=ref,
            raw_content='{"form": "newsletter_optin_v3", "subscribed": true}',
            captured_at=at,
            rights_basis="self_reported_optin",
            content_media_type="application/json",
            subject_type="person",
            subject_id=person_id,
        ),
        actor=ACTOR,
    )
    return evidence.id


def _grant(session, business_id, cp_id, ev_id, *, at=GRANT_AT):
    ConsentReceiptStore(session, business_id).record(
        ConsentReceiptInput(
            brand_key=BRAND,
            contact_point_id=cp_id,
            channel="email",
            purpose=PURPOSE,
            action="grant",
            notice_version="notice-2026.3",
            form_version="newsletter_optin_v3",
            language="en",
            evidence_id=ev_id,
            collected_at=at,
        ),
        actor=ACTOR,
    )


def _withdraw(session, business_id, cp_id, *, at=WITHDRAW_AT):
    ConsentWithdrawalService(session).withdraw_authenticated(
        AuthenticatedWithdrawal(business_id=business_id, actor_id=ACTOR.id, roles=("user",)),
        ScopeRef(brand_key=BRAND, contact_point_id=cp_id, channel="email", purpose=PURPOSE),
        now=at,
    )


def _reconsent_input(cp_id, ev_id, *, action="grant", at=RECONSENT_AT) -> ReconsentInput:
    return ReconsentInput(
        brand_key=BRAND,
        contact_point_id=cp_id,
        channel="email",
        purpose=PURPOSE,
        action=action,
        notice_version="notice-2026.4",
        form_version="newsletter_optin_v4",
        language="en",
        evidence_id=ev_id,
        collected_at=at,
    )


def _state(session, business_id, cp_id):
    rows = [
        r
        for r in ConsentReceiptStore(session, business_id).for_contact_point(cp_id)
        if r.channel == "email" and r.purpose == PURPOSE
    ]
    return derive_permission_state(events_from_receipts(rows)).state


def _row_count(session, business_id, cp_id) -> int:
    return len(ConsentReceiptStore(session, business_id).for_contact_point(cp_id))


def test_command_restores_with_fresh_evidence_and_emits_outbox(session_factory):
    with session_factory() as session:
        person, cp = _seed_contact(session, BIZ_A)
        ev_orig = _capture(session, BIZ_A, person, ref="https://s.example/f/1", at=GRANT_AT)
        _grant(session, BIZ_A, cp, ev_orig)
        _withdraw(session, BIZ_A, cp)
        session.commit()

    with session_factory() as session:
        assert _state(session, BIZ_A, cp) is PermissionState.WITHDRAWN
        # A genuinely fresh opt-in: a NEW evidence object captured after the withdrawal.
        ev_fresh = _capture(session, BIZ_A, person, ref="https://s.example/f/2", at=RECONSENT_AT)
        proj = ConsentReconsentService(session, BIZ_A).reconsent(
            _reconsent_input(cp, ev_fresh), actor=ACTOR, now=RECONSENT_AT
        )
        assert proj.state is PermissionState.GRANTED
        session.commit()

    with session_factory() as session:
        assert _state(session, BIZ_A, cp) is PermissionState.GRANTED
        # append-only: grant, withdraw, re-grant are all preserved
        actions = sorted(
            r.action for r in ConsentReceiptStore(session, BIZ_A).for_contact_point(cp)
        )
        assert actions == ["grant", "grant", "withdraw"]
        assert (
            session.query(OutboxMessage)
            .filter(OutboxMessage.event_type == OUTBOX_EVENT_SCOPE_RECONSENTED)
            .count()
            == 1
        )


@pytest.mark.parametrize("bad_action", ["withdraw", "decline"])
def test_command_rejects_non_affirmative_action(session_factory, bad_action):
    with session_factory() as session:
        person, cp = _seed_contact(session, BIZ_A)
        ev_orig = _capture(session, BIZ_A, person, ref="https://s.example/f/1", at=GRANT_AT)
        _grant(session, BIZ_A, cp, ev_orig)
        _withdraw(session, BIZ_A, cp)
        session.commit()

    with session_factory() as session:
        ev_fresh = _capture(session, BIZ_A, person, ref="https://s.example/f/2", at=RECONSENT_AT)
        before = _row_count(session, BIZ_A, cp)
        with pytest.raises(ConsentReconsentError) as exc:
            ConsentReconsentService(session, BIZ_A).reconsent(
                _reconsent_input(cp, ev_fresh, action=bad_action), actor=ACTOR, now=RECONSENT_AT
            )
        assert exc.value.code == REASON_NOT_AFFIRMATIVE
        session.rollback()

    with session_factory() as session:
        assert _state(session, BIZ_A, cp) is PermissionState.WITHDRAWN
        assert _row_count(session, BIZ_A, cp) == before


def test_command_refuses_replayed_original_receipt_and_stages_nothing(session_factory):
    with session_factory() as session:
        person, cp = _seed_contact(session, BIZ_A)
        ev_orig = _capture(session, BIZ_A, person, ref="https://s.example/f/1", at=GRANT_AT)
        _grant(session, BIZ_A, cp, ev_orig)
        _withdraw(session, BIZ_A, cp)
        session.commit()
        before = _row_count(session, BIZ_A, cp)

    with session_factory() as session:
        # Re-present the ORIGINAL opt-in evidence as if it were a fresh opt-in.
        with pytest.raises(ConsentReconsentError) as exc:
            ConsentReconsentService(session, BIZ_A).reconsent(
                _reconsent_input(cp, ev_orig), actor=ACTOR, now=RECONSENT_AT
            )
        assert exc.value.code == REASON_STALE_EVIDENCE
        session.rollback()

    with session_factory() as session:
        assert _state(session, BIZ_A, cp) is PermissionState.WITHDRAWN
        assert _row_count(session, BIZ_A, cp) == before
        assert (
            session.query(OutboxMessage)
            .filter(OutboxMessage.event_type == OUTBOX_EVENT_SCOPE_RECONSENTED)
            .count()
            == 0
        )


def test_command_refuses_stale_import_before_withdrawal(session_factory):
    with session_factory() as session:
        person, cp = _seed_contact(session, BIZ_A)
        ev_orig = _capture(session, BIZ_A, person, ref="https://s.example/f/1", at=GRANT_AT)
        _grant(session, BIZ_A, cp, ev_orig)
        _withdraw(session, BIZ_A, cp)
        session.commit()
        before = _row_count(session, BIZ_A, cp)

    with session_factory() as session:
        ev_fresh = _capture(session, BIZ_A, person, ref="https://s.example/f/2", at=GRANT_AT)
        # Fresh evidence id, but the opt-in was collected BEFORE the withdrawal (a stale import).
        with pytest.raises(ConsentReconsentError) as exc:
            ConsentReconsentService(session, BIZ_A).reconsent(
                _reconsent_input(cp, ev_fresh, at=GRANT_AT - timedelta(days=1)),
                actor=ACTOR,
                now=RECONSENT_AT,
            )
        assert exc.value.code == REASON_NOT_AFTER_WITHDRAWAL
        session.rollback()

    with session_factory() as session:
        assert _state(session, BIZ_A, cp) is PermissionState.WITHDRAWN
        assert _row_count(session, BIZ_A, cp) == before


def test_command_refuses_provider_refresh_without_evidence(session_factory):
    with session_factory() as session:
        person, cp = _seed_contact(session, BIZ_A)
        ev_orig = _capture(session, BIZ_A, person, ref="https://s.example/f/1", at=GRANT_AT)
        _grant(session, BIZ_A, cp, ev_orig)
        _withdraw(session, BIZ_A, cp)
        session.commit()
        before = _row_count(session, BIZ_A, cp)

    with session_factory() as session:
        with pytest.raises(ConsentReconsentError) as exc:
            ConsentReconsentService(session, BIZ_A).reconsent(
                _reconsent_input(cp, ""), actor=ACTOR, now=RECONSENT_AT
            )
        assert exc.value.code == REASON_MISSING_EVIDENCE
        session.rollback()

    with session_factory() as session:
        assert _state(session, BIZ_A, cp) is PermissionState.WITHDRAWN
        assert _row_count(session, BIZ_A, cp) == before


def test_command_refuses_reconsent_when_scope_not_withdrawn(session_factory):
    with session_factory() as session:
        person, cp = _seed_contact(session, BIZ_A)
        ev_orig = _capture(session, BIZ_A, person, ref="https://s.example/f/1", at=GRANT_AT)
        _grant(session, BIZ_A, cp, ev_orig)
        session.commit()  # granted, never withdrawn

    with session_factory() as session:
        ev_fresh = _capture(session, BIZ_A, person, ref="https://s.example/f/2", at=RECONSENT_AT)
        with pytest.raises(ConsentReconsentError) as exc:
            ConsentReconsentService(session, BIZ_A).reconsent(
                _reconsent_input(cp, ev_fresh), actor=ACTOR, now=RECONSENT_AT
            )
        assert exc.value.code == REASON_NOT_WITHDRAWN
        session.rollback()


def test_command_cannot_reconsent_another_tenants_contact_point(session_factory):
    with session_factory() as session:
        person_a, cp_a = _seed_contact(session, BIZ_A)
        ev_a = _capture(session, BIZ_A, person_a, ref="https://s.example/f/1", at=GRANT_AT)
        _grant(session, BIZ_A, cp_a, ev_a)
        _withdraw(session, BIZ_A, cp_a)
        session.commit()

    with session_factory() as session:
        # Business B tries to re-consent A's contact point — it does not exist in B (not-found). The
        # gate refuses before any evidence/row is touched, so a placeholder evidence id suffices.
        with pytest.raises(ConsentReconsentError) as exc:
            ConsentReconsentService(session, BIZ_B).reconsent(
                _reconsent_input(cp_a, "ev-b-placeholder"), actor=ACTOR, now=RECONSENT_AT
            )
        # B sees no withdrawn history for a contact point it doesn't own → nothing to re-consent.
        assert exc.value.code == REASON_NOT_WITHDRAWN
        session.rollback()

    with session_factory() as session:
        assert _state(session, BIZ_A, cp_a) is PermissionState.WITHDRAWN


# --- Postgres-backed: the real transaction / RLS path for the durable write ------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")


@pytest.mark.skipif(not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres")
def test_reconsent_over_postgres_requires_fresh_evidence():
    """Exercise the real receipt+outbox write path on a disposable PostgreSQL — never SQLite for the
    durable path. A replayed original receipt is refused and stages nothing; a fresh affirmative
    receipt collected after the withdrawal restores the scope to GRANTED."""
    engine = create_engine(PG_URL, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        factory = make_session_factory(engine)
        with factory() as session:
            person, cp = _seed_contact(session, BIZ_A)
            ev_orig = _capture(session, BIZ_A, person, ref="https://s.example/f/1", at=GRANT_AT)
            _grant(session, BIZ_A, cp, ev_orig)
            _withdraw(session, BIZ_A, cp)
            session.commit()

        # replayed original receipt → refused, nothing staged
        with factory() as session:
            with pytest.raises(ConsentReconsentError) as exc:
                ConsentReconsentService(session, BIZ_A).reconsent(
                    _reconsent_input(cp, ev_orig), actor=ACTOR, now=RECONSENT_AT
                )
            assert exc.value.code == REASON_STALE_EVIDENCE
            session.rollback()

        with factory() as session:
            assert _state(session, BIZ_A, cp) is PermissionState.WITHDRAWN

        # fresh evidence after the withdrawal → restored
        with factory() as session:
            ev_fresh = _capture(
                session, BIZ_A, person, ref="https://s.example/f/2", at=RECONSENT_AT
            )
            proj = ConsentReconsentService(session, BIZ_A).reconsent(
                _reconsent_input(cp, ev_fresh), actor=ACTOR, now=RECONSENT_AT
            )
            assert proj.state is PermissionState.GRANTED
            session.commit()

        with factory() as session:
            assert _state(session, BIZ_A, cp) is PermissionState.GRANTED
            assert (
                session.query(OutboxMessage)
                .filter(OutboxMessage.event_type == OUTBOX_EVENT_SCOPE_RECONSENTED)
                .count()
                == 1
            )
            assert (
                session.query(ConsentReceipt)
                .filter(ConsentReceipt.business_id == BIZ_A)
                .filter(ConsentReceipt.contact_point_id == cp)
                .count()
                == 3
            )
    finally:
        Base.metadata.drop_all(engine)
