"""DCA-061 — require provenance on observed fields.

Pins the load-bearing behaviour of :mod:`app.services.field_observation` — the single sanctioned
writer of :class:`~app.db.models.FieldObservation`:

1. **An agent-written field missing required provenance is rejected, never silently canonicalized.**
   A directly OBSERVED field that names no source/version is refused (the central case), as is one
   missing the retrieval reference, observed timestamp, collection scope, or rights basis — each
   staging NO row. A complete observation (observed or inferred) persists with its full provenance.
2. **Provenance points at a real, reviewable source.** A ``source_key`` that is not catalogued, or a
   ``source_version`` that does not exist, is refused — an observed field can never cite a fabricated
   source.
3. **Scoped + a denial changes nothing + no host record.** Reads/writes are confined to one business
   (B can never read A's field observations; a cross-tenant subject reads as not-found), every refusal
   stages nothing, and recording a field creates no prospect/host record. Each successful create
   stages an atomic DCA-026 audit entry in the SAME unit of work whose references never carry the raw
   field value.

The store logic is unit-tested on in-memory SQLite; the real transaction / RLS path is exercised on a
disposable PostgreSQL (``DCA_TEST_DATABASE_URL``), never relying on SQLite for that. The Postgres test
skips when no URL is set. The RLS/migration paths for the table are covered by ``tests/test_rls.py``
and ``tests/test_migrations.py``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine

from app.audit.sink import ActorType, AuditActor
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import AuditEvent, FieldObservation, ProspectCandidate
from app.services.contact_point import ContactPointStore, PersonInput
from app.services.field_observation import (
    AUDIT_ACTION_RECORD_FIELD,
    REASON_INVALID_SCOPE,
    REASON_MISSING_FIELD_KEY,
    REASON_MISSING_FIELD_VALUE,
    REASON_MISSING_OBSERVED_AT,
    REASON_MISSING_PROVENANCE_REF,
    REASON_MISSING_RIGHTS_BASIS,
    REASON_MISSING_SCOPE,
    REASON_MISSING_SOURCE_REFERENCE,
    REASON_SOURCE_NOT_FOUND,
    REASON_SOURCE_VERSION_NOT_FOUND,
    REASON_SUBJECT_NOT_FOUND,
    REASON_UNKNOWN_INFERENCE_LABEL,
    REASON_UNKNOWN_SUBJECT_TYPE,
    FieldObservationError,
    FieldObservationInput,
    FieldObservationStore,
)
from app.services.org_observation import ObservationInput, OrgObservationStore
from app.services.source_catalog import SourceCatalogStore, SourceDraftInput
from app.services.source_terms import SourceTermsInput, SourceTermsStore

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"
ACTOR = AuditActor(id="00000000-0000-4000-9000-0000000a0061", type=ActorType.USER)
OBSERVED_AT = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _seed_source(session, key: str = "mca_master_data") -> tuple[str, int]:
    """Catalogue a (platform control-plane) source + one terms version so a field can cite real
    provenance. Returns ``(source_key, version)``."""
    SourceCatalogStore(session).create_draft(
        SourceDraftInput(
            key=key,
            kind="official",
            access_method="api",
            name="MCA Master Data",
            india_coverage={"nationwide": True},
        )
    )
    terms = SourceTermsStore(session).add_version(
        SourceTermsInput(
            source_key=key,
            operations={"collect": True, "store": True},
            purposes=["prospecting"],
            contract_evidence={"reference": "LIC-1", "kind": "licence", "uri": "file://lic"},
        )
    )
    return key, terms.version


def _seed_org_subject(session, business_id: str) -> str:
    """Record one organization observation and return its id (the field's subject)."""
    org = OrgObservationStore(session, business_id).create(
        ObservationInput(entity_type="legal_entity", display_name="Synthetic Textiles Pvt Ltd"),
        actor=ACTOR,
    )
    return org.id


def _complete_observed(subject_id: str, src_key: str, ver: int, **over) -> FieldObservationInput:
    base = dict(
        subject_type="organization",
        subject_id=subject_id,
        field_key="annual_revenue_inr",
        field_value="120000000",
        inference_label="observed",
        source_key=src_key,
        source_version=ver,
        provenance_ref="https://synthetic.registry.example/mca/record/42#revenue",
        observed_at=OBSERVED_AT,
        scope="nationwide",
        rights_basis="source_terms:collect+store;purpose=prospecting",
        retention_until=OBSERVED_AT + timedelta(days=365),
    )
    base.update(over)
    return FieldObservationInput(**base)  # type: ignore[arg-type]


# --- a complete observation persists with its full provenance -------------------------------------


def test_complete_observed_field_persists_with_provenance(session_factory):
    with session_factory() as session:
        src, ver = _seed_source(session)
        subject = _seed_org_subject(session, BIZ_A)
        store = FieldObservationStore(session, BIZ_A)
        obs = store.record(_complete_observed(subject, src, ver), actor=ACTOR)
        session.commit()
        obs_id = obs.id

    with session_factory() as session:
        store = FieldObservationStore(session, BIZ_A)
        row = store.get(obs_id)
        assert row is not None
        assert row.field_key == "annual_revenue_inr"
        assert row.field_value == "120000000"
        assert row.inference_label == "observed"
        assert row.source_key == "mca_master_data"
        assert row.source_version == ver
        assert row.provenance_ref.endswith("#revenue")
        assert row.observed_at is not None
        assert row.scope == "nationwide"
        assert row.rights_basis.startswith("source_terms:")
        assert row.retention_until is not None


def test_inferred_field_persists_without_a_source_but_with_provenance(session_factory):
    """An INFERRED field is derived, not fetched, so it may carry no source — but it still requires a
    derivation reference, observed timestamp, scope and rights basis, and is labelled inferred so it is
    never mistaken for an observed fact."""
    with session_factory() as session:
        subject = _seed_org_subject(session, BIZ_A)
        store = FieldObservationStore(session, BIZ_A)
        obs = store.record(
            FieldObservationInput(
                subject_type="organization",
                subject_id=subject,
                field_key="segment",
                field_value="mid_market",
                inference_label="inferred",
                source_key="",
                source_version=None,
                provenance_ref="derived_from:annual_revenue_inr+employee_count (rule v3)",
                observed_at=OBSERVED_AT,
                scope="KA",
                rights_basis="derived;purpose=scoring",
            ),
            actor=ACTOR,
        )
        session.commit()
        assert obs.inference_label == "inferred"
        assert obs.source_key == ""
        assert obs.source_version is None
        assert obs.scope == "KA"  # a recognized Indian state/UT normalizes through


# --- the central acceptance: a field missing required provenance is rejected, nothing staged -------


def test_observed_field_without_source_reference_is_rejected_and_stages_nothing(session_factory):
    with session_factory() as session:
        src, ver = _seed_source(session)
        subject = _seed_org_subject(session, BIZ_A)
        session.commit()

    with session_factory() as session:
        store = FieldObservationStore(session, BIZ_A)
        with pytest.raises(FieldObservationError) as exc:
            store.record(
                _complete_observed(subject, src, ver, source_key="", source_version=None),
                actor=ACTOR,
            )
        assert exc.value.code == REASON_MISSING_SOURCE_REFERENCE
        session.rollback()

    # The refused value was never canonicalized: no field-observation row, no audit entry for it.
    with session_factory() as session:
        assert session.query(FieldObservation).count() == 0
        assert (
            session.query(AuditEvent).filter(AuditEvent.action == AUDIT_ACTION_RECORD_FIELD).count()
            == 0
        )


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (dict(source_key="not_catalogued"), REASON_SOURCE_NOT_FOUND),
        (dict(source_version=99), REASON_SOURCE_VERSION_NOT_FOUND),
        (dict(provenance_ref="  "), REASON_MISSING_PROVENANCE_REF),
        (dict(observed_at=None), REASON_MISSING_OBSERVED_AT),
        (dict(scope=""), REASON_MISSING_SCOPE),
        (dict(scope="California"), REASON_INVALID_SCOPE),
        (dict(rights_basis=""), REASON_MISSING_RIGHTS_BASIS),
        (dict(field_key="   "), REASON_MISSING_FIELD_KEY),
        (dict(field_value=""), REASON_MISSING_FIELD_VALUE),
        (dict(inference_label="guessed"), REASON_UNKNOWN_INFERENCE_LABEL),
        (dict(subject_type="widget"), REASON_UNKNOWN_SUBJECT_TYPE),
    ],
)
def test_each_missing_or_invalid_provenance_piece_is_rejected(
    session_factory, mutation, expected_code
):
    with session_factory() as session:
        src, ver = _seed_source(session)
        subject = _seed_org_subject(session, BIZ_A)
        store = FieldObservationStore(session, BIZ_A)
        with pytest.raises(FieldObservationError) as exc:
            store.record(_complete_observed(subject, src, ver, **mutation), actor=ACTOR)
        assert exc.value.code == expected_code
        session.rollback()
    with session_factory() as session:
        assert session.query(FieldObservation).count() == 0


