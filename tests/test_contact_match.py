"""DCA-068 — match contacts without merging consent.

Pins the load-bearing behaviours of :mod:`app.matching.contact_match` — the pure, deterministic,
conservative person identity resolver that sits on top of the DCA-064/065/066 normalizers and the
DCA-067 organization matcher:

1. **Only a compatible PERSONAL contact point confirms a person** (a shared per-person email mailbox).
2. **A shared / recycled number or a common name NEVER transfers a person's permission** — it is at
   most a ``REVIEW`` candidate, and when refuted by different personal mailboxes it is ``NO_MATCH``
   (two different people on a recycled/shared contact point) — never a ``MATCH``.
3. **Reassignment / role changes require review** — a shared personal mailbox with a different
   employer/role is a ``REVIEW`` (reassignment) flag, never a silent carry-over.
4. **A denial changes nothing + the result is reproducible** (the resolver is pure; it merges nothing
   and transfers no permission).

The resolver is pure (no DB), so these run network-free with no datastore. The read-only observation
adapter is exercised on in-memory SQLite; the real transaction/RLS path for the underlying
person/contact-point tables is covered by ``tests/test_contact_point.py`` / ``tests/test_rls.py`` on
disposable Postgres.
"""

from __future__ import annotations

from app.audit.sink import ActorType, AuditActor
from app.db.base import Base, make_engine, make_session_factory
from app.matching import (
    Confidence,
    ContactMatchOutcome,
    ContactSignalKind,
    SignalRelation,
    SignalStrength,
    build_contact_subject,
    build_subject,
    match_contacts,
    subject_from_person_observation,
)
from app.services.contact_point import ContactPointInput, ContactPointStore, PersonInput
from app.services.org_observation import ExternalIdInput, ObservationInput, OrgObservationStore

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"
ACTOR = AuditActor(id="00000000-0000-4000-9000-0000000a0068", type=ActorType.USER)

# A checksum-valid synthetic Indian mobile number (leading digit 9), shared below to model a
# recycled/shared number — no real subscriber.
RECYCLED_MOBILE = "9900112233"


# -- strong personal anchor MATCH -------------------------------------------------------------------


def test_shared_personal_mailbox_is_a_match() -> None:
    a = build_contact_subject(name="Ramesh Kumar", emails=["ramesh@acme.in"])
    b = build_contact_subject(name="R. Kumar", emails=["Ramesh@ACME.in"])
    result = match_contacts(a, b)
    assert result.outcome is ContactMatchOutcome.MATCH
    assert result.confidence is Confidence.HIGH
    assert result.relation(ContactSignalKind.PERSONAL_EMAIL) is SignalRelation.AGREE
    assert result.auto_merged is False
    assert result.permission_transferred is False


def test_shared_personal_mailbox_with_same_employer_is_a_match() -> None:
    acme = build_subject(name="Acme Pvt Ltd", domains=["acme.in"])
    a = build_contact_subject(emails=["ramesh@acme.in"], employer=acme, role_title="Buyer")
    b = build_contact_subject(emails=["ramesh@acme.in"], employer=acme, role_title="buyer")
    result = match_contacts(a, b)
    assert result.outcome is ContactMatchOutcome.MATCH
    assert result.requires_reassignment_review is False


# -- reassignment / role change requires review -----------------------------------------------------


def test_same_person_different_employer_is_a_reassignment_review() -> None:
    # Same personal mailbox, but the employer organization differs (different CINs) => a job change.
    acme = build_subject(name="Acme", identifiers=[("cin", "U72900MH2010PTC123456")])
    globex = build_subject(name="Globex", identifiers=[("cin", "U72900KA2011PTC654321")])
    a = build_contact_subject(emails=["ramesh@personal.in"], employer=acme)
    b = build_contact_subject(emails=["ramesh@personal.in"], employer=globex)
    result = match_contacts(a, b)
    assert result.outcome is ContactMatchOutcome.REVIEW
    assert result.requires_reassignment_review is True
    assert result.relation(ContactSignalKind.EMPLOYMENT) is SignalRelation.CONFLICT
    # Identity agreed, but nothing was merged and no permission moved across the employment change.
    assert result.auto_merged is False
    assert result.permission_transferred is False


