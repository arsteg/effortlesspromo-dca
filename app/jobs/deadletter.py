"""DCA-105 — inspect and requeue a dead-letter job.

Key: ``job-deadletter`` · source sections 2, 3, 8. Stage: Durable Python execution.

DCA-104 ended a job's lifecycle by DEAD-LETTERING it (a permanent policy/auth failure, or a transient
failure that exhausted its attempt cap): the row is parked ``DEAD_LETTER``, its lease cleared, carrying a
PII-safe ``last_error`` note. This story is the operator-facing recovery for that terminal state — a
single capability to **inspect** a parked job's safe reason and, when an operator has fixed the
underlying cause, **requeue** it for another attempt.

Two load-bearing rules, both fail-closed:

- **Only an authorized platform OPERATOR may requeue.** Recovering a parked job is a platform-operations
  decision over the DCA's OWN background-work machinery (the durable queue is operational infrastructure),
  so it is gated on the platform-plane scope
  :attr:`~app.identity.permissions.Scope.JOB_DEADLETTER_REQUEUE` — a *distinct* authority from the
  read scope (``platform.ops.read``), so merely being able to READ platform ops never implies the right
  to MUTATE a job's lifecycle. A tenant administrator holds only business-plane scopes, so their roles
  yield no platform scope and a requeue is refused BEFORE any state is read or written (the relevant
  denial changes nothing). Authority is checked FIRST, exactly like the DCA-050 operator revocation and
  the DCA-084 erasure.

- **A requeued job retains its logical side-effect identity.** Requeue flips the SAME row
  ``DEAD_LETTER`` → ``QUEUED`` in place — it never inserts a new job. The row id and, crucially, the
  ``(business_id, job_type, idempotency_key)`` dedup identity and the ``payload_ref`` / ``payload_digest``
  are all preserved, so the requeued job is the *same logical unit of work* a future worker would run:
  any downstream idempotency keyed on that identity still collapses a re-run to the one logical job
  rather than duplicating its side effects. (A "corrected" job is one whose payload — living behind the
  opaque ``payload_ref`` — the operator fixed out of band; requeue re-runs that same logical job, it does
  not mint a different one.) The attempt counter is reset to ``0`` so the DCA-104 bounded-retry budget
  starts fresh, and ``available_at`` is set to ``now`` so the claim path can pick it up again; the
  dead-letter ``last_error`` is left in place as the inspectable record of why it was parked (a future
  attempt overwrites it).

Tenant isolation is preserved: the store is bound to one verified ``business_id`` (the owning tenant the
operator names), every read/write is confined to it, and a cross-tenant / unknown job id reads as
``NOT_FOUND`` — an operator can never reach another tenant's job by id. The who/when/why is recorded as a
DCA-026 audit entry staged in the SAME unit of work as the flip (atomic audit), scoped to the owning
business and attributed to the platform actor.

**Execution stays a disabled future capability.** Requeuing a job makes it *claimable* again; it does NOT
run it. The handler body (:meth:`~app.jobs.handlers.JobHandler.run`) still raises ``NotImplementedError``,
there is no worker loop that drains the queue, and no path here re-dispatches the requeued ``QUEUED`` row.
A requeue grants nothing and sends nothing — the DCA has no send path, and acceptance of a prospect is not
recipient consent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.sink import ActorType, AuditActor, AuditSink, assert_opaque_text
from app.db.models import DurableJobRequest, JobRequestStatus
from app.identity.permissions import Scope, scopes_for_platform_roles

# The audited action code for an operator requeue (a controlled code, like the other DCA writes).
AUDIT_ACTION_REQUEUE = "deadletter_job.requeued"

# Authorization refusal (raised FIRST, before any state is read or written, so an unauthorized caller —
# notably a tenant administrator — learns nothing about the job and changes nothing).
REASON_NOT_AUTHORIZED = "deadletter_requeue_not_authorized"

# Requeue refusals (each raised / returned BEFORE the row is mutated, so a refusal changes nothing).
REASON_MISSING_REASON = "missing_requeue_reason"


class JobRequeueAuthorizationError(Exception):
    """The caller is not authorized to requeue a dead-letter job (not a platform operator holding the
    requeue scope).

    Raised BEFORE any job is read or staged, so a tenant administrator's (or any non-operator's) attempt
    leaves no trace and reveals nothing about the queue. ``code`` is :data:`REASON_NOT_AUTHORIZED`.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = REASON_NOT_AUTHORIZED


