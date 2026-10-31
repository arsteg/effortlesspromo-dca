"""DCA-143 — convert ONE staged (DONE) discovery task into provenanced observations.

This story is the next step of the still-disabled discovery pipeline after a DCA-142 task is executed:
it CONVERTS exactly ONE of that run's DONE task results into normalized OBSERVATIONS. DCA-142 stages a
PERMITTED raw checkpoint (``staged_result``) when it runs the DCA-123 organization-dataset adapter; this
converter reads the organization identities that task imported and records each PERMITTED
organization-level field as a DCA-061 :class:`~app.db.models.FieldObservation` that CITES a DCA-062
:class:`~app.db.models.EvidenceObject` capturing the checkpoint — connecting deterministic parsing to
observation validation/storage, preserving source evidence and task idempotency.

There is **no network fetch, no model call and no recipient send.** The conversion reads
already-collected, already-permitted data (the DCA-142 execution record + the org observations it
imported) and runs the two sanctioned observation writers; it never bypasses them and never fetches,
spends or sends.

Every guard is checked fail-closed, in order, so the two acceptance criteria hold:

- **Retrying one task does not duplicate observations; forbidden fields never persist.**
  ``(business_id, run_id, task_index)`` is unique, so a task converts ONCE: re-converting is a
  deterministic no-op that returns the existing :class:`~app.db.models.DiscoveryTaskConversion` record
  and records nothing new. Each candidate field is screened against the
  :data:`PERMITTED_OBSERVATION_FIELDS` allowlist BEFORE it is written — a field not on the list (e.g. an
  external-id scheme that is not a permitted organization field) is DROPPED (counted, never persisted),
  so a forbidden field can never enter the field set.

- **Reproduce the result and the relevant denial without unintended state changes.** Authority is
  checked FIRST; then the task must be a DONE execution of a run in THIS business (an unknown /
  cross-tenant run or task reads as not-found; a ``DENIED`` / ``FAILED`` task — which staged no result —
  is refused as not-convertible). Each refusal is raised BEFORE the session is mutated, so it changes
  nothing. The converter stages into the caller's session and never commits — the conversion record, the
  captured evidence, the field observations and their atomic DCA-026 audit entries commit together, in
  the caller's unit of work.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.sink import ActorType, AuditActor, AuditSink
from app.db.models import (
    DiscoveryTaskConversion,
    DiscoveryTaskExecution,
    DiscoveryTaskStatus,
    FieldInferenceLabel,
    FieldSubjectType,
    OrgExternalId,
    OrgObservation,
    OrgObservationImport,
)
from app.identity.permissions import Scope, scopes_for_business_roles
from app.services.discovery_run import DiscoveryRunStore
from app.services.evidence_store import EvidenceInput, EvidenceObjectStore
from app.services.field_observation import FieldObservationInput, FieldObservationStore
from app.services.org_observation import OrgObservationStore
from app.services.source_entitlement import BusinessSourceEntitlementStore

# The ALLOWLIST of ORGANIZATION-level field keys a conversion may record. Every key is a company fact;
# the list contains NO contact channel, so the converter has no permitted field that could carry
# personal contact data. A candidate field not on this list is DROPPED, never recorded — regardless of
# what the staged identity carries. (Consistent with the DCA-124 website adapter's org allowlist.)
PERMITTED_OBSERVATION_FIELDS: frozenset[str] = frozenset(
    {"legal_name", "description", "industry", "cin", "pan", "gstin"}
)

# The content-kind recorded for the captured checkpoint evidence object (DCA-062). Its raw payload is
# the opaque staged_result (counts + row refs only — never contact data); retention is derived from the
# cited source terms, so a no-retention source leaves only the reference + digest.
EVIDENCE_CONTENT_KIND = "discovery_task_checkpoint"

# The version of the conversion contract provenance rows are written under (expand→migrate→contract).
CONVERSION_SCHEMA_VERSION = 1

# The audited action code for converting a staged task into observations (a controlled code).
AUDIT_ACTION_CONVERT_TASK = "discovery_run.task.converted"

# Where a conversion records its origin on the provenance / citation.
CONVERSION_SOURCE = "discovery_observation_conversion"

# Stable refusal codes raised (never recorded) for a malformed / ineligible request — each BEFORE any
# state is mutated, so the attempt changes nothing.
REASON_NOT_AUTHORIZED = "task_conversion_not_authorized"
REASON_EXECUTION_NOT_FOUND = "discovery_task_execution_not_found"
REASON_TASK_NOT_CONVERTIBLE = "discovery_task_not_convertible"
REASON_SOURCE_VERSION_UNRESOLVED = "source_terms_version_unresolved"


class TaskConversionError(Exception):
    """A task conversion was refused with a stable ``code`` for a malformed / ineligible request (an
    unknown or cross-tenant task, a task that is not a DONE execution, or a task whose source terms
    version cannot be resolved for provenance). Raised before the session is mutated, so a refusal
    changes nothing. ``detail`` names the offending value."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class TaskConversionAuthorizationError(TaskConversionError):
    """The caller is not authorized to convert a discovery task.

    A distinct type so an HTTP boundary can map it to a 403 while the other codes map to a 404/409.
    Raised FIRST — before any state is read or written. ``code`` is :data:`REASON_NOT_AUTHORIZED`.
    """

    def __init__(self, message: str) -> None:
        super().__init__(REASON_NOT_AUTHORIZED, message)