def test_subject_must_exist_in_this_business(session_factory):
    """A field cannot attach to an unknown subject, nor to another business's subject (not-found)."""
    with session_factory() as session:
        src, ver = _seed_source(session)
        subject_a = _seed_org_subject(session, BIZ_A)  # owned by A
        session.commit()

    with session_factory() as session:
        # Business B tries to attach a field to A's organization — reads as not-found, stages nothing.
        store_b = FieldObservationStore(session, BIZ_B)
        with pytest.raises(FieldObservationError) as exc:
            store_b.record(_complete_observed(subject_a, src, ver), actor=ACTOR)
        assert exc.value.code == REASON_SUBJECT_NOT_FOUND
        session.rollback()

    with session_factory() as session:
        assert session.query(FieldObservation).count() == 0


def test_person_subject_is_supported(session_factory):
    with session_factory() as session:
        src, ver = _seed_source(session)
        org = _seed_org_subject(session, BIZ_A)
        person = ContactPointStore(session, BIZ_A).create_person(
            PersonInput(display_name="Priya Sharma", org_observation_id=org), actor=ACTOR
        )
        store = FieldObservationStore(session, BIZ_A)
        obs = store.record(
            _complete_observed(
                person.id, src, ver, subject_type="person", field_key="job_title", field_value="CPO"
            ),
            actor=ACTOR,
        )
        session.commit()
        assert obs.subject_type == "person"
        assert len(store.for_subject("person", person.id)) == 1


