"""DCA-123 — import an approved organization dataset (the first concrete source adapter).

Pins the load-bearing behaviours of :mod:`app.sources.org_dataset_import` — the manifest-driven importer
that loads an APPROVED + entitled organization dataset into the DCA-059 org-observation store with
durable source provenance, without ever fetching:

1. **Approved import records org observations with provenance.** An approved+entitled source imports the
   dataset's legal entity + establishments (with their external ids) and records an
   :class:`~app.db.models.OrgObservationImport` row per observation carrying the source key, dataset id,
   source version, license and the checkpointable ``source_row_ref``.
2. **A denied / unapproved manifest imports nothing.** An unentitled source returns an un-authorized
   result (the DCA-048 preflight reason) and writes NO observation, provenance or audit row; an
   incomplete manifest is refused before any state is touched.
3. **Idempotent re-import does not duplicate.** Re-running the same import skips every already-imported
   row and creates nothing new.
4. **Organization-only.** A row carrying personal contact fields is refused; no person/contact row is
   ever written.
5. **Tenant isolation.** Two businesses importing the same dataset get independent, business-scoped
   observations; neither can see the other's import provenance.

The store/importer logic is unit-tested on in-memory SQLite; the real transaction / RLS path is
exercised on a disposable PostgreSQL (``DCA_TEST_DATABASE_URL``), never relying on SQLite for that. The
RLS/migration paths for the table are covered by ``tests/test_rls.py`` and ``tests/test_migrations.py``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine

from app.audit.sink import ActorType, AuditActor
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import (
    AuditEvent,
    ContactPoint,
    OrgExternalId,
    OrgObservation,
    OrgObservationImport,
    PersonObservation,
    Source,
    SourceStatus,
)
from app.services.org_observation import ExternalIdInput
from app.services.source_approval import (
    ReviewerPrincipal,
    SourceApprovalReview,
    SourceApprovalStore,
)
from app.services.source_catalog import SourceCatalogStore, SourceDraftInput
from app.services.source_entitlement import (
    BusinessEntitler,
    BusinessSourceEntitlementInput,
    BusinessSourceEntitlementStore,
)
from app.services.source_preflight import REASON_SOURCE_NOT_ENTITLED
from app.services.source_terms import SourceTermsInput, SourceTermsStore
from app.sources.org_dataset_import import (
    ORG_DATASET_IMPORT_CAPABILITY,
    REASON_MISSING_LICENSE,
    REASON_MISSING_SOURCE_VERSION,
    REASON_PERSONAL_FIELDS,
    DatasetManifest,
    DatasetRow,
    OrgDatasetImporter,
    OrgDatasetImportError,
)

SOURCE_KEY = "mca_master_data"
BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
ACTOR = AuditActor(id="00000000-0000-4000-9000-0000000a0123", type=ActorType.USER)

ADMIN = BusinessEntitler(subject="00000000-0000-4000-9000-0000000a0123", roles=("owner", "admin"))
OPERATOR = ReviewerPrincipal(subject="op-001", roles=("platform_operator",))


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def _manifest(**overrides) -> DatasetManifest:
    base = {
        "dataset_id": "mca-2026-q3",
        "source_key": SOURCE_KEY,
        "source_version": "2026-09-30",
        "license": "data.gov.in-GODL-1.0",
        "purpose": "prospecting",
    }
    base.update(overrides)
    return DatasetManifest(**base)


def _rows() -> list[DatasetRow]:
    """A small synthetic approved dataset: one legal entity + two state establishments."""
    return [
        DatasetRow(
            row_ref="le-1",
            entity_type="legal_entity",
            display_name="Synthetic Textiles Pvt Ltd",
            external_ids=(ExternalIdInput("cin", "U17120MH2026PTC900099"),),
        ),
        DatasetRow(
            row_ref="est-mh",
            entity_type="establishment",
            display_name="Synthetic Textiles — Maharashtra",
            parent_row_ref="le-1",
            state_code="MH",
            external_ids=(ExternalIdInput("gstin", "27AAACS0000A1Z5"),),
        ),
        DatasetRow(
            row_ref="est-ka",
            entity_type="establishment",
            display_name="Synthetic Textiles — Karnataka",
            parent_row_ref="le-1",
            state_code="KA",
            external_ids=(ExternalIdInput("gstin", "29AAACS0000A1Z1"),),
        ),
    ]


def _catalogue_approve_enable(session, *, source_key: str = SOURCE_KEY) -> None:
    """Catalogue (DCA-044), record terms (DCA-045), approve (DCA-046) and TEST-ONLY enable a source so
    it is the in-force platform ceiling for collect/prospecting. Enabling is synthetic: the running
    service never promotes a source to ENABLED (a disabled future capability)."""
    SourceCatalogStore(session).create_draft(
        SourceDraftInput(
            key=source_key,
            kind="official",
            access_method="bulk_download",
            trust_tier="official_registry",
            india_coverage={"nationwide": True},
        )
    )
    SourceTermsStore(session).add_version(
        SourceTermsInput(
            source_key=source_key,
            operations={"collect": True, "store": True},
            purposes=["prospecting"],
            contract_evidence={"reference": "MCA-TOS-2026", "kind": "terms_of_use"},
        )
    )
    SourceApprovalStore(session).approve(
        source_key,
        1,
        SourceApprovalReview(
            reviewer_id="op-001",
            evidence={"reference": "RIGHTS-REVIEW-2026-11", "kind": "legal_review"},
            reviewed_at=NOW - timedelta(days=1),
            expires_at=NOW + timedelta(days=365),
        ),
        OPERATOR,
    )
    src = session.query(Source).filter(Source.key == source_key).one()
    src.status = SourceStatus.ENABLED.value
    src.enabled = True


def _entitle(session, business_id: str, *, source_key: str = SOURCE_KEY) -> None:
    BusinessSourceEntitlementStore(session, business_id).set_entitlement(
        BusinessSourceEntitlementInput(
            source_key=source_key,
            enabled=True,
            operations={"collect": True},
            purposes=["prospecting"],
        ),
        ADMIN,
        now=NOW,
    )


# --- 1) approved import records observations + provenance ------------------------------------------


def test_approved_import_records_observations_with_provenance(session_factory):
    with session_factory() as session:
        _catalogue_approve_enable(session)
        _entitle(session, BIZ_A)
        session.commit()

    with session_factory() as session:
        importer = OrgDatasetImporter(session, BIZ_A)
        result = importer.import_dataset(_manifest(), _rows(), ACTOR, now=NOW)
        assert result.authorized is True
        assert result.reason is None
        assert result.imported_count == 3 and result.skipped_count == 0
        assert result.refused_rows == ()
        session.commit()

    with session_factory() as session:
        # Three organization observations, one legal entity + two establishments, with their externals.
        obs = session.query(OrgObservation).all()
        assert {o.entity_type for o in obs} == {"legal_entity", "establishment"}
        assert session.query(OrgExternalId).count() == 3  # 1 CIN + 2 GSTIN

        # Provenance: one row per observation, carrying the source attribution + checkpointable ref.
        importer = OrgDatasetImporter(session, BIZ_A)
        provenance = importer.imports_for("mca-2026-q3", SOURCE_KEY)
        assert len(provenance) == 3
        by_ref = {p.source_row_ref: p for p in provenance}
        assert set(by_ref) == {"le-1", "est-mh", "est-ka"}
        for p in provenance:
            assert p.source_key == SOURCE_KEY
            assert p.dataset_id == "mca-2026-q3"
            assert p.source_version == "2026-09-30"
            assert p.dataset_license == "data.gov.in-GODL-1.0"
            # The provenance points at a real observation in THIS business.
            assert session.query(OrgObservation).filter(OrgObservation.id == p.observation_id).one()


# --- 2) a denied / unapproved manifest imports nothing ---------------------------------------------


def test_unentitled_source_imports_nothing(session_factory):
    with session_factory() as session:
        # Approved at the platform level + enabled, but the business has NO entitlement -> denied.
        _catalogue_approve_enable(session)
        session.commit()

    with session_factory() as session:
        importer = OrgDatasetImporter(session, BIZ_A)
        result = importer.import_dataset(_manifest(), _rows(), ACTOR, now=NOW)
        assert result.authorized is False
        assert result.reason == REASON_SOURCE_NOT_ENTITLED
        assert result.imported == () and result.skipped == ()
        session.rollback()

    with session_factory() as session:
        # A denial changed nothing: no observation, no external id, no provenance, no audit.
        assert session.query(OrgObservation).count() == 0
        assert session.query(OrgExternalId).count() == 0
        assert session.query(OrgObservationImport).count() == 0
        assert session.query(AuditEvent).count() == 0


def test_incomplete_manifest_is_rejected_before_any_state(session_factory):
    with session_factory() as session:
        _catalogue_approve_enable(session)
        _entitle(session, BIZ_A)
        session.commit()

    with session_factory() as session:
        importer = OrgDatasetImporter(session, BIZ_A)
        with pytest.raises(OrgDatasetImportError) as exc:
            importer.import_dataset(_manifest(license=""), _rows(), ACTOR, now=NOW)
        assert exc.value.code == REASON_MISSING_LICENSE

        with pytest.raises(OrgDatasetImportError) as exc2:
            importer.import_dataset(_manifest(source_version="  "), _rows(), ACTOR, now=NOW)
        assert exc2.value.code == REASON_MISSING_SOURCE_VERSION
        session.rollback()

    with session_factory() as session:
        assert session.query(OrgObservation).count() == 0
        assert session.query(OrgObservationImport).count() == 0


# --- 3) idempotent re-import does not duplicate ----------------------------------------------------


def test_idempotent_reimport_does_not_duplicate(session_factory):
    with session_factory() as session:
        _catalogue_approve_enable(session)
        _entitle(session, BIZ_A)
        session.commit()

    with session_factory() as session:
        OrgDatasetImporter(session, BIZ_A).import_dataset(_manifest(), _rows(), ACTOR, now=NOW)
        session.commit()

    # Re-run the SAME import: every row is skipped, nothing new is created.
    with session_factory() as session:
        result = OrgDatasetImporter(session, BIZ_A).import_dataset(
            _manifest(), _rows(), ACTOR, now=NOW
        )
        assert result.authorized is True
        assert result.imported_count == 0
        assert result.skipped_count == 3
        session.commit()

    with session_factory() as session:
        assert session.query(OrgObservation).count() == 3  # unchanged
        assert session.query(OrgExternalId).count() == 3  # unchanged
        assert session.query(OrgObservationImport).count() == 3  # unchanged


# --- 4) organization-only: personal fields are refused, no person/contact row written --------------


def test_capability_is_organization_only():
    assert ORG_DATASET_IMPORT_CAPABILITY.is_organization_only is True
    assert ORG_DATASET_IMPORT_CAPABILITY.handles_personal_contacts is False


def test_personal_fields_are_refused_and_no_contact_row_written(session_factory):
    with session_factory() as session:
        _catalogue_approve_enable(session)
        _entitle(session, BIZ_A)
        session.commit()

    rows = [
        DatasetRow(
            row_ref="le-1",
            entity_type="legal_entity",
            display_name="Clean Org Pvt Ltd",
            external_ids=(ExternalIdInput("cin", "U17120MH2026PTC900042"),),
        ),
        # A row that smuggles in personal contact data — must be refused, never partially imported.
        DatasetRow(
            row_ref="le-2",
            entity_type="legal_entity",
            display_name="Leaky Org Pvt Ltd",
            contact={"email": "founder@leaky.example", "phone": "+919000000000"},
        ),
    ]

    with session_factory() as session:
        result = OrgDatasetImporter(session, BIZ_A).import_dataset(
            _manifest(), rows, ACTOR, now=NOW
        )
        assert result.authorized is True
        assert result.imported_count == 1  # only the clean org row
        assert result.refused_rows == (("le-2", REASON_PERSONAL_FIELDS),)
        session.commit()

    with session_factory() as session:
        # The org row imported; the personal row wrote nothing anywhere — and NO person/contact rows.
        assert session.query(OrgObservation).count() == 1
        assert session.query(OrgObservationImport).count() == 1
        assert session.query(PersonObservation).count() == 0
        assert session.query(ContactPoint).count() == 0


# --- 5) tenant isolation ---------------------------------------------------------------------------


def test_two_businesses_import_the_same_dataset_independently(session_factory):
    with session_factory() as session:
        _catalogue_approve_enable(session)
        _entitle(session, BIZ_A)
        _entitle(session, BIZ_B)
        session.commit()

    with session_factory() as session:
        OrgDatasetImporter(session, BIZ_A).import_dataset(_manifest(), _rows(), ACTOR, now=NOW)
        OrgDatasetImporter(session, BIZ_B).import_dataset(_manifest(), _rows(), ACTOR, now=NOW)
        session.commit()

    with session_factory() as session:
        a = OrgDatasetImporter(session, BIZ_A).imports_for("mca-2026-q3", SOURCE_KEY)
        b = OrgDatasetImporter(session, BIZ_B).imports_for("mca-2026-q3", SOURCE_KEY)
        assert len(a) == 3 and len(b) == 3
        # The SAME dataset row ref maps to a DIFFERENT, business-scoped observation in each tenant.
        a_obs = {p.source_row_ref: p.observation_id for p in a}
        b_obs = {p.source_row_ref: p.observation_id for p in b}
        assert set(a_obs) == set(b_obs)
        assert all(a_obs[ref] != b_obs[ref] for ref in a_obs)
        # Each provenance row is owned by its own business (never cross-tenant).
        assert all(p.business_id == BIZ_A for p in a)
        assert all(p.business_id == BIZ_B for p in b)


# --- 6) Postgres-backed: the real transaction / RLS path ------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")


@pytest.mark.skipif(not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres")
def test_import_over_postgres():
    """Exercise the real approved-import + idempotent re-import path on a disposable PostgreSQL with
    real transactions — never SQLite for the durable write path."""
    engine = create_engine(PG_URL, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        factory = make_session_factory(engine)
        with factory() as session:
            _catalogue_approve_enable(session)
            _entitle(session, BIZ_A)
            session.commit()

        with factory() as session:
            result = OrgDatasetImporter(session, BIZ_A).import_dataset(
                _manifest(), _rows(), ACTOR, now=NOW
            )
            assert result.imported_count == 3
            session.commit()

        with factory() as session:
            again = OrgDatasetImporter(session, BIZ_A).import_dataset(
                _manifest(), _rows(), ACTOR, now=NOW
            )
            assert again.imported_count == 0 and again.skipped_count == 3
            session.commit()

        with factory() as session:
            assert session.query(OrgObservation).count() == 3
            assert session.query(OrgObservationImport).count() == 3
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
