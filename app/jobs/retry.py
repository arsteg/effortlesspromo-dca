"""DCA-104 — schedule bounded retries.

Key: ``job-retry`` · source sections 2, 3, 8. Stage: Durable Python execution.

One capability: when a worker reports that a leased job attempt FAILED, decide — deterministically and
fail-closed — whether that failure is worth RETRYING and, if so, WHEN. Three things make the decision
"bounded":

- **Error classification.** A failure carries a stable, PII-safe :class:`JobFailureCategory`. Each
  category maps (``classify_failure``) to exactly one :class:`JobFailureClass`: ``TRANSIENT`` (a
  temporary condition another attempt may clear — a timeout, a network blip, a provider 5xx/throttle) or
  ``PERMANENT`` (a condition another attempt will NEVER clear — a compliance/policy denial, a revoked
  source authorization, a failed credential, an invalid payload, or an explicit "do not retry"). This is
  the headline acceptance: **a PERMANENT policy/auth failure is never retried as if it were transient** —
  it is dead-lettered immediately, even when attempts remain.
- **Attempt caps.** A transient failure is retried only while the job still has attempts left
  (``attempt_count < max_attempts`` — ``attempt_count`` is the count DCA-101 already bumped when it
  leased this attempt). Once the cap is reached the job is dead-lettered (``DEAD_LETTERED_EXHAUSTED``), so
  a flapping transient error cannot be retried forever.
- **Capped exponential backoff with jitter + provider Retry-After.** The next-run time for a retry is
  ``now + delay`` where ``delay`` is an exponential backoff (``BASE * 2**(attempt-1)``) CAPPED at
  ``MAX_BACKOFF_SECONDS`` and spread with equal jitter (so a thundering herd of simultaneous failures do
  not all retry on the same tick). A provider-supplied ``Retry-After`` is honoured as a FLOOR — a retry
  is never scheduled sooner than the provider said it is safe to — clamped to ``MAX_RETRY_AFTER_SECONDS``
  so an absurd header cannot park a job indefinitely. The jitter source is injectable
  (:class:`random.Random`) so the schedule is deterministic in tests / the manual check.

Two layers, matching the DCA-101/102/103 shape:

- A **pure** decision layer (:func:`classify_failure`, :func:`compute_backoff_seconds`,
  :func:`plan_retry`) that is a deterministic function of its inputs and touches no database — so the
  classification and the backoff schedule can be reasoned about and tested in isolation.
- A **tenant-scoped, fenced store** (:class:`JobRetryScheduler`) that applies a plan to a running job's
  row, but ONLY after re-validating the current lease fence in the same short transaction as the write —
  exactly the DCA-103 completion discipline. A stale/replaced worker (its job was reclaimed) or one whose
  lease already lapsed cannot schedule a retry or dead-letter a job that has moved on; each refusal
  changes NOTHING and returns the relevant denial.

**Execution stays a disabled future capability.** Scheduling a retry records WHEN a job may next be
attempted (status ``FAILED`` with ``available_at`` pushed out) or parks it (``DEAD_LETTER``); it does NOT
run the handler (:meth:`~app.jobs.handlers.JobHandler.run` still raises ``NotImplementedError`` and no
path here invokes it), and there is NO worker loop that re-dispatches a due ``FAILED`` row — that
re-dispatch is the currently-disabled worker capability. A retry schedules nothing to send: the DCA has
no send path, and acceptance of a prospect is not recipient consent.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import DurableJobRequest, JobRequestStatus
from app.jobs.store import _contains_raw_pii

# Capped exponential backoff parameters. The exponential grows ``BASE * 2**(attempt-1)`` but is clamped
# at MAX_BACKOFF_SECONDS so the delay never runs away; equal jitter (JITTER_RATIO of the window is
# randomised) spreads simultaneous failures across the window so they do not all retry on the same tick.
BASE_BACKOFF_SECONDS = 2.0
MAX_BACKOFF_SECONDS = 600.0
JITTER_RATIO = 0.5
# A provider Retry-After is honoured as a floor, but clamped so a hostile/absurd header cannot park a job
# effectively forever (the DCA stays in control of its own schedule bounds).
MAX_RETRY_AFTER_SECONDS = 3600.0


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalise a DB datetime to tz-aware UTC before comparing to an aware ``now``.

    SQLite reads timestamps back NAIVE (dev/manual-check path); PostgreSQL keeps them aware. Treat a naive
    value as UTC so the live-only comparison never raises on a naive-vs-aware mismatch (same discipline as
    :mod:`app.jobs.claim` / :mod:`app.jobs.complete`)."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


class JobFailureCategory(str, Enum):
    """The stable, PII-safe category of a reported attempt failure — the classifier's only input.

    A category is a fixed code (never a free-text message, never tenant data) a future worker records for
    an attempt it could not complete. ``classify_failure`` maps each to TRANSIENT or PERMANENT:

    TRANSIENT — another attempt may clear it:
    - ``TIMEOUT`` — the attempt did not finish in time.
    - ``NETWORK`` — a connection/transport error reaching a dependency.
    - ``PROVIDER_UNAVAILABLE`` — a dependency returned a server-side (5xx) / unavailable error.
    - ``RATE_LIMITED`` — a dependency throttled the attempt (usually carrying a Retry-After).

    PERMANENT — another attempt will NEVER clear it, so it is dead-lettered immediately (not retried):
    - ``POLICY_DENIED`` — a compliance/rulepack decision refused the work.
    - ``AUTHORIZATION_REVOKED`` — the source rights the attempt relied on are no longer in force.
    - ``AUTHENTICATION_FAILED`` — a credential/identity was rejected.
    - ``PAYLOAD_INVALID`` — the payload failed its typed contract (a DCA-100 validation failure).
    - ``NOT_RETRYABLE`` — an explicit, caller-declared non-retryable failure.

    ``UNKNOWN`` — an unclassified failure; treated as TRANSIENT (so it is retried, but still strictly
    bounded by the attempt cap and backoff). Misreading a PERMANENT failure as transient is the one
    outcome the acceptance forbids, so only clearly-permanent categories are permanent; everything else
    (including UNKNOWN) is a bounded transient.
    """

    TIMEOUT = "timeout"
    NETWORK = "network"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    RATE_LIMITED = "rate_limited"
    POLICY_DENIED = "policy_denied"
    AUTHORIZATION_REVOKED = "authorization_revoked"
    AUTHENTICATION_FAILED = "authentication_failed"
    PAYLOAD_INVALID = "payload_invalid"
    NOT_RETRYABLE = "not_retryable"
    UNKNOWN = "unknown"


class JobFailureClass(str, Enum):
    """Whether a failure is worth retrying at all — the result of classification.

    ``TRANSIENT`` may be retried (bounded by the attempt cap + backoff); ``PERMANENT`` is never retried
    and is dead-lettered on the first occurrence, even when attempts remain."""

    TRANSIENT = "transient"
    PERMANENT = "permanent"


# The categories that are PERMANENT — a policy/auth/validation condition another attempt cannot clear.
# Everything NOT in this set (including UNKNOWN) classifies as a bounded TRANSIENT.
_PERMANENT_CATEGORIES: frozenset[JobFailureCategory] = frozenset(
    {
        JobFailureCategory.POLICY_DENIED,
        JobFailureCategory.AUTHORIZATION_REVOKED,
        JobFailureCategory.AUTHENTICATION_FAILED,
        JobFailureCategory.PAYLOAD_INVALID,
        JobFailureCategory.NOT_RETRYABLE,
    }
)


def classify_failure(category: JobFailureCategory) -> JobFailureClass:
    """Map a failure category to its retry class — pure, total and deterministic.

    PERMANENT iff the category is a policy/auth/validation condition (``_PERMANENT_CATEGORIES``); every
    other category — transient infrastructure errors AND the catch-all ``UNKNOWN`` — is TRANSIENT. This is
    the single place "is this retryable?" is decided, so the completion path and any future worker agree."""
    return (
        JobFailureClass.PERMANENT
        if category in _PERMANENT_CATEGORIES
        else JobFailureClass.TRANSIENT
    )


@dataclass(frozen=True)
class FailureSignal:
    """A PII-safe description of ONE failed attempt — the input a worker hands the scheduler.

    ``category`` drives classification; ``error_note`` is a short, stable, PII-safe note recorded to the
    row's ``last_error`` (screened for raw contact data before anything is written — raw PII never lands
    on the durable row, the same rule the queue enforces on ``payload_ref``); ``retry_after_seconds`` is an
    optional provider Retry-After (seconds) honoured as a FLOOR on the next-run delay."""

    category: JobFailureCategory
    error_note: str = ""
    retry_after_seconds: float | None = None

    @property
    def failure_class(self) -> JobFailureClass:
        return classify_failure(self.category)


def compute_backoff_seconds(
    attempt_count: int,
    *,
    rng: random.Random,
    retry_after_seconds: float | None = None,
) -> float:
    """The delay (seconds) before a transient retry — capped exponential backoff + jitter + Retry-After.

    ``BASE_BACKOFF_SECONDS * 2**(attempt_count-1)`` CAPPED at ``MAX_BACKOFF_SECONDS``, then spread with
    equal jitter into ``[capped*(1-JITTER_RATIO), capped]`` using ``rng`` (injected so the schedule is
    deterministic in tests). A positive ``retry_after_seconds`` raises the delay to at least that many
    seconds (a provider floor — never retry sooner than it said), clamped to ``MAX_RETRY_AFTER_SECONDS``.
    Pure: a deterministic function of its inputs (given ``rng``); touches no state."""
    attempt = max(1, int(attempt_count))
    exponential = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
    capped = min(exponential, MAX_BACKOFF_SECONDS)
    low = capped * (1.0 - JITTER_RATIO)
    delay = low + rng.random() * (capped - low)
    if retry_after_seconds is not None and retry_after_seconds > 0:
        delay = max(delay, min(float(retry_after_seconds), MAX_RETRY_AFTER_SECONDS))
    return delay


class RetryAction(str, Enum):
    """What :func:`plan_retry` decided to do with a failed attempt.

    - ``SCHEDULE_RETRY`` — a transient failure with attempts remaining: retry at ``next_available_at``.
    - ``DEAD_LETTER_PERMANENT`` — a permanent failure: never retried, parked immediately.
    - ``DEAD_LETTER_EXHAUSTED`` — a transient failure whose attempt cap is reached: parked, no more tries.
    """

    SCHEDULE_RETRY = "schedule_retry"
    DEAD_LETTER_PERMANENT = "dead_letter_permanent"
    DEAD_LETTER_EXHAUSTED = "dead_letter_exhausted"


@dataclass(frozen=True)
class RetryPlan:
    """The pure, inspectable decision for one failed attempt — no state applied yet.

    Computed by :func:`plan_retry` from the failure class, the job's attempt bookkeeping and ``now``. On a
    retry it carries the backoff ``delay_seconds`` and the resulting ``next_available_at``; on a dead-letter
    both are ``None`` (nothing is scheduled)."""

    action: RetryAction
    failure_class: JobFailureClass
    attempt_count: int
    max_attempts: int
    delay_seconds: float | None = None
    next_available_at: datetime | None = None

    @property
    def will_retry(self) -> bool:
        return self.action is RetryAction.SCHEDULE_RETRY

    @property
    def attempts_remaining(self) -> int:
        return max(0, int(self.max_attempts) - int(self.attempt_count))


def plan_retry(
    signal: FailureSignal,
    *,
    attempt_count: int,
    max_attempts: int,
    now: datetime,
    rng: random.Random,
) -> RetryPlan:
    """Decide what to do with a failed attempt — pure, deterministic, fail-closed.

    Order matters and encodes the acceptance criteria:

    1. **Permanent first.** A ``PERMANENT`` failure (policy/auth/validation) is dead-lettered immediately
       (``DEAD_LETTER_PERMANENT``) REGARDLESS of attempts remaining — it is never retried as a transient.
    2. **Attempt cap.** A transient failure whose ``attempt_count >= max_attempts`` is dead-lettered
       (``DEAD_LETTER_EXHAUSTED``) — the cap is honoured, no forever-retrying.
    3. **Schedule.** Otherwise a retry is scheduled at ``now + compute_backoff_seconds(...)`` (capped
       exponential backoff + jitter + any provider Retry-After floor)."""
    cls = signal.failure_class
    attempts = int(attempt_count)
    cap = int(max_attempts)
    if cls is JobFailureClass.PERMANENT:
        return RetryPlan(RetryAction.DEAD_LETTER_PERMANENT, cls, attempts, cap)
    if attempts >= cap:
        return RetryPlan(RetryAction.DEAD_LETTER_EXHAUSTED, cls, attempts, cap)
    delay = compute_backoff_seconds(
        attempts, rng=rng, retry_after_seconds=signal.retry_after_seconds
    )
    return RetryPlan(
        RetryAction.SCHEDULE_RETRY,
        cls,
        attempts,
        cap,
        delay_seconds=delay,
        next_available_at=now + timedelta(seconds=delay),
    )


class RetryScheduleOutcome(str, Enum):
    """Why a :meth:`JobRetryScheduler.record_failure` did (or did not) change the row — the denial reason.

    Applied outcomes (the row changed):
    - ``RETRY_SCHEDULED`` — a transient failure scheduled a bounded retry (status ``FAILED``, next-run
      pushed out, lease cleared).
    - ``DEAD_LETTERED_PERMANENT`` — a permanent failure parked the job (``DEAD_LETTER``), no retry.
    - ``DEAD_LETTERED_EXHAUSTED`` — a transient failure at its attempt cap parked the job (``DEAD_LETTER``).

    Refusals (nothing changed — the fenced-lease denials, exactly as DCA-103):
    - ``NOT_FOUND`` — no such job in this tenant (a cross-tenant id reads as not-found).
    - ``STALE_LEASE`` — the presented ``(worker_id, fence_token)`` is not the current lease, or the row is
      no longer ``RUNNING`` (it was reclaimed, so the fencing token moved on).
    - ``LEASE_EXPIRED`` — the lease has already lapsed; a reclaimable job may not be failed by its old
      holder.
    """

    RETRY_SCHEDULED = "retry_scheduled"
    DEAD_LETTERED_PERMANENT = "dead_lettered_permanent"
    DEAD_LETTERED_EXHAUSTED = "dead_lettered_exhausted"
    NOT_FOUND = "not_found"
    STALE_LEASE = "stale_lease"
    LEASE_EXPIRED = "lease_expired"


@dataclass(frozen=True)
class RetryScheduleResult:
    """The inert outcome of one :meth:`JobRetryScheduler.record_failure` — the applied plan, or a refusal.

    On an applied outcome it carries the resulting ``status``, the ``attempt_count`` / ``max_attempts`` and
    (for a scheduled retry) the ``failure_class`` / ``delay_seconds`` / ``next_available_at`` so a reviewer
    can inspect the next-run time directly; on a refusal it carries only the outcome (the row is unchanged).
    Holding one runs nothing."""

    outcome: RetryScheduleOutcome
    job_id: str
    business_id: str = ""
    status: str = ""
    fence_token: int = 0
    attempt_count: int = 0
    max_attempts: int = 0
    failure_class: JobFailureClass | None = None
    delay_seconds: float | None = None
    next_available_at: datetime | None = None
    detail: str = ""

    @property
    def scheduled_retry(self) -> bool:
        return self.outcome is RetryScheduleOutcome.RETRY_SCHEDULED

    @property
    def dead_lettered(self) -> bool:
        return self.outcome in (
            RetryScheduleOutcome.DEAD_LETTERED_PERMANENT,
            RetryScheduleOutcome.DEAD_LETTERED_EXHAUSTED,
        )

    @property
    def changed(self) -> bool:
        """True iff the row was mutated (a retry scheduled or the job dead-lettered)."""
        return self.scheduled_retry or self.dead_lettered


class JobErrorNoteContainsPiiError(Exception):
    """A failure's ``error_note`` carried raw contact data (an email/phone).

    Raised BEFORE any row is read, so a rejected failure leaves the job exactly as it was. ``last_error``
    stores only a short, stable, PII-safe note; raw contact data belongs nowhere on the durable row (the
    same rule the queue enforces on ``payload_ref`` / ``result_ref``)."""


class JobRetryScheduler:
    """Tenant-scoped, worker-side scheduler of a fenced retry/dead-letter (DCA-104).

    Bound to one verified ``business_id`` — a job is read/updated only within that business, so a worker
    can never fail another tenant's job (a cross-tenant id reads as not-found). Like
    :class:`~app.jobs.complete.JobCompletionStore` it OWNS its transaction: :meth:`record_failure` locks the
    row ``FOR UPDATE``, re-checks the current lease fence, and commits (or rolls back) the applied plan in
    one short transaction. Nothing here runs a job — it only schedules WHEN one may next be attempted, or
    parks it."""

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    @property
    def business_id(self) -> str:
        return self._business_id

    def _locked_current(self, job_id: str) -> DurableJobRequest | None:
        """The job by id WITHIN this tenant, locked ``FOR UPDATE`` (a cross-tenant id reads as not-found).

        ``FOR UPDATE`` makes the fence re-check and the write atomic against a concurrent reclaim on
        PostgreSQL; the SQLite dialect omits the clause (serial dev/manual-check path)."""
        return self._session.scalars(
            select(DurableJobRequest)
            .where(
                DurableJobRequest.id == job_id,
                DurableJobRequest.business_id == self._business_id,
            )
            .with_for_update()
        ).first()

    def record_failure(
        self,
        job_id: str,
        worker_id: str,
        fence_token: int,
        signal: FailureSignal,
        *,
        now: datetime,
        rng: random.Random | None = None,
    ) -> RetryScheduleResult:
        """Record a failed attempt and apply a bounded retry/dead-letter plan, or refuse without change.

        The update is honoured only when the presented ``(worker_id, fence_token)`` is the CURRENT lease on
        a ``RUNNING`` row (else ``STALE_LEASE`` — a reclaim bumped the fencing token) and the lease has not
        lapsed (``now < lease_expires_at``; else ``LEASE_EXPIRED`` — a reclaimable job may not be failed by
        its old holder); an unknown/cross-tenant id is ``NOT_FOUND``. Each refusal rolls back and changes
        NOTHING — exactly the DCA-103 fenced-lease discipline.

        When the fence holds, :func:`plan_retry` decides the outcome from the failure class + the row's
        attempt bookkeeping:

        - a PERMANENT failure → ``DEAD_LETTER`` (never retried, even with attempts left);
        - a TRANSIENT failure at its attempt cap → ``DEAD_LETTER`` (cap honoured);
        - otherwise → status ``FAILED`` with ``available_at`` pushed out by capped exponential backoff +
          jitter + any provider Retry-After floor (the scheduled retry).

        In every applied case the lease is cleared and the PII-safe ``error_note`` is recorded to
        ``last_error``. ``attempt_count`` is NOT incremented here — DCA-101 already bumped it when it leased
        this attempt. An ``error_note`` carrying raw contact data raises :class:`JobErrorNoteContainsPiiError`
        before anything is read. Scheduling a retry records WHEN the job may next run; it EXECUTES nothing,
        and no path here re-dispatches a due ``FAILED`` row (that is the disabled future worker loop)."""
        if not worker_id:
            raise ValueError("worker_id must be a non-empty worker identifier")
        # PII is refused before any state is read, so a rejected failure leaves the job untouched.
        if signal.error_note and _contains_raw_pii(signal.error_note):
            raise JobErrorNoteContainsPiiError("error_note must not contain raw contact data")
        rng = rng or random.Random()

        row = self._locked_current(job_id)
        if row is None:
            # A cross-tenant / unknown id: end the (read-only) transaction so no lock/scope lingers.
            self._session.rollback()
            return RetryScheduleResult(RetryScheduleOutcome.NOT_FOUND, job_id)

        # 1) Fenced on the current lease: RUNNING, owned by this worker, at this exact fencing token. A
        #    reclaim bumped lease_version (and may have changed the owner), so a replaced worker's token is
        #    stale and refused here — it cannot fail/retry a job that moved on to another worker.
        if (
            row.status != JobRequestStatus.RUNNING.value
            or row.lease_owner != worker_id
            or int(row.lease_version) != int(fence_token)
        ):
            self._session.rollback()
            return RetryScheduleResult(
                RetryScheduleOutcome.STALE_LEASE,
                job_id,
                business_id=row.business_id,
                status=row.status,
                fence_token=int(row.lease_version),
                attempt_count=int(row.attempt_count),
                max_attempts=int(row.max_attempts),
            )

        # 2) Live-only: an already-lapsed lease is reclaimable, so its old holder may not fail/retry it.
        expiry = _as_utc(row.lease_expires_at)
        if expiry is not None and now >= expiry:
            self._session.rollback()
            return RetryScheduleResult(
                RetryScheduleOutcome.LEASE_EXPIRED,
                job_id,
                business_id=row.business_id,
                status=row.status,
                fence_token=int(row.lease_version),
                attempt_count=int(row.attempt_count),
                max_attempts=int(row.max_attempts),
            )

        # Fence holds — decide (purely) what to do with this attempt, then apply it in this transaction.
        plan = plan_retry(
            signal,
            attempt_count=int(row.attempt_count),
            max_attempts=int(row.max_attempts),
            now=now,
            rng=rng,
        )

        # In every applied case the lease is cleared (the attempt is over) and the PII-safe note recorded.
        row.lease_owner = None
        row.lease_expires_at = None
        row.last_error = signal.error_note or None
        row.updated_at = now

        if plan.will_retry:
            # Scheduled retry: FAILED (retry-pending) with the next-run time pushed out. The row is NOT
            # re-claimable by the current claim path (which picks QUEUED / lapsed-RUNNING) — re-dispatching
            # a due FAILED row is the disabled future worker loop; this story only records WHEN it may run.
            assert plan.next_available_at is not None  # will_retry always carries a next-run time
            row.status = JobRequestStatus.FAILED.value
            row.available_at = plan.next_available_at
            outcome = RetryScheduleOutcome.RETRY_SCHEDULED
        else:
            # Dead-letter: a permanent failure, or a transient one whose attempt cap is reached. Parked for
            # inspection; never retried. available_at is left as-is (it schedules nothing while parked).
            row.status = JobRequestStatus.DEAD_LETTER.value
            outcome = (
                RetryScheduleOutcome.DEAD_LETTERED_PERMANENT
                if plan.action is RetryAction.DEAD_LETTER_PERMANENT
                else RetryScheduleOutcome.DEAD_LETTERED_EXHAUSTED
            )

        result = RetryScheduleResult(
            outcome,
            job_id=row.id,
            business_id=row.business_id,
            status=row.status,
            fence_token=int(row.lease_version),
            attempt_count=int(row.attempt_count),
            max_attempts=int(row.max_attempts),
            failure_class=plan.failure_class,
            delay_seconds=plan.delay_seconds,
            next_available_at=plan.next_available_at,
        )
        self._session.commit()
        return result
