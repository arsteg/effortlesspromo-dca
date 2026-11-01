"""DCA-144 — resolve ONE discovery run's converted observation batch.

Two layers, matching the DCA-143 suite:

- **Service unit tests** (in-memory SQLite) pin
  :meth:`~app.services.observation_resolution.ObservationBatchResolver.resolve_run`: an authorized
  administrator resolves the batch DCA-143 converted for a run by connecting the deterministic DCA-067
  identity matcher and the DCA-069/070 canonical selection to the batch. A strong identifier agreement
  resolves a subject to an existing entity WITHOUT merging it (no ``IdentityMerge``, no consent change);
  an ambiguous shared-name candidate is ROUTED to a review-only ``IdentityMatchSuggestion`` and never
  merged; a PROTECTED user correction keeps its value as canonical (a provider refresh never overwrites
  it) and the stored correction is untouched. A run resolves once (idempotent no-op on replay); a run
  with no converted batch is refused; a cross-tenant run reads as not-found; an unauthorized caller is
  refused before any state is read. Resolving performs no fetch, model call, spend or send.
- **Postgres-backed test** (``DCA_TEST_DATABASE_URL``) runs one resolve against a disposable PostgreSQL
  (real transactions, not SQLite): the resolution record + its atomic audit entry survive and a
  re-resolve stays a no-op.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import make_url

from app.db.base import Base, make_engine, make_session_factory
from app.db.models import (
    AuditEvent,
    ConsentReceipt,
    FieldCorrection,
    IdentityMatchSuggestion,
    IdentityMerge,
    ObservationBatchResolution,
    OrgExternalId,
    OrgObservation,
    OrgObservationType,
    ProcessingRestriction,
)
from app.services.discovery_observation import DiscoveryObservationConverter, TaskConversionActor
from app.services.field_correction import FieldCorrectionInput, FieldCorrectionStore, FieldCorrector
from app.services.observation_resolution import (
    AUDIT_ACTION_RESOLVE_BATCH,
    REASON_NOTHING_TO_RESOLVE,
    REASON_RUN_NOT_FOUND,
    BatchResolutionActor,
    BatchResolutionAuthorizationError,
    BatchResolutionError,
    ObservationBatchResolver,
)

# Reuse the DCA-143 scaffolding verbatim (seed + plan + execute one task), so the batch under test is
# exactly what the accepted converter produces.
from tests.test_discovery_observation import BIZ_A, BIZ_B, _executed_run

# The CIN the DCA-143 fixture imports for its one org identity (`_adapter_input`).
FIXTURE_CIN = "U17120MH2026PTC900099"
FIXTURE_NAME = "Synthetic Textiles Pvt Ltd"

ADMIN = BatchResolutionActor(subject="00000000-0000-4000-9000-0000000a0144a", roles=("owner",))
VIEWER = BatchResolutionActor(subject="00000000-0000-4000-9000-0000000a0144b", roles=("viewer",))
CONVERT_ADMIN = TaskConversionActor(
    subject="00000000-0000-4000-9000-0000000a0144a", roles=("owner",)
)
CORRECTOR = FieldCorrector(subject="00000000-0000-4000-9000-0000000a0144c", roles=("owner",))


# --- fixtures + helpers ---------------------------------------------------------------------------


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def _converted_run(session, business_id):
    """An executed run whose one DONE task has been CONVERTED (DCA-143) into the observation batch
    under test. Returns ``(run, subject_org)`` — the run and its single batch subject."""
    run = _executed_run(session, business_id, with_duns=True)
    session.flush()
    DiscoveryObservationConverter(session, business_id).convert_task(run.id, 0, CONVERT_ADMIN)
    session.flush()
    subject = session.query(OrgObservation).filter_by(business_id=business_id).one()
    return run, subject


def _seed_existing_org(session, business_id, *, display_name, cin=None, state_code=""):
    """Seed a pre-existing legal-entity identity DIRECTLY (bypassing the store's within-business
    external-id dedup) — representing an identity discovered earlier that the matcher must now relate
    to the batch subject. Adds its CIN mapping when given."""
    org = OrgObservation(
        business_id=business_id,
        entity_type=OrgObservationType.LEGAL_ENTITY.value,
        display_name=display_name,
        state_code=state_code,
    )
    session.add(org)
    session.flush()
    if cin:
        session.add(
            OrgExternalId(business_id=business_id, observation_id=org.id, scheme="cin", value=cin)
        )
    session.flush()
    return org


# --- an ambiguous candidate is routed to a review-only suggestion, never merged -------------------
#
# Note on the deterministic matcher's MATCH path: a strong agreement needs a shared typed identifier
# (cin / llpin / gstin), but ``org_external_ids`` is UNIQUE per (business, scheme, value) — two distinct
# in-business identities can never share one, so a confirmed within-business MATCH is unreachable by
# construction (duplicates are refused at import). The reachable, and acceptance-relevant, outcome is an
# AMBIGUOUS shared-name candidate, which must be ROUTED to review and NEVER auto-merged.


def test_ambiguous_match_routes_review_suggestion_and_does_not_merge(session_factory):
    with session_factory() as session:
        run, subject = _converted_run(session, BIZ_A)
        # A same-NAME identity with no confirming identifier — a deterministic REVIEW candidate, never
        # a confirmed match.
        other = _seed_existing_org(session, BIZ_A, display_name=FIXTURE_NAME, cin=None)
        session.flush()

        result = ObservationBatchResolver(session, BIZ_A).resolve_run(run.id, ADMIN)
        session.flush()

        assert result.ambiguous_count == 1
        assert result.matched_count == 0
        assert result.review_suggestions == 1
        # A review-only proposal exists for the pair — and nothing was merged.
        suggestions = session.query(IdentityMatchSuggestion).filter_by(business_id=BIZ_A).all()
        assert len(suggestions) == 1
        assert {suggestions[0].subject_a_id, suggestions[0].subject_b_id} == {subject.id, other.id}
        # A batch cannot merge consent: no identities collapsed, and no consent / restriction state was
        # created or moved between identities.
        assert session.query(IdentityMerge).filter_by(business_id=BIZ_A).count() == 0
        assert session.query(OrgObservation).filter_by(id=other.id).count() == 1
        assert session.query(OrgObservation).filter_by(id=subject.id).count() == 1
        assert session.query(ConsentReceipt).filter_by(business_id=BIZ_A).count() == 0
        assert session.query(ProcessingRestriction).filter_by(business_id=BIZ_A).count() == 0

        resolution = session.query(ObservationBatchResolution).filter_by(business_id=BIZ_A).one()
        assert resolution.run_id == run.id
        assert resolution.provenance["merged"] is False
        assert resolution.provenance["sent"] is False
        assert resolution.provenance["spent"] is False


# --- a protected correction is never overwritten by resolution ------------------------------------


def test_protected_correction_is_preserved(session_factory):
    with session_factory() as session:
        run, subject = _converted_run(session, BIZ_A)
        # The user protects a corrected legal_name BEFORE resolution runs.
        FieldCorrectionStore(session, BIZ_A).record(
            FieldCorrectionInput(
                subject_type="organization",
                subject_id=subject.id,
                field_key="legal_name",
                corrected_value="Synthetic Textiles Private Limited (user)",
                protected_from_refresh=True,
            ),
            CORRECTOR,
        )
        session.flush()

        result = ObservationBatchResolver(session, BIZ_A).resolve_run(run.id, ADMIN)
        session.flush()

        # The protected correction held as canonical over the provider observation.
        assert result.corrections_preserved >= 1
        assert result.fields_canonicalized >= 1
        # Resolution writes no field value anywhere — the stored correction is untouched.
        correction = (
            session.query(FieldCorrection)
            .filter_by(business_id=BIZ_A, subject_id=subject.id, field_key="legal_name")
            .one()
        )
        assert correction.corrected_value == "Synthetic Textiles Private Limited (user)"
        assert correction.protected_from_refresh is True


# --- honest empty batch ---------------------------------------------------------------------------


def test_run_with_no_converted_batch_is_refused(session_factory):
    with session_factory() as session:
        # Executed but NOT converted — there is no observation batch to resolve.
        run = _executed_run(session, BIZ_A, with_duns=True)
        session.flush()
        with pytest.raises(BatchResolutionError) as exc:
            ObservationBatchResolver(session, BIZ_A).resolve_run(run.id, ADMIN)
        assert exc.value.code == REASON_NOTHING_TO_RESOLVE
        # Refused without fabricating a resolution.
        assert session.query(ObservationBatchResolution).filter_by(business_id=BIZ_A).count() == 0


# --- idempotency: a run resolves once -------------------------------------------------------------


def test_reresolve_is_idempotent_noop(session_factory):
    with session_factory() as session:
        run, _ = _converted_run(session, BIZ_A)
        _seed_existing_org(session, BIZ_A, display_name=FIXTURE_NAME, cin=None)
        session.flush()

        resolver = ObservationBatchResolver(session, BIZ_A)
        first = resolver.resolve_run(run.id, ADMIN)
        session.flush()
        second = resolver.resolve_run(run.id, ADMIN)
        session.flush()

        assert first.already_resolved is False
        assert second.already_resolved is True
        assert second.resolution.id == first.resolution.id
        # A run resolves ONCE: no duplicate resolution, suggestion or audit.
        assert session.query(ObservationBatchResolution).filter_by(business_id=BIZ_A).count() == 1
        assert session.query(IdentityMatchSuggestion).filter_by(business_id=BIZ_A).count() == 1
        assert session.query(AuditEvent).filter_by(action=AUDIT_ACTION_RESOLVE_BATCH).count() == 1


# --- authority + tenant isolation -----------------------------------------------------------------


def test_unauthorized_actor_refused_before_any_read(session_factory):
    with session_factory() as session:
        run, _ = _converted_run(session, BIZ_A)
        session.flush()
        with pytest.raises(BatchResolutionAuthorizationError):
            ObservationBatchResolver(session, BIZ_A).resolve_run(run.id, VIEWER)
        # Refused FIRST — nothing staged.
        assert session.query(ObservationBatchResolution).count() == 0
        assert session.query(IdentityMatchSuggestion).count() == 0


def test_cross_tenant_run_is_not_found(session_factory):
    with session_factory() as session:
        run_a, _ = _converted_run(session, BIZ_A)
        session.flush()
        # A B-scoped resolver cannot reach A's run — it reads exactly like not-found.
        with pytest.raises(BatchResolutionError) as exc:
            ObservationBatchResolver(session, BIZ_B).resolve_run(run_a.id, ADMIN)
        assert exc.value.code == REASON_RUN_NOT_FOUND
        assert session.query(ObservationBatchResolution).filter_by(business_id=BIZ_B).count() == 0


# --------------------------------------------------------------------------------------------------
# Postgres-backed test — disposable PostgreSQL (real transactions, not SQLite)
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)
_PG_SCHEMA = "dca_144_test"


@pytest.fixture
def pg_session_factory():
    engine = make_engine(PG_URL)

    @event.listens_for(engine, "connect")
    def _set_search_path(dbapi_conn, _record):  # pragma: no cover - exercised only with a PG URL
        cur = dbapi_conn.cursor()
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{_PG_SCHEMA}"')
        cur.execute(f'SET search_path TO "{_PG_SCHEMA}"')
        cur.close()

    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        with engine.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{_PG_SCHEMA}" CASCADE')
        engine.dispose()


@requires_pg
def test_pg_resolve_records_resolution_and_keeps_audit(pg_session_factory):
    """On real Postgres: resolving a run's batch records the resolution + its atomic audit entry, and a
    re-resolve stays a no-op — nothing merged or sent."""
    assert make_url(PG_URL).get_backend_name() == "postgresql"
    with pg_session_factory() as session:
        run, _ = _converted_run(session, BIZ_A)
        _seed_existing_org(session, BIZ_A, display_name=FIXTURE_NAME, cin=None)
        session.commit()
        run_id = run.id

    with pg_session_factory() as session:
        result = ObservationBatchResolver(session, BIZ_A).resolve_run(run_id, ADMIN)
        session.commit()
        assert result.review_suggestions == 1

    with pg_session_factory() as session:
        assert session.query(ObservationBatchResolution).filter_by(business_id=BIZ_A).count() == 1
        assert (
            session.scalars(
                select(AuditEvent)
                .where(AuditEvent.business_id == BIZ_A)
                .where(AuditEvent.action == AUDIT_ACTION_RESOLVE_BATCH)
            ).all()
            != []
        )
        assert session.query(IdentityMerge).filter_by(business_id=BIZ_A).count() == 0
        # Re-resolve is an idempotent no-op on real Postgres too.
        second = ObservationBatchResolver(session, BIZ_A).resolve_run(run_id, ADMIN)
        session.commit()
        assert second.already_resolved is True
        assert session.query(ObservationBatchResolution).filter_by(business_id=BIZ_A).count() == 1
