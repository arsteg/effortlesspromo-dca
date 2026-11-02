"""DCA-145 — create ONE scoped prospect suggestion from a resolved discovery observation.

Two layers, matching the DCA-143 / DCA-144 suites:

- **Service unit tests** (in-memory SQLite) pin
  :meth:`~app.services.prospect_suggestion.ProspectSuggestionCreator.suggest`: an authorized
  administrator turns one eligible resolved organization subject into a business-scoped candidate with a
  deterministic ICP score/rationale, the targeting version it was scored against, an expiry and a
  visibility constraint. The suggestion is RESEARCH-ONLY (never a sendable contact) and creating it adds
  the subject to NO audience / consent surface; a run whose batch was not resolved is refused; an
  unknown / cross-tenant / off-batch subject reads as not-found; a cross-tenant run reads as not-found;
  an unauthorized caller is refused before any state is read; re-suggesting is an idempotent no-op.
  Suggesting performs no fetch, model call, spend, send or auto-acceptance.
- **Postgres-backed test** (``DCA_TEST_DATABASE_URL``) runs one suggest against a disposable PostgreSQL
  (real transactions, not SQLite): the suggestion + its atomic audit entry survive, it is research-only
  and a re-suggest stays a no-op.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import make_url

from app.audit.sink import ActorType, AuditActor
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import (
    AuditEvent,
    ConsentReceipt,
    FieldInferenceLabel,
    OrgObservation,
    OrgObservationType,
    ProspectSuggestion,
    SuggestionVisibility,
)
from app.services.field_observation import FieldObservationInput, FieldObservationStore
from app.services.observation_resolution import BatchResolutionActor, ObservationBatchResolver
from app.services.prospect_suggestion import (
    AUDIT_ACTION_SUGGEST,
    DIMENSION_INDUSTRY,
    REASON_RUN_NOT_FOUND,
    REASON_RUN_NOT_RESOLVED,
    REASON_SUBJECT_NOT_ELIGIBLE,
    ProspectSuggestionActor,
    ProspectSuggestionAuthorizationError,
    ProspectSuggestionCreator,
    ProspectSuggestionError,
)

# Reuse the DCA-143/144 scaffolding verbatim (seed + plan + execute + convert one task), so the batch
# under test is exactly what the accepted pipeline produces.
from tests.test_discovery_observation import ALLOWED_KEY, BIZ_A, BIZ_B
from tests.test_observation_resolution import _converted_run

ADMIN = ProspectSuggestionActor(subject="00000000-0000-4000-9000-0000000a0145a", roles=("owner",))
VIEWER = ProspectSuggestionActor(subject="00000000-0000-4000-9000-0000000a0145b", roles=("viewer",))
RESOLVE_ADMIN = BatchResolutionActor(
    subject="00000000-0000-4000-9000-0000000a0145a", roles=("owner",)
)
FIELD_ACTOR = AuditActor(id="00000000-0000-4000-9000-0000000a0145c", type=ActorType.USER)


# --- fixtures + helpers ---------------------------------------------------------------------------


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def _resolved_run(session, business_id, *, with_industry=True):
    """A run whose converted observation batch has been RESOLVED (DCA-144). Optionally records an
    ``industry`` field observation matching the seeded ICP so the deterministic score is a crisp 0.5
    (industry matches; geography does not — the legal entity carries no state). Returns (run, subject)."""
    run, subject = _converted_run(session, business_id)
    if with_industry:
        FieldObservationStore(session, business_id).record(
            FieldObservationInput(
                subject_type="organization",
                subject_id=subject.id,
                field_key="industry",
                field_value="information technology",  # matches the DCA-143 fixture ICP industries
                inference_label=FieldInferenceLabel.OBSERVED.value,
                provenance_ref=f"discovery_run:{run.id}/task:0#evidence=seed",
                observed_at=datetime.now(UTC),
                scope="nationwide",
                rights_basis="test_fixture",
                source_key=ALLOWED_KEY,
                source_version=1,
            ),
            FIELD_ACTOR,
        )
    session.flush()
    ObservationBatchResolver(session, business_id).resolve_run(run.id, RESOLVE_ADMIN)
    session.flush()
    return run, subject


# --- a suggestion is created with score/rationale/version/expiry/visibility -----------------------


def test_suggest_creates_scored_research_only_suggestion(session_factory):
    with session_factory() as session:
        run, subject = _resolved_run(session, BIZ_A)

        result = ProspectSuggestionCreator(session, BIZ_A).suggest(run.id, subject.id, ADMIN)
        session.flush()

        # Score is deterministic: industry matched, geography did not -> 1/2 = 0.5.
        assert result.already_suggested is False
        assert result.match_score == 0.5
        assert DIMENSION_INDUSTRY in result.matched_dimensions
        assert result.rationale  # a non-empty human-readable rationale
        assert result.scored_icp_version == run.icp_version == 1
        assert result.visibility == SuggestionVisibility.RESEARCH_ONLY.value
        assert result.sendable is False
        assert result.expires_at is not None and result.expires_at > result.suggestion.created_at

        row = session.query(ProspectSuggestion).filter_by(business_id=BIZ_A).one()
        assert row.run_id == run.id
        assert row.subject_id == subject.id
        assert row.subject_type == "organization"
        assert row.visibility == SuggestionVisibility.RESEARCH_ONLY.value
        assert row.status == "suggested"
        assert row.match_score == 0.5
        assert row.scored_icp_version == 1
        assert row.provenance["suggested"] is True
        assert row.provenance["sent"] is False
        assert row.provenance["spent"] is False
        assert row.provenance["autoAccepted"] is False

        events = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.business_id == BIZ_A)
            .where(AuditEvent.action == AUDIT_ACTION_SUGGEST)
        ).all()
        assert len(events) == 1
        assert events[0].references["visibility"] == SuggestionVisibility.RESEARCH_ONLY.value
        assert events[0].references["sendable"] is False


# --- research-only org is non-sendable and adds NO audience membership (the load-bearing acceptance) --


def test_research_only_org_is_non_sendable_and_adds_no_audience_membership(session_factory):
    with session_factory() as session:
        run, subject = _resolved_run(session, BIZ_A)
        consent_before = session.query(ConsentReceipt).filter_by(business_id=BIZ_A).count()

        result = ProspectSuggestionCreator(session, BIZ_A).suggest(run.id, subject.id, ADMIN)
        session.flush()

        # A research-only organization is distinguishable from a sendable contact: it is NOT sendable…
        assert result.visibility == SuggestionVisibility.RESEARCH_ONLY.value
        assert result.sendable is False
        row = session.query(ProspectSuggestion).filter_by(business_id=BIZ_A).one()
        assert row.provenance["sendable"] is False
        # …and creating it adds NO audience membership — no consent/recipient state is created
        # (acceptance is not consent, and the DCA has no audience/send surface).
        assert row.provenance["addedToAudience"] is False
        assert session.query(ConsentReceipt).filter_by(business_id=BIZ_A).count() == consent_before


# --- honest ordering: an unresolved run cannot be suggested from ----------------------------------


def test_unresolved_run_is_refused_no_state(session_factory):
    with session_factory() as session:
        # Converted but NOT resolved — there is no resolved batch to suggest from.
        run, subject = _converted_run(session, BIZ_A)
        session.flush()
        with pytest.raises(ProspectSuggestionError) as exc:
            ProspectSuggestionCreator(session, BIZ_A).suggest(run.id, subject.id, ADMIN)
        assert exc.value.code == REASON_RUN_NOT_RESOLVED
        # Refused without staging a suggestion.
        assert session.query(ProspectSuggestion).filter_by(business_id=BIZ_A).count() == 0


# --- ineligible / unknown subject reads as not-found ----------------------------------------------


def test_unknown_or_offbatch_subject_is_not_found(session_factory):
    with session_factory() as session:
        run, _ = _resolved_run(session, BIZ_A)
        # An org that exists in this business but is NOT part of the run's resolved batch is off-batch.
        off_batch = OrgObservation(
            business_id=BIZ_A,
            entity_type=OrgObservationType.LEGAL_ENTITY.value,
            display_name="Unrelated Co",
        )
        session.add(off_batch)
        session.flush()

        for bad_subject in ("does-not-exist", off_batch.id):
            with pytest.raises(ProspectSuggestionError) as exc:
                ProspectSuggestionCreator(session, BIZ_A).suggest(run.id, bad_subject, ADMIN)
            assert exc.value.code == REASON_SUBJECT_NOT_ELIGIBLE
        # No stray suggestion staged.
        assert session.query(ProspectSuggestion).filter_by(business_id=BIZ_A).count() == 0


def test_cross_tenant_run_is_not_found(session_factory):
    with session_factory() as session:
        run_a, subject_a = _resolved_run(session, BIZ_A)
        session.flush()
        # A B-scoped creator cannot reach A's run — it reads exactly like not-found.
        with pytest.raises(ProspectSuggestionError) as exc:
            ProspectSuggestionCreator(session, BIZ_B).suggest(run_a.id, subject_a.id, ADMIN)
        assert exc.value.code == REASON_RUN_NOT_FOUND
        assert session.query(ProspectSuggestion).filter_by(business_id=BIZ_B).count() == 0


# --- authority ------------------------------------------------------------------------------------


def test_unauthorized_actor_refused_before_any_read(session_factory):
    with session_factory() as session:
        run, subject = _resolved_run(session, BIZ_A)
        session.flush()
        with pytest.raises(ProspectSuggestionAuthorizationError):
            ProspectSuggestionCreator(session, BIZ_A).suggest(run.id, subject.id, VIEWER)
        # Refused FIRST — nothing staged.
        assert session.query(ProspectSuggestion).count() == 0


# --- idempotency: a subject is suggested once per run ---------------------------------------------


def test_resuggest_is_idempotent_noop(session_factory):
    with session_factory() as session:
        run, subject = _resolved_run(session, BIZ_A)
        creator = ProspectSuggestionCreator(session, BIZ_A)
        first = creator.suggest(run.id, subject.id, ADMIN)
        session.flush()
        second = creator.suggest(run.id, subject.id, ADMIN)
        session.flush()

        assert first.already_suggested is False
        assert second.already_suggested is True
        assert second.suggestion.id == first.suggestion.id
        # Suggested ONCE: no duplicate suggestion, no second audit.
        assert session.query(ProspectSuggestion).filter_by(business_id=BIZ_A).count() == 1
        assert session.query(AuditEvent).filter_by(action=AUDIT_ACTION_SUGGEST).count() == 1


# --- neutral score when the ICP sets no comparable dimension (deterministic fallback) -------------


def test_neutral_score_without_matching_fields(session_factory):
    with session_factory() as session:
        # No industry field recorded and no matching geography -> industry/geography both unmatched.
        run, subject = _resolved_run(session, BIZ_A, with_industry=False)
        result = ProspectSuggestionCreator(session, BIZ_A).suggest(run.id, subject.id, ADMIN)
        session.flush()
        # The ICP sets industry + geography, neither matches -> 0/2 = 0.0 (still a valid, reproducible
        # score); the suggestion is created research-only regardless.
        assert result.match_score == 0.0
        assert result.matched_dimensions == []
        assert result.visibility == SuggestionVisibility.RESEARCH_ONLY.value


# --------------------------------------------------------------------------------------------------
# Postgres-backed test — disposable PostgreSQL (real transactions, not SQLite)
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)
_PG_SCHEMA = "dca_145_test"


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
def test_pg_suggest_records_research_only_and_keeps_audit(pg_session_factory):
    """On real Postgres: suggesting one resolved subject records a research-only suggestion + its atomic
    audit entry, and a re-suggest stays a no-op — nothing sent, spent or made sendable."""
    assert make_url(PG_URL).get_backend_name() == "postgresql"
    with pg_session_factory() as session:
        run, subject = _resolved_run(session, BIZ_A)
        session.commit()
        run_id, subject_id = run.id, subject.id

    with pg_session_factory() as session:
        result = ProspectSuggestionCreator(session, BIZ_A).suggest(run_id, subject_id, ADMIN)
        session.commit()
        assert result.visibility == SuggestionVisibility.RESEARCH_ONLY.value
        assert result.sendable is False

    with pg_session_factory() as session:
        row = session.query(ProspectSuggestion).filter_by(business_id=BIZ_A).one()
        assert row.provenance["addedToAudience"] is False
        assert (
            session.scalars(
                select(AuditEvent)
                .where(AuditEvent.business_id == BIZ_A)
                .where(AuditEvent.action == AUDIT_ACTION_SUGGEST)
            ).all()
            != []
        )
        # No consent/recipient state created by suggesting.
        assert session.query(ConsentReceipt).filter_by(business_id=BIZ_A).count() == 0
        # Re-suggest is an idempotent no-op on real Postgres too.
        second = ProspectSuggestionCreator(session, BIZ_A).suggest(run_id, subject_id, ADMIN)
        session.commit()
        assert second.already_suggested is True
        assert session.query(ProspectSuggestion).filter_by(business_id=BIZ_A).count() == 1
