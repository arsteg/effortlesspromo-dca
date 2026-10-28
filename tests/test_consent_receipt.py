"""DCA-074 — persist SCOPED, EVIDENCED permission (consent) receipts.

Pins the load-bearing behaviour of :mod:`app.services.consent_receipt` — the single sanctioned writer
of :class:`~app.db.models.ConsentReceipt`:

1. **An imported "subscribed" flag without evidence does not create a valid receipt.** A permission is
   a scoped receipt that must cite captured evidence (a same-business :class:`EvidenceObject`, DCA-062)
   plus the full scope — brand, contact point, channel, purpose, notice/form version, language, action
   and the collection time. A flag-only import (no evidence, and typically no scope) is refused and
   stages NO row; permissions are never stored as a global contact attribute.
2. **Another tenant cannot read a receipt by id.** Reads/writes are confined to one business; a
   cross-tenant id reads as not-found, and the same contact value in another business has no receipt.
3. **A denial changes nothing + no host record.** Every refusal stages no row; each successful record
   stages an atomic DCA-026 audit entry whose references never carry the raw contact value; and no
   prospect/host record is created. Append-only: a later withdrawal is a new row.

The store logic is unit-tested on in-memory SQLite; the real transaction / RLS path is exercised on a
disposable PostgreSQL (``DCA_TEST_DATABASE_URL``), never relying on SQLite for that. The Postgres test
skips when no URL is set. The RLS/migration paths for the table are covered by ``tests/test_rls.py``
and ``tests/test_migrations.py``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine

from app.audit.sink import ActorType, AuditActor
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import AuditEvent, ConsentReceipt, ProspectCandidate
from app.services.consent_receipt import (
    AUDIT_ACTION_RECORD_RECEIPT,
    REASON_CHANNEL_MISMATCH,
    REASON_CONTACT_POINT_NOT_FOUND,
    REASON_EVIDENCE_NOT_FOUND,
    REASON_EVIDENCE_SUBJECT_MISMATCH,
    REASON_MISSING_BRAND,
    REASON_MISSING_CONTACT_POINT,
    REASON_MISSING_EVIDENCE,
    REASON_MISSING_FORM_VERSION,
    REASON_MISSING_LANGUAGE,
    REASON_MISSING_NOTICE_VERSION,
    REASON_MISSING_PURPOSE,
    REASON_UNKNOWN_ACTION,
    ConsentReceiptError,
    ConsentReceiptInput,
    ConsentReceiptStore,
)
from app.services.contact_point import (
    ContactPointInput,
    ContactPointStore,
    PersonInput,
)
from app.services.evidence_store import EvidenceInput, EvidenceObjectStore
from app.services.source_catalog import SourceCatalogStore, SourceDraftInput
from app.services.source_terms import SourceTermsInput, SourceTermsStore

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"
ACTOR = AuditActor(id="00000000-0000-4000-9000-0000000a0074", type=ActorType.USER)
COLLECTED_AT = datetime(2026, 10, 25, 14, 30, tzinfo=UTC)
CAPTURED_AT = datetime(2026, 10, 25, 14, 30, tzinfo=UTC)
EMAIL = "priya@synthetic.example"
FORM_RAW = '{"form": "newsletter_optin_v3", "subscribed": true, "ts": "2026-10-25T14:30:00Z"}'


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _seed_evidence_source(session, key: str = "optin_form") -> int:
    """Catalogue a source + one terms version permitting store (so captured form proof is retained).

    Idempotent: the source catalog is platform control-plane (not tenant-scoped), so when the same key
    already exists (e.g. seeded for Business A then reused for Business B over one DB) it is reused."""
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


def _seed_person_and_evidence(session, business_id: str, *, email: str = EMAIL):
    """Record a person + an email contact point, and capture one evidence object (the form proof)
    linked to that person. Returns (person_id, contact_point_id, evidence_id)."""
    cp_store = ContactPointStore(session, business_id)
    person = cp_store.create_person(PersonInput(display_name="Priya Synthetic"), actor=ACTOR)
    cp = cp_store.add_contact_point(
        ContactPointInput(
            person_observation_id=person.id,
            channel="email",
            value=email,
            evidence="self_reported",
        ),
        actor=ACTOR,
    )
    ver = _seed_evidence_source(session)
    evidence = EvidenceObjectStore(session, business_id).store(
        EvidenceInput(
            source_key="optin_form",
            source_version=ver,
            content_kind="optin_record",
            content_ref="https://synthetic.example/forms/newsletter/submission/42",
            raw_content=FORM_RAW,
            captured_at=CAPTURED_AT,
            rights_basis="self_reported_optin;purpose=marketing_email",
            content_media_type="application/json",
            subject_type="person",
            subject_id=person.id,
        ),
        actor=ACTOR,
    )
    return person.id, cp.id, evidence.id


def _receipt_input(contact_point_id: str, evidence_id: str, **over) -> ConsentReceiptInput:
    base = dict(
        brand_key="AcmeNewsletter",
        contact_point_id=contact_point_id,
        channel="email",
        purpose="marketing_email",
        action="grant",
        notice_version="notice-2026.3",
        form_version="newsletter_optin_v3",
        language="en",
        evidence_id=evidence_id,
        collected_at=COLLECTED_AT,
    )
    base.update(over)
    return ConsentReceiptInput(**base)  # type: ignore[arg-type]


# --- the headline: a complete receipt is stored; a flag-only import is rejected -------------------


def test_complete_receipt_is_stored_with_full_scope(session_factory):
    with session_factory() as session:
        _person, cp_id, ev_id = _seed_person_and_evidence(session, BIZ_A)
        receipt = ConsentReceiptStore(session, BIZ_A).record(
            _receipt_input(cp_id, ev_id), actor=ACTOR
        )
        session.commit()
        rid = receipt.id

    with session_factory() as session:
        row = ConsentReceiptStore(session, BIZ_A).get(rid)
        assert row is not None
        # The permission is a SCOPED receipt — every scoping dimension is persisted.
        assert row.brand_key == "acmenewsletter"
        assert row.subject_type == "person"
        assert row.contact_point_id == cp_id
        assert row.channel == "email"
        assert row.purpose == "marketing_email"
        assert row.action == "grant"
        assert row.notice_version == "notice-2026.3"
        assert row.form_version == "newsletter_optin_v3"
        assert row.language == "en"
        assert row.evidence_id == ev_id
        assert row.collected_at.replace(tzinfo=None) == COLLECTED_AT.replace(tzinfo=None)


def test_flag_only_import_without_evidence_is_not_a_valid_receipt(session_factory):
    """The acceptance criterion: an imported 'subscribed' flag with no evidence creates no receipt."""
    with session_factory() as session:
        _person, cp_id, _ev = _seed_person_and_evidence(session, BIZ_A)
        store = ConsentReceiptStore(session, BIZ_A)
        with pytest.raises(ConsentReceiptError) as exc:
            # A flag-only import: it knows the contact point is "subscribed" but carries no evidence.
            store.record(_receipt_input(cp_id, evidence_id=""), actor=ACTOR)
        assert exc.value.code == REASON_MISSING_EVIDENCE
        session.commit()
        # The refusal staged nothing — no receipt exists.
        assert session.query(ConsentReceipt).count() == 0


def test_evidence_must_resolve_in_this_business(session_factory):
    """A cited evidence id that does not exist in this business is refused (no cross-tenant grounding)."""
    with session_factory() as session:
        _person, cp_id, _ev = _seed_person_and_evidence(session, BIZ_A)
        store = ConsentReceiptStore(session, BIZ_A)
        with pytest.raises(ConsentReceiptError) as exc:
            store.record(
                _receipt_input(cp_id, evidence_id="00000000-0000-4000-9000-000000000bad"),
                actor=ACTOR,
            )
        assert exc.value.code == REASON_EVIDENCE_NOT_FOUND
        session.commit()
        assert session.query(ConsentReceipt).count() == 0


@pytest.mark.parametrize(
    ("over", "expected_code"),
    [
        ({"brand_key": ""}, REASON_MISSING_BRAND),
        ({"purpose": "   "}, REASON_MISSING_PURPOSE),
        ({"action": "subscribe"}, REASON_UNKNOWN_ACTION),
        ({"notice_version": ""}, REASON_MISSING_NOTICE_VERSION),
        ({"form_version": ""}, REASON_MISSING_FORM_VERSION),
        ({"language": ""}, REASON_MISSING_LANGUAGE),
    ],
)
def test_incomplete_scope_is_refused_and_stages_nothing(session_factory, over, expected_code):
    with session_factory() as session:
        _person, cp_id, ev_id = _seed_person_and_evidence(session, BIZ_A)
        store = ConsentReceiptStore(session, BIZ_A)
        with pytest.raises(ConsentReceiptError) as exc:
            store.record(_receipt_input(cp_id, ev_id, **over), actor=ACTOR)
        assert exc.value.code == expected_code
        session.commit()
        assert session.query(ConsentReceipt).count() == 0


def test_missing_contact_point_is_refused(session_factory):
    with session_factory() as session:
        _person, _cp_id, ev_id = _seed_person_and_evidence(session, BIZ_A)
        store = ConsentReceiptStore(session, BIZ_A)
        with pytest.raises(ConsentReceiptError) as exc:
            store.record(_receipt_input("", ev_id), actor=ACTOR)
        assert exc.value.code == REASON_MISSING_CONTACT_POINT
        session.commit()
        assert session.query(ConsentReceipt).count() == 0


def test_contact_point_must_exist_in_this_business(session_factory):
    with session_factory() as session:
        _person, _cp_id, ev_id = _seed_person_and_evidence(session, BIZ_A)
        store = ConsentReceiptStore(session, BIZ_A)
        with pytest.raises(ConsentReceiptError) as exc:
            store.record(_receipt_input("00000000-0000-4000-9000-000000000bad", ev_id), actor=ACTOR)
        assert exc.value.code == REASON_CONTACT_POINT_NOT_FOUND


def test_channel_must_match_the_contact_point(session_factory):
    with session_factory() as session:
        _person, cp_id, ev_id = _seed_person_and_evidence(session, BIZ_A)
        store = ConsentReceiptStore(session, BIZ_A)
        with pytest.raises(ConsentReceiptError) as exc:
            # the contact point is an email; claim a permission on 'mobile' → mismatch
            store.record(_receipt_input(cp_id, ev_id, channel="mobile"), actor=ACTOR)
        assert exc.value.code == REASON_CHANNEL_MISMATCH


def test_evidence_about_another_subject_is_refused(session_factory):
    """A subject-linked evidence object must be about the contact point's owner."""
    with session_factory() as session:
        cp_store = ContactPointStore(session, BIZ_A)
        # the subject we record a permission for
        p1 = cp_store.create_person(PersonInput(display_name="Priya"), actor=ACTOR)
        cp1 = cp_store.add_contact_point(
            ContactPointInput(
                person_observation_id=p1.id, channel="email", value=EMAIL, evidence="self_reported"
            ),
            actor=ACTOR,
        )
        # a different person, and evidence captured ABOUT them
        p2 = cp_store.create_person(PersonInput(display_name="Rahul"), actor=ACTOR)
        ver = _seed_evidence_source(session)
        ev = EvidenceObjectStore(session, BIZ_A).store(
            EvidenceInput(
                source_key="optin_form",
                source_version=ver,
                content_kind="optin_record",
                content_ref="https://synthetic.example/forms/x",
                raw_content=FORM_RAW,
                captured_at=CAPTURED_AT,
                rights_basis="x",
                subject_type="person",
                subject_id=p2.id,
            ),
            actor=ACTOR,
        )
        store = ConsentReceiptStore(session, BIZ_A)
        with pytest.raises(ConsentReceiptError) as exc:
            store.record(_receipt_input(cp1.id, ev.id), actor=ACTOR)
        assert exc.value.code == REASON_EVIDENCE_SUBJECT_MISMATCH