@dataclass(frozen=True)
class TaskConversionActor:
    """The business principal converting a staged task.

    ``subject`` is an opaque actor id (never a name/email) recorded on the conversion record and its
    atomic audit entries. ``roles`` are the caller's verified business-plane membership roles (from the
    tenant context, NEVER a bare token claim). :meth:`may_convert` grants the action only when those
    roles yield :attr:`~app.identity.permissions.Scope.RUN_PLAN_APPROVE` — converting a run's executed
    task is part of the same chargeable-discovery authority that approved the plan (DCA-141) and executed
    it (DCA-142). A read-only viewer and a platform operator (holding no business scope) cannot perform
    it.
    """

    subject: str
    roles: tuple[str, ...] = ()
    actor_type: str = "user"

    def may_convert(self) -> bool:
        return Scope.RUN_PLAN_APPROVE in scopes_for_business_roles(self.roles)

    def as_audit_actor(self) -> AuditActor:
        return AuditActor(id=self.subject, type=ActorType(self.actor_type))


@dataclass
class TaskConversionResult:
    """The inert, inspectable outcome of one convert attempt.

    ``conversion`` is the staged :class:`~app.db.models.DiscoveryTaskConversion` record. ``already_converted``
    is ``True`` when this was an idempotent no-op (the task had already been converted).
    ``observations_recorded`` is the number of PERMITTED field observations written this run (0 on a
    replay); ``fields_dropped`` the number of candidate fields screened out by the allowlist; ``evidence_id``
    the captured checkpoint evidence the facts cite. Nothing here is ever sent or charged.
    """

    business_id: str
    conversion: DiscoveryTaskConversion
    observations_recorded: int
    fields_dropped: int
    org_observation_count: int
    evidence_id: str | None
    already_converted: bool = False


