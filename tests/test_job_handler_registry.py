"""DCA-100 — the typed Python job-handler registry.

Two layers:

- **Always-on logic tests** drive the registry's contract with synthetic typed handlers: all registered
  handlers are discoverable; an unknown job type and an unknown version of a known type both fail safely
  (distinct, typed errors, nothing returned); a payload that violates the handler's Pydantic schema is
  rejected with a PII-safe field list and never produces a runnable ``ValidatedJob``; version support
  resolves the right handler per ``schema_version``; and registration is explicit + one-to-one.
- **End-to-end on a disposable PostgreSQL** (``DCA_TEST_DATABASE_URL``): persist a DCA-099 durable job
  request on the real engine, read it back, and feed its ``(job_type, schema_version)`` + a payload
  through the registry gate — a supported job validates, an unsupported type is refused — proving the two
  stories compose with no state change on the denial. Skips when no URL is supplied.

Spin up a throwaway Postgres, e.g.:
    docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55499:5432 postgres:16-alpine
    export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55499/dca"
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.db.base import Base, make_engine, make_session_factory
from app.db.models import DurableJobRequest
from app.jobs.handlers import (
    DuplicateHandlerError,
    JobHandler,
    JobHandlerError,
    JobHandlerRegistry,
    JobPayloadValidationError,
    UnknownJobTypeError,
    UnsupportedSchemaVersionError,
    ValidatedJob,
    build_default_registry,
)
from app.jobs.store import JobEnqueueRequest, JobRequestStore

BIZ_A = "11111111-1111-1111-1111-11111111a100"


# --------------------------------------------------------------------------------------------------
# Synthetic typed handlers (the "test handler" the manual script registers too).
# --------------------------------------------------------------------------------------------------


class ResearchPayloadV1(BaseModel):
    """A pointer-only payload — no raw contact data, consistent with the durable-queue discipline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject_ref: str = Field(min_length=1)
    max_fields: int = Field(ge=1, le=50)


