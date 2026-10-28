"""DCA-076 — withdraw ONE permission scope.

Pins the load-bearing behaviour of the withdrawal command (:mod:`app.services.consent_withdrawal`) and
its signed public token (:mod:`app.consent.withdrawal_token`):

1. **A withdrawal restricts exactly ONE scope, immediately.** An authenticated withdrawal (or a valid
   public token) appends a WITHDRAW receipt and the scope projects WITHDRAWN on the very next read; the
   prior grant is preserved (append-only) and an outbox event is emitted atomically.
2. **It never erases another tenant's — or another scope's — independent permission.** Withdrawing
   Business A's email scope leaves A's SMS scope GRANTED and leaves Business B's independent permission
   for the SAME email value GRANTED. The command is confined to one business + one contact point.
3. **The relevant denials change nothing.** An unauthorized actor, a disabled public path, and an
   invalid / forged / expired / scope-mismatched token are each refused BEFORE the session is mutated,
   so no receipt and no outbox event are staged.

The command logic is unit-tested on in-memory SQLite; the real transaction / RLS path is exercised on a
disposable PostgreSQL (``DCA_TEST_DATABASE_URL``), never relying on SQLite for the durable path. The
token module is pure (no DB) and tested directly.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine

from app.audit.sink import ActorType, AuditActor
from app.consent import PermissionState, issue_withdrawal_token, verify_withdrawal_token
from app.consent.withdrawal_token import (
    REASON_BAD_SIGNATURE,
    REASON_EXPIRED,
    REASON_INVALID_FORMAT,
    REASON_NO_SECRET,
    REASON_UNSUPPORTED_VERSION,
    WithdrawalTokenError,
)
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import ConsentReceipt, OutboxMessage, ProspectCandidate
from app.services.consent_receipt import (
    REASON_CHANNEL_MISMATCH,
    REASON_CONTACT_POINT_NOT_FOUND,
    ConsentReceiptInput,
    ConsentReceiptStore,
)
from app.services.consent_withdrawal import (
    OUTBOX_EVENT_SCOPE_WITHDRAWN,
    REASON_NOT_AUTHORIZED,
    REASON_PUBLIC_WITHDRAWAL_DISABLED,
    AuthenticatedWithdrawal,
    ConsentWithdrawalAuthorizationError,
    ConsentWithdrawalError,
    ConsentWithdrawalService,
    PublicTokenWithdrawal,
    ScopeRef,
)
from app.services.contact_point import ContactPointInput, ContactPointStore, PersonInput
from app.services.evidence_store import EvidenceInput, EvidenceObjectStore
from app.services.source_catalog import SourceCatalogStore, SourceDraftInput
from app.services.source_terms import SourceTermsInput, SourceTermsStore

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"
ACTOR = AuditActor(id="00000000-0000-4000-9000-0000000a0076", type=ActorType.USER)
GRANT_AT = datetime(2026, 10, 25, 14, 30, tzinfo=UTC)
WITHDRAW_AT = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
EMAIL = "priya@synthetic.example"
MOBILE = "+919812345670"
FORM_RAW = '{"form": "newsletter_optin_v3", "subscribed": true, "ts": "2026-10-25T14:30:00Z"}'
SECRET = "unit-test-withdrawal-secret"


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


def _seed_contact(session, business_id: str, *, channel: str, value: str):
    """Create a person + a contact point on ``channel`` and capture one evidence object about them.
    Returns (person_id, contact_point_id, evidence_id)."""
    cp_store = ContactPointStore(session, business_id)
    person = cp_store.create_person(PersonInput(display_name="Priya Synthetic"), actor=ACTOR)
    cp = cp_store.add_contact_point(
        ContactPointInput(
            person_observation_id=person.id, channel=channel, value=value, evidence="self_reported"
        ),
        actor=ACTOR,
    )
    ver = _seed_source(session)
    evidence = EvidenceObjectStore(session, business_id).store(
        EvidenceInput(
            source_key="optin_form",
            source_version=ver,
            content_kind="optin_record",
            content_ref="https://synthetic.example/forms/newsletter/submission/42",
            raw_content=FORM_RAW,
            captured_at=GRANT_AT,
            rights_basis="self_reported_optin",
            content_media_type="application/json",
            subject_type="person",
            subject_id=person.id,
        ),
        actor=ACTOR,
    )
    return person.id, cp.id, evidence.id


def _grant(session, business_id, cp_id, ev_id, *, channel="email", purpose="marketing_email"):
    ConsentReceiptStore(session, business_id).record(
        ConsentReceiptInput(
            brand_key="AcmeNewsletter",
            contact_point_id=cp_id,
            channel=channel,
            purpose=purpose,
            action="grant",
            notice_version="notice-2026.3",
            form_version="newsletter_optin_v3",
            language="en",
            evidence_id=ev_id,
            collected_at=GRANT_AT,
        ),
        actor=ACTOR,
    )


def _auth(business_id: str, roles=("user",)) -> AuthenticatedWithdrawal:
    return AuthenticatedWithdrawal(business_id=business_id, actor_id=ACTOR.id, roles=tuple(roles))


def _email_scope(cp_id: str) -> ScopeRef:
    return ScopeRef(
        brand_key="AcmeNewsletter",
        contact_point_id=cp_id,
        channel="email",
        purpose="marketing_email",
    )


def _state(session, business_id, cp_id, *, channel="email", purpose="marketing_email"):
    from app.consent import derive_permission_state, events_from_receipts

    rows = [
        r
        for r in ConsentReceiptStore(session, business_id).for_contact_point(cp_id)
        if r.channel == channel and r.purpose == purpose
    ]
    return derive_permission_state(events_from_receipts(rows)).state


# ---------------------------------------------------------------------------------------------------
# the pure public-token module
# ---------------------------------------------------------------------------------------------------


def test_token_roundtrip_binds_exactly_one_scope():
    iat = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
    token = issue_withdrawal_token(
        SECRET,
        business_id=BIZ_A,
        contact_point_id="cp-1",
        channel="Email",
        purpose="Marketing_Email",
        brand_key="AcmeNewsletter",
        issued_at=iat,
        expires_at=iat + timedelta(days=30),
    )
    claims = verify_withdrawal_token(SECRET, token, now=iat + timedelta(days=1))
    assert claims.business_id == BIZ_A
    assert claims.contact_point_id == "cp-1"
    assert claims.channel == "email"  # normalized
    assert claims.purpose == "marketing_email"
    assert claims.brand_key == "acmenewsletter"


def test_token_is_deterministic():
    iat = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
    kw = dict(
        business_id=BIZ_A,
        contact_point_id="cp-1",
        channel="email",
        purpose="marketing_email",
        brand_key="b",
        issued_at=iat,
        expires_at=iat + timedelta(days=30),
    )
    assert issue_withdrawal_token(SECRET, **kw) == issue_withdrawal_token(SECRET, **kw)


def test_token_tampered_scope_fails_signature():
    iat = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
    token = issue_withdrawal_token(
        SECRET,
        business_id=BIZ_A,
        contact_point_id="cp-1",
        channel="email",
        purpose="marketing_email",
        brand_key="b",
        issued_at=iat,
        expires_at=iat + timedelta(days=30),
    )
    version, payload_b64, sig = token.split(".")
    # Re-sign with a DIFFERENT business id under a different secret, keep A's signature → mismatch.
    forged_payload = issue_withdrawal_token(
        "attacker-secret",
        business_id=BIZ_B,
        contact_point_id="cp-1",
        channel="email",
        purpose="marketing_email",
        brand_key="b",
        issued_at=iat,
        expires_at=iat + timedelta(days=30),
    ).split(".")[1]
    forged = f"{version}.{forged_payload}.{sig}"
    with pytest.raises(WithdrawalTokenError) as exc:
        verify_withdrawal_token(SECRET, forged, now=iat + timedelta(days=1))
    assert exc.value.code == REASON_BAD_SIGNATURE


@pytest.mark.parametrize(
    ("token", "secret", "expected"),
    [
        ("not-a-token", SECRET, REASON_INVALID_FORMAT),
        ("a.b.c", SECRET, REASON_UNSUPPORTED_VERSION),
        ("dcaw1.b.c", SECRET, REASON_BAD_SIGNATURE),
        ("", SECRET, REASON_INVALID_FORMAT),
    ],
)
def test_token_malformed_is_refused(token, secret, expected):
    with pytest.raises(WithdrawalTokenError) as exc:
        verify_withdrawal_token(secret, token)
    assert exc.value.code == expected


def test_token_wrong_version_refused():
    iat = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
    token = issue_withdrawal_token(
        SECRET,
        business_id=BIZ_A,
        contact_point_id="cp-1",
        channel="email",
        purpose="marketing_email",
        brand_key="b",
        issued_at=iat,
        expires_at=iat + timedelta(days=30),
    )
    _version, payload_b64, sig = token.split(".")
    with pytest.raises(WithdrawalTokenError) as exc:
        verify_withdrawal_token(SECRET, f"dcaw9.{payload_b64}.{sig}", now=iat)
    assert exc.value.code == REASON_UNSUPPORTED_VERSION


def test_token_expired_refused():
    iat = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
    token = issue_withdrawal_token(
        SECRET,
        business_id=BIZ_A,
        contact_point_id="cp-1",
        channel="email",
        purpose="marketing_email",
        brand_key="b",
        issued_at=iat,
        expires_at=iat + timedelta(hours=1),
    )
    with pytest.raises(WithdrawalTokenError) as exc:
        verify_withdrawal_token(SECRET, token, now=iat + timedelta(hours=2))
    assert exc.value.code == REASON_EXPIRED


def test_token_no_secret_refused():
    with pytest.raises(WithdrawalTokenError) as exc:
        verify_withdrawal_token("", "dcaw1.x.y")
    assert exc.value.code == REASON_NO_SECRET


# ---------------------------------------------------------------------------------------------------
# the authenticated withdrawal command
# ---------------------------------------------------------------------------------------------------


def test_authenticated_withdrawal_restricts_scope_immediately_and_emits_outbox(session_factory):
    with session_factory() as session:
        _p, cp_id, ev_id = _seed_contact(session, BIZ_A, channel="email", value=EMAIL)
        _grant(session, BIZ_A, cp_id, ev_id)
        session.commit()

    with session_factory() as session:
        assert _state(session, BIZ_A, cp_id) == PermissionState.GRANTED  # granted before withdrawal
        proj = ConsentWithdrawalService(session).withdraw_authenticated(
            _auth(BIZ_A), _email_scope(cp_id), now=WITHDRAW_AT
        )
        # immediate restriction — the returned projection already reflects WITHDRAWN
        assert proj.state == PermissionState.WITHDRAWN
        session.commit()

    with session_factory() as session:
        # append-only: the grant is preserved and a new withdraw row was added (order is by recorded
        # time, which is not asserted here — correctness is the derived state, below).
        timeline = ConsentReceiptStore(session, BIZ_A).for_contact_point(cp_id)
        assert sorted(r.action for r in timeline) == ["grant", "withdraw"]
        assert _state(session, BIZ_A, cp_id) == PermissionState.WITHDRAWN
        # exactly one outbox event, carrying opaque references only (no raw contact value)
        events = (
            session.query(OutboxMessage)
            .filter(OutboxMessage.event_type == OUTBOX_EVENT_SCOPE_WITHDRAWN)
            .all()
        )
        assert len(events) == 1
        assert EMAIL not in repr(events[0].payload)
        assert events[0].payload["contactPointId"] == cp_id
        # no prospect/host record created
        assert session.query(ProspectCandidate).count() == 0


def test_withdrawal_does_not_erase_other_scope_or_other_tenant(session_factory):
    """THE acceptance criterion: A/email withdrawal leaves A/SMS and B/email independently GRANTED."""
    # Business A: email + SMS permissions on one person.
    with session_factory() as session:
        cp_store = ContactPointStore(session, BIZ_A)
        person = cp_store.create_person(PersonInput(display_name="Priya"), actor=ACTOR)
        email_cp = cp_store.add_contact_point(
            ContactPointInput(
                person_observation_id=person.id,
                channel="email",
                value=EMAIL,
                evidence="self_reported",
            ),
            actor=ACTOR,
        )
        sms_cp = cp_store.add_contact_point(
            ContactPointInput(
                person_observation_id=person.id,
                channel="mobile",
                value=MOBILE,
                evidence="self_reported",
            ),
            actor=ACTOR,
        )
        ver = _seed_source(session)
        ev = EvidenceObjectStore(session, BIZ_A).store(
            EvidenceInput(
                source_key="optin_form",
                source_version=ver,
                content_kind="optin_record",
                content_ref="https://synthetic.example/f/1",
                raw_content=FORM_RAW,
                captured_at=GRANT_AT,
                rights_basis="x",
                subject_type="person",
                subject_id=person.id,
            ),
            actor=ACTOR,
        )
        _grant(session, BIZ_A, email_cp.id, ev.id, channel="email", purpose="marketing_email")
        _grant(session, BIZ_A, sms_cp.id, ev.id, channel="mobile", purpose="marketing_sms")
        session.commit()
        a_email_cp, a_sms_cp = email_cp.id, sms_cp.id

    # Business B: its OWN independent email permission for the SAME email value.
    with session_factory() as session:
        _bp, b_email_cp, b_ev = _seed_contact(session, BIZ_B, channel="email", value=EMAIL)
        _grant(session, BIZ_B, b_email_cp, b_ev)
        session.commit()

    # Withdraw ONLY A/email.
    with session_factory() as session:
        ConsentWithdrawalService(session).withdraw_authenticated(
            _auth(BIZ_A), _email_scope(a_email_cp), now=WITHDRAW_AT
        )
        session.commit()

    # Inspect A/email, A/SMS, B/email.
    with session_factory() as session:
        assert _state(session, BIZ_A, a_email_cp, channel="email", purpose="marketing_email") == (
            PermissionState.WITHDRAWN
        )
        assert _state(session, BIZ_A, a_sms_cp, channel="mobile", purpose="marketing_sms") == (
            PermissionState.GRANTED  # A's SMS scope is untouched
        )
        assert _state(session, BIZ_B, b_email_cp, channel="email", purpose="marketing_email") == (
            PermissionState.GRANTED  # B's independent permission for the same email is untouched
        )
        # the withdrawal is confined to A: B has no withdraw row, and exactly one outbox event exists
        assert (
            session.query(ConsentReceipt)
            .filter(ConsentReceipt.business_id == BIZ_B)
            .filter(ConsentReceipt.action == "withdraw")
            .count()
            == 0
        )
        assert (
            session.query(OutboxMessage)
            .filter(OutboxMessage.event_type == OUTBOX_EVENT_SCOPE_WITHDRAWN)
            .count()
            == 1
        )


@pytest.mark.parametrize("roles", [("viewer",), (), ("platform_operator",)])
def test_unauthorized_actor_is_refused_and_stages_nothing(session_factory, roles):
    with session_factory() as session:
        _p, cp_id, ev_id = _seed_contact(session, BIZ_A, channel="email", value=EMAIL)
        _grant(session, BIZ_A, cp_id, ev_id)
        session.commit()

    with session_factory() as session:
        with pytest.raises(ConsentWithdrawalAuthorizationError) as exc:
            ConsentWithdrawalService(session).withdraw_authenticated(
                _auth(BIZ_A, roles=roles), _email_scope(cp_id), now=WITHDRAW_AT
            )
        assert exc.value.code == REASON_NOT_AUTHORIZED
        session.commit()
        # the denial changed nothing: the grant stands, no withdraw row, no outbox event
        assert _state(session, BIZ_A, cp_id) == PermissionState.GRANTED
        assert (
            session.query(ConsentReceipt).filter(ConsentReceipt.action == "withdraw").count() == 0
        )
        assert session.query(OutboxMessage).count() == 0


def test_contact_point_in_another_business_reads_as_not_found(session_factory):
    with session_factory() as session:
        _bp, b_cp_id, _bev = _seed_contact(session, BIZ_B, channel="email", value=EMAIL)
        session.commit()

    with session_factory() as session:
        # An authorized A user tries to withdraw using B's contact-point id → not-found, nothing staged.
        with pytest.raises(ConsentWithdrawalError) as exc:
            ConsentWithdrawalService(session).withdraw_authenticated(
                _auth(BIZ_A), _email_scope(b_cp_id), now=WITHDRAW_AT
            )
        assert exc.value.code == REASON_CONTACT_POINT_NOT_FOUND
        session.commit()
        assert (
            session.query(ConsentReceipt).filter(ConsentReceipt.action == "withdraw").count() == 0
        )
        assert session.query(OutboxMessage).count() == 0


def test_channel_mismatch_is_refused(session_factory):
    with session_factory() as session:
        _p, cp_id, ev_id = _seed_contact(session, BIZ_A, channel="email", value=EMAIL)
        _grant(session, BIZ_A, cp_id, ev_id)
        session.commit()

    with session_factory() as session:
        bad_scope = ScopeRef(
            brand_key="AcmeNewsletter",
            contact_point_id=cp_id,
            channel="mobile",  # the contact point is an email
            purpose="marketing_email",
        )
        with pytest.raises(ConsentWithdrawalError) as exc:
            ConsentWithdrawalService(session).withdraw_authenticated(
                _auth(BIZ_A), bad_scope, now=WITHDRAW_AT
            )
        assert exc.value.code == REASON_CHANNEL_MISMATCH
        session.commit()
        assert session.query(OutboxMessage).count() == 0


# ---------------------------------------------------------------------------------------------------
# the public-token withdrawal command
# ---------------------------------------------------------------------------------------------------


def test_public_token_withdraws_exactly_its_bound_scope(session_factory):
    with session_factory() as session:
        _p, cp_id, ev_id = _seed_contact(session, BIZ_A, channel="email", value=EMAIL)
        _grant(session, BIZ_A, cp_id, ev_id)
        session.commit()

    token = issue_withdrawal_token(
        SECRET,
        business_id=BIZ_A,
        contact_point_id=cp_id,
        channel="email",
        purpose="marketing_email",
        brand_key="AcmeNewsletter",
        issued_at=WITHDRAW_AT,
        expires_at=WITHDRAW_AT + timedelta(days=30),
    )
    with session_factory() as session:
        proj = ConsentWithdrawalService(session, token_secret=SECRET).withdraw_with_public_token(
            PublicTokenWithdrawal(token=token), now=WITHDRAW_AT + timedelta(days=1)
        )
        assert proj.state == PermissionState.WITHDRAWN
        session.commit()

    with session_factory() as session:
        assert _state(session, BIZ_A, cp_id) == PermissionState.WITHDRAWN
        events = (
            session.query(OutboxMessage)
            .filter(OutboxMessage.event_type == OUTBOX_EVENT_SCOPE_WITHDRAWN)
            .all()
        )
        assert len(events) == 1
        assert events[0].payload["withdrawnVia"] == "public_token"


def test_public_token_path_disabled_without_secret(session_factory):
    with session_factory() as session:
        _p, cp_id, ev_id = _seed_contact(session, BIZ_A, channel="email", value=EMAIL)
        _grant(session, BIZ_A, cp_id, ev_id)
        session.commit()

    token = issue_withdrawal_token(
        SECRET,
        business_id=BIZ_A,
        contact_point_id=cp_id,
        channel="email",
        purpose="marketing_email",
        brand_key="AcmeNewsletter",
        issued_at=WITHDRAW_AT,
        expires_at=WITHDRAW_AT + timedelta(days=30),
    )
    with session_factory() as session:
        # service built with NO secret → public path fails closed, nothing staged
        with pytest.raises(ConsentWithdrawalAuthorizationError) as exc:
            ConsentWithdrawalService(session).withdraw_with_public_token(
                PublicTokenWithdrawal(token=token), now=WITHDRAW_AT
            )
        assert exc.value.code == REASON_PUBLIC_WITHDRAWAL_DISABLED
        session.commit()
        assert _state(session, BIZ_A, cp_id) == PermissionState.GRANTED
        assert session.query(OutboxMessage).count() == 0


def test_forged_public_token_is_refused_and_stages_nothing(session_factory):
    with session_factory() as session:
        _p, cp_id, ev_id = _seed_contact(session, BIZ_A, channel="email", value=EMAIL)
        _grant(session, BIZ_A, cp_id, ev_id)
        session.commit()

    # Token minted under a DIFFERENT secret than the service trusts → bad signature.
    forged = issue_withdrawal_token(
        "attacker-secret",
        business_id=BIZ_A,
        contact_point_id=cp_id,
        channel="email",
        purpose="marketing_email",
        brand_key="AcmeNewsletter",
        issued_at=WITHDRAW_AT,
        expires_at=WITHDRAW_AT + timedelta(days=30),
    )
    with session_factory() as session:
        with pytest.raises(ConsentWithdrawalAuthorizationError) as exc:
            ConsentWithdrawalService(session, token_secret=SECRET).withdraw_with_public_token(
                PublicTokenWithdrawal(token=forged), now=WITHDRAW_AT
            )
        assert exc.value.code == REASON_BAD_SIGNATURE
        session.commit()
        assert _state(session, BIZ_A, cp_id) == PermissionState.GRANTED
        assert session.query(OutboxMessage).count() == 0


# ---------------------------------------------------------------------------------------------------
# the real transaction / RLS path on disposable Postgres
# ---------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")


@pytest.mark.skipif(not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres")
def test_withdrawal_over_postgres_is_tenant_isolated():
    """Exercise the real receipt+outbox write path on a disposable PostgreSQL — never SQLite for the
    durable path. A/email withdrawal restricts A/email immediately and leaves B/email GRANTED."""
    engine = create_engine(PG_URL, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        factory = make_session_factory(engine)
        with factory() as session:
            _ap, a_cp, a_ev = _seed_contact(session, BIZ_A, channel="email", value=EMAIL)
            _grant(session, BIZ_A, a_cp, a_ev)
            _bp, b_cp, b_ev = _seed_contact(session, BIZ_B, channel="email", value=EMAIL)
            _grant(session, BIZ_B, b_cp, b_ev)
            session.commit()

        with factory() as session:
            proj = ConsentWithdrawalService(session).withdraw_authenticated(
                _auth(BIZ_A), _email_scope(a_cp), now=WITHDRAW_AT
            )
            assert proj.state == PermissionState.WITHDRAWN
            session.commit()

        with factory() as session:
            assert _state(session, BIZ_A, a_cp) == PermissionState.WITHDRAWN
            assert _state(session, BIZ_B, b_cp) == PermissionState.GRANTED
            assert (
                session.query(OutboxMessage)
                .filter(OutboxMessage.event_type == OUTBOX_EVENT_SCOPE_WITHDRAWN)
                .count()
                == 1
            )
    finally:
        Base.metadata.drop_all(engine)
