"""DCA-062 — store only PERMITTED evidence objects (license-aware retention, tenant-scoped access).

Pins the load-bearing behaviour of :mod:`app.services.evidence_store` — the single sanctioned writer
of :class:`~app.db.models.EvidenceObject`:

1. **A no-retention source leaves no raw content copy.** Evidence captured under terms that permit raw
   retention keeps the raw payload (``RAW_RETAINED``); evidence captured under a no-retention source —
   the ``store`` right withheld, a no-retention policy, or a zero-day window — keeps only the locator
   reference + a non-reversible digest (``REFERENCE_ONLY``) and NEVER writes the raw payload. The mode
   is derived from the cited terms, never from the caller.
2. **Another tenant cannot retrieve an evidence object by ID.** Reads/writes are confined to one
   business; a cross-tenant id reads as not-found.
3. **A denial changes nothing + no host record.** Every refusal stages no row; capturing evidence
   creates no prospect/host record; each successful capture stages an atomic DCA-026 audit entry whose
   references never carry the raw payload or locator.

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
from app.db.models import AuditEvent, EvidenceObject, ProspectCandidate
from app.services.contact_point import ContactPointStore, PersonInput
from app.services.evidence_store import (
    AUDIT_ACTION_STORE_EVIDENCE,
    RETENTION_REASON_NO_RETENTION_POLICY,
    RETENTION_REASON_STORE_NOT_PERMITTED,
    RETENTION_REASON_ZERO_WINDOW,
    REASON_MISSING_CAPTURED_AT,
    REASON_MISSING_CONTENT_KIND,
    REASON_MISSING_CONTENT_REF,
    REASON_MISSING_RAW_CONTENT,
    REASON_MISSING_RIGHTS_BASIS,
    REASON_MISSING_SOURCE_REFERENCE,
    REASON_SOURCE_NOT_FOUND,
    REASON_SOURCE_VERSION_NOT_FOUND,
    REASON_SUBJECT_NOT_FOUND,
    REASON_UNKNOWN_SUBJECT_TYPE,
    EvidenceInput,
    EvidenceObjectStore,
    EvidenceStoreError,
    _digest,
    decide_retention,
)
from app.services.org_observation import ObservationInput, OrgObservationStore
from app.services.source_catalog import SourceCatalogStore, SourceDraftInput
from app.services.source_terms import SourceTermsInput, SourceTermsStore

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"
ACTOR = AuditActor(id="00000000-0000-4000-9000-0000000a0062", type=ActorType.USER)
CAPTURED_AT = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
RAW = '{"cin": "U17110KA1990PTC011089", "revenue_inr": 120000000}'


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _seed_source(
    session,
    key: str,
    *,
    operations: dict,
    retention: dict | None = None,
    field_rights: dict | None = None,
) -> int:
    """Catalogue a (platform control-plane) source + one terms version with the given rights/retention.
    Returns the terms version."""
    SourceCatalogStore(session).create_draft(
        SourceDraftInput(
            key=key,
            kind="official",
            access_method="api",
            name="Synthetic Source",
            india_coverage={"nationwide": True},
        )
    )
    terms = SourceTermsStore(session).add_version(
        SourceTermsInput(
            source_key=key,
            operations=operations,
            field_rights=field_rights or {},
            purposes=["prospecting"],
            retention=retention or {},
            contract_evidence={"reference": "LIC-1", "kind": "licence", "uri": "file://lic"},
        )
    )
    return terms.version


def _input(src: str, ver: int, **over) -> EvidenceInput:
    base = dict(
        source_key=src,
        source_version=ver,
        content_kind="registry_record",
        content_ref="https://synthetic.registry.example/mca/record/42",
        raw_content=RAW,
        captured_at=CAPTURED_AT,
        rights_basis="source_terms:collect+store;purpose=prospecting",
        content_media_type="application/json",
    )
    base.update(over)
    return EvidenceInput(**base)  # type: ignore[arg-type]


# --- the headline: a no-retention source leaves no raw content copy -------------------------------


def test_permitted_source_retains_the_raw_payload(session_factory):
    with session_factory() as session:
        ver = _seed_source(
            session,
            "store_ok",
            operations={"collect": True, "store": True},
            retention={"max_days": 365, "policy": "retain"},
        )
        obj = EvidenceObjectStore(session, BIZ_A).store(_input("store_ok", ver), actor=ACTOR)
        session.commit()
        obj_id = obj.id

    with session_factory() as session:
        row = EvidenceObjectStore(session, BIZ_A).get(obj_id)
        assert row is not None
        assert row.retention_mode == "raw_retained"
        assert row.retention_reason == ""
        assert row.raw_content == RAW  # raw payload kept — the licence permits it
        assert row.content_ref  # reference always kept
        assert row.content_digest == _digest(RAW)  # digest always kept
        # The retention window is honoured: captured_at + max_days (SQLite returns naive datetimes).
        expected = CAPTURED_AT + timedelta(days=365)
        assert row.retention_until.replace(tzinfo=None) == expected.replace(tzinfo=None)


@pytest.mark.parametrize(
    ("operations", "retention", "expected_reason"),
    [
        # store right withheld
        (
            {"collect": True, "store": False},
            {"max_days": 365},
            RETENTION_REASON_STORE_NOT_PERMITTED,
        ),
        # store granted but a no-retention policy
        (
            {"collect": True, "store": True},
            {"policy": "no_retention"},
            RETENTION_REASON_NO_RETENTION_POLICY,
        ),
        # store granted but a zero-day window
        (
            {"collect": True, "store": True},
            {"max_days": 0, "policy": "retain"},
            RETENTION_REASON_ZERO_WINDOW,
        ),
    ],
)
def test_no_retention_source_keeps_only_a_reference_not_raw(
    session_factory, operations, retention, expected_reason
):
    with session_factory() as session:
        ver = _seed_source(session, "no_raw", operations=operations, retention=retention)
        obj = EvidenceObjectStore(session, BIZ_A).store(_input("no_raw", ver), actor=ACTOR)
        session.commit()
        obj_id = obj.id

    with session_factory() as session:
        row = EvidenceObjectStore(session, BIZ_A).get(obj_id)
        assert row is not None
        assert row.retention_mode == "reference_only"
        assert row.retention_reason == expected_reason
        assert row.raw_content is None  # NO raw content copy — the headline acceptance
        assert row.content_ref  # the locator reference IS preserved
        assert row.content_digest == _digest(RAW)  # a non-reversible digest IS preserved
        assert row.retention_until is None


def test_per_field_override_can_make_one_kind_reference_only(session_factory):
    """A field override wins over the source-wide default: a source-wide store grant can be withheld
    for a specific content kind, making just that kind reference-only."""
    with session_factory() as session:
        ver = _seed_source(
            session,
            "mixed",
            operations={"collect": True, "store": True},
            retention={"max_days": 90},
            field_rights={"sensitive_doc": {"store": False}},
        )
        store = EvidenceObjectStore(session, BIZ_A)
        kept = store.store(_input("mixed", ver, content_kind="registry_record"), actor=ACTOR)
        dropped = store.store(
            _input("mixed", ver, content_kind="sensitive_doc", content_ref="s3://x/doc"),
            actor=ACTOR,
        )
        session.commit()
        assert kept.retention_mode == "raw_retained"
        assert dropped.retention_mode == "reference_only"
        assert dropped.raw_content is None
        assert dropped.retention_reason == RETENTION_REASON_STORE_NOT_PERMITTED


def test_decide_retention_is_pure(session_factory):
    """The retention screen is a pure, side-effect-free read of the terms."""
    with session_factory() as session:
        ver = _seed_source(
            session,
            "pure",
            operations={"store": True},
            retention={"max_days": 30, "policy": "retain"},
        )
        terms = SourceTermsStore(session).get_version("pure", ver)
        decision = decide_retention(terms, "registry_record")
        assert decision.raw_permitted is True
        assert decision.mode == "raw_retained"
        assert decision.max_days == 30


# --- tenant isolation: another tenant cannot retrieve an evidence object by ID --------------------


def test_tenant_isolation_b_cannot_read_a_evidence_object(session_factory):
    with session_factory() as session:
        ver = _seed_source(session, "iso", operations={"store": True}, retention={"max_days": 10})
        obj = EvidenceObjectStore(session, BIZ_A).store(_input("iso", ver), actor=ACTOR)
        session.commit()
        obj_id = obj.id

    with session_factory() as session:
        store_b = EvidenceObjectStore(session, BIZ_B)
        assert store_b.get(obj_id) is None  # cross-tenant id reads as not-found
        assert store_b.objects() == []


def test_cross_tenant_subject_is_rejected_and_stages_nothing(session_factory):
    """A capture cannot link to another business's subject — it reads as not-found, stages nothing."""
    with session_factory() as session:
        ver = _seed_source(session, "subj", operations={"store": True})
        org_a = OrgObservationStore(session, BIZ_A).create(
            ObservationInput(entity_type="legal_entity", display_name="A Co"), actor=ACTOR
        )
        session.commit()
        a_org_id = org_a.id

    with session_factory() as session:
        store_b = EvidenceObjectStore(session, BIZ_B)
        with pytest.raises(EvidenceStoreError) as exc:
            store_b.store(
                _input("subj", ver, subject_type="organization", subject_id=a_org_id), actor=ACTOR
            )
        assert exc.value.code == REASON_SUBJECT_NOT_FOUND
        session.rollback()

    with session_factory() as session:
        assert session.query(EvidenceObject).count() == 0