class ResearchPayloadV2(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    subject_ref: str = Field(min_length=1)
    max_fields: int = Field(ge=1, le=50)
    region: str = Field(min_length=2, max_length=2)  # a new required field in v2


class ResearchHandlerV1(JobHandler):
    job_type = "prospect.research"
    schema_version = 1
    payload_model = ResearchPayloadV1

    def __init__(self) -> None:
        self.ran = 0

    def run(self, payload: BaseModel) -> None:  # the registry must never call this in DCA-100
        self.ran += 1


class ResearchHandlerV2(ResearchHandlerV1):
    schema_version = 2
    payload_model = ResearchPayloadV2


def _registry() -> tuple[JobHandlerRegistry, ResearchHandlerV1, ResearchHandlerV2]:
    reg = JobHandlerRegistry()
    v1 = ResearchHandlerV1()
    v2 = ResearchHandlerV2()
    reg.register(v1)
    reg.register(v2)
    return reg, v1, v2


# --------------------------------------------------------------------------------------------------
# Discovery + explicit, one-to-one registration.
# --------------------------------------------------------------------------------------------------


def test_all_registered_handlers_are_discoverable():
    reg, _, _ = _registry()
    descriptors = reg.descriptors()
    seen = {(d.job_type, d.schema_version) for d in descriptors}
    assert seen == {("prospect.research", 1), ("prospect.research", 2)}
    assert {d.payload_model for d in descriptors} == {"ResearchPayloadV1", "ResearchPayloadV2"}
    assert reg.is_registered("prospect.research", 1)
    assert reg.known_versions("prospect.research") == (1, 2)


def test_default_registry_is_empty_future_handlers_disabled():
    reg = build_default_registry()
    assert reg.descriptors() == ()
    # An empty registry still fails closed: every job type is unknown, so nothing can run.
    with pytest.raises(UnknownJobTypeError):
        reg.resolve("prospect.research", 1)


def test_duplicate_registration_is_refused():
    reg, _, _ = _registry()
    with pytest.raises(DuplicateHandlerError):
        reg.register(ResearchHandlerV1())  # same (job_type, schema_version) already bound


def test_handler_missing_payload_model_is_refused():
    class Bad(JobHandler):
        job_type = "x"
        # no payload_model

    reg = JobHandlerRegistry()
    with pytest.raises(JobHandlerError):
        reg.register(Bad())


# --------------------------------------------------------------------------------------------------
# Unknown job types / versions fail safely.
# --------------------------------------------------------------------------------------------------


def test_unknown_job_type_fails_safely():
    reg, _, _ = _registry()
    with pytest.raises(UnknownJobTypeError):
        reg.resolve("does.not.exist", 1)
    with pytest.raises(UnknownJobTypeError):
        reg.prepare("does.not.exist", 1, {"subject_ref": "ref://x", "max_fields": 3})


def test_known_type_unknown_version_is_distinct_and_fails_safely():
    reg, _, _ = _registry()
    with pytest.raises(UnsupportedSchemaVersionError) as info:
        reg.resolve("prospect.research", 99)
    assert info.value.known_versions == (1, 2)
    # It is NOT reported as an unknown type — the type is known, only the version is not.
    assert not isinstance(info.value, UnknownJobTypeError)


# --------------------------------------------------------------------------------------------------
# Payload validation is the gate before execution.
# --------------------------------------------------------------------------------------------------


def test_valid_payload_prepares_a_typed_runnable_without_executing():
    reg, v1, _ = _registry()
    job = reg.prepare("prospect.research", 1, {"subject_ref": "ref://org/42", "max_fields": 5})
    assert isinstance(job, ValidatedJob)
    assert isinstance(job.payload, ResearchPayloadV1)
    assert job.payload.subject_ref == "ref://org/42"
    assert job.job_type == "prospect.research" and job.schema_version == 1
    # The registry resolved + validated only — it NEVER ran the handler.
    assert v1.ran == 0


def test_invalid_payload_never_reaches_execution():
    reg, v1, _ = _registry()
    # max_fields out of range + a forbidden extra key -> validation fails, no ValidatedJob produced.
    with pytest.raises(JobPayloadValidationError) as info:
        reg.prepare(
            "prospect.research",
            1,
            {"subject_ref": "ref://x", "max_fields": 999, "surprise": "boom"},
        )
    issues = {(e.loc, e.issue) for e in info.value.errors}
    assert ("max_fields", "less_than_equal") in issues
    assert ("surprise", "extra_forbidden") in issues
    assert v1.ran == 0  # execution unreachable for an invalid payload


def test_validation_error_is_pii_safe():
    reg, _, _ = _registry()
    with pytest.raises(JobPayloadValidationError) as info:
        # subject_ref missing; if present it could carry a reference we must not echo.
        reg.prepare(
            "prospect.research", 1, {"subject_ref": "secret-value://leak", "max_fields": "x"}
        )
    rendered = str(info.value)
    # The error names the field + issue but never the submitted value.
    assert "secret-value" not in rendered
    assert "max_fields" in rendered


def test_version_support_resolves_the_right_handler():
    reg, v1, v2 = _registry()
    # v2 requires the extra `region` field; the same payload that passes v1 fails v2.
    reg.prepare("prospect.research", 1, {"subject_ref": "ref://x", "max_fields": 3})
    with pytest.raises(JobPayloadValidationError):
        reg.prepare("prospect.research", 2, {"subject_ref": "ref://x", "max_fields": 3})
    job_v2 = reg.prepare(
        "prospect.research", 2, {"subject_ref": "ref://x", "max_fields": 3, "region": "IN"}
    )
    assert isinstance(job_v2.payload, ResearchPayloadV2)
    assert reg.resolve("prospect.research", 2) is v2
    assert reg.resolve("prospect.research", 1) is v1


# --------------------------------------------------------------------------------------------------
# End-to-end with the DCA-099 durable store on a disposable PostgreSQL (skips without a URL).
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)


@pytest.fixture
def pg_factory():
    engine = make_engine(PG_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    Base.metadata.drop_all(engine)
    engine.dispose()


@requires_pg
def test_persisted_job_resolves_through_registry_on_real_postgres(pg_factory):
    reg, _, _ = _registry()
    now = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)

    # Persist a real DCA-099 durable job request, then read back its (type, version).
    with pg_factory() as session:
        row, created = JobRequestStore(session, BIZ_A).enqueue(
            JobEnqueueRequest(
                job_type="prospect.research",
                idempotency_key="k-100",
                payload_ref="dca-payload://synthetic/run-1",
                schema_version=1,
            ),
            now=now,
        )
        session.commit()
        job_type, version = row.job_type, row.schema_version
    assert created is True

    # The persisted job's (type, version) resolves + validates through the registry gate.
    job = reg.prepare(job_type, version, {"subject_ref": "ref://org/42", "max_fields": 5})
    assert job.job_type == "prospect.research" and job.schema_version == 1

    # An unsupported type is refused and leaves the durable queue unchanged.
    with pg_factory() as session:
        before = session.scalar(
            select(func.count())
            .select_from(DurableJobRequest)
            .where(DurableJobRequest.business_id == BIZ_A)
        )
    with pytest.raises(UnknownJobTypeError):
        reg.prepare("prospect.unregistered", 1, {"subject_ref": "ref://x", "max_fields": 1})
    with pg_factory() as session:
        after = session.scalar(
            select(func.count())
            .select_from(DurableJobRequest)
            .where(DurableJobRequest.business_id == BIZ_A)
        )
    assert before == after == 1  # the registry touches no DB; the denial changed nothing
