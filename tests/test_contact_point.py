"""DCA-060 — store tenant-private contact points.

Pins the load-bearing behaviours of :mod:`app.services.contact_point` — the single sanctioned writer
of :class:`~app.db.models.PersonObservation` and its separate :class:`~app.db.models.ContactPoint`
value rows:

1. **Tenant-private; the same global mapping cannot expose A's email to B.** A stores a private
   person + email contact point linked to its organization; even when B points its OWN organization
   observation at the SAME external/global organization id, B cannot read A's person, A's contact
   point, or resolve A's private email — reads are confined to one business, there is no global index
   on the value.
2. **Imported values are never auto-promoted.** An ``IMPORTED`` contact point is written only to the
   business's private tables; recording it creates no prospect/host record.
3. **A denial changes nothing.** Every refusal (missing name, cross-tenant org link, unknown person,
   unknown channel/evidence, empty value, duplicate contact point) stages no row. Each successful
   create stages an atomic DCA-026 audit entry in the SAME unit of work — and the raw contact value
   is never written to the trail.

The store logic is unit-tested on in-memory SQLite; the real transaction / RLS path is exercised on a
disposable PostgreSQL (``DCA_TEST_DATABASE_URL``), never relying on SQLite for that. The Postgres test
skips when no URL is set. The RLS/migration paths for the two tables are covered by ``tests/test_rls.py``
and ``tests/test_migrations.py``.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine

from app.audit.sink import ActorType, AuditActor
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import (
    AuditEvent,
    ContactPoint,
    DiscoveryRun,
    PersonObservation,
    ProspectCandidate,
)
from app.services.contact_point import (
    AUDIT_ACTION_RECORD_CONTACT_POINT,
    AUDIT_ACTION_RECORD_PERSON,
    REASON_DUPLICATE_CONTACT_POINT,
    REASON_MISSING_CONTACT_VALUE,
    REASON_MISSING_PERSON_NAME,
    REASON_PERSON_NOT_FOUND,
    REASON_PERSON_ORG_NOT_FOUND,
    REASON_UNKNOWN_CHANNEL,
    REASON_UNKNOWN_EVIDENCE,
    ContactPointError,
    ContactPointInput,
    ContactPointStore,
    PersonInput,
)
from app.services.org_observation import (
    ExternalIdInput,
    ObservationInput,
    OrgObservationStore,
)

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"
ACTOR = AuditActor(id="00000000-0000-4000-9000-0000000a0060", type=ActorType.USER)

# A shared/global organization id both businesses may legitimately record against their own org.
SHARED_PLATFORM_ORG_ID = "org-9f3c-shared-global"
A_PRIVATE_EMAIL = "priya.sharma@synthetictextiles.example"


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _seed_org(store: OrgObservationStore) -> str:
    """Create a legal-entity org observation carrying the shared global platform-org id; return its id."""
    le = store.create(
        ObservationInput(
            entity_type="legal_entity",
            display_name="Synthetic Textiles Pvt Ltd",
            external_ids=[ExternalIdInput("platform_org_id", SHARED_PLATFORM_ORG_ID)],
        ),
        actor=ACTOR,
    )
    return le.id


# --- tenant-private: the same global mapping cannot expose A's email to B --------------------------


def test_business_b_cannot_reach_business_as_private_email_via_shared_org(session_factory):
    # Business A: an org (sharing a global platform-org id), a person on it, and a private email.
    with session_factory() as session:
        org_store_a = OrgObservationStore(session, BIZ_A)
        a_org_id = _seed_org(org_store_a)
        store_a = ContactPointStore(session, BIZ_A)
        person = store_a.create_person(
            PersonInput(display_name="Priya Sharma", org_observation_id=a_org_id, role_title="CPO"),
            actor=ACTOR,
        )
        cp = store_a.add_contact_point(
            ContactPointInput(
                person_observation_id=person.id,
                channel="email",
                value=A_PRIVATE_EMAIL,
                evidence="self_reported",
            ),
            actor=ACTOR,
        )
        session.commit()
        a_org_id, a_person_id, a_cp_id = a_org_id, person.id, cp.id

    # Business B records its OWN org observation against the SAME shared global platform-org id.
    with session_factory() as session:
        org_store_b = OrgObservationStore(session, BIZ_B)
        b_org_id = _seed_org(org_store_b)
        session.commit()
        b_org_id = b_org_id

    with session_factory() as session:
        store_b = ContactPointStore(session, BIZ_B)
        # B resolves the shared global id to ITS OWN org — a different row than A's.
        b_resolved = OrgObservationStore(session, BIZ_B).resolve_external(
            "platform_org_id", SHARED_PLATFORM_ORG_ID
        )
        assert b_resolved is not None and b_resolved.id == b_org_id and b_org_id != a_org_id

        # B cannot read A's person or A's contact point (cross-tenant ids read as not-found).
        assert store_b.get_person(a_person_id) is None
        assert store_b.get_contact_point(a_cp_id) is None
        assert store_b.persons_of_org(a_org_id) == []  # A's org id is invisible to B
        assert store_b.persons_of_org(b_org_id) == []  # B has recorded no people yet

        # The decisive check: B cannot resolve A's private email — no global index exposes it.
        assert store_b.resolve_contact("email", A_PRIVATE_EMAIL) is None

    # A still reaches its own private email.
    with session_factory() as session:
        store_a = ContactPointStore(session, BIZ_A)
        a_hit = store_a.resolve_contact("email", A_PRIVATE_EMAIL.upper())  # case-insensitive email
        assert a_hit is not None and a_hit.id == a_cp_id and a_hit.business_id == BIZ_A
        assert [c.value for c in store_a.contact_points_of(a_person_id)] == [A_PRIVATE_EMAIL]


def test_the_same_value_may_be_recorded_privately_by_two_businesses(session_factory):
    with session_factory() as session:
        a = ContactPointStore(session, BIZ_A)
        a_person = a.create_person(PersonInput(display_name="A Person"), actor=ACTOR)
        a.add_contact_point(
            ContactPointInput(a_person.id, "mobile", "+919876500000", "verified"), actor=ACTOR
        )
        b = ContactPointStore(session, BIZ_B)
        b_person = b.create_person(PersonInput(display_name="B Person"), actor=ACTOR)
        b.add_contact_point(
            ContactPointInput(b_person.id, "mobile", "+919876500000", "imported"), actor=ACTOR
        )
        session.commit()

    with session_factory() as session:
        a_hit = ContactPointStore(session, BIZ_A).resolve_contact("mobile", "+919876500000")
        b_hit = ContactPointStore(session, BIZ_B).resolve_contact("mobile", "+919876500000")
        assert a_hit is not None and b_hit is not None
        # The same value resolves to a DIFFERENT, business-scoped contact point in each tenant.
        assert a_hit.id != b_hit.id
        assert a_hit.business_id == BIZ_A and b_hit.business_id == BIZ_B


# --- imported values are never auto-promoted + no host record --------------------------------------


def test_imported_contact_point_creates_no_host_or_prospect_record(session_factory):
    with session_factory() as session:
        store = ContactPointStore(session, BIZ_A)
        person = store.create_person(PersonInput(display_name="Imported Lead"), actor=ACTOR)
        store.add_contact_point(
            ContactPointInput(person.id, "email", "lead@example.test", "imported"), actor=ACTOR
        )
        session.commit()

    with session_factory() as session:
        # Written ONLY to this business's private tables — no prospect/host/discovery record exists.
        assert session.query(PersonObservation).count() == 1
        assert session.query(ContactPoint).count() == 1
        assert session.query(ProspectCandidate).count() == 0
        assert session.query(DiscoveryRun).count() == 0


# --- shape refusals: each stages no row ------------------------------------------------------------


def _assert_no_rows(session_factory):
    with session_factory() as session:
        assert session.query(PersonObservation).count() == 0
        assert session.query(ContactPoint).count() == 0
        assert session.query(AuditEvent).count() == 0


def test_person_without_a_name_is_refused(session_factory):
    with session_factory() as session:
        store = ContactPointStore(session, BIZ_A)
        with pytest.raises(ContactPointError) as exc:
            store.create_person(PersonInput(display_name="   "), actor=ACTOR)
        assert exc.value.code == REASON_MISSING_PERSON_NAME
        session.rollback()
    _assert_no_rows(session_factory)


def test_person_cannot_link_to_another_businesss_organization(session_factory):
    with session_factory() as session:
        a_org_id = _seed_org(OrgObservationStore(session, BIZ_A))
        session.commit()
        a_org_id = a_org_id

    # Business B tries to hang a person off Business A's organization -> not found (no cross-tenant link).
    with session_factory() as session:
        store_b = ContactPointStore(session, BIZ_B)
        with pytest.raises(ContactPointError) as exc:
            store_b.create_person(
                PersonInput(display_name="B person on A org", org_observation_id=a_org_id),
                actor=ACTOR,
            )
        assert exc.value.code == REASON_PERSON_ORG_NOT_FOUND
        session.rollback()

    with session_factory() as session:
        assert ContactPointStore(session, BIZ_B).persons() == []  # B created nothing


def test_unknown_channel_evidence_empty_value_and_missing_person_are_refused(session_factory):
    with session_factory() as session:
        store = ContactPointStore(session, BIZ_A)
        person = store.create_person(PersonInput(display_name="P"), actor=ACTOR)
        session.commit()
        person_id = person.id

    with session_factory() as session:
        store = ContactPointStore(session, BIZ_A)

        with pytest.raises(ContactPointError) as exc:
            store.add_contact_point(
                ContactPointInput("does-not-exist", "email", "x@y.test", "verified"), actor=ACTOR
            )
        assert exc.value.code == REASON_PERSON_NOT_FOUND

        with pytest.raises(ContactPointError) as exc:
            store.add_contact_point(
                ContactPointInput(person_id, "fax", "x@y.test", "verified"), actor=ACTOR
            )
        assert exc.value.code == REASON_UNKNOWN_CHANNEL

        with pytest.raises(ContactPointError) as exc:
            store.add_contact_point(
                ContactPointInput(person_id, "email", "x@y.test", "gut_feeling"), actor=ACTOR
            )
        assert exc.value.code == REASON_UNKNOWN_EVIDENCE

        with pytest.raises(ContactPointError) as exc:
            store.add_contact_point(
                ContactPointInput(person_id, "email", "   ", "verified"), actor=ACTOR
            )
        assert exc.value.code == REASON_MISSING_CONTACT_VALUE
        session.rollback()

    # Only the one person exists; every refused contact point staged nothing.
    with session_factory() as session:
        assert session.query(PersonObservation).count() == 1
        assert session.query(ContactPoint).count() == 0


def test_duplicate_contact_point_within_one_business_is_refused(session_factory):
    with session_factory() as session:
        store = ContactPointStore(session, BIZ_A)
        person = store.create_person(PersonInput(display_name="P"), actor=ACTOR)
        store.add_contact_point(
            ContactPointInput(person.id, "email", "Dup@Example.test", "self_reported"), actor=ACTOR
        )
        session.commit()
        person_id = person.id

    with session_factory() as session:
        store = ContactPointStore(session, BIZ_A)
        with pytest.raises(ContactPointError) as exc:
            # Same email, different casing -> normalized to the same value -> duplicate.
            store.add_contact_point(
                ContactPointInput(person_id, "email", "dup@example.test", "imported"), actor=ACTOR
            )
        assert exc.value.code == REASON_DUPLICATE_CONTACT_POINT
        session.rollback()

    with session_factory() as session:
        assert session.query(ContactPoint).count() == 1  # the duplicate staged nothing


# --- atomic audit: never carries the raw contact value ---------------------------------------------


def test_creates_stage_atomic_audit_entries_without_the_raw_value(session_factory):
    with session_factory() as session:
        store = ContactPointStore(session, BIZ_A)
        person = store.create_person(PersonInput(display_name="Priya Sharma"), actor=ACTOR)
        store.add_contact_point(
            ContactPointInput(person.id, "email", A_PRIVATE_EMAIL, "self_reported"), actor=ACTOR
        )
        session.commit()

    with session_factory() as session:
        audits = session.query(AuditEvent).order_by(AuditEvent.id).all()
        actions = {a.action for a in audits}
        assert actions == {AUDIT_ACTION_RECORD_PERSON, AUDIT_ACTION_RECORD_CONTACT_POINT}
        for entry in audits:
            assert entry.business_id == BIZ_A
            # The raw email / display name are tenant-private and must never land in the trail.
            blob = repr(entry.references) + entry.reason
            assert A_PRIVATE_EMAIL not in blob
            assert "Priya" not in blob
        cp_audit = next(a for a in audits if a.action == AUDIT_ACTION_RECORD_CONTACT_POINT)
        assert cp_audit.references["channel"] == "email"
        assert cp_audit.references["evidence"] == "self_reported"


# --- Postgres-backed: the real transaction / RLS path ---------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")


@pytest.mark.skipif(not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres")
def test_create_and_reads_over_postgres():
    """Exercise the real create → read path on a disposable PostgreSQL with real transactions — never
    SQLite for the durable read/write path. A stores a private person + email under its org; B, with
    its own org on the SAME shared global id, cannot resolve A's email or read A's person."""
    engine = create_engine(PG_URL, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        factory = make_session_factory(engine)
        with factory() as session:
            a_org_id = _seed_org(OrgObservationStore(session, BIZ_A))
            store_a = ContactPointStore(session, BIZ_A)
            person = store_a.create_person(
                PersonInput(display_name="Priya Sharma", org_observation_id=a_org_id), actor=ACTOR
            )
            cp = store_a.add_contact_point(
                ContactPointInput(person.id, "email", A_PRIVATE_EMAIL, "self_reported"), actor=ACTOR
            )
            session.commit()
            a_person_id, a_cp_id = person.id, cp.id

        with factory() as session:
            b_org_id = _seed_org(OrgObservationStore(session, BIZ_B))
            session.commit()
            b_org_id = b_org_id

        with factory() as session:
            store_b = ContactPointStore(session, BIZ_B)
            assert store_b.get_person(a_person_id) is None
            assert store_b.get_contact_point(a_cp_id) is None
            assert store_b.resolve_contact("email", A_PRIVATE_EMAIL) is None

        with factory() as session:
            store_a = ContactPointStore(session, BIZ_A)
            hit = store_a.resolve_contact("email", A_PRIVATE_EMAIL)
            assert hit is not None and hit.id == a_cp_id and hit.business_id == BIZ_A
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
