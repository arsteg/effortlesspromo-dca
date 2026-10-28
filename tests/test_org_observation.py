"""DCA-059 — store DCA organization observations.

Pins the load-bearing behaviours of :mod:`app.services.org_observation` — the single sanctioned writer
of :class:`~app.db.models.OrgObservation` and its separate :class:`~app.db.models.OrgExternalId`
mapping:

1. **Legal-entity vs establishment distinctions.** Two establishments can hang off one legal entity and
   get DISTINCT identifiers (their own observation ids AND their own GSTIN mappings); a legal entity may
   not name a parent, an establishment must, and an establishment can only hang off a legal entity.
2. **No global visibility.** Reads/writes are confined to one business; a cross-tenant parent reads as
   not-found (never links), the SAME external id may be recorded by two businesses, and Business B can
   never read Business A's observations or resolve its external ids.
3. **A denial changes nothing + no host record.** Every refusal stages no observation/mapping row, and
   recording an identity creates no prospect/host record. Each successful create stages an atomic
   DCA-026 audit entry in the SAME unit of work.

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
    DiscoveryRun,
    OrgExternalId,
    OrgObservation,
    ProspectCandidate,
)
from app.services.org_observation import (
    AUDIT_ACTION_RECORD_OBSERVATION,
    REASON_DUPLICATE_EXTERNAL_ID,
    REASON_ESTABLISHMENT_REQUIRES_PARENT,
    REASON_INVALID_STATE_CODE,
    REASON_LEGAL_ENTITY_HAS_PARENT,
    REASON_MISSING_EXTERNAL_VALUE,
    REASON_MISSING_NAME,
    REASON_PARENT_NOT_FOUND,
    REASON_PARENT_NOT_LEGAL_ENTITY,
    REASON_UNKNOWN_ENTITY_TYPE,
    REASON_UNKNOWN_EXTERNAL_SCHEME,
    ExternalIdInput,
    ObservationInput,
    OrgObservationError,
    OrgObservationStore,
)

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"
ACTOR = AuditActor(id="00000000-0000-4000-9000-0000000a0059", type=ActorType.USER)


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _legal_entity(name: str, **extra) -> ObservationInput:
    return ObservationInput(entity_type="legal_entity", display_name=name, **extra)


def _establishment(parent_id: str, name: str, **extra) -> ObservationInput:
    return ObservationInput(
        entity_type="establishment", display_name=name, parent_observation_id=parent_id, **extra
    )


# --- legal entity vs establishment: distinct establishments under one legal entity -----------------


def test_two_establishments_under_one_legal_entity_have_distinct_identifiers(session_factory):
    with session_factory() as session:
        store = OrgObservationStore(session, BIZ_A)
        parent = store.create(
            _legal_entity(
                "Synthetic Textiles Pvt Ltd",
                external_ids=[ExternalIdInput("cin", "U17120MH2026PTC900099")],
            ),
            actor=ACTOR,
        )
        parent_id = parent.id
        est_mh = store.create(
            _establishment(
                parent_id,
                "Synthetic Textiles — Maharashtra",
                state_code="MH",
                external_ids=[ExternalIdInput("gstin", "27AAACS0000A1Z5")],
            ),
            actor=ACTOR,
        )
        est_ka = store.create(
            _establishment(
                parent_id,
                "Synthetic Textiles — Karnataka",
                state_code="KA",
                external_ids=[ExternalIdInput("gstin", "29AAACS0000A1Z1")],
            ),
            actor=ACTOR,
        )
        session.commit()
        mh_id, ka_id = est_mh.id, est_ka.id

    with session_factory() as session:
        store = OrgObservationStore(session, BIZ_A)
        parent = store.get(parent_id)
        assert parent is not None and parent.entity_type == "legal_entity"
        assert parent.parent_observation_id is None

        establishments = store.establishments_of(parent_id)
        assert {e.id for e in establishments} == {mh_id, ka_id}
        # Distinct identities and distinct states — never conflated.
        assert mh_id != ka_id
        assert {e.state_code for e in establishments} == {"MH", "KA"}
        assert all(e.parent_observation_id == parent_id for e in establishments)

        # Distinct external GSTIN mappings, each resolving back to its own establishment.
        mh_gstins = [x.value for x in store.external_ids_of(mh_id)]
        ka_gstins = [x.value for x in store.external_ids_of(ka_id)]
        assert mh_gstins == ["27AAACS0000A1Z5"]
        assert ka_gstins == ["29AAACS0000A1Z1"]
        assert store.resolve_external("gstin", "27AAACS0000A1Z5").id == mh_id
        assert (
            store.resolve_external("gstin", "29aaacs0000a1z1").id == ka_id
        )  # case-insensitive value
        # The legal entity carries its CIN; resolving it lands on the parent.
        assert store.resolve_external("cin", "U17120MH2026PTC900099").id == parent_id


def test_recording_an_observation_creates_no_host_or_prospect_record(session_factory):
    with session_factory() as session:
        store = OrgObservationStore(session, BIZ_A)
        store.create(_legal_entity("Acme Synthetic Pvt Ltd"), actor=ACTOR)
        session.commit()

    with session_factory() as session:
        assert session.query(OrgObservation).count() == 1
        assert session.query(ProspectCandidate).count() == 0  # no host/prospect record
        assert session.query(DiscoveryRun).count() == 0


# --- shape refusals: each stages no row ------------------------------------------------------------


def _assert_no_rows(session_factory):
    with session_factory() as session:
        assert session.query(OrgObservation).count() == 0
        assert session.query(OrgExternalId).count() == 0
        assert session.query(AuditEvent).count() == 0


def test_legal_entity_with_a_parent_is_refused(session_factory):
    with session_factory() as session:
        store = OrgObservationStore(session, BIZ_A)
        with pytest.raises(OrgObservationError) as exc:
            store.create(
                ObservationInput(
                    entity_type="legal_entity",
                    display_name="Bad LE",
                    parent_observation_id="some-parent",
                ),
                actor=ACTOR,
            )
        assert exc.value.code == REASON_LEGAL_ENTITY_HAS_PARENT
        session.rollback()
    _assert_no_rows(session_factory)


def test_establishment_without_a_parent_is_refused(session_factory):
    with session_factory() as session:
        store = OrgObservationStore(session, BIZ_A)
        with pytest.raises(OrgObservationError) as exc:
            store.create(
                ObservationInput(entity_type="establishment", display_name="Orphan branch"),
                actor=ACTOR,
            )
        assert exc.value.code == REASON_ESTABLISHMENT_REQUIRES_PARENT
        session.rollback()
    _assert_no_rows(session_factory)


def test_establishment_cannot_hang_off_a_non_legal_entity(session_factory):
    with session_factory() as session:
        store = OrgObservationStore(session, BIZ_A)
        parent = store.create(_legal_entity("Parent LE"), actor=ACTOR)
        child = store.create(_establishment(parent.id, "Child establishment"), actor=ACTOR)
        session.commit()
        child_id = child.id
    with session_factory() as session:
        store = OrgObservationStore(session, BIZ_A)
        with pytest.raises(OrgObservationError) as exc:
            store.create(_establishment(child_id, "Grandchild"), actor=ACTOR)
        assert exc.value.code == REASON_PARENT_NOT_LEGAL_ENTITY
        session.rollback()
    # Only the legal entity + its one establishment exist — the refused grandchild staged nothing.
    with session_factory() as session:
        assert session.query(OrgObservation).count() == 2


def test_unknown_entity_type_name_state_and_scheme_are_refused(session_factory):
    with session_factory() as session:
        store = OrgObservationStore(session, BIZ_A)
        with pytest.raises(OrgObservationError) as exc:
            store.create(ObservationInput(entity_type="person", display_name="x"), actor=ACTOR)
        assert exc.value.code == REASON_UNKNOWN_ENTITY_TYPE

        with pytest.raises(OrgObservationError) as exc:
            store.create(_legal_entity("   "), actor=ACTOR)
        assert exc.value.code == REASON_MISSING_NAME

        with pytest.raises(OrgObservationError) as exc:
            store.create(_legal_entity("LE", state_code="ZZ"), actor=ACTOR)
        assert exc.value.code == REASON_INVALID_STATE_CODE

        with pytest.raises(OrgObservationError) as exc:
            store.create(
                _legal_entity("LE", external_ids=[ExternalIdInput("linkedin", "acme")]),
                actor=ACTOR,
            )
        assert exc.value.code == REASON_UNKNOWN_EXTERNAL_SCHEME

        with pytest.raises(OrgObservationError) as exc:
            store.create(
                _legal_entity("LE", external_ids=[ExternalIdInput("cin", "  ")]), actor=ACTOR
            )
        assert exc.value.code == REASON_MISSING_EXTERNAL_VALUE
        session.rollback()
    _assert_no_rows(session_factory)


# --- no global visibility --------------------------------------------------------------------------


def test_establishment_cannot_link_to_another_businesss_parent(session_factory):
    with session_factory() as session:
        a_parent = OrgObservationStore(session, BIZ_A).create(_legal_entity("A LE"), actor=ACTOR)
        session.commit()
        a_parent_id = a_parent.id

    # Business B tries to hang an establishment off Business A's legal entity -> not found (no link).
    with session_factory() as session:
        store_b = OrgObservationStore(session, BIZ_B)
        with pytest.raises(OrgObservationError) as exc:
            store_b.create(_establishment(a_parent_id, "B branch on A parent"), actor=ACTOR)
        assert exc.value.code == REASON_PARENT_NOT_FOUND
        session.rollback()

    with session_factory() as session:
        # B created nothing; A still has exactly its one legal entity.
        assert OrgObservationStore(session, BIZ_B).observations() == []
        assert len(OrgObservationStore(session, BIZ_A).observations()) == 1


def test_the_same_external_id_may_be_recorded_by_two_businesses(session_factory):
    gstin = "27AAACS0000A1Z5"
    with session_factory() as session:
        a = OrgObservationStore(session, BIZ_A)
        a_le = a.create(_legal_entity("A LE"), actor=ACTOR)
        a.create(
            _establishment(
                a_le.id, "A MH", state_code="MH", external_ids=[ExternalIdInput("gstin", gstin)]
            ),
            actor=ACTOR,
        )
        b = OrgObservationStore(session, BIZ_B)
        b_le = b.create(_legal_entity("B LE"), actor=ACTOR)
        b.create(
            _establishment(
                b_le.id, "B MH", state_code="MH", external_ids=[ExternalIdInput("gstin", gstin)]
            ),
            actor=ACTOR,
        )
        session.commit()

    with session_factory() as session:
        a_hit = OrgObservationStore(session, BIZ_A).resolve_external("gstin", gstin)
        b_hit = OrgObservationStore(session, BIZ_B).resolve_external("gstin", gstin)
        assert a_hit is not None and b_hit is not None
        # The same external id resolves to a DIFFERENT, business-scoped observation in each tenant.
        assert a_hit.id != b_hit.id
        assert a_hit.business_id == BIZ_A and b_hit.business_id == BIZ_B


def test_duplicate_external_id_within_one_business_is_refused(session_factory):
    gstin = "27AAACS0000A1Z5"
    with session_factory() as session:
        store = OrgObservationStore(session, BIZ_A)
        le = store.create(_legal_entity("LE"), actor=ACTOR)
        store.create(
            _establishment(
                le.id, "MH", state_code="MH", external_ids=[ExternalIdInput("gstin", gstin)]
            ),
            actor=ACTOR,
        )
        session.commit()
        le_id = le.id

    with session_factory() as session:
        store = OrgObservationStore(session, BIZ_A)
        with pytest.raises(OrgObservationError) as exc:
            store.create(
                _establishment(
                    le_id,
                    "MH duplicate",
                    state_code="MH",
                    external_ids=[ExternalIdInput("gstin", gstin)],
                ),
                actor=ACTOR,
            )
        assert exc.value.code == REASON_DUPLICATE_EXTERNAL_ID
        session.rollback()

    with session_factory() as session:
        # The duplicate staged nothing: still one legal entity + one establishment + one mapping.
        assert session.query(OrgObservation).count() == 2
        assert session.query(OrgExternalId).count() == 1


def test_business_b_cannot_read_business_as_observation(session_factory):
    with session_factory() as session:
        a_le = OrgObservationStore(session, BIZ_A).create(_legal_entity("A LE"), actor=ACTOR)
        session.commit()
        a_id = a_le.id
    with session_factory() as session:
        assert (
            OrgObservationStore(session, BIZ_B).get(a_id) is None
        )  # not-found, never cross-tenant
        assert OrgObservationStore(session, BIZ_A).get(a_id) is not None


# --- atomic audit ----------------------------------------------------------------------------------


def test_create_stages_an_atomic_audit_entry_in_the_same_unit_of_work(session_factory):
    with session_factory() as session:
        OrgObservationStore(session, BIZ_A).create(
            _legal_entity("LE", external_ids=[ExternalIdInput("cin", "U17120MH2026PTC900099")]),
            actor=ACTOR,
        )
        session.commit()
    with session_factory() as session:
        audits = session.query(AuditEvent).all()
        assert len(audits) == 1
        entry = audits[0]
        assert entry.action == AUDIT_ACTION_RECORD_OBSERVATION
        assert entry.business_id == BIZ_A
        assert entry.references["entityType"] == "legal_entity"
        assert entry.references["externalSchemes"] == ["cin"]
        assert entry.references["externalIdCount"] == 1


# --- Postgres-backed: the real transaction / RLS path ---------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")


@pytest.mark.skipif(not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres")
def test_create_and_reads_over_postgres():
    """Exercise the real create → read path on a disposable PostgreSQL with real transactions — never
    SQLite for the durable read/write path. Two establishments under one legal entity get distinct
    ids + GSTINs; a cross-tenant parent is refused and stages nothing."""
    engine = create_engine(PG_URL, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        factory = make_session_factory(engine)
        with factory() as session:
            store = OrgObservationStore(session, BIZ_A)
            le = store.create(
                _legal_entity(
                    "PG LE", external_ids=[ExternalIdInput("cin", "U17120MH2026PTC900099")]
                ),
                actor=ACTOR,
            )
            mh = store.create(
                _establishment(
                    le.id,
                    "PG MH",
                    state_code="MH",
                    external_ids=[ExternalIdInput("gstin", "27AAACS0000A1Z5")],
                ),
                actor=ACTOR,
            )
            ka = store.create(
                _establishment(
                    le.id,
                    "PG KA",
                    state_code="KA",
                    external_ids=[ExternalIdInput("gstin", "29AAACS0000A1Z1")],
                ),
                actor=ACTOR,
            )
            session.commit()
            le_id, mh_id, ka_id = le.id, mh.id, ka.id

        with factory() as session:
            store = OrgObservationStore(session, BIZ_A)
            assert {e.id for e in store.establishments_of(le_id)} == {mh_id, ka_id}
            assert store.resolve_external("gstin", "27AAACS0000A1Z5").id == mh_id
            assert store.resolve_external("gstin", "29AAACS0000A1Z1").id == ka_id

        with factory() as session:
            store_b = OrgObservationStore(session, BIZ_B)
            with pytest.raises(OrgObservationError) as exc:
                store_b.create(_establishment(le_id, "B on A"), actor=ACTOR)
            assert exc.value.code == REASON_PARENT_NOT_FOUND
            session.rollback()
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