def test_same_person_different_role_is_a_role_change_review() -> None:
    a = build_contact_subject(emails=["ramesh@personal.in"], role_title="Procurement Head")
    b = build_contact_subject(emails=["ramesh@personal.in"], role_title="Finance Head")
    result = match_contacts(a, b)
    assert result.outcome is ContactMatchOutcome.REVIEW
    assert result.requires_reassignment_review is True
    assert result.relation(ContactSignalKind.ROLE) is SignalRelation.CONFLICT


# -- the DECISIVE guarantee: a shared/recycled number or common name never transfers permission ------


def test_recycled_number_with_different_personal_mailboxes_is_no_match() -> None:
    # Two different people reachable on the SAME recycled/shared number but with different PERSONAL
    # mailboxes: the number must not transfer one person's permission to the other => NO_MATCH.
    a = build_contact_subject(
        name="Ramesh Kumar", emails=["ramesh@acme.in"], phones=[RECYCLED_MOBILE]
    )
    b = build_contact_subject(
        name="Suresh Rao", emails=["suresh@globex.in"], phones=[RECYCLED_MOBILE]
    )
    result = match_contacts(a, b)
    assert result.outcome is ContactMatchOutcome.NO_MATCH
    assert result.confidence is Confidence.HIGH
    assert result.relation(ContactSignalKind.PERSONAL_EMAIL) is SignalRelation.CONFLICT
    # The shared number is recorded, but WEAK — it did not collapse the two people.
    phone = next(s for s in result.signals if s.kind is ContactSignalKind.PHONE)
    assert phone.strength is SignalStrength.WEAK
    assert result.auto_merged is False
    assert result.permission_transferred is False


def test_shared_number_alone_is_a_review_candidate_not_a_match() -> None:
    # A shared/recycled number with no personal anchors is a candidate to review, never a match.
    a = build_contact_subject(name="Ramesh Kumar", phones=[RECYCLED_MOBILE])
    b = build_contact_subject(name="Suresh Rao", phones=[RECYCLED_MOBILE])
    result = match_contacts(a, b)
    assert result.outcome is ContactMatchOutcome.REVIEW
    assert result.confidence is Confidence.LOW
    assert result.relation(ContactSignalKind.PHONE) is SignalRelation.AGREE
    assert result.permission_transferred is False


def test_common_name_alone_is_a_review_candidate_not_a_match() -> None:
    a = build_contact_subject(name="Ramesh Kumar")
    b = build_contact_subject(name="ramesh kumar")
    result = match_contacts(a, b)
    assert result.outcome is ContactMatchOutcome.REVIEW
    assert result.confidence is Confidence.LOW


def test_shared_role_mailbox_alone_is_a_review_candidate_not_a_match() -> None:
    # A functional/role mailbox is answered by many people: weak, never a personal anchor.
    a = build_contact_subject(name="Front Desk", emails=["info@acme.in"])
    b = build_contact_subject(name="Reception", emails=["info@acme.in"])
    result = match_contacts(a, b)
    assert result.outcome is ContactMatchOutcome.REVIEW
    assert result.relation(ContactSignalKind.SHARED_MAILBOX) is SignalRelation.AGREE
    # A role mailbox is NEVER counted as a personal anchor.
    assert result.relation(ContactSignalKind.PERSONAL_EMAIL) is None


def test_number_and_name_together_are_medium_review() -> None:
    a = build_contact_subject(name="Ramesh Kumar", phones=[RECYCLED_MOBILE])
    b = build_contact_subject(name="RAMESH KUMAR", phones=[RECYCLED_MOBILE])
    result = match_contacts(a, b)
    assert result.outcome is ContactMatchOutcome.REVIEW
    assert result.confidence is Confidence.MEDIUM


# -- employer corroborates / contradicts, never decides on weak evidence -----------------------------


def test_employer_corroboration_promotes_low_to_medium() -> None:
    acme = build_subject(name="Acme", identifiers=[("gstin", "27AAPFU0939F1ZV")])
    a = build_contact_subject(name="Ramesh Kumar", employer=acme)
    b = build_contact_subject(name="Ramesh Kumar", employer=acme)
    result = match_contacts(a, b)
    assert result.outcome is ContactMatchOutcome.REVIEW
    assert result.confidence is Confidence.MEDIUM  # name (low) + same employer corroboration
    assert result.relation(ContactSignalKind.EMPLOYMENT) is SignalRelation.CORROBORATE


