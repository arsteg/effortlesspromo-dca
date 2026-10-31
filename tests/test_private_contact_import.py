"""DCA-126 — import a client's private contact CSV (the fourth concrete source adapter).

Pins the load-bearing behaviours of :mod:`app.sources.private_contact_import` — the caller-supplied
private contact-list importer that writes a CLIENT'S OWN persons + contact points into the DCA-060
tenant-private store, the FIRST adapter that writes PERSONAL data:

1. **A valid private CSV imports persons + contact points with the client-asserted basis + provenance.**
   Each imported contact point carries ``evidence='imported'`` and a :class:`~app.db.models.ContactImport`
   provenance row records the client-asserted basis, the provenance label and the checkpointable row ref —
   and NO :class:`~app.db.models.ConsentReceipt` is ever created (importing is not recipient consent).
2. **A suppressed / restricted contact is EXCLUDED and only flagged (no contactable record).** A value on
   the host suppression list, or under an in-force processing restriction, is not stored as a
   ``ContactPoint`` — it is excluded and flagged; a ``subscribed`` flag never overrides that.
3. **Idempotent re-import.** Re-running the same import skips every already-imported row and creates
   nothing new.
4. **Tenant isolation.** Two businesses importing the same CSV get independent, business-scoped persons /
   contacts; neither can see the other's imported contacts or provenance.
5. **Malformed rows are refused per-row** (missing name, no/invalid contact value) with a safe row-level
   error and import nothing — no person, no contact, no fabricated consent.

Logic is unit-tested on in-memory SQLite; the real transaction / RLS path is exercised on a disposable
PostgreSQL (``DCA_TEST_DATABASE_URL``), never relying on SQLite for that. The RLS/migration paths for the
table are covered by ``tests/test_rls.py`` and ``tests/test_migrations.py``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine

from app.audit.sink import ActorType, AuditActor
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import (
    ConsentReceipt,
    ContactImport,
    ContactImportStatus,
    ContactPoint,
    PersonObservation,
)
from app.services.processing_restriction import (
    ProcessingRestrictionInput,
    ProcessingRestrictionStore,
    RestrictionPrincipal,
)
from app.sources.private_contact_import import (
    PRIVATE_CONTACT_IMPORT_CAPABILITY,
    REASON_RESTRICTED,
    REASON_ROW_MISSING_NAME,
    REASON_ROW_NO_CONTACT_VALUE,
    REASON_ROW_NO_VALID_CONTACT,
    REASON_SUPPRESSED,
    PrivateContactImporter,
    PrivateContactImportError,
    PrivateImportManifest,
)
from app.suppression.contract import (
    SuppressionAvailability,
    SuppressionEntry,
    SuppressionSnapshot,
)

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
ACTOR = AuditActor(id="00000000-0000-4000-9000-0000000a0126", type=ActorType.USER)
RESTRICTOR = RestrictionPrincipal(subject="00000000-0000-4000-9000-0000000a0126", roles=("user",))

SUPPRESSED_EMAIL = "optedout@synthetic-a.example"
SUPPRESSED_MOBILE_E164 = "+919900000000"


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def _manifest(**overrides) -> PrivateImportManifest:
    base = {
        "dataset_id": "crm-export-2026-q3",
        "basis": "existing_customer",
        "purpose": "account_servicing",
        "source_label": "acme_crm_export",
        "source_version": "2026-09-30",
    }
    base.update(overrides)
    return PrivateImportManifest(**base)


def _rows() -> list[dict]:
    """A small synthetic private contact list: three contacts, each with a name + reachable values."""
    return [
        {
            "row_ref": "c-1",
            "name": "Asha Rao",
            "role_title": "Procurement Head",
            "email": "Asha.Rao@Buyer.example",
            "mobile": "9812345678",
            "subscribed": "true",
        },
        {
            "row_ref": "c-2",
            "name": "Vikram Singh",
            "email": "vikram@buyer.example",
        },
        {
            "row_ref": "c-3",
            "name": "Meera Nair",
            "mobile": "+91 90000 12345",
        },
    ]


def _snapshot(entries=(), *, business_id: str = BIZ_A) -> SuppressionSnapshot:
    return SuppressionSnapshot(
        business_id=business_id,
        availability=SuppressionAvailability.AVAILABLE,
        version="fake-suppression-v1",
        as_of=NOW,
        covered_channels=("email", "sms", "whatsapp"),
        entries=tuple(entries),
        host_owns_consent=True,
        synthetic=True,
    )


# --- 1) a valid private CSV imports persons + contact points with basis + provenance ---------------


def test_capability_declares_personal_contacts():
    cap = PRIVATE_CONTACT_IMPORT_CAPABILITY
    assert cap.handles_personal_contacts is True
    assert cap.is_organization_only is False
    from app.sources.capabilities import AdapterOperation

    assert AdapterOperation.ENRICH_CONTACT in cap.operations


def test_valid_csv_imports_persons_contacts_with_basis_and_no_consent(session_factory):
    with session_factory() as session:
        importer = PrivateContactImporter(session, BIZ_A)
        result = importer.import_contacts(
            _manifest(), _rows(), ACTOR, suppression=_snapshot(), now=NOW
        )
        assert result.imported_count == 3
        assert result.skipped_count == 0
        assert result.error_count == 0
        assert result.contacts_imported == 4  # 2 for Asha (email+mobile) + 1 + 1
        assert result.contacts_excluded == 0
        session.commit()

    with session_factory() as session:
        persons = (
            session.query(PersonObservation).filter(PersonObservation.business_id == BIZ_A).all()
        )
        assert len(persons) == 3
        points = session.query(ContactPoint).filter(ContactPoint.business_id == BIZ_A).all()
        assert len(points) == 4
        # Every imported value is tagged with the IMPORTED identity-evidence class, never VERIFIED.
        assert {p.evidence for p in points} == {"imported"}
        # The email was normalized (lower-cased); the mobile normalized to E.164.
        assert "asha.rao@buyer.example" in {p.value for p in points}
        assert "+919812345678" in {p.value for p in points}

        prov = PrivateContactImporter(session, BIZ_A).imports_for("crm-export-2026-q3")
        assert len(prov) == 3
        for row in prov:
            assert row.basis == "existing_customer"
            assert row.source_label == "acme_crm_export"
            assert row.purpose == "account_servicing"
            assert row.status == ContactImportStatus.IMPORTED.value
        # The subscribed flag was RECORDED (as a claim) but must never become consent.
        asha = next(r for r in prov if r.source_row_ref == "c-1")
        assert asha.asserted_subscribed is True

        # Importing is NOT recipient consent: no ConsentReceipt was ever fabricated.
        assert session.query(ConsentReceipt).count() == 0


# --- 2) a suppressed / restricted contact is excluded and only flagged -----------------------------


def test_suppressed_and_restricted_contacts_are_excluded_and_flagged(session_factory):
    with session_factory() as session:
        # The business restricts the MOBILE channel for all purposes (brand/purpose empty => applies).
        ProcessingRestrictionStore(session, BIZ_A).restrict(
            ProcessingRestrictionInput(channel="mobile"), RESTRICTOR, now=NOW
        )
        session.commit()

    entries = (
        SuppressionEntry(contact_point=SUPPRESSED_EMAIL, channel="email", reason="unsubscribe"),
    )
    rows = [
        {
            "row_ref": "s-1",
            "name": "Suppressed Sam",
            "email": SUPPRESSED_EMAIL,  # on the host suppression list -> excluded
            "subscribed": "true",  # a subscribed flag must NOT override suppression
        },
        {
            "row_ref": "s-2",
            "name": "Restricted Rita",
            "mobile": "9811111111",  # mobile channel is restricted -> excluded
        },
        {
            "row_ref": "s-3",
            "name": "Mixed Mohan",
            "email": "mohan@buyer.example",  # clean -> imported
            "mobile": "9822222222",  # restricted -> excluded
        },
    ]

    with session_factory() as session:
        importer = PrivateContactImporter(session, BIZ_A)
        result = importer.import_contacts(
            _manifest(dataset_id="suppressed-list"),
            rows,
            ACTOR,
            suppression=_snapshot(entries),
            now=NOW,
        )
        assert result.imported_count == 3  # a person is created for each (identity node)
        assert result.error_count == 0
        assert result.contacts_imported == 1  # only Mohan's clean email
        assert result.contacts_excluded == 3  # Sam email + Rita mobile + Mohan mobile
        session.commit()

    with session_factory() as session:
        points = session.query(ContactPoint).filter(ContactPoint.business_id == BIZ_A).all()
        # No contactable record for the suppressed email or any restricted mobile.
        assert {p.value for p in points} == {"mohan@buyer.example"}

        prov = {
            r.source_row_ref: r
            for r in PrivateContactImporter(session, BIZ_A).imports_for("suppressed-list")
        }
        sam = prov["s-1"]
        assert sam.status == ContactImportStatus.EXCLUDED.value
        assert sam.contacts_imported == 0 and sam.contacts_excluded == 1
        assert ["email", REASON_SUPPRESSED] in sam.exclusion_flags

        rita = prov["s-2"]
        assert rita.status == ContactImportStatus.EXCLUDED.value
        assert ["mobile", REASON_RESTRICTED] in rita.exclusion_flags

        mohan = prov["s-3"]
        assert mohan.status == ContactImportStatus.PARTIALLY_EXCLUDED.value
        assert mohan.contacts_imported == 1 and mohan.contacts_excluded == 1
        assert ["mobile", REASON_RESTRICTED] in mohan.exclusion_flags

        # Still no consent fabricated despite the subscribed flag on the suppressed row.
        assert session.query(ConsentReceipt).count() == 0


# --- 3) idempotent re-import does not duplicate ----------------------------------------------------


def test_idempotent_reimport_does_not_duplicate(session_factory):
    with session_factory() as session:
        PrivateContactImporter(session, BIZ_A).import_contacts(
            _manifest(), _rows(), ACTOR, suppression=_snapshot(), now=NOW
        )
        session.commit()

    with session_factory() as session:
        again = PrivateContactImporter(session, BIZ_A).import_contacts(
            _manifest(), _rows(), ACTOR, suppression=_snapshot(), now=NOW
        )
        assert again.imported_count == 0
        assert again.skipped_count == 3
        session.commit()

    with session_factory() as session:
        assert session.query(PersonObservation).count() == 3  # unchanged
        assert session.query(ContactPoint).count() == 4  # unchanged
        assert session.query(ContactImport).count() == 3  # unchanged


# --- 4) tenant isolation ---------------------------------------------------------------------------


def test_two_businesses_import_the_same_csv_independently(session_factory):
    with session_factory() as session:
        PrivateContactImporter(session, BIZ_A).import_contacts(
            _manifest(), _rows(), ACTOR, suppression=_snapshot(business_id=BIZ_A), now=NOW
        )
        PrivateContactImporter(session, BIZ_B).import_contacts(
            _manifest(), _rows(), ACTOR, suppression=_snapshot(business_id=BIZ_B), now=NOW
        )
        session.commit()

    with session_factory() as session:
        a = PrivateContactImporter(session, BIZ_A).imports_for("crm-export-2026-q3")
        b = PrivateContactImporter(session, BIZ_B).imports_for("crm-export-2026-q3")
        assert len(a) == 3 and len(b) == 3
        # The SAME row ref maps to a DIFFERENT, business-scoped person in each tenant.
        a_people = {r.source_row_ref: r.person_observation_id for r in a}
        b_people = {r.source_row_ref: r.person_observation_id for r in b}
        assert set(a_people) == set(b_people)
        assert all(a_people[ref] != b_people[ref] for ref in a_people)
        assert all(r.business_id == BIZ_A for r in a)
        assert all(r.business_id == BIZ_B for r in b)

        # B's importer resolves A's contact value to not-found (no global index on the value).
        store_b = PrivateContactImporter(session, BIZ_B)._contacts
        assert (
            store_b.resolve_contact("email", "asha.rao@buyer.example") is not None
        )  # B's own copy
        # A person id from A does not exist in B.
        assert store_b.get_person(a[0].person_observation_id) is None


# --- 5) malformed rows are refused per-row ---------------------------------------------------------


def test_malformed_rows_are_refused_per_row(session_factory):
    rows = [
        {"row_ref": "ok", "name": "Good Contact", "email": "good@buyer.example"},
        {"row_ref": "noname", "email": "x@buyer.example"},  # missing name
        {"row_ref": "nocontact", "name": "No Reach"},  # no contact column at all
        {"row_ref": "bademail", "name": "Bad Email", "email": "not-an-email"},  # only invalid value
    ]
    with session_factory() as session:
        importer = PrivateContactImporter(session, BIZ_A)
        result = importer.import_contacts(_manifest(dataset_id="malformed"), rows, ACTOR, now=NOW)
        assert result.imported_count == 1
        reasons = {e.row_ref: e.reason for e in result.row_errors}
        assert reasons == {
            "noname": REASON_ROW_MISSING_NAME,
            "nocontact": REASON_ROW_NO_CONTACT_VALUE,
            "bademail": REASON_ROW_NO_VALID_CONTACT,
        }
        session.commit()

    with session_factory() as session:
        # Only the one good row produced a person/contact/provenance; malformed rows left no state.
        assert session.query(PersonObservation).count() == 1
        assert session.query(ContactPoint).count() == 1
        assert session.query(ContactImport).count() == 1


def test_incomplete_manifest_is_rejected_before_any_state(session_factory):
    with session_factory() as session:
        importer = PrivateContactImporter(session, BIZ_A)
        with pytest.raises(PrivateContactImportError) as exc:
            importer.import_contacts(_manifest(basis=""), _rows(), ACTOR, now=NOW)
        assert exc.value.code == "manifest_missing_basis"
        with pytest.raises(PrivateContactImportError) as exc2:
            importer.import_contacts(_manifest(basis="totally-made-up"), _rows(), ACTOR, now=NOW)
        assert exc2.value.code == "manifest_unknown_basis"
        session.rollback()

    with session_factory() as session:
        assert session.query(PersonObservation).count() == 0
        assert session.query(ContactImport).count() == 0


# --- 6) Postgres-backed: the real transaction / RLS path ------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")


@pytest.mark.skipif(not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres")
def test_import_over_postgres():
    """Exercise the real import + idempotent re-import path on a disposable PostgreSQL with real
    transactions — never SQLite for the durable write path."""
    engine = create_engine(PG_URL, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        factory = make_session_factory(engine)
        with factory() as session:
            result = PrivateContactImporter(session, BIZ_A).import_contacts(
                _manifest(), _rows(), ACTOR, suppression=_snapshot(), now=NOW
            )
            assert result.imported_count == 3 and result.contacts_imported == 4
            session.commit()

        with factory() as session:
            again = PrivateContactImporter(session, BIZ_A).import_contacts(
                _manifest(), _rows(), ACTOR, suppression=_snapshot(), now=NOW
            )
            assert again.imported_count == 0 and again.skipped_count == 3
            session.commit()

        with factory() as session:
            assert session.query(PersonObservation).count() == 3
            assert session.query(ContactPoint).count() == 4
            assert session.query(ContactImport).count() == 3
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