# --- tenant isolation -----------------------------------------------------------------------------


def test_tenant_isolation_b_cannot_read_a_receipt(session_factory):
    """Permissions are tenant-private: B cannot read A's receipt by id, and the SAME email recorded by
    B carries no permission (receipts are not global contact attributes)."""
    with session_factory() as session:
        _person, cp_id, ev_id = _seed_person_and_evidence(session, BIZ_A)
        a_receipt = ConsentReceiptStore(session, BIZ_A).record(
            _receipt_input(cp_id, ev_id), actor=ACTOR
        )
        session.commit()
        a_rid = a_receipt.id

    with session_factory() as session:
        # B recorded the SAME email for its own person — but holds no permission for it.
        _bp, _bcp, _bev = _seed_person_and_evidence(session, BIZ_B, email=EMAIL)
        session.commit()

    with session_factory() as session:
        store_b = ConsentReceiptStore(session, BIZ_B)
        assert store_b.get(a_rid) is None  # A's receipt is not visible to B
        assert store_b.receipts() == []  # B granted nothing → it has no permission for the email


def test_cross_tenant_contact_point_reads_as_not_found(session_factory):
    """A does not learn B's contact point exists: B's contact-point id is not-found for A, so a
    receipt attempt against it is refused exactly like an unknown id."""
    with session_factory() as session:
        _bp, b_cp_id, _bev = _seed_person_and_evidence(session, BIZ_B)
        session.commit()

    with session_factory() as session:
        _ap, _acp, a_ev = _seed_person_and_evidence(session, BIZ_A, email="other@synthetic.example")
        store = ConsentReceiptStore(session, BIZ_A)
        with pytest.raises(ConsentReceiptError) as exc:
            store.record(_receipt_input(b_cp_id, a_ev), actor=ACTOR)
        assert exc.value.code == REASON_CONTACT_POINT_NOT_FOUND