def test_observations_are_append_only_history_not_deduplicated(session_factory):
    """The same field observed twice is TWO evidence rows — provenance history is preserved."""
    with session_factory() as session:
        src, ver = _seed_source(session)
        subject = _seed_org_subject(session, BIZ_A)
        store = FieldObservationStore(session, BIZ_A)
        store.record(
            _complete_observed(subject, src, ver, provenance_ref="ref-1", observed_at=OBSERVED_AT),
            actor=ACTOR,
        )
        store.record(
            _complete_observed(
                subject,
                src,
                ver,
                provenance_ref="ref-2",
                observed_at=OBSERVED_AT + timedelta(days=30),
            ),
            actor=ACTOR,
        )
        session.commit()
        history = store.for_subject("organization", subject)
        assert len(history) == 2
        assert {h.provenance_ref for h in history} == {"ref-1", "ref-2"}


def test_each_create_is_atomically_audited_and_never_carries_the_raw_value(session_factory):
    secret_value = "₹12,34,56,789 confidential"
    with session_factory() as session:
        src, ver = _seed_source(session)
        subject = _seed_org_subject(session, BIZ_A)
        store = FieldObservationStore(session, BIZ_A)
        store.record(_complete_observed(subject, src, ver, field_value=secret_value), actor=ACTOR)
        session.commit()

    with session_factory() as session:
        audits = (
            session.query(AuditEvent).filter(AuditEvent.action == AUDIT_ACTION_RECORD_FIELD).all()
        )
        assert len(audits) == 1
        blob = repr(audits[0].references) + audits[0].reason
        assert secret_value not in blob  # the raw field value is never audited
        assert audits[0].references["fieldKey"] == "annual_revenue_inr"
        assert audits[0].references["sourceKey"] == "mca_master_data"


def test_no_prospect_or_host_record_is_created(session_factory):
    with session_factory() as session:
        src, ver = _seed_source(session)
        subject = _seed_org_subject(session, BIZ_A)
        FieldObservationStore(session, BIZ_A).record(
            _complete_observed(subject, src, ver), actor=ACTOR
        )
        session.commit()
        assert session.query(ProspectCandidate).count() == 0


def test_tenant_isolation_b_cannot_read_a_field_observation(session_factory):
    with session_factory() as session:
        src, ver = _seed_source(session)
        subject = _seed_org_subject(session, BIZ_A)
        obs = FieldObservationStore(session, BIZ_A).record(
            _complete_observed(subject, src, ver), actor=ACTOR
        )
        session.commit()
        obs_id = obs.id

    with session_factory() as session:
        store_b = FieldObservationStore(session, BIZ_B)
        assert store_b.get(obs_id) is None  # cross-tenant id reads as not-found
        assert store_b.observations() == []


# --- Postgres-backed: the real transaction / RLS path ---------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")


@pytest.mark.skipif(not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres")
def test_record_and_reject_over_postgres():
    """Exercise the real record → read path AND the central rejection on a disposable PostgreSQL with
    real transactions — never SQLite for the durable path. A complete observed field persists; a field
    missing its source reference is refused and stages nothing (the transaction rolls back clean)."""
    engine = create_engine(PG_URL, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        factory = make_session_factory(engine)
        with factory() as session:
            src, ver = _seed_source(session)
            subject = _seed_org_subject(session, BIZ_A)
            obs = FieldObservationStore(session, BIZ_A).record(
                _complete_observed(subject, src, ver), actor=ACTOR
            )
            session.commit()
            obs_id, subject_id = obs.id, subject

        with factory() as session:
            store = FieldObservationStore(session, BIZ_A)
            assert store.get(obs_id) is not None
            assert len(store.for_subject("organization", subject_id)) == 1

        with factory() as session:
            store = FieldObservationStore(session, BIZ_A)
            with pytest.raises(FieldObservationError) as exc:
                store.record(
                    _complete_observed(subject_id, src, ver, source_key="", source_version=None),
                    actor=ACTOR,
                )
            assert exc.value.code == REASON_MISSING_SOURCE_REFERENCE
            session.rollback()

        with factory() as session:
            # The refusal left exactly the one committed observation — nothing was canonicalized.
            assert session.query(FieldObservation).count() == 1
    finally:
        Base.metadata.drop_all(engine)