def test_employer_conflict_keeps_candidate_low_but_does_not_flip_outcome() -> None:
    acme = build_subject(name="Acme", identifiers=[("cin", "U72900MH2010PTC123456")])
    globex = build_subject(name="Globex", identifiers=[("cin", "U72900KA2011PTC654321")])
    a = build_contact_subject(name="Ramesh Kumar", phones=[RECYCLED_MOBILE], employer=acme)
    b = build_contact_subject(name="Ramesh Kumar", phones=[RECYCLED_MOBILE], employer=globex)
    result = match_contacts(a, b)
    # name + number would be MEDIUM, but the different employer knocks it back to LOW.
    assert result.outcome is ContactMatchOutcome.REVIEW
    assert result.confidence is Confidence.LOW
    assert result.relation(ContactSignalKind.EMPLOYMENT) is SignalRelation.CONFLICT


def test_employer_alone_is_too_weak_to_propose_a_candidate() -> None:
    acme = build_subject(name="Acme", identifiers=[("gstin", "27AAPFU0939F1ZV")])
    a = build_contact_subject(name="Alpha", employer=acme)
    b = build_contact_subject(name="Beta", employer=acme)
    result = match_contacts(a, b)
    assert result.outcome is ContactMatchOutcome.INSUFFICIENT
    assert result.confidence is Confidence.NONE


# -- insufficient ------------------------------------------------------------------------------------


def test_no_shared_evidence_is_insufficient() -> None:
    a = build_contact_subject(name="Alpha", emails=["alpha@a.in"], phones=["9811111111"])
    b = build_contact_subject(name="Beta", emails=["beta@b.in"], phones=["9822222222"])
    result = match_contacts(a, b)
    assert result.outcome is ContactMatchOutcome.INSUFFICIENT


# -- builder collects denials; malformed values never become evidence --------------------------------


def test_build_contact_subject_collects_denials_without_raising() -> None:
    subject = build_contact_subject(
        name="\x00\x00",  # non-blank but holds no normalizable content -> refused
        emails=["not an email", "@nolocal.in", "two@@ats.in"],  # malformed -> refused
        phones=["call me"],  # no dialable digit -> refused
    )
    assert subject.name is None
    assert subject.personal_emails == ()
    assert subject.shared_mailboxes == ()
    assert subject.phones == ()
    fields = {d.field for d in subject.denials}
    assert fields == {"name", "email", "phone"}


def test_malformed_email_does_not_fabricate_a_match() -> None:
    a = build_contact_subject(name="Ramesh", emails=["GARBAGE"])
    b = build_contact_subject(name="Ramesh", emails=["GARBAGE"])
    result = match_contacts(a, b)
    # Both emails were refused (collected as denials), so there is NO personal anchor — only the shared
    # name remains, i.e. a weak review candidate, never a confirmed match.
    assert result.outcome is ContactMatchOutcome.REVIEW
    assert all(d.field == "email" for d in a.denials)


def test_unresolved_phone_is_dropped_not_a_denial_and_never_an_anchor() -> None:
    # A bare national number with NO country evidence is ambiguous (DCA-066 UNRESOLVED) — not a denial,
    # but also not a comparable key, so it never forms a phone anchor.
    a = build_contact_subject(name="Ramesh", phones=[("9900112233", None)])
    assert a.phones == ()
    assert a.denials == ()


# -- purity / reproducibility ------------------------------------------------------------------------


def test_result_is_reproducible_and_symmetric() -> None:
    a = build_contact_subject(name="Ramesh", emails=["ramesh@acme.in"], phones=[RECYCLED_MOBILE])
    b = build_contact_subject(name="Suresh", emails=["suresh@globex.in"], phones=[RECYCLED_MOBILE])
    first = match_contacts(a, b)
    second = match_contacts(a, b)
    reversed_ = match_contacts(b, a)
    assert first.outcome is second.outcome is reversed_.outcome is ContactMatchOutcome.NO_MATCH
    assert first.confidence is second.confidence is reversed_.confidence
    assert first.permission_transferred is False


# -- read-only, tenant-scoped observation adapter ----------------------------------------------------


def _session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _record_person(
    contact_store: ContactPointStore,
    session,
    *,
    name: str,
    email: str,
    phone: str,
    org_id: str | None = None,
):
    person = contact_store.create_person(
        PersonInput(display_name=name, org_observation_id=org_id), ACTOR
    )
    session.flush()
    contact_store.add_contact_point(
        ContactPointInput(
            person_observation_id=person.id, channel="email", value=email, evidence="imported"
        ),
        ACTOR,
    )
    contact_store.add_contact_point(
        ContactPointInput(
            person_observation_id=person.id, channel="mobile", value=phone, evidence="imported"
        ),
        ACTOR,
    )
    session.flush()
    return person


