"""DCA-106 — recover abandoned job leases.

Key: ``lease-recovery`` · source sections 2, 3, 8. Stage: Durable Python execution.

One capability: a RECOVERY SWEEP that finds ``RUNNING`` rows whose lease has LAPSED — the signature of a
worker that crashed or stalled mid-attempt — and safely makes that work retryable again under a NEW
fence, so a process crash never permanently loses the work AND the crashed process can never later commit
a result for a job that has moved on. This is the automated, proactive counterpart to the opportunistic
reclaim already inside :meth:`~app.jobs.claim.JobClaimStore.claim_next` (which reclaims a lapsed lease
only when some worker happens to pull the queue next); the sweep heals abandoned leases on its own
schedule, independently of whether a claimer is waiting, and keeps the recovery BOUNDED.

Two load-bearing rules, both fail-closed:

- **A crash does not lose work, and the old holder is fenced off.** For each abandoned lease the sweep
  bumps ``lease_version`` — a NEW fencing token — and clears ``lease_owner`` / ``lease_expires_at`` /
  ``lease_deadline``, then flips the row back to ``QUEUED`` and makes it due (``available_at = now``) so a
  healthy worker can pick it up again. Bumping the fence is the safety guarantee: the crashed worker still
  holds its OLD ``(lease_owner, fence_token)``, and because the row is no longer ``RUNNING`` at that token,
  its :meth:`~app.jobs.complete.JobCompletionStore.complete` / ``renew`` / ``record_failure`` are all
  refused as a stale lease — "its old process cannot later commit." (The lapsed holder's own commit was
  already refused by the DCA-103 live-only rule; the fence bump makes the denial hold even if a new holder
  then takes the row.)

- **Recovery is BOUNDED — it never resurrects a job past its attempt cap.** The crashed attempt already
  consumed one attempt (DCA-101 bumped ``attempt_count`` when it leased the job). A sweep that always
  re-queued would let a job whose worker crashes every attempt be recovered forever. So when a recovered
  job has already used its whole budget (``attempt_count >= max_attempts``) the sweep DEAD-LETTERS it
  instead of re-queueing — the same cap DCA-104 honours on a reported failure, applied to an *unreported*
  crash. A dead-lettered recovery is parked for operator inspection / requeue (DCA-105), never silently
  retried. Either way the fence is bumped, so the crashed holder is fenced off regardless of the outcome.

The sweep only ever touches an ABANDONED lease (``RUNNING`` with ``lease_expires_at <= now``): a LIVE
lease (its worker still within the window), a ``QUEUED`` / ``FAILED`` / ``SUCCEEDED`` / ``DEAD_LETTER`` /
``CANCELLED`` row, and any OTHER tenant's row are all left exactly as they are — a recovery run that finds
no abandoned lease changes nothing. Tenant isolation is preserved: the store is bound to one verified
``business_id`` and every read/write is confined to it.

Portability + concurrency note: the sweep selects with ``FOR UPDATE SKIP LOCKED`` and commits a SHORT
transaction, so it never blocks on (and is never blocked by) a live claim/complete holding a row, and two
sweeps racing the same abandoned row recover it exactly once. ``FOR UPDATE SKIP LOCKED`` is a PostgreSQL
feature (the SQLite dialect omits it), which is why the focused tests exercise the crash → recover →
commit-denied flow and the concurrent-sweep safety on a disposable PostgreSQL, never SQLite; the recovery
*logic* can still be driven serially on SQLite for dev / the manual check.

**Execution stays a disabled future capability.** Recovering a lease makes the job CLAIMABLE again (or
parks it); it does NOT run it. The handler body (:meth:`~app.jobs.handlers.JobHandler.run`) still raises
``NotImplementedError``, there is no worker loop that drains the queue, and no path here re-dispatches a
recovered ``QUEUED`` row. A recovery grants nothing and sends nothing — the DCA has no send path, and
acceptance of a prospect is not recipient consent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import DurableJobRequest, JobRequestStatus

# How many abandoned leases one sweep recovers at most. A sweep is a short transaction over a bounded
# batch so it never holds a long scan; a scheduler calls it repeatedly to drain a large backlog. Generous
# enough for a normal run, small enough to keep each transaction short.
DEFAULT_RECOVERY_BATCH = 100

# PII-safe notes stamped on a recovered row's ``last_error`` so an inspector (DCA-105) can see WHY the row
# was reset. Stable codes, never contact data — the same discipline every other durable-row note obeys. A
# future attempt overwrites the re-queued note; the dead-letter note is the parked job's safe reason.
RECOVERY_NOTE = "lease_recovered_after_abandonment"
RECOVERY_EXHAUSTED_NOTE = "lease_recovery_attempts_exhausted"


class LeaseRecoveryOutcome(str, Enum):
    """What the sweep did with ONE abandoned lease — the inspectable per-job result.

    - ``RECOVERED`` — the job still had attempts left, so it was made retryable: flipped back to
      ``QUEUED`` and due (``available_at = now``) under a bumped fence.
    - ``DEAD_LETTERED_EXHAUSTED`` — the crashed attempt was the job's last (``attempt_count >=
      max_attempts``), so instead of resurrecting it forever the sweep parked it ``DEAD_LETTER`` (the
      DCA-104 attempt cap, applied to an unreported crash). Still fenced off; recoverable by an operator
      requeue (DCA-105).
    """

    RECOVERED = "recovered"
    DEAD_LETTERED_EXHAUSTED = "dead_lettered_exhausted"


@dataclass(frozen=True)
class RecoveredLease:
    """An inert record of ONE abandoned lease the sweep acted on — never a runnable.

    Carries only opaque / non-identifying fields: the row id + tenant, the logical ``job_type``, the
    crashed holder's PRIOR ``(lease_owner, fence_token)`` and the NEW ``fence_token`` that fences it off,
    the attempt bookkeeping, the lapsed expiry, and the resulting status/outcome. It never exposes the
    payload or any contact data. Holding one runs nothing."""

    job_id: str
    business_id: str
    job_type: str
    outcome: LeaseRecoveryOutcome
    status: str
    prior_lease_owner: str | None
    prior_fence_token: int
    fence_token: int
    attempt_count: int
    max_attempts: int
    lease_expired_at: datetime | None

    @property
    def requeued(self) -> bool:
        return self.outcome is LeaseRecoveryOutcome.RECOVERED

    @property
    def dead_lettered(self) -> bool:
        return self.outcome is LeaseRecoveryOutcome.DEAD_LETTERED_EXHAUSTED


@dataclass(frozen=True)
class LeaseRecoveryReport:
    """The inert outcome of one :meth:`JobLeaseRecoveryStore.recover_abandoned` sweep.

    Lists every abandoned lease recovered in the run (empty when none were found — the sweep then changed
    nothing), scoped to the one business the store is bound to. A reviewer reads the counts and the
    per-job :class:`RecoveredLease` records to confirm the fence advanced and the right outcome applied."""

    business_id: str
    swept_at: datetime
    recovered: tuple[RecoveredLease, ...] = field(default_factory=tuple)

    @property
    def total(self) -> int:
        return len(self.recovered)

    @property
    def requeued_count(self) -> int:
        return sum(1 for r in self.recovered if r.requeued)

    @property
    def dead_lettered_count(self) -> int:
        return sum(1 for r in self.recovered if r.dead_lettered)

    @property
    def changed(self) -> bool:
        """True iff the sweep acted on at least one abandoned lease."""
        return bool(self.recovered)


class JobLeaseRecoveryStore:
    """Tenant-scoped recovery sweep for abandoned durable-job leases (DCA-106).

    Bound to one verified ``business_id`` — every read and write is confined to that business, so a sweep
    can never recover or touch another tenant's job (the same discipline as
    :class:`~app.jobs.claim.JobClaimStore`). Like the claim store it OWNS its transaction:
    :meth:`recover_abandoned` selects a bounded batch with ``FOR UPDATE SKIP LOCKED``, applies the recovery
    in place and COMMITS the short transaction — releasing every row lock before returning. Nothing here
    runs a job; recovery only makes work claimable again (or parks it)."""

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    @property
    def business_id(self) -> str:
        return self._business_id

    def recover_abandoned(
        self, *, now: datetime, limit: int = DEFAULT_RECOVERY_BATCH
    ) -> LeaseRecoveryReport:
        """Recover this business's abandoned leases, or change nothing if there are none.

        An abandoned lease is a ``RUNNING`` row whose ``lease_expires_at <= now`` — a worker that crashed
        or stalled past its lease. The sweep selects up to ``limit`` such rows (oldest-lapsed first) with
        ``FOR UPDATE SKIP LOCKED`` — so it skips, never blocks on, a row a live claim/complete currently
        holds, and two concurrent sweeps recover any one row exactly once — and for each:

        - bumps ``lease_version`` (a NEW fencing token) and clears ``lease_owner`` / ``lease_expires_at`` /
          ``lease_deadline``, so the crashed holder's old token is stale and it can no longer commit/renew/
          fail the job ("its old process cannot later commit");
        - if the job still has attempts left (``attempt_count < max_attempts``) flips it back to ``QUEUED``
          and due (``available_at = now``) so it is retryable (``RECOVERED``); otherwise parks it
          ``DEAD_LETTER`` (``DEAD_LETTERED_EXHAUSTED`` — the attempt cap honoured for an unreported crash),
          never resurrecting a job past its budget;
        - records a PII-safe ``last_error`` note of why the row was reset.

        A LIVE lease, a non-``RUNNING`` row and any other tenant's row are never selected, so a sweep that
        finds no abandoned lease leaves every row untouched. The short transaction is COMMITTED here; the
        returned :class:`LeaseRecoveryReport` is inert. Recovery makes work claimable; it EXECUTES
        nothing (execution stays a disabled future capability)."""
        batch = max(1, int(limit))
        stmt = (
            select(DurableJobRequest)
            .where(
                DurableJobRequest.business_id == self._business_id,
                DurableJobRequest.status == JobRequestStatus.RUNNING.value,
                DurableJobRequest.lease_expires_at.is_not(None),
                DurableJobRequest.lease_expires_at <= now,
            )
            # Oldest-lapsed first (then oldest-created) — a stable, fair recovery order.
            .order_by(DurableJobRequest.lease_expires_at, DurableJobRequest.created_at)
            .limit(batch)
            # Lock exactly the abandoned rows, and skip (never block on) a row a live claim/complete still
            # holds. PostgreSQL-only; the SQLite dialect omits FOR UPDATE (serial dev / manual-check path).
            .with_for_update(skip_locked=True)
        )
        rows = self._session.scalars(stmt).all()

        recovered: list[RecoveredLease] = []
        for row in rows:
            prior_owner = row.lease_owner
            prior_fence = int(row.lease_version)
            attempts = int(row.attempt_count)
            cap = int(row.max_attempts)
            lease_expired_at = row.lease_expires_at

            # NEW FENCE — the crashed holder's old (lease_owner, fence_token) is now stale, so it can no
            # longer commit/renew/fail this job even if it comes back. Clear the lapsed lease.
            row.lease_version = prior_fence + 1
            row.lease_owner = None
            row.lease_expires_at = None
            row.lease_deadline = None
            row.updated_at = now

            if attempts >= cap:
                # Budget exhausted by the crashed attempt: park it (never resurrect past the cap). It stays
                # recoverable by an operator requeue (DCA-105); available_at is left as-is while parked.
                row.status = JobRequestStatus.DEAD_LETTER.value
                row.last_error = RECOVERY_EXHAUSTED_NOTE
                outcome = LeaseRecoveryOutcome.DEAD_LETTERED_EXHAUSTED
            else:
                # Attempts remain: make the work retryable again — claimable and due right now.
                row.status = JobRequestStatus.QUEUED.value
                row.available_at = now
                row.last_error = RECOVERY_NOTE
                outcome = LeaseRecoveryOutcome.RECOVERED

            recovered.append(
                RecoveredLease(
                    job_id=row.id,
                    business_id=row.business_id,
                    job_type=row.job_type,
                    outcome=outcome,
                    status=row.status,
                    prior_lease_owner=prior_owner,
                    prior_fence_token=prior_fence,
                    fence_token=int(row.lease_version),
                    attempt_count=attempts,
                    max_attempts=cap,
                    lease_expired_at=lease_expired_at,
                )
            )

        # Commit the short sweep: every FOR UPDATE row lock is released HERE, before returning.
        self._session.commit()
        return LeaseRecoveryReport(
            business_id=self._business_id,
            swept_at=now,
            recovered=tuple(recovered),
        )
