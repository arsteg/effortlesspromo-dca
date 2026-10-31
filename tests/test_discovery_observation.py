"""DCA-143 — convert ONE staged (DONE) discovery task into provenanced observations.

Two layers, matching the DCA-140 / DCA-141 / DCA-142 suites:

- **Service unit tests** (in-memory SQLite) pin
  :meth:`~app.services.discovery_observation.DiscoveryObservationConverter.convert_task`: an authorized
  administrator converting a DONE task reads the organization identities the DCA-142 task imported and
  records each PERMITTED organization-level field as a provenanced field observation that cites a
  captured checkpoint evidence object (retention derived from the source terms). A candidate field that
  is not on the allowlist is DROPPED, never persisted; re-converting a task is an idempotent no-op (no
  duplicate observations); a task that is not DONE (``DENIED`` / ``FAILED``) is refused; a cross-tenant
  task reads as not-found; an unauthorized caller is refused before any state is read. Converting
  performs no fetch, model call, spend or send.
- **Postgres-backed test** (``DCA_TEST_DATABASE_URL``) runs one conversion against a disposable
  PostgreSQL (real transactions, not SQLite): the conversion records the observations + evidence and the
  atomic audit entry survives, and a re-convert stays a no-op.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import make_url

from app.db.base import Base, make_engine, make_session_factory
from app.db.models import (
    AuditEvent,
    BudgetLimit,
    BusinessSourceEntitlement,
    DiscoveryPlan,
    DiscoveryTaskConversion,
    DiscoveryTaskStatus,
    EvidenceObject,
    EvidenceRetentionMode,
    FieldInferenceLabel,
    FieldObservation,
    IcpConfirmation,
    IcpDraft,
    OrgObservation,
    Source,
    SourceAccessMethod,
    SourceApprovalStatus,
    SourceKind,
    SourceStatus,
    SourceTerms,
    SourceTermsApproval,
    SourceTermsStatus,
    TenantAgent,
)
from app.identity.fake import BUSINESS_A, BUSINESS_B
from app.services.discovery_observation import (
    AUDIT_ACTION_CONVERT_TASK,
    PERMITTED_OBSERVATION_FIELDS,
    REASON_EXECUTION_NOT_FOUND,
    REASON_TASK_NOT_CONVERTIBLE,
    DiscoveryObservationConverter,
    TaskConversionActor,
    TaskConversionAuthorizationError,
    TaskConversionError,
)
from app.services.discovery_planner import PLAN_PURPOSE
from app.services.discovery_run import create_discovery_run
from app.services.discovery_task_execution import DiscoveryTaskExecutor, OrgDatasetTaskInput
from app.services.discovery_task_execution import TaskExecutionActor
from app.services.org_observation import ExternalIdInput
from app.services.run_plan_approval import RunPlanApprover, approve_run_plan
from app.sources.org_dataset_import import DatasetManifest, DatasetRow
from tests.conftest import make_settings

BIZ_A = BUSINESS_A
BIZ_B = BUSINESS_B

ADMIN = TaskConversionActor(subject="00000000-0000-4000-9000-0000000a0143a", roles=("owner",))
VIEWER = TaskConversionActor(subject="00000000-0000-4000-9000-0000000a0143b", roles=("viewer",))
EXEC_ADMIN = TaskExecutionActor(subject="00000000-0000-4000-9000-0000000a0143a", roles=("owner",))

ALLOWED_KEY = "mca_master_data"
ALLOWED_FIELDS = ["org_name", "org_identifier"]


# --- seed helpers (mirror the DCA-142 suite) ------------------------------------------------------


def _seed_confirmed_icp(session, business_id, *, version=1) -> None:
    session.add(
        IcpDraft(
            business_id=business_id,
            version=version,
            industries=["information technology"],
            size_bands=["small"],
            roles=["owner"],
            geography=["KA", "MH"],
            status="draft",
        )
    )
    session.add(
        IcpConfirmation(
            business_id=business_id, version=version, actor_id="seed", actor_type="user"
        )
    )


def _seed_entitled_source(session, business_id, key=ALLOWED_KEY, fields=ALLOWED_FIELDS) -> None:
    now = datetime.now(UTC)
    if not session.query(Source).filter_by(key=key).count():
        session.add(
            Source(
                key=key,
                name=key,
                kind=SourceKind.OFFICIAL.value,
                trust_tier="official_registry",
                capabilities=["org_discovery"],
                fields=list(fields),
                access_method=SourceAccessMethod.BULK_DOWNLOAD.value,
                india_coverage={"nationwide": True, "states": []},
                status=SourceStatus.ENABLED.value,
                enabled=True,
                permissions=[],
            )
        )
        session.add(
            SourceTerms(
                source_key=key,
                version=1,
                operation_rights={"collect": True},
                field_rights={},
                purposes=[PLAN_PURPOSE],
                contract_evidence={"reference": "lic-1", "kind": "licence", "uri": ""},
                status=SourceTermsStatus.ACTIVE.value,
            )
        )
        session.add(
            SourceTermsApproval(
                source_key=key,
                terms_version=1,
                reviewer_id="op-1",
                reviewer_type="platform_operator",
                review_evidence={"reference": "rev-1", "kind": "review", "uri": ""},
                reviewed_at=now - timedelta(days=1),
                effective_from=now - timedelta(days=1),
                expires_at=now + timedelta(days=365),
                approved_operation_rights={"collect": True},
                approved_field_rights={},
                status=SourceApprovalStatus.ACTIVE.value,
            )
        )
    session.add(
        BusinessSourceEntitlement(
            business_id=business_id,
            source_key=key,
            version=1,
            enabled=True,
            operation_rights={"collect": True},
            field_rights={},
            purposes=[PLAN_PURPOSE],
            approved_terms_version=1,
            actor_id="seed",
            actor_type="user",
        )
    )


def _seed_budget(session, business_id, *, per_run=1_000_000) -> None:
    session.add(
        BudgetLimit(
            business_id=business_id,
            version=1,
            currency="INR",
            daily_cap_minor=10_000_000,
            monthly_cap_minor=100_000_000,
            per_run_cap_minor=per_run,
            timezone="Asia/Kolkata",
            actor_id="seed",
            actor_type="user",
        )
    )


def _planned_run(session, business_id):
    existing = session.scalar(select(TenantAgent).where(TenantAgent.business_id == business_id))
    if existing is None:
        session.add(TenantAgent(business_id=business_id, country="IN", enabled=True))
        session.flush()
    run = create_discovery_run(session, make_settings(), business_id).run
    assert run is not None
    session.add(
        DiscoveryPlan(
            business_id=business_id,
            version=1,
            icp_version=run.icp_version,
            status="draft",
            tasks=[
                {
                    "sourceKey": ALLOWED_KEY,
                    "fields": ["org_name"],
                    "filters": {"geography": ["KA"]},
                    "maxQueries": 2,
                }
            ],
            excluded=[],
            task_cap=5,
            query_cap=3,
            rationale="reviewed discovery plan",
            overall_confidence=0.8,
            source="discovery_plan",
            source_version="pv-1",
            provenance={},
        )
    )
    session.flush()
    approve_run_plan(
        session, business_id, run.id, 1, RunPlanApprover(subject="appr", roles=("owner",))
    )
    session.flush()
    return run


def _adapter_input(*, with_duns: bool = True) -> OrgDatasetTaskInput:
    external = [ExternalIdInput("cin", "U17120MH2026PTC900099")]
    if with_duns:
        # A valid external-id scheme that is NOT a permitted observation field — must be DROPPED.
        external.append(ExternalIdInput("duns", "123456789"))
    rows = (
        DatasetRow(
            row_ref="le-1",
            entity_type="legal_entity",
            display_name="Synthetic Textiles Pvt Ltd",
            external_ids=tuple(external),
        ),
    )
    manifest = DatasetManifest(
        dataset_id="mca-2026-q3",
        source_key=ALLOWED_KEY,
        source_version="2026-09-30",
        license="data.gov.in-GODL-1.0",
        purpose=PLAN_PURPOSE,
    )
    return OrgDatasetTaskInput(manifest=manifest, rows=rows)


def _executed_run(session, business_id, *, with_duns: bool = True):
    """Seed + plan + EXECUTE one task (DCA-142) so a DONE execution with org observations exists."""
    _seed_confirmed_icp(session, business_id)
    _seed_entitled_source(session, business_id)
    _seed_budget(session, business_id)
    run = _planned_run(session, business_id)
    session.flush()
    result = DiscoveryTaskExecutor(session, business_id).execute_task(
        run.id, 0, EXEC_ADMIN, adapter_input=_adapter_input(with_duns=with_duns)
    )
    session.flush()
    assert result.status == DiscoveryTaskStatus.DONE.value
    return run


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


# --- converting a staged task records the expected, PERMITTED observations ------------------------


def test_convert_records_permitted_observations_and_drops_forbidden_field(session_factory):
    with session_factory() as session:
        run = _executed_run(session, BIZ_A, with_duns=True)
        session.flush()

        result = DiscoveryObservationConverter(session, BIZ_A).convert_task(run.id, 0, ADMIN)
        session.flush()

        # legal_name + cin are recorded (2); the duns external id is NOT a permitted field -> dropped.
        assert result.already_converted is False
        assert result.observations_recorded == 2
        assert result.fields_dropped == 1
        assert result.org_observation_count == 1

        org = session.query(OrgObservation).filter_by(business_id=BIZ_A).one()
        fields = session.scalars(
            select(FieldObservation)
            .where(FieldObservation.business_id == BIZ_A)
            .where(FieldObservation.subject_id == org.id)
        ).all()
        recorded_keys = {f.field_key for f in fields}
        assert recorded_keys == {"legal_name", "cin"}
        # A forbidden field never persists — no 'duns' row exists at all.
        assert "duns" not in recorded_keys
        assert all(k in PERMITTED_OBSERVATION_FIELDS for k in recorded_keys)
        # Each recorded field is OBSERVED, grounded in the source + terms version and cites the evidence.
        for f in fields:
            assert f.inference_label == FieldInferenceLabel.OBSERVED.value
            assert f.source_key == ALLOWED_KEY
            assert f.source_version == 1
            assert f.provenance_ref.endswith(f"#evidence={result.evidence_id}")

        # Source evidence was preserved: a checkpoint evidence object the facts cite. The seeded source
        # grants no 'store' right, so retention is honored REFERENCE_ONLY (no raw copy kept).
        evidence = session.query(EvidenceObject).filter_by(id=result.evidence_id).one()
        assert evidence.retention_mode == EvidenceRetentionMode.REFERENCE_ONLY.value
        assert evidence.raw_content is None

        conversion = session.query(DiscoveryTaskConversion).filter_by(business_id=BIZ_A).one()
        assert conversion.observations_recorded == 2
        assert conversion.fields_dropped == 1
        assert conversion.provenance["converted"] is True
        assert conversion.provenance["sent"] is False
        assert conversion.provenance["spent"] is False

        # One atomic convert-task audit entry.
        events = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.business_id == BIZ_A)
            .where(AuditEvent.action == AUDIT_ACTION_CONVERT_TASK)
        ).all()
        assert len(events) == 1
        assert events[0].references["observationsRecorded"] == 2
        assert events[0].references["fieldsDropped"] == 1


# --- idempotency: a task converts once ------------------------------------------------------------


def test_reconvert_is_idempotent_noop(session_factory):
    with session_factory() as session:
        run = _executed_run(session, BIZ_A, with_duns=True)
        session.flush()

        converter = DiscoveryObservationConverter(session, BIZ_A)
        first = converter.convert_task(run.id, 0, ADMIN)
        session.flush()
        second = converter.convert_task(run.id, 0, ADMIN)
        session.flush()

        assert first.already_converted is False
        assert second.already_converted is True
        assert second.conversion.id == first.conversion.id
        # A task converts ONCE: no duplicate observations / conversions / evidence, and no second audit.
        assert session.query(DiscoveryTaskConversion).filter_by(business_id=BIZ_A).count() == 1
        assert session.query(FieldObservation).filter_by(business_id=BIZ_A).count() == 2
        assert session.query(EvidenceObject).filter_by(business_id=BIZ_A).count() == 1
        assert session.query(AuditEvent).filter_by(action=AUDIT_ACTION_CONVERT_TASK).count() == 1


# --- wrong state: a non-DONE task is refused ------------------------------------------------------


def test_denied_task_is_not_convertible(session_factory):
    with session_factory() as session:
        _seed_confirmed_icp(session, BIZ_A)
        _seed_entitled_source(session, BIZ_A)
        _seed_budget(session, BIZ_A)
        run = _planned_run(session, BIZ_A)
        # Revoke the source so the DCA-142 execution is DENIED (no staged result).
        session.query(BusinessSourceEntitlement).filter_by(business_id=BIZ_A).delete()
        session.flush()
        denied = DiscoveryTaskExecutor(session, BIZ_A).execute_task(
            run.id, 0, EXEC_ADMIN, adapter_input=_adapter_input()
        )
        session.flush()
        assert denied.status == DiscoveryTaskStatus.DENIED.value

        with pytest.raises(TaskConversionError) as exc:
            DiscoveryObservationConverter(session, BIZ_A).convert_task(run.id, 0, ADMIN)
        assert exc.value.code == REASON_TASK_NOT_CONVERTIBLE
        # Refused before any state change — no conversion / field / evidence row was staged.
        assert session.query(DiscoveryTaskConversion).count() == 0
        assert session.query(FieldObservation).count() == 0
        assert session.query(EvidenceObject).count() == 0


def test_unknown_task_is_not_found(session_factory):
    with session_factory() as session:
        run = _executed_run(session, BIZ_A)
        session.flush()
        with pytest.raises(TaskConversionError) as exc:
            DiscoveryObservationConverter(session, BIZ_A).convert_task(run.id, 5, ADMIN)
        assert exc.value.code == REASON_EXECUTION_NOT_FOUND


# --- authority + tenant isolation -----------------------------------------------------------------


def test_unauthorized_actor_refused(session_factory):
    with session_factory() as session:
        run = _executed_run(session, BIZ_A)
        session.flush()
        with pytest.raises(TaskConversionAuthorizationError):
            DiscoveryObservationConverter(session, BIZ_A).convert_task(run.id, 0, VIEWER)
        # Refused FIRST — nothing staged.
        assert session.query(DiscoveryTaskConversion).count() == 0


def test_cross_tenant_task_is_not_found(session_factory):
    with session_factory() as session:
        run_a = _executed_run(session, BIZ_A)
        session.flush()
        # A B-scoped converter cannot reach A's task — it reads exactly like not-found.
        with pytest.raises(TaskConversionError) as exc:
            DiscoveryObservationConverter(session, BIZ_B).convert_task(run_a.id, 0, ADMIN)
        assert exc.value.code == REASON_EXECUTION_NOT_FOUND
        # No conversion / field observation leaked into B.
        assert session.query(DiscoveryTaskConversion).filter_by(business_id=BIZ_B).count() == 0
        assert session.query(FieldObservation).filter_by(business_id=BIZ_B).count() == 0


# --------------------------------------------------------------------------------------------------
# Postgres-backed test — disposable PostgreSQL (real transactions, not SQLite)
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)
_PG_SCHEMA = "dca_143_test"


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
def test_pg_convert_records_observations_and_keeps_audit(pg_session_factory):
    """On real Postgres: converting one DONE task records the permitted observations + evidence, the
    atomic audit entry survives, and a re-convert stays a no-op — nothing sent."""
    assert make_url(PG_URL).get_backend_name() == "postgresql"
    with pg_session_factory() as session:
        run = _executed_run(session, BIZ_A, with_duns=True)
        session.commit()
        run_id = run.id

    with pg_session_factory() as session:
        result = DiscoveryObservationConverter(session, BIZ_A).convert_task(run_id, 0, ADMIN)
        session.commit()
        assert result.observations_recorded == 2
        assert result.fields_dropped == 1

    with pg_session_factory() as session:
        assert session.query(FieldObservation).filter_by(business_id=BIZ_A).count() == 2
        assert (
            session.scalars(
                select(AuditEvent)
                .where(AuditEvent.business_id == BIZ_A)
                .where(AuditEvent.action == AUDIT_ACTION_CONVERT_TASK)
            ).all()
            != []
        )
        # Re-convert is an idempotent no-op on real Postgres too.
        second = DiscoveryObservationConverter(session, BIZ_A).convert_task(run_id, 0, ADMIN)
        session.commit()
        assert second.already_converted is True
        assert session.query(FieldObservation).filter_by(business_id=BIZ_A).count() == 2