def test_subject_linkage_within_business_is_supported(session_factory):
    with session_factory() as session:
        ver = _seed_source(session, "link", operations={"store": True})
        org = OrgObservationStore(session, BIZ_A).create(
            ObservationInput(entity_type="legal_entity", display_name="A Co"), actor=ACTOR
        )
        person = ContactPointStore(session, BIZ_A).create_person(
            PersonInput(display_name="Priya Sharma", org_observation_id=org.id), actor=ACTOR
        )
        store = EvidenceObjectStore(session, BIZ_A)
        obj = store.store(
            _input("link", ver, subject_type="person", subject_id=person.id), actor=ACTOR
        )
        session.commit()
        assert obj.subject_type == "person"
        assert len(store.for_subject("person", person.id)) == 1


# --- denials change nothing -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (dict(source_key=""), REASON_MISSING_SOURCE_REFERENCE),
        (dict(source_key="not_catalogued"), REASON_SOURCE_NOT_FOUND),
        (dict(source_version=99), REASON_SOURCE_VERSION_NOT_FOUND),
        (dict(content_kind="  "), REASON_MISSING_CONTENT_KIND),
        (dict(content_ref=""), REASON_MISSING_CONTENT_REF),
        (dict(raw_content="   "), REASON_MISSING_RAW_CONTENT),
        (dict(captured_at=None), REASON_MISSING_CAPTURED_AT),
        (dict(rights_basis=""), REASON_MISSING_RIGHTS_BASIS),
        (dict(subject_type="widget", subject_id="x"), REASON_UNKNOWN_SUBJECT_TYPE),
        (dict(subject_type="organization", subject_id="unknown"), REASON_SUBJECT_NOT_FOUND),
    ],
)
def test_each_missing_or_invalid_piece_is_rejected(session_factory, mutation, expected_code):
    with session_factory() as session:
        ver = _seed_source(session, "deny", operations={"store": True})
        store = EvidenceObjectStore(session, BIZ_A)
        with pytest.raises(EvidenceStoreError) as exc:
            store.store(_input("deny", ver, **mutation), actor=ACTOR)
        assert exc.value.code == expected_code
        session.rollback()
    with session_factory() as session:
        assert session.query(EvidenceObject).count() == 0