class DiscoveryObservationConverter:
    """Convert ONE DONE discovery task into provenanced observations, for ONE business (tenant-scoped).

    Construct with the active session and the verified business scope (a route/worker resolves it from
    the membership-verified tenant context). Composes the DCA-062 evidence store and the DCA-061
    field-observation store over the organization identities a DCA-142 task imported; it writes only
    organization-level facts and their provenance, never a contact. Every query is confined to this
    business — there is no argument to widen the scope. The converter stages into the caller's session
    and never commits.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id
        self._observations = OrgObservationStore(session, business_id)
        self._fields = FieldObservationStore(session, business_id)
        self._evidence = EvidenceObjectStore(session, business_id)
        self._entitlements = BusinessSourceEntitlementStore(session, business_id)

    # -- reads (tenant-scoped) ----------------------------------------------------------------------

    def conversion_for(self, run_id: str, task_index: int) -> DiscoveryTaskConversion | None:
        """The conversion record for one (run, task) within this business, or ``None`` (the idempotency
        read; a cross-tenant / unknown run reads as not-found)."""
        if not run_id:
            return None
        stmt = (
            select(DiscoveryTaskConversion)
            .where(DiscoveryTaskConversion.business_id == self._business_id)
            .where(DiscoveryTaskConversion.run_id == run_id)
            .where(DiscoveryTaskConversion.task_index == task_index)
        )
        return self._session.scalars(stmt).one_or_none()

    def _execution_for(self, run_id: str, task_index: int) -> DiscoveryTaskExecution | None:
        stmt = (
            select(DiscoveryTaskExecution)
            .where(DiscoveryTaskExecution.business_id == self._business_id)
            .where(DiscoveryTaskExecution.run_id == run_id)
            .where(DiscoveryTaskExecution.task_index == task_index)
        )
        return self._session.scalars(stmt).one_or_none()

    # -- the one convert path -----------------------------------------------------------------------

    def convert_task(
        self,
        run_id: str,
        task_index: int,
        actor: TaskConversionActor,
        *,
        now: datetime | None = None,
    ) -> TaskConversionResult:
        """Convert the staged result of task ``task_index`` of run ``run_id`` into observations.

        Order (each gate leaves prior state unchanged when it refuses):

        1. **Authority FIRST.** A caller lacking :attr:`~app.identity.permissions.Scope.RUN_PLAN_APPROVE`
           is refused (:class:`TaskConversionAuthorizationError`) before any state is read.
        2. **The task exists in THIS business.** The run must exist within this business and carry an
           execution for ``task_index``; otherwise refused (:data:`REASON_EXECUTION_NOT_FOUND`) — an
           unknown or cross-tenant run / task reads exactly like not-found.
        3. **The task is convertible.** Only a :attr:`~app.db.models.DiscoveryTaskStatus.DONE` execution
           (which staged a result) can be converted; a ``DENIED`` / ``FAILED`` task is refused
           (:data:`REASON_TASK_NOT_CONVERTIBLE`).
        4. **Idempotent replay.** An existing conversion for (run, task_index) is returned unchanged (no
           duplicate observations, no new audit) — a task converts once.
        5. **Normalize.** The source terms version is resolved from the business's entitlement (the
           provenance the facts cite); the organization identities the task imported are read; the
           checkpoint is captured as a license-aware DCA-062 evidence object; and each PERMITTED
           organization-level field is recorded as a DCA-061 field observation that cites that evidence.
           A candidate field not on :data:`PERMITTED_OBSERVATION_FIELDS` is dropped, never written.

        On success an atomic DCA-026 audit entry is staged in the caller's unit of work (on top of the
        per-row audits the evidence / field stores already stage). Converting a task reads already
        permitted data and performs no fetch, model call, spend or send.
        """
        moment = now or datetime.now(UTC)

        # 1) Authority FIRST — an unauthorized caller is refused with no state touched.
        if not actor.may_convert():
            raise TaskConversionAuthorizationError(
                "converting a discovery task requires a business administrator holding "
                f"'{Scope.RUN_PLAN_APPROVE.value}'; a read-only viewer or a platform operator "
                "(holding no business scope) cannot perform it"
            )

        # 2) The task must exist WITHIN this business (an unknown / cross-tenant run or task is not-found).
        run = DiscoveryRunStore(self._session, self._business_id).get(run_id)
        execution = self._execution_for(run_id, task_index) if run is not None else None
        if run is None or execution is None:
            raise TaskConversionError(
                REASON_EXECUTION_NOT_FOUND,
                "no such executed discovery task in this business (an unknown or cross-tenant id is "
                "not-found)",
                detail=run_id,
            )

        # 3) Only a DONE execution (which staged a result) is convertible; DENIED / FAILED is refused.
        if execution.status != DiscoveryTaskStatus.DONE.value:
            raise TaskConversionError(
                REASON_TASK_NOT_CONVERTIBLE,
                f"task #{task_index} of run '{run_id}' is '{execution.status}', not done — only a "
                "done task staged a result that can be converted into observations",
                detail=execution.status,
            )

        # 4) Idempotent replay — a task converts ONCE; return the existing record unchanged.
        existing = self.conversion_for(run_id, task_index)
        if existing is not None:
            return TaskConversionResult(
                business_id=self._business_id,
                conversion=existing,
                observations_recorded=0,
                fields_dropped=existing.fields_dropped,
                org_observation_count=existing.org_observation_count,
                evidence_id=existing.evidence_id,
                already_converted=True,
            )

        staged = dict(execution.staged_result or {})
        source_key = (execution.source_key or "").strip()

        # The SourceTerms version the recorded observations + evidence are grounded in. The task ran under
        # an entitled source, so the business's entitlement names the approved terms version to cite.
        source_version = self._resolve_terms_version(source_key, moment)
        if source_version is None:
            raise TaskConversionError(
                REASON_SOURCE_VERSION_UNRESOLVED,
                f"cannot resolve an approved source terms version for '{source_key}' to ground the "
                "observations' provenance",
                detail=source_key,
            )

        actor_audit = actor.as_audit_actor()

        # 5a) Capture the staged checkpoint as a license-aware evidence object (DCA-062) — the grounding
        #     each recorded fact cites. Its raw payload is the opaque staged_result (counts + row refs,
        #     no contact data); retention is DERIVED from the cited terms (a no-retention source keeps
        #     only the reference + digest).
        checkpoint_ref = f"discovery_run:{run_id}/task:{task_index}"
        rights_basis = f"{CONVERSION_SOURCE}:{source_key}"
        evidence = self._evidence.store(
            EvidenceInput(
                source_key=source_key,
                source_version=source_version,
                content_kind=EVIDENCE_CONTENT_KIND,
                content_ref=checkpoint_ref,
                raw_content=json.dumps(staged, sort_keys=True, default=str),
                captured_at=moment,
                rights_basis=rights_basis,
                content_media_type="application/json",
            ),
            actor=actor_audit,
            now=moment,
        )
        provenance_ref = f"{checkpoint_ref}#evidence={evidence.id}"

        # 5b) Read the organization identities this task imported and record each PERMITTED field.
        observations = self._imported_observations(staged, source_key)
        recorded = 0
        dropped = 0
        for obs in observations:
            scope = obs.state_code or "nationwide"
            external_ids = self._observations.external_ids_of(obs.id)
            for field_key, field_value in _candidate_fields(obs, external_ids):
                if field_key not in PERMITTED_OBSERVATION_FIELDS:
                    # A field not on the allowlist is DROPPED — never written to the field set.
                    dropped += 1
                    continue
                self._fields.record(
                    FieldObservationInput(
                        subject_type=FieldSubjectType.ORGANIZATION.value,
                        subject_id=obs.id,
                        field_key=field_key,
                        field_value=field_value,
                        inference_label=FieldInferenceLabel.OBSERVED.value,
                        provenance_ref=provenance_ref,
                        observed_at=moment,
                        scope=scope,
                        rights_basis=rights_basis,
                        source_key=source_key,
                        source_version=source_version,
                    ),
                    actor=actor_audit,
                    now=moment,
                )
                recorded += 1

        conversion = self._stage_conversion(
            run_id=run_id,
            task_index=task_index,
            execution_id=execution.id,
            source_key=source_key,
            source_version=source_version,
            evidence_id=evidence.id,
            org_observation_count=len(observations),
            observations_recorded=recorded,
            fields_dropped=dropped,
            actor=actor,
            moment=moment,
        )
        return TaskConversionResult(
            business_id=self._business_id,
            conversion=conversion,
            observations_recorded=recorded,
            fields_dropped=dropped,
            org_observation_count=len(observations),
            evidence_id=evidence.id,
        )

    # -- internals ----------------------------------------------------------------------------------

    def _resolve_terms_version(self, source_key: str, moment: datetime) -> int | None:
        """The approved SourceTerms version to cite as provenance — the version the business is entitled
        under (falling back to the in-force platform approval's terms version). ``None`` when neither
        resolves (the task could not have run under a resolvable source)."""
        if not source_key:
            return None
        entitlement = self._entitlements.latest(source_key)
        if entitlement is not None and entitlement.approved_terms_version:
            return entitlement.approved_terms_version
        approval = self._entitlements.in_force_approval(source_key, now=moment)
        return approval.terms_version if approval is not None else None

    def _imported_observations(self, staged: dict, source_key: str) -> list[OrgObservation]:
        """The organization observations this staged task referenced, resolved WITHIN this business.

        Maps the staged checkpoint's row refs (imported + skipped) to the DCA-123
        :class:`~app.db.models.OrgObservationImport` provenance rows for the dataset, then loads each
        observation tenant-scoped. Deterministic, oldest-first; a ref with no in-business import is
        ignored (never a cross-tenant read)."""
        dataset_id = str(staged.get("datasetId") or "").strip()
        if not dataset_id or not source_key:
            return []
        refs: list[str] = []
        seen: set[str] = set()
        for key in ("importedRowRefs", "skippedRowRefs"):
            for ref in staged.get(key) or []:
                token = str(ref)
                if token not in seen:
                    seen.add(token)
                    refs.append(token)
        if not refs:
            return []
        stmt = (
            select(OrgObservationImport)
            .where(OrgObservationImport.business_id == self._business_id)
            .where(OrgObservationImport.source_key == source_key)
            .where(OrgObservationImport.dataset_id == dataset_id)
            .where(OrgObservationImport.source_row_ref.in_(refs))
        )
        imports = list(self._session.scalars(stmt).all())
        # Order by the staged ref order so the conversion is deterministic/reproducible.
        order = {ref: i for i, ref in enumerate(refs)}
        imports.sort(key=lambda im: order.get(im.source_row_ref, len(order)))
        resolved: list[OrgObservation] = []
        for im in imports:
            obs = self._observations.get(im.observation_id)
            if obs is not None:
                resolved.append(obs)
        return resolved

    def _stage_conversion(
        self,
        *,
        run_id: str,
        task_index: int,
        execution_id: str,
        source_key: str,
        source_version: int,
        evidence_id: str,
        org_observation_count: int,
        observations_recorded: int,
        fields_dropped: int,
        actor: TaskConversionActor,
        moment: datetime,
    ) -> DiscoveryTaskConversion:
        """Stage the conversion record + its atomic DCA-026 audit entry in the caller's unit of work."""
        provenance = {
            "source": CONVERSION_SOURCE,
            "runId": run_id,
            "taskIndex": task_index,
            "executionId": execution_id,
            # Load-bearing invariants: a conversion reads already-permitted data and never sends or
            # charges. ``converted`` records that the staged result was normalized into observations.
            "converted": True,
            "sent": False,
            "spent": False,
            "autoApproved": False,
        }
        conversion = DiscoveryTaskConversion(
            business_id=self._business_id,
            run_id=run_id,
            task_index=task_index,
            execution_id=execution_id,
            source_key=source_key,
            source_version=source_version,
            evidence_id=evidence_id,
            org_observation_count=org_observation_count,
            observations_recorded=observations_recorded,
            fields_dropped=fields_dropped,
            schema_version=CONVERSION_SCHEMA_VERSION,
            provenance=provenance,
            created_at=moment,
        )
        self._session.add(conversion)

        # Atomic audit (SEC-001/BR-122): staged into the caller's session so it commits with the record.
        # References stay OPAQUE — run/task/execution ids, the source key+version, the evidence id and
        # counts — never a contact value. Converting a task writes observations but sends / charges nothing.
        AuditSink(
            self._session, business_id=self._business_id, actor=actor.as_audit_actor()
        ).record(
            action=AUDIT_ACTION_CONVERT_TASK,
            reason=(
                f"converted staged task #{task_index} of run '{run_id}' into "
                f"{observations_recorded} observation(s) ({fields_dropped} field(s) dropped)"
            ),
            references={
                "runId": run_id,
                "taskIndex": task_index,
                "executionId": execution_id,
                "sourceKey": source_key,
                "sourceVersion": source_version,
                "evidenceId": evidence_id,
                "orgObservationCount": org_observation_count,
                "observationsRecorded": observations_recorded,
                "fieldsDropped": fields_dropped,
            },
        )
        return conversion


def _candidate_fields(
    obs: OrgObservation, external_ids: list[OrgExternalId]
) -> list[tuple[str, str]]:
    """The candidate (field_key, field_value) pairs derived from one organization observation and its
    external-id mappings, in a stable order, BEFORE the permitted-field allowlist is applied.

    The display name becomes ``legal_name``; each attached external register identifier becomes a
    candidate keyed by its scheme (``cin`` / ``pan`` / ``gstin`` / … — only the permitted subset is
    later recorded, the rest are dropped). Pure, deterministic, side-effect free."""
    candidates: list[tuple[str, str]] = []
    display_name = (obs.display_name or "").strip()
    if display_name:
        candidates.append(("legal_name", display_name))
    for ext in sorted(external_ids or [], key=lambda x: (x.scheme, x.value)):
        value = (ext.value or "").strip()
        if value:
            candidates.append((ext.scheme, value))
    return candidates


__all__ = [
    "PERMITTED_OBSERVATION_FIELDS",
    "EVIDENCE_CONTENT_KIND",
    "CONVERSION_SCHEMA_VERSION",
    "AUDIT_ACTION_CONVERT_TASK",
    "CONVERSION_SOURCE",
    "REASON_NOT_AUTHORIZED",
    "REASON_EXECUTION_NOT_FOUND",
    "REASON_TASK_NOT_CONVERTIBLE",
    "REASON_SOURCE_VERSION_UNRESOLVED",
    "TaskConversionError",
    "TaskConversionAuthorizationError",
    "TaskConversionActor",
    "TaskConversionResult",
    "DiscoveryObservationConverter",
]