# --- append-only history + atomic audit + no host record ------------------------------------------


def test_withdrawal_is_a_new_row_not_an_edit(session_factory):
    with session_factory() as session:
        _person, cp_id, ev_id = _seed_person_and_evidence(session, BIZ_A)
        store = ConsentReceiptStore(session, BIZ_A)
        store.record(_receipt_input(cp_id, ev_id, action="grant"), actor=ACTOR)
        store.record(_receipt_input(cp_id, ev_id, action="withdraw"), actor=ACTOR)
        session.commit()

    with session_factory() as session:
        timeline = ConsentReceiptStore(session, BIZ_A).for_contact_point(cp_id)
        assert [r.action for r in timeline] == ["grant", "withdraw"]  # both preserved


def test_record_stages_atomic_audit_without_contact_value_and_no_host_record(session_factory):
    with session_factory() as session:
        _person, cp_id, ev_id = _seed_person_and_evidence(session, BIZ_A)
        ConsentReceiptStore(session, BIZ_A).record(_receipt_input(cp_id, ev_id), actor=ACTOR)
        session.commit()

    with session_factory() as session:
        audits = (
            session.query(AuditEvent).filter(AuditEvent.action == AUDIT_ACTION_RECORD_RECEIPT).all()
        )
        assert len(audits) == 1
        # The raw contact value is tenant-private and NEVER audited.
        assert not any(EMAIL in (repr(a.references) + a.reason) for a in audits)
        # No prospect/host record is created and nothing is promoted.
        assert session.query(ProspectCandidate).count() == 0
        assert session.query(ConsentReceipt).count() == 1