# --- atomic audit without the raw payload, and no host record -------------------------------------


def test_each_store_is_atomically_audited_without_the_raw_payload(session_factory):
    with session_factory() as session:
        ver = _seed_source(session, "aud", operations={"store": True}, retention={"max_days": 5})
        EvidenceObjectStore(session, BIZ_A).store(_input("aud", ver), actor=ACTOR)
        session.commit()

    with session_factory() as session:
        audits = (
            session.query(AuditEvent).filter(AuditEvent.action == AUDIT_ACTION_STORE_EVIDENCE).all()
        )
        assert len(audits) == 1
        blob = repr(audits[0].references) + audits[0].reason
        assert RAW not in blob  # the raw payload is never audited
        # the locator is not audited either; the safe digest IS present
        assert "synthetic.registry.example" not in blob
        assert audits[0].references["contentDigest"] == _digest(RAW)
        assert audits[0].references["retentionMode"] == "raw_retained"


def test_no_prospect_or_host_record_is_created(session_factory):
    with session_factory() as session:
        ver = _seed_source(session, "nohost", operations={"store": True})
        EvidenceObjectStore(session, BIZ_A).store(_input("nohost", ver), actor=ACTOR)
        session.commit()
        assert session.query(ProspectCandidate).count() == 0


# --- Postgres-backed: the real transaction / RLS path ---------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")


@pytest.mark.skipif(not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres")
def test_store_and_reference_only_over_postgres():
    """Exercise the real store/read path AND the license-aware retention on a disposable PostgreSQL
    with real transactions — never SQLite for the durable path. A permitted source keeps the raw
    payload; a no-retention source keeps only a reference, and a cross-tenant id reads as not-found."""
    engine = create_engine(PG_URL, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        factory = make_session_factory(engine)
        with factory() as session:
            raw_ver = _seed_source(
                session,
                "pg_raw",
                operations={"store": True},
                retention={"max_days": 30, "policy": "retain"},
            )
            ref_ver = _seed_source(
                session, "pg_ref", operations={"store": True}, retention={"policy": "no_retention"}
            )
            store = EvidenceObjectStore(session, BIZ_A)
            raw_obj = store.store(_input("pg_raw", raw_ver), actor=ACTOR)
            ref_obj = store.store(_input("pg_ref", ref_ver), actor=ACTOR)
            session.commit()
            raw_id, ref_id = raw_obj.id, ref_obj.id

        with factory() as session:
            store = EvidenceObjectStore(session, BIZ_A)
            assert store.get(raw_id).raw_content == RAW
            ref_row = store.get(ref_id)
            assert ref_row.raw_content is None  # no raw copy for a no-retention source
            assert ref_row.content_digest == _digest(RAW)  # but the reference/digest survive

        with factory() as session:
            # Another tenant cannot retrieve either evidence object by id.
            store_b = EvidenceObjectStore(session, BIZ_B)
            assert store_b.get(raw_id) is None
            assert store_b.get(ref_id) is None
    finally:
        Base.metadata.drop_all(engine)
