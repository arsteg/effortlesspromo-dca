"""DCA-109 — run the Python worker process.

Key: ``worker-loop`` · source sections 2, 3, 8. Stage: Durable Python execution.

One capability: a worker PROCESS/entry point that ties the already-built durable-job mechanics into a
single supervised loop — the counterpart to the host job engine the DCA deliberately does NOT depend on
(acceptance: "no host job engine is needed"). Each pass does three things, all over the stores shipped by
the earlier stories, never any new side effect:

- **Maintenance ticks (safe, already-shipped).** For each enabled tenant it runs the DCA-106 lease
  RECOVERY sweep (:class:`~app.jobs.recovery.JobLeaseRecoveryStore`) — healing a crashed worker's
  abandoned lease — and the DCA-108 due-schedule tick (:class:`~app.services.schedule_tick.DueScheduleEnqueuer`)
  — turning due recurring schedules into enqueued durable jobs. Both only ENQUEUE / re-queue work; neither
  executes a handler or sends anything.
- **Claim + drive the lifecycle.** It then CLAIMS due jobs (DCA-101
  :class:`~app.jobs.claim.JobClaimStore`) and drives each to a safe terminal state. Claiming is a real
  lifecycle mechanic (short transaction, ``FOR UPDATE SKIP LOCKED``, lease + fencing token).
- **Graceful shutdown between safe checkpoints.** A shutdown signal (:meth:`DurableJobWorker.request_shutdown`)
  STOPS NEW CLAIMS at the next checkpoint and RELINQUISHES any claim held mid-flight back to the queue
  (DCA-101 ``release``) so the work is preserved for another worker — never lost, never left wedged
  ``RUNNING`` until its lease lapses (acceptance: "shutdown stops new claims and preserves/relinquishes
  active work safely").

**Execution stays a DISABLED future capability — the headline boundary of this story.** A handler's body
(:meth:`~app.jobs.handlers.JobHandler.run`) still raises ``NotImplementedError`` and **no path here ever
invokes it**. :data:`EXECUTION_ENABLED` is hard-wired ``False``: the worker claims a job and drives the
lifecycle MECHANICS, but it never runs a handler, performs AI/network I/O, or sends anything — the DCA has
no send path, and acceptance of a prospect is not recipient consent. A claimed job is therefore handled
*safely* rather than executed, fail-closed, so the worker never crash-loops and never sends:

- a job whose ``(job_type, schema_version)`` resolves to **no registered handler** (the production
  registry is deliberately empty) is PARKED ``DEAD_LETTER`` with a PII-safe note — a bad/unknown job type
  cannot spin the queue;
- a job whose handler *is* registered is still PARKED ``DEAD_LETTER`` (note
  :data:`PARK_EXECUTION_DISABLED`) because execution is disabled — the worker recognises the disabled
  capability and refuses to run it rather than calling ``run`` and catching a crash.

Parking uses the DCA-104 :class:`~app.jobs.retry.JobRetryScheduler` with a PERMANENT (``NOT_RETRYABLE``)
category, so a parked job reaches a terminal, inspectable state (DCA-105) an operator can requeue once the
execution capability ships — it is never silently retried forever and is no longer claimable, so the drain
always terminates. When :data:`EXECUTION_ENABLED` is flipped on by a FUTURE story this is where the real
``prepare`` → ``run`` → :class:`~app.jobs.complete.JobCompletionStore` commit / retry path is wired in.

FAIR cross-business scheduling (DCA-110) — the load-bearing guarantee this module now adds.
-------------------------------------------------------------------------------------------
A pass no longer drains one tenant to its batch cap before the next tenant gets a turn — which let a
single large backlog monopolize a pass. Instead :meth:`DurableJobWorker.drain_fair` claims jobs
ROUND-ROBIN across the enabled tenants under TWO bounds, so one busy tenant cannot starve another
(acceptance: "B receives progress while A has a large backlog; global and tenant limits are enforced"):

- **Per-business cap (the TENANT limit).** At most ``max_jobs_per_business`` jobs are claimed for any one
  tenant in a pass. A tenant with a huge backlog is capped at this many and the rest of its jobs stay
  ``QUEUED`` for the next pass — it can never consume the whole pass.
- **Global cap (the GLOBAL limit).** At most ``max_jobs_per_pass`` jobs are claimed across ALL tenants in
  a pass, so a pass is always bounded regardless of how many tenants or how deep each backlog is.
- **Round-robin selection.** Each round takes ONE claimable job per still-active tenant, in a stable
  tenant order, so a tenant with a single queued job is serviced in the SAME pass as a tenant with
  thousands — the small tenant never waits behind the big one's backlog.

Fairness is a SELECTION policy layered on top of DCA-101 claiming; it changes which tenant's job is
claimed next, never HOW a job is claimed. Every claim still goes through the tenant-scoped
:class:`~app.jobs.claim.JobClaimStore` (``FOR UPDATE SKIP LOCKED``, lease + fencing token), so tenant
isolation / RLS and the single-current-claim guarantee are fully preserved — a worker still only ever
touches jobs of the tenant it is scoped to. And execution stays the SAME disabled future capability: a
fairly-claimed job is PARKED ``DEAD_LETTER`` exactly as before, never run and never sent.

HIERARCHICAL emergency stops (DCA-111) — the load-bearing guarantee this module now enforces.
------------------------------------------------------------------------------------------------
Before it claims/advances a business's jobs OR runs that business's maintenance ticks, the worker now
consults the :class:`~app.services.emergency_stop.EmergencyStopResolver` at its checkpoints
(:meth:`DurableJobWorker.is_claiming_blocked`). The stop is a two-tier kill switch where the higher tier
DOMINATES the lower (acceptance: "a pause stops new relevant side effects at checkpoints while unrelated
allowed business work can continue"):

- **Platform-wide stop (top tier).** While it is active NO tenant's jobs are claimed/advanced and NO due
  schedule is enqueued for ANY business — it dominates every per-business state.
- **Per-business stop (lower tier).** While a business's own stop is active only THAT business is paused;
  every other, un-stopped business keeps working normally in the same pass.

The gate lives in exactly one place — :meth:`DurableJobWorker._claim_and_handle_one`, the single step both
drains share — so a stopped scope's claim simply returns ``None`` (like nothing-due); the maintenance loop
additionally skips a stopped business's recovery/schedule ticks. The gate only ever ADDS a block (a stop
makes the worker do LESS), so it can never cause a job to run or a send to occur, and clearing a stop
resumes the scope on the next pass. Setting/clearing a stop is authority-gated and audited in
:mod:`app.services.emergency_stop`; this module only READS the effective state. Execution stays the SAME
disabled future capability — a claimable (un-stopped) job is still PARKED ``DEAD_LETTER``, never run, never
sent.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.models import JobRequestStatus, TenantAgent
from app.jobs.claim import DEFAULT_LEASE_SECONDS, JobClaim, JobClaimStore
from app.jobs.handlers import (
    JobHandlerRegistry,
    UnknownJobTypeError,
    UnsupportedSchemaVersionError,
    build_default_registry,
)
from app.jobs.recovery import JobLeaseRecoveryStore, LeaseRecoveryReport
from app.jobs.retry import FailureSignal, JobFailureCategory, JobRetryScheduler
from app.services.emergency_stop import EmergencyStopResolver
from app.services.schedule_tick import DueScheduleEnqueuer, ScheduleTickReport

log = logging.getLogger("dca.jobworker")

# THE boundary of this story: actual handler execution is a DISABLED future capability. While this is
# False the worker drives the claim/lease/park lifecycle mechanics but never runs a handler, never does
# AI/network I/O and never sends. A future execution story flips this and wires the real run/complete path.
EXECUTION_ENABLED = False

# How many claimable jobs one pass drains per business at most — a bounded batch so a pass is never
# unbounded. Parking is terminal (a parked job is no longer claimable), so the drain always terminates;
# the batch simply caps how much one pass does before yielding to maintenance / shutdown checks. It is
# the hard ceiling the DCA-110 per-business cap is additionally clamped to (a cap wider than the batch
# can never drain more than the batch).
DEFAULT_DRAIN_BATCH = 100

# DCA-110 fair-scheduling defaults (used when a Settings value is absent / a caller does not override).
# The per-business cap is the TENANT limit (max jobs one tenant gets per pass); the per-pass cap is the
# GLOBAL limit (max jobs claimed across all tenants per pass). Both are bounded so no tenant is starved
# and no pass is unbounded; a caller/Settings may tune them.
DEFAULT_MAX_JOBS_PER_BUSINESS = 25
DEFAULT_MAX_JOBS_PER_PASS = 200

# PII-safe notes stamped on a parked job's ``last_error`` so an inspector (DCA-105) sees WHY it was parked.
# Stable codes, never contact data — the same discipline every other durable-row note obeys.
PARK_NO_HANDLER = "worker_no_handler_registered"
PARK_EXECUTION_DISABLED = "worker_execution_disabled"


def _default_worker_id() -> str:
    """A stable-enough, unique lease-owner id for ONE worker process (host + pid + a short nonce)."""
    return f"dca-worker-{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


class ClaimDisposition(str, Enum):
    """What the worker did with ONE claimed job — the inspectable per-job outcome.

    Every value is a SAFE, non-executing disposition (execution is disabled):

    - ``PARKED_NO_HANDLER`` — no handler is registered for the job's ``(job_type, schema_version)``; the
      job was parked ``DEAD_LETTER`` (a bad/unknown type cannot spin the queue).
    - ``PARKED_EXECUTION_DISABLED`` — a handler IS registered, but execution is a disabled future
      capability, so the worker refused to run it and parked it ``DEAD_LETTER`` for operator inspection.
    - ``RELINQUISHED`` — a shutdown raced the claim, so the worker released the still-held job back to the
      queue (``QUEUED``) rather than wedging it ``RUNNING``; the work is preserved for another worker.
    """

    PARKED_NO_HANDLER = "parked_no_handler"
    PARKED_EXECUTION_DISABLED = "parked_execution_disabled"
    RELINQUISHED = "relinquished"


@dataclass(frozen=True)
class HandledJob:
    """An inert record of ONE claimed job the worker acted on — never a runnable.

    Carries only opaque / non-identifying fields: the row id + tenant, the logical ``job_type``, the
    disposition and the resulting status, plus the fencing token the worker held and the attempt
    bookkeeping. It never exposes the payload or any contact data. Holding one runs nothing.
    """

    job_id: str
    business_id: str
    job_type: str
    disposition: ClaimDisposition
    status: str
    fence_token: int
    attempt_count: int
    max_attempts: int

    @property
    def parked(self) -> bool:
        return self.disposition in (
            ClaimDisposition.PARKED_NO_HANDLER,
            ClaimDisposition.PARKED_EXECUTION_DISABLED,
        )

    @property
    def relinquished(self) -> bool:
        return self.disposition is ClaimDisposition.RELINQUISHED


@dataclass(frozen=True)
class WorkerPassReport:
    """The inert outcome of ONE :meth:`DurableJobWorker.run_pass` — maintenance + the jobs it drove.

    ``recovered`` / ``scheduled`` map each enabled business to its DCA-106 recovery and DCA-108 schedule
    reports for this pass; ``handled`` lists every claimed job the pass acted on. A reviewer reads the
    counts to confirm the worker healed abandoned leases, fired due schedules and drove claimed jobs to a
    safe terminal state — without running a handler or sending anything. Holding one runs nothing.
    """

    worker_id: str
    started_at: datetime
    business_ids: tuple[str, ...] = ()
    recovered: dict[str, LeaseRecoveryReport] = field(default_factory=dict)
    scheduled: dict[str, ScheduleTickReport] = field(default_factory=dict)
    handled: tuple[HandledJob, ...] = ()
    stopped_early: bool = False

    @property
    def parked_count(self) -> int:
        return sum(1 for h in self.handled if h.parked)

    @property
    def relinquished_count(self) -> int:
        return sum(1 for h in self.handled if h.relinquished)

    @property
    def recovered_count(self) -> int:
        return sum(r.total for r in self.recovered.values())

    @property
    def enqueued_from_schedules(self) -> int:
        return sum(s.created_count for s in self.scheduled.values())

    @property
    def handled_by_business(self) -> dict[str, int]:
        """How many jobs this pass drove PER business — the DCA-110 fairness evidence. A busy tenant is
        capped at the per-business limit while another tenant still shows progress in the same pass."""
        counts: dict[str, int] = {}
        for job in self.handled:
            counts[job.business_id] = counts.get(job.business_id, 0) + 1
        return counts


class DurableJobWorker:
    """The DCA-109 worker process: a supervised loop over the shipped durable-job mechanics.

    Bound to a ``session_factory`` for the DCA-owned database and a ``Settings``. Every per-tenant
    operation goes through a tenant-scoped store (claim / recovery / schedule / retry), so the worker can
    never read or touch another tenant's job. It OWNS no long transaction — each mechanic commits its own
    short unit of work — so a crash between checkpoints is always recoverable by the DCA-106 sweep.

    Execution is a DISABLED future capability (:data:`EXECUTION_ENABLED` is ``False``): the worker claims
    and drives the lifecycle but never runs a handler, performs I/O or sends. A claimed job is parked
    (``DEAD_LETTER``) safely; a shutdown relinquishes held work back to the queue.

    DCA-111: every pass consults the hierarchical emergency stop
    (:meth:`is_claiming_blocked`) at its checkpoints, so a platform-wide stop pauses every business and a
    per-business stop pauses only that business, while un-stopped businesses keep working.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        *,
        registry: JobHandlerRegistry | None = None,
        worker_id: str | None = None,
        clock: Callable[[], datetime] | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        drain_batch: int = DEFAULT_DRAIN_BATCH,
        max_jobs_per_business: int | None = None,
        max_jobs_per_pass: int | None = None,
    ) -> None:
        self._factory = session_factory
        self._settings = settings
        # The production registry is deliberately empty (DCA-100); a caller may inject one for tests.
        self._registry = registry if registry is not None else build_default_registry()
        self._worker_id = worker_id or _default_worker_id()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lease_seconds = int(lease_seconds)
        self._drain_batch = max(1, int(drain_batch))
        # DCA-110 fair-scheduling bounds. A caller may override; otherwise take the Settings value, then
        # the module default. Both are clamped to at least 1 so a pass always makes some fair progress.
        self._max_jobs_per_business = max(
            1,
            int(
                max_jobs_per_business
                if max_jobs_per_business is not None
                else getattr(
                    settings, "worker_max_jobs_per_business_per_pass", DEFAULT_MAX_JOBS_PER_BUSINESS
                )
            ),
        )
        self._max_jobs_per_pass = max(
            1,
            int(
                max_jobs_per_pass
                if max_jobs_per_pass is not None
                else getattr(settings, "worker_max_jobs_per_pass", DEFAULT_MAX_JOBS_PER_PASS)
            ),
        )
        # The graceful-shutdown latch. Set once (idempotent); every checkpoint reads it.
        self._shutdown = threading.Event()

    @property
    def worker_id(self) -> str:
        return self._worker_id

    @property
    def shutting_down(self) -> bool:
        return self._shutdown.is_set()

    def request_shutdown(self) -> None:
        """Ask the worker to stop at the next safe checkpoint (idempotent). Stops NEW claims; any claim
        held mid-flight is relinquished back to the queue. A blocked inter-pass sleep wakes immediately."""
        self._shutdown.set()

    # ---------------------------------------------------------------- tenant enumeration
    def enabled_business_ids(self) -> tuple[str, ...]:
        """Every enabled tenant's business id in a stable order. The DCA-110 fair drain round-robins
        ACROSS this set (one job per tenant per round), so the enumeration order only decides the
        micro-order within a round, never lets an earlier tenant starve a later one."""
        with self._factory() as session:
            rows = session.scalars(
                select(TenantAgent.business_id)
                .where(TenantAgent.enabled.is_(True))
                .order_by(TenantAgent.business_id)
            ).all()
        return tuple(rows)

    # ---------------------------------------------------------------- maintenance mechanics
    def recover(self, business_id: str, *, now: datetime) -> LeaseRecoveryReport:
        """Run the DCA-106 recovery sweep for one tenant (heal abandoned leases). Its own transaction."""
        with self._factory() as session:
            return JobLeaseRecoveryStore(session, business_id).recover_abandoned(now=now)

    def tick_schedules(self, business_id: str, *, now: datetime) -> ScheduleTickReport:
        """Run the DCA-108 due-schedule tick for one tenant (enqueue due cycles). Its own transaction."""
        with self._factory() as session:
            return DueScheduleEnqueuer(session, business_id).tick(now=now)

    # ---------------------------------------------------------------- emergency-stop gate (DCA-111)
    def is_claiming_blocked(self, business_id: str) -> bool:
        """Is this business's background work PAUSED by a hierarchical emergency stop (DCA-111)?

        The worker's checkpoint read: it opens its own short session and asks the
        :class:`~app.services.emergency_stop.EmergencyStopResolver` whether the PLATFORM-WIDE stop is
        active (dominates every tenant) OR this business's OWN stop is active. A stop can only ADD a
        block — it never enables work — so this is the fail-closed gate consulted before claiming/advancing
        a job or running a tenant's maintenance ticks. A business with no stop (and no platform stop) is
        never blocked, so unrelated allowed work continues untouched. Reads no payload; touches nothing."""
        with self._factory() as session:
            return EmergencyStopResolver(session).is_claiming_blocked(business_id)

    # ---------------------------------------------------------------- claim + drive
    def claim_one(self, business_id: str, *, now: datetime) -> JobClaim | None:
        """Claim the next due job for one tenant (DCA-101), or ``None``. A real lifecycle mechanic."""
        with self._factory() as session:
            return JobClaimStore(session, business_id).claim_next(
                self._worker_id, now=now, lease_seconds=self._lease_seconds
            )

    def relinquish(self, claim: JobClaim, *, now: datetime) -> bool:
        """Give a still-held claim back to the queue (DCA-101 ``release``) — the graceful-shutdown path.

        Fenced on the claim's ``(worker_id, fence_token)``: only the current holder may release, so a
        claim the worker no longer legitimately holds changes nothing. Relinquishing preserves the work
        (it returns ``QUEUED``, claimable by another worker); it executes, succeeds and fails nothing."""
        with self._factory() as session:
            return JobClaimStore(session, claim.business_id).release(
                claim.job_id, self._worker_id, claim.fence_token, now=now
            )

    def _park(self, claim: JobClaim, *, now: datetime, note: str) -> int:
        """Park a claimed job ``DEAD_LETTER`` via the DCA-104 scheduler (PERMANENT / non-retryable).

        Returns the fencing token recorded on the parked row. Fenced on the claim's current lease, so a
        job that moved on changes nothing. Parking is terminal and inspectable (DCA-105); the job is no
        longer claimable, so the drain terminates and the worker never crash-loops. Nothing runs."""
        with self._factory() as session:
            result = JobRetryScheduler(session, claim.business_id).record_failure(
                claim.job_id,
                self._worker_id,
                claim.fence_token,
                FailureSignal(category=JobFailureCategory.NOT_RETRYABLE, error_note=note),
                now=now,
            )
            return result.fence_token

    def handle_claim(self, claim: JobClaim, *, now: datetime) -> HandledJob:
        """Drive ONE already-claimed job to a safe terminal state WITHOUT executing it.

        Execution is a disabled future capability, so the worker resolves the handler only to CLASSIFY the
        job (no handler vs a registered-but-unrunnable handler) and parks it ``DEAD_LETTER`` either way —
        it never calls :meth:`~app.jobs.handlers.JobHandler.run`, does no I/O and sends nothing. The FUTURE
        execution story replaces the park branch with the real ``prepare`` → ``run`` → complete/retry path."""
        try:
            self._registry.resolve(claim.job_type, claim.schema_version)
            has_handler = True
        except (UnknownJobTypeError, UnsupportedSchemaVersionError):
            has_handler = False

        if not has_handler:
            note = PARK_NO_HANDLER
            disposition = ClaimDisposition.PARKED_NO_HANDLER
        else:
            # A handler exists, but execution is disabled: recognise the disabled capability and park the
            # job rather than invoking a body that would raise NotImplementedError. Never calls run().
            note = PARK_EXECUTION_DISABLED
            disposition = ClaimDisposition.PARKED_EXECUTION_DISABLED

        fence_token = self._park(claim, now=now, note=note)
        log.info(
            "worker parked job %s (type=%s) as %s", claim.job_id, claim.job_type, disposition.value
        )
        return HandledJob(
            job_id=claim.job_id,
            business_id=claim.business_id,
            job_type=claim.job_type,
            disposition=disposition,
            status=JobRequestStatus.DEAD_LETTER.value,
            fence_token=fence_token,
            attempt_count=claim.attempt_count,
            max_attempts=claim.max_attempts,
        )

    def _claim_and_handle_one(self, business_id: str, *, now: datetime) -> HandledJob | None:
        """Claim ONE due job for a tenant and drive it to a safe terminal state, or ``None`` if nothing
        is claimable. The single shared step both :meth:`drain_business` and the DCA-110
        :meth:`drain_fair` build on, so the shutdown-race / relinquish discipline lives in exactly one
        place. Shutdown is honoured at two checkpoints: BEFORE the claim (stop NEW claims → ``None``), and
        immediately AFTER a claim that raced the shutdown (relinquish the active work back to the queue
        and return a ``RELINQUISHED`` record). It is ALSO the DCA-111 emergency-stop checkpoint: if a
        hierarchical stop (platform-wide, or this business's own) is active, no new claim is made
        (returns ``None``, exactly like nothing-due) — so a paused scope never has a job claimed or
        advanced while an un-stopped business keeps working. Every claim is tenant-scoped (DCA-101), so
        this never touches another tenant's job; nothing here runs a handler or sends."""
        if self._shutdown.is_set():
            return None  # graceful: stop claiming NEW work
        if self.is_claiming_blocked(business_id):
            return None  # DCA-111: an emergency stop pauses this scope — claim/advance nothing
        claim = self.claim_one(business_id, now=now)
        if claim is None:
            return None  # nothing more due for this tenant
        if self._shutdown.is_set():
            # A shutdown raced this claim: relinquish the active work safely (back to QUEUED) rather than
            # leaving it wedged RUNNING until its lease lapses.
            self.relinquish(claim, now=now)
            return HandledJob(
                job_id=claim.job_id,
                business_id=claim.business_id,
                job_type=claim.job_type,
                disposition=ClaimDisposition.RELINQUISHED,
                status="queued",
                fence_token=claim.fence_token,
                attempt_count=claim.attempt_count,
                max_attempts=claim.max_attempts,
            )
        return self.handle_claim(claim, now=now)

    def drain_business(
        self, business_id: str, *, now: datetime, max_jobs: int | None = None
    ) -> list[HandledJob]:
        """Claim and safely drive ONE tenant's due jobs until none remain, the batch cap, or shutdown.

        A single-tenant drain (used directly by tests / the graceful-shutdown demo). Shutdown is honoured
        via :meth:`_claim_and_handle_one`: it stops claiming new work and relinquishes a claim that raced
        the stop, after which this loop ends. A parked job is terminal and no longer claimable, so the
        loop always terminates — the worker never crash-loops on an unrunnable job."""
        limit = self._drain_batch if max_jobs is None else max(0, int(max_jobs))
        handled: list[HandledJob] = []
        for _ in range(limit):
            result = self._claim_and_handle_one(business_id, now=now)
            if result is None:
                break  # shutdown before the claim, or nothing more due
            handled.append(result)
            if result.relinquished:
                break  # a shutdown raced the claim; the work was relinquished — stop
        return handled

    def drain_fair(
        self,
        business_ids: tuple[str, ...],
        *,
        now: datetime,
        max_jobs_per_business: int | None = None,
        max_jobs_per_pass: int | None = None,
    ) -> list[HandledJob]:
        """DCA-110 — claim + safely drive due jobs FAIRLY across tenants, so no busy tenant starves others.

        The load-bearing guarantee: jobs are claimed ROUND-ROBIN across ``business_ids`` under two bounds,
        so a tenant with a large backlog can never monopolize a pass while another tenant waits:

        - **Per-business cap (TENANT limit).** At most ``max_jobs_per_business`` (default: the configured
          per-pass per-business cap, itself clamped to the drain batch) jobs are claimed for any ONE
          tenant; the rest of its backlog stays ``QUEUED`` for a later pass.
        - **Global cap (GLOBAL limit).** At most ``max_jobs_per_pass`` (default: the configured per-pass
          cap) jobs are claimed across ALL tenants, so the drain is always bounded.
        - **Round-robin.** Each round claims ONE job per still-active tenant in the given stable order, so
          a tenant with a single queued job is serviced in the SAME pass as one with a deep backlog.

        A tenant drops out of the rotation once it hits its cap or has nothing more claimable. The loop
        ends when every tenant has dropped out, the global cap is hit, a round makes no progress, or a
        shutdown lands. Fairness only chooses WHICH tenant's job is claimed next — every claim is still the
        tenant-scoped DCA-101 claim (lease + fencing token), so tenant isolation / RLS and the
        single-current-claim guarantee are preserved, and a claimed job is parked exactly as before
        (execution stays a disabled future capability — nothing is run or sent)."""
        if not business_ids:
            return []  # no enabled tenants → a no-op pass
        per_business = (
            self._max_jobs_per_business
            if max_jobs_per_business is None
            else max(0, int(max_jobs_per_business))
        )
        # A per-business cap can never let a single tenant exceed the hard drain-batch ceiling.
        per_business = min(per_business, self._drain_batch)
        global_cap = (
            self._max_jobs_per_pass if max_jobs_per_pass is None else max(0, int(max_jobs_per_pass))
        )

        handled: list[HandledJob] = []
        remaining: dict[str, int] = {bid: per_business for bid in business_ids}
        # Active = tenants still eligible this pass (cap not yet hit, still have claimable work). Preserve
        # the stable enumeration order so the round-robin is deterministic.
        active = [bid for bid in business_ids if remaining[bid] > 0]

        while active and len(handled) < global_cap and not self._shutdown.is_set():
            progressed = False
            # One claim per still-active tenant this round → fair interleaving across tenants.
            for business_id in list(active):
                if self._shutdown.is_set() or len(handled) >= global_cap:
                    break
                if remaining[business_id] <= 0:
                    active.remove(business_id)
                    continue
                result = self._claim_and_handle_one(business_id, now=now)
                if result is None:
                    # Shutdown before the claim, or this tenant has nothing more due — drop it.
                    active.remove(business_id)
                    continue
                handled.append(result)
                remaining[business_id] -= 1
                progressed = True
                if result.relinquished:
                    # A shutdown raced the claim; the work was relinquished safely. Stop the whole drain.
                    return handled
                if remaining[business_id] <= 0:
                    active.remove(business_id)  # tenant hit its per-pass cap
            if not progressed:
                break  # a full round claimed nothing new — the queue is drained for this pass
        return handled

    # ---------------------------------------------------------------- the pass + the loop
    def run_pass(
        self, *, now: datetime | None = None, max_jobs_per_business: int | None = None
    ) -> WorkerPassReport:
        """Run ONE full pass: per-tenant maintenance ticks, then FAIRLY claim + drive due jobs. Honours
        shutdown.

        Order: for each enabled tenant run the DCA-106 recovery sweep and the DCA-108 due-schedule tick
        (safe, enqueue-only); then drain claimable jobs ROUND-ROBIN across tenants (DCA-110
        :meth:`drain_fair`) under the per-business (TENANT) and per-pass (GLOBAL) caps, so a busy tenant
        never starves another. ``max_jobs_per_business`` overrides the per-business cap for this pass
        (e.g. a test narrowing it). Every stage checks the shutdown latch so a stop request halts at the
        next safe checkpoint. A pass that finds nothing due changes nothing. Nothing here runs a handler
        or sends."""
        moment = now or self._clock()
        business_ids = self.enabled_business_ids()
        recovered: dict[str, LeaseRecoveryReport] = {}
        scheduled: dict[str, ScheduleTickReport] = {}
        handled: list[HandledJob] = []
        stopped_early = False

        for business_id in business_ids:
            if self._shutdown.is_set():
                stopped_early = True
                break
            # DCA-111: a hierarchical emergency stop (platform-wide, or this business's own) pauses this
            # business's background work — skip BOTH its maintenance ticks (recovery sweep + due-schedule
            # enqueue, a new side effect) this pass. An un-stopped business is unaffected and continues.
            if self.is_claiming_blocked(business_id):
                continue
            recovered[business_id] = self.recover(business_id, now=moment)
            if self._shutdown.is_set():
                stopped_early = True
                break
            scheduled[business_id] = self.tick_schedules(business_id, now=moment)

        if not self._shutdown.is_set():
            handled.extend(
                self.drain_fair(
                    business_ids, now=moment, max_jobs_per_business=max_jobs_per_business
                )
            )
        if self._shutdown.is_set():
            stopped_early = True

        return WorkerPassReport(
            worker_id=self._worker_id,
            started_at=moment,
            business_ids=business_ids,
            recovered=recovered,
            scheduled=scheduled,
            handled=tuple(handled),
            stopped_early=stopped_early or self._shutdown.is_set(),
        )

    def run(self, *, max_passes: int | None = None, idle_sleep_seconds: float | None = None) -> int:
        """The supervised worker loop: run passes until shutdown (or ``max_passes``). Returns pass count.

        Between passes it sleeps on the shutdown latch (interruptible) for ``idle_sleep_seconds`` (default:
        the configured ``worker_interval_seconds``) so a stop request wakes the loop immediately. A bounded
        ``max_passes`` makes the loop testable without hanging; the real entry point passes neither bound.
        A crash between passes leaves every job in a recoverable state (short committed units of work)."""
        interval = (
            self._settings.worker_interval_seconds
            if idle_sleep_seconds is None
            else idle_sleep_seconds
        )
        passes = 0
        while not self._shutdown.is_set():
            if max_passes is not None and passes >= max_passes:
                break
            self.run_pass()
            passes += 1
            if max_passes is not None and passes >= max_passes:
                break
            if self._shutdown.is_set():
                break
            # Interruptible inter-pass sleep: request_shutdown() wakes it at once.
            self._shutdown.wait(timeout=interval)
        log.info("DCA durable-job worker loop exited after %d pass(es)", passes)
        return passes