class JobRequeueError(Exception):
    """A requeue was refused with a stable ``code`` for an INPUT problem (e.g. a missing reason).

    Raised before the row is mutated, so a refusal leaves the job exactly as it was. ``detail`` names the
    offending value when useful (never a raw contact value)."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class RequeuePrincipal:
    """The platform principal inspecting / requeuing a dead-letter job.

    ``subject`` is an opaque operator id (never a name/email) recorded as the actor on the atomic audit
    entry. ``roles`` are the caller's PLATFORM-plane roles (from the authenticated, signed principal —
    never a bare header). :meth:`may_requeue` grants the mutate only when those roles yield
    :attr:`~app.identity.permissions.Scope.JOB_DEADLETTER_REQUEUE`; :meth:`may_inspect` gates the
    read on :attr:`~app.identity.permissions.Scope.PLATFORM_OPS_READ`. A tenant administrator's business
    roles never yield either platform scope.

    ``actor_type`` is the audit actor kind: an operator acting on a tenant's job (recorded in that
    tenant's trail) is attributed as :attr:`~app.audit.sink.ActorType.PLATFORM_SUPPORT` so a platform
    action in a tenant context is attributable as such (the DCA-026 distinction), never as an ordinary
    tenant user.
    """

    subject: str
    roles: tuple[str, ...] = ()
    actor_type: str = ActorType.PLATFORM_SUPPORT.value

    def may_inspect(self) -> bool:
        return Scope.PLATFORM_OPS_READ in scopes_for_platform_roles(self.roles)

    def may_requeue(self) -> bool:
        return Scope.JOB_DEADLETTER_REQUEUE in scopes_for_platform_roles(self.roles)

    def as_actor(self) -> AuditActor:
        return AuditActor(id=self.subject, type=ActorType(self.actor_type))


@dataclass(frozen=True)
class DeadLetterDetail:
    """A PII-safe detail view of ONE parked (or any) durable job, for operator inspection.

    Carries only opaque / non-identifying fields: the row id, its tenant scope, the logical dedup
    identity (``job_type`` / ``idempotency_key``), the opaque ``payload_ref`` + one-way ``payload_digest``
    (NEVER the payload itself), the lifecycle status, the attempt bookkeeping and the PII-safe
    ``last_error`` note (the "safe reason" the job was parked). It never exposes the payload or any
    contact data. ``is_dead_letter`` is the convenience flag an operator view keys on.
    """

    job_id: str
    business_id: str
    job_type: str
    idempotency_key: str
    schema_version: int
    status: str
    payload_ref: str
    payload_digest: str
    payload_metadata: dict
    attempt_count: int
    max_attempts: int
    last_error: str | None
    available_at: datetime | None
    created_at: datetime | None
    updated_at: datetime | None
    completed_at: datetime | None

    @property
    def is_dead_letter(self) -> bool:
        return self.status == JobRequestStatus.DEAD_LETTER.value


class RequeueOutcome(str, Enum):
    """Why a :meth:`JobDeadLetterStore.requeue` did (or did not) change the row — the inspectable result.

    ``REQUEUED`` is the only success; every other value is a refusal that changed NOTHING:

    - ``NOT_FOUND`` — no such job in this tenant (a cross-tenant / unknown id reads as not-found).
    - ``NOT_DEAD_LETTER`` — the job is not parked in ``DEAD_LETTER`` (it is queued / running / succeeded /
      failed / cancelled), so there is nothing to recover; requeue only ever revives a dead-lettered job.
      Idempotent: a second requeue of a job already revived to ``QUEUED`` is a no-op refusal.
    """

    REQUEUED = "requeued"
    NOT_FOUND = "not_found"
    NOT_DEAD_LETTER = "not_dead_letter"


@dataclass(frozen=True)
class RequeueResult:
    """The inert outcome of one :meth:`JobDeadLetterStore.requeue` — the applied flip, or a refusal.

    On ``REQUEUED`` it carries the revived ``status`` (``queued``) plus the PRESERVED logical identity
    (``job_type`` / ``idempotency_key``) and the attempt bookkeeping after the reset, so a reviewer can
    confirm the same logical job was revived (not a new one). On a refusal it carries only the outcome
    (and ``prior_status`` for a ``NOT_DEAD_LETTER``); the row is unchanged. Holding one runs nothing."""

    outcome: RequeueOutcome
    job_id: str
    business_id: str = ""
    status: str = ""
    job_type: str = ""
    idempotency_key: str = ""
    attempt_count: int = 0
    max_attempts: int = 0
    prior_status: str = ""
    prior_attempt_count: int = 0
    detail: str = ""

    @property
    def changed(self) -> bool:
        """True iff the row was revived (``DEAD_LETTER`` → ``QUEUED``)."""
        return self.outcome is RequeueOutcome.REQUEUED


class JobDeadLetterStore:
    """Tenant-scoped, operator-facing inspector/requeuer of a dead-letter job (DCA-105).

    Bound to one verified ``business_id`` (the owning tenant an operator names) — a job is read / revived
    only within that business, so an operator can never reach another tenant's job (a cross-tenant id
    reads as not-found). Like :class:`~app.services.data_erasure.OperationalDataEraser` it stages into the
    caller's session and performs no I/O of its own (never commits / flushes / opens a transaction): the
    flip and its atomic DCA-026 audit entry commit together in the caller's unit of work. Nothing here
    runs a job — requeue only makes it claimable again."""

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    @property
    def business_id(self) -> str:
        return self._business_id

    def _current(self, job_id: str) -> DurableJobRequest | None:
        """The job by id WITHIN this tenant, or ``None`` (a cross-tenant / unknown id reads as not-found)."""
        return self._session.scalars(
            select(DurableJobRequest).where(
                DurableJobRequest.id == job_id,
                DurableJobRequest.business_id == self._business_id,
            )
        ).first()

    def _locked_current(self, job_id: str) -> DurableJobRequest | None:
        """As :meth:`_current`, but locked ``FOR UPDATE`` so the status re-check and the flip are atomic
        against a concurrent requeue on PostgreSQL (two operators cannot both revive the same parked job).
        The SQLite dialect omits the clause (serial dev / manual-check path)."""
        return self._session.scalars(
            select(DurableJobRequest)
            .where(
                DurableJobRequest.id == job_id,
                DurableJobRequest.business_id == self._business_id,
            )
            .with_for_update()
        ).first()

    def inspect(self, job_id: str, principal: RequeuePrincipal) -> DeadLetterDetail | None:
        """Return a PII-safe :class:`DeadLetterDetail` for a job within this tenant, or ``None``.

        Authorization is checked FIRST: a caller whose roles lack
        :attr:`~app.identity.permissions.Scope.PLATFORM_OPS_READ` is refused with
        :class:`JobRequeueAuthorizationError` before any job is read (so an unauthorized caller learns
        nothing). An unknown / cross-tenant id reads as ``None``. The detail carries only opaque /
        non-identifying fields and the PII-safe ``last_error`` — never the payload or any contact data.
        Reading changes nothing."""
        if not principal.may_inspect():
            raise JobRequeueAuthorizationError(
                "inspecting a dead-letter job requires a platform operator holding "
                f"'{Scope.PLATFORM_OPS_READ.value}'; a tenant administrator holds no platform scope"
            )
        row = self._current(job_id)
        if row is None:
            return None
        return DeadLetterDetail(
            job_id=row.id,
            business_id=row.business_id,
            job_type=row.job_type,
            idempotency_key=row.idempotency_key,
            schema_version=int(row.schema_version),
            status=row.status,
            payload_ref=row.payload_ref,
            payload_digest=row.payload_digest,
            payload_metadata=dict(row.payload_metadata or {}),
            attempt_count=int(row.attempt_count),
            max_attempts=int(row.max_attempts),
            last_error=row.last_error,
            available_at=row.available_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
            completed_at=row.completed_at,
        )

    def requeue(
        self,
        job_id: str,
        principal: RequeuePrincipal,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> RequeueResult:
        """Revive a dead-lettered job for another attempt, or refuse without changing anything.

        Authorization is checked FIRST: a caller whose roles lack
        :attr:`~app.identity.permissions.Scope.JOB_DEADLETTER_REQUEUE` is refused with
        :class:`JobRequeueAuthorizationError` before any state is read or written (the relevant denial —
        a tenant administrator can never requeue). Then a non-empty, opaque ``reason`` is required
        (:class:`JobRequeueError`; screened like an audit reference so it carries no contact data), raised
        before the row is mutated.

        The flip is honoured only when the job is parked in ``DEAD_LETTER``; otherwise the row is left
        untouched and a refusal outcome is returned (``NOT_FOUND`` for an unknown / cross-tenant id,
        ``NOT_DEAD_LETTER`` for any non-parked status). On success the SAME row flips ``DEAD_LETTER`` →
        ``QUEUED`` in place — PRESERVING its ``(business_id, job_type, idempotency_key)`` logical identity
        and its ``payload_ref`` / ``payload_digest`` (so the revived job is the same logical unit of work,
        not a duplicate) — the attempt counter is reset to ``0`` (a fresh DCA-104 budget), ``available_at``
        is set to ``now`` (claimable again) and any residual lease is cleared. A DCA-026 audit entry
        (who/when/why, opaque references) is staged in the SAME unit of work. The result is returned
        uncommitted; the caller owns the transaction. Reviving a job makes it claimable; it EXECUTES
        nothing."""
        # 1) Authority FIRST — an unauthorized caller is refused with no state touched.
        if not principal.may_requeue():
            raise JobRequeueAuthorizationError(
                "requeuing a dead-letter job requires a platform operator holding "
                f"'{Scope.JOB_DEADLETTER_REQUEUE.value}'; a tenant administrator holds no platform "
                "scope, and the read scope alone does not grant it"
            )

        reason_text = (reason or "").strip()
        if not reason_text:
            raise JobRequeueError(
                REASON_MISSING_REASON,
                "a requeue must carry a non-empty reason (an opaque code/explanation of why the parked "
                "job is being revived)",
            )
        # Screen the reason like an audit reference: it is persisted on the audit entry and must not
        # carry a raw contact value / secret. A refusal here is raised before the row is mutated.
        assert_opaque_text("reason", reason_text)

        moment = now or datetime.now(UTC)

        row = self._locked_current(job_id)
        if row is None:
            # A cross-tenant / unknown id changes nothing.
            return RequeueResult(RequeueOutcome.NOT_FOUND, job_id)

        if row.status != JobRequestStatus.DEAD_LETTER.value:
            # Only a parked job can be revived — a queued/running/succeeded/failed/cancelled job (and a
            # job already requeued to QUEUED) is left exactly as it is. Nothing changes.
            return RequeueResult(
                RequeueOutcome.NOT_DEAD_LETTER,
                job_id=row.id,
                business_id=row.business_id,
                status=row.status,
                job_type=row.job_type,
                idempotency_key=row.idempotency_key,
                attempt_count=int(row.attempt_count),
                max_attempts=int(row.max_attempts),
                prior_status=row.status,
                prior_attempt_count=int(row.attempt_count),
                detail=row.status,
            )

        prior_attempts = int(row.attempt_count)
        # Revive the SAME row in place — the logical identity (id + job_type + idempotency_key) and the
        # payload pointer/digest are untouched, so the revived job is the same logical unit of work. Reset
        # the attempt budget, make it claimable again, and clear any residual lease. The dead-letter
        # last_error is kept as the inspectable record of why it was parked (a future attempt overwrites).
        row.status = JobRequestStatus.QUEUED.value
        row.attempt_count = 0
        row.available_at = moment
        row.lease_owner = None
        row.lease_expires_at = None
        row.lease_deadline = None
        row.updated_at = moment

        # Atomic audit (SEC-001/BR-122): staged into the caller's session so it commits with the flip.
        # References stay OPAQUE — the job id, the logical dedup identity, the prior status and attempt
        # counts — and the audit retains only this permitted minimal metadata, never the payload.
        AuditSink(self._session, business_id=row.business_id, actor=principal.as_actor()).record(
            action=AUDIT_ACTION_REQUEUE,
            reason=reason_text,
            references={
                "jobId": row.id,
                "jobType": row.job_type,
                "idempotencyKey": row.idempotency_key,
                "payloadDigest": row.payload_digest,
                "priorStatus": JobRequestStatus.DEAD_LETTER.value,
                "priorAttemptCount": prior_attempts,
                "maxAttempts": int(row.max_attempts),
                "authorityPlane": "platform",
                "authorityScope": Scope.JOB_DEADLETTER_REQUEUE.value,
            },
        )

        return RequeueResult(
            RequeueOutcome.REQUEUED,
            job_id=row.id,
            business_id=row.business_id,
            status=row.status,
            job_type=row.job_type,
            idempotency_key=row.idempotency_key,
            attempt_count=int(row.attempt_count),
            max_attempts=int(row.max_attempts),
            prior_status=JobRequestStatus.DEAD_LETTER.value,
            prior_attempt_count=prior_attempts,
        )