# --- Postgres-backed: the real transaction / RLS path ---------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")


@pytest.mark.skipif(not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres")
def test_store_and_tenant_isolation_over_postgres():
    """Exercise the real record/read path on a disposable PostgreSQL with real transactions — never
    SQLite for the durable path. A complete receipt stores; a flag-only import stages nothing; and a
    cross-tenant id reads as not-found."""
    engine = create_engine(PG_URL, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        factory = make_session_factory(engine)
        with factory() as session:
            _person, cp_id, ev_id = _seed_person_and_evidence(session, BIZ_A)
            receipt = ConsentReceiptStore(session, BIZ_A).record(
                _receipt_input(cp_id, ev_id), actor=ACTOR
            )
            # a flag-only import stages nothing even though the contact point exists
            with pytest.raises(ConsentReceiptError):
                ConsentReceiptStore(session, BIZ_A).record(
                    _receipt_input(cp_id, evidence_id=""), actor=ACTOR
                )
            session.commit()
            rid = receipt.id

        with factory() as session:
            assert ConsentReceiptStore(session, BIZ_A).get(rid) is not None
            assert ConsentReceiptStore(session, BIZ_B).get(rid) is None  # cross-tenant → not-found
            assert (
                session.query(ConsentReceipt).count() == 1
            )  # only the permitted receipt persisted
    finally:
        Base.metadata.drop_all(engine)