def test_adapter_matches_same_person_via_each_tenants_own_store() -> None:
    # The SAME person legitimately recorded by two businesses (contact points are per-business). Each
    # subject is built through that business's OWN scoped store — neither store reads the other's rows —
    # yet the pure matcher still concludes the two observations describe the same person.
    factory = _session_factory()
    with factory() as session:
        store_a = ContactPointStore(session, BIZ_A)
        store_b = ContactPointStore(session, BIZ_B)
        person_a = _record_person(
            store_a, session, name="Ramesh Kumar", email="ramesh@acme.in", phone="9811100011"
        )
        person_b = _record_person(
            store_b, session, name="R Kumar", email="ramesh@acme.in", phone="9811100011"
        )

        # Tenant isolation: A's store cannot see B's person.
        assert store_a.get_person(person_b.id) is None

        subj_a = subject_from_person_observation(store_a, person_a)
        subj_b = subject_from_person_observation(store_b, person_b)

    result = match_contacts(subj_a, subj_b)
    assert result.outcome is ContactMatchOutcome.MATCH
    assert result.relation(ContactSignalKind.PERSONAL_EMAIL) is SignalRelation.AGREE
    # No write occurred: the adapter is read-only, nothing is merged, no permission transfers.
    assert result.auto_merged is False
    assert result.permission_transferred is False


def test_adapter_recycled_number_keeps_separate_identities_with_unchanged_permissions() -> None:
    # Two DIFFERENT people recorded by two businesses that happen to share a recycled mobile number but
    # have different personal mailboxes. The matcher keeps them SEPARATE (NO_MATCH): one person's
    # permission never reaches the other, and nothing is merged.
    factory = _session_factory()
    with factory() as session:
        store_a = ContactPointStore(session, BIZ_A)
        store_b = ContactPointStore(session, BIZ_B)
        person_a = _record_person(
            store_a, session, name="Ramesh Kumar", email="ramesh@acme.in", phone=RECYCLED_MOBILE
        )
        person_b = _record_person(
            store_b, session, name="Suresh Rao", email="suresh@globex.in", phone=RECYCLED_MOBILE
        )
        subj_a = subject_from_person_observation(store_a, person_a)
        subj_b = subject_from_person_observation(store_b, person_b)

    result = match_contacts(subj_a, subj_b)
    assert result.outcome is ContactMatchOutcome.NO_MATCH
    assert result.relation(ContactSignalKind.PERSONAL_EMAIL) is SignalRelation.CONFLICT
    assert result.auto_merged is False
    assert result.permission_transferred is False


def test_adapter_builds_employer_and_flags_reassignment() -> None:
    # A person whose linked employer differs between two tenants' records (and who shares a personal
    # mailbox) is flagged as a reassignment to review, built via the tenant-scoped org store.
    factory = _session_factory()
    with factory() as session:
        contact_a = ContactPointStore(session, BIZ_A)
        contact_b = ContactPointStore(session, BIZ_B)
        org_a = OrgObservationStore(session, BIZ_A)
        org_b = OrgObservationStore(session, BIZ_B)
        acme = org_a.create(
            ObservationInput(
                entity_type="legal_entity",
                display_name="Acme",
                external_ids=[ExternalIdInput(scheme="cin", value="U72900MH2010PTC123456")],
            ),
            ACTOR,
        )
        globex = org_b.create(
            ObservationInput(
                entity_type="legal_entity",
                display_name="Globex",
                external_ids=[ExternalIdInput(scheme="cin", value="U72900KA2011PTC654321")],
            ),
            ACTOR,
        )
        session.flush()
        person_a = _record_person(
            contact_a,
            session,
            name="Ramesh Kumar",
            email="ramesh@personal.in",
            phone="9811100011",
            org_id=acme.id,
        )
        person_b = _record_person(
            contact_b,
            session,
            name="Ramesh Kumar",
            email="ramesh@personal.in",
            phone="9811100011",
            org_id=globex.id,
        )
        subj_a = subject_from_person_observation(contact_a, person_a, org_store=org_a)
        subj_b = subject_from_person_observation(contact_b, person_b, org_store=org_b)

    result = match_contacts(subj_a, subj_b)
    assert result.outcome is ContactMatchOutcome.REVIEW
    assert result.requires_reassignment_review is True
    assert result.relation(ContactSignalKind.EMPLOYMENT) is SignalRelation.CONFLICT
