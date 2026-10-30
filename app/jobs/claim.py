"""DCA-101 — claim durable jobs safely across workers.

Key: ``job-claim`` · source sections 2, 3, 8. Stage: Durable Python execution.

One capability: let many workers pull work off the DCA-099 durable queue WITHOUT two workers ever
holding the same *current* claim, and WITHOUT a slow worker holding a database lock while it does
external I/O. The mechanism is the standard safe-queue pattern on PostgreSQL:

- **Short transaction + ``SELECT ... FOR UPDATE SKIP LOCKED``.** :meth:`JobClaimStore.claim_next`
  opens a transaction, selects ONE claimable row with ``FOR UPDATE SKIP LOCKED`` (so a row another
  worker is mid-claim on is skipped, not waited on — no thundering herd, no blocking), flips it to
  ``RUNNING`` with a lease, and **COMMITS**. The row lock is held only for that tiny flip and is
  released at commit, *before* the caller ever touches the handler — so slow external work can never
  pin a row lock (acceptance: "slow external work does not hold row locks indefinitely").
- **Lease + fencing token.** The commit stamps ``lease_owner`` (the claiming worker),
  ``lease_expires_at`` (when the claim lapses), and bumps ``lease_version`` — a monotonic FENCING
  TOKEN handed to the claimer. While the lease is live no other worker can claim the row (the claim
  query excludes leased rows), so two workers never hold the same current claim. If the worker crashes
  or stalls past ``lease_expires_at``, another worker may RECLAIM the row — bumping the fencing token
  again, which makes the original holder's token stale. A guarded op presented with a stale token is
  refused (:meth:`JobClaimStore.renew` / :meth:`JobClaimStore.release`), so the lapsed worker can never
  stomp a job that has moved on.

**Execution stays a disabled future capability.** Claiming a job LEASES it; it does not run it. The
handler body (:meth:`~app.jobs.handlers.JobHandler.run`) raises ``NotImplementedError`` and no path
here invokes it, there is no worker loop that drains the queue, and nothing advances a job to
succeeded/failed/dead-letter. A claim grants nothing and sends nothing — the DCA has no send path, and
acceptance of a prospect is not recipient consent.

**Bounded lease renewal (DCA-102).** :meth:`JobClaimStore.renew` is the heartbeat a long-but-live job
uses to keep its claim from lapsing, and it is bounded on three axes so a lapsed/replaced/over-running
worker can never hold a claim it no longer legitimately holds: it is fenced on the current
``(lease_owner, lease_version)`` (a *replaced* worker's token is stale and refused), it is refused once
the lease has already lapsed (an *expired* worker must let the job be reclaimed, not self-renew), and it
never pushes the lease past the row's absolute ``lease_deadline`` (so a stuck/looping worker cannot
heartbeat forever and starve a reclaim). Renewing keeps a claim alive; it still executes nothing.

Portability note: ``FOR UPDATE SKIP LOCKED`` is a PostgreSQL feature and is where the real
cross-worker safety lives, which is why the focused tests exercise two concurrent workers on a
disposable PostgreSQL (never SQLite). On SQLite the ``FOR UPDATE`` clause is simply omitted by the
dialect, so the lease/fencing *logic* can still be driven serially (dev/manual-check), but the
row-lock isolation is only real on PostgreSQL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import DurableJobRequest, JobRequestStatus

# How long a fresh claim's lease lasts unless the caller overrides it. Deliberately short: a lease only
# needs to outlive a healthy handler's work; a crashed/stalled worker's job becomes reclaimable once it
# lapses. A future worker heartbeats (:meth:`JobClaimStore.renew`) to keep a long-but-live job leased.
DEFAULT_LEASE_SECONDS = 300

# DCA-102 bounded-extension rules. A heartbeat keeps a long-but-live job leased, but within hard bounds
# so a stuck/looping worker can never hold a claim forever and starve a reclaim:
#   1. A single renewal window is capped at MAX_LEASE_SECONDS (a caller cannot ask for an unbounded one).
#   2. The lease is pushed out only up to the row's absolute ``lease_deadline`` (stamped at claim time as
#      now + MAX_LEASE_LIFETIME_SECONDS); a renewal never extends past it.
# Both are generous enough that DEFAULT_LEASE_SECONDS and normal heartbeats are never clamped.
MAX_LEASE_SECONDS = 900
MAX_LEASE_LIFETIME_SECONDS = 3600


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalise a DB datetime to tz-aware UTC before comparing to an aware ``now``.

    SQLite reads timestamps back NAIVE (dev/manual-check path); PostgreSQL keeps them aware. Treat a
    naive value as UTC so the DCA-102 live-only / deadline comparisons never raise on a naive-vs-aware
    mismatch (same discipline as the tenants accept route)."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


@dataclass(frozen=True)
class JobClaim:
    """An inert record of ONE successful claim — the lease a worker was handed, never a runnable.

    It carries the pointer + digest of the leased job (never raw PII, exactly like the row) and the
    ``fence_token`` the worker must present to any guarded follow-up op. Holding a :class:`JobClaim` does
    not execute anything: a future worker would resolve the handler and run it OUTSIDE any transaction;
    this story never does.
    """

    job_id: str
    business_id: str
    job_type: str
    schema_version: int
    payload_ref: str
    payload_digest: str
    payload_metadata: dict = field(default_factory=dict)
    attempt_count: int = 0
    max_attempts: int = 0
    worker_id: str = ""
    # The monotonic lease version the claimer holds — the fencing token. A later op is honoured only
    # while the row's lease_version still equals this; a superseded (reclaimed) lease makes it stale.
    fence_token: int = 0
    lease_expires_at: datetime | None = None
    # DCA-102: the absolute latest a heartbeat may push ``lease_expires_at`` out to for THIS claim. A
    # renewal is bounded by it; the worker can read it to know how much total runway a claim has left.
    lease_deadline: datetime | None = None


class JobClaimStore:
    """Tenant-scoped, worker-side claimer for the durable job queue.

    Bound to one verified ``business_id`` — every claim and every guarded op is filtered to that
    business, so a worker can never claim or touch another tenant's job (the same discipline as
    :class:`~app.jobs.store.JobRequestStore`). Unlike the enqueue store, the claimer OWNS its
    transaction: :meth:`claim_next` commits the short claim itself so the row lock is released before the
    caller does any work. Nothing here runs a job.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    @property
    def business_id(self) -> str:
        return self._business_id

    def claim_next(
        self,
        worker_id: str,
        *,
        now: datetime,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> JobClaim | None:
        """Claim the next available job in a SHORT transaction, or ``None`` if nothing is claimable.

        Claimable = a row in this business that is due (``available_at <= now``) and NOT currently
        leased — either ``QUEUED`` (never claimed) or ``RUNNING`` with a lapsed lease
        (``lease_expires_at <= now``, i.e. a crashed/stalled worker's job, reclaimed). The row is
        selected with ``FOR UPDATE SKIP LOCKED`` so a row another worker is mid-claim on is skipped
        rather than waited on, then flipped to ``RUNNING`` with a fresh lease and an incremented fencing
        token, and the transaction is COMMITTED — releasing the row lock before the caller touches any
        handler. The returned :class:`JobClaim` carries the fencing token; the job is leased, not run.

        ``worker_id`` must be a non-empty identifier for the claiming worker (it is stamped as the lease
        owner so a guarded follow-up op can prove it still holds the lease)."""
        if not worker_id:
            raise ValueError("worker_id must be a non-empty worker identifier")

        stmt = (
            select(DurableJobRequest)
            .where(
                DurableJobRequest.business_id == self._business_id,
                DurableJobRequest.available_at <= now,
                or_(
                    DurableJobRequest.status == JobRequestStatus.QUEUED.value,
                    (DurableJobRequest.status == JobRequestStatus.RUNNING.value)
                    & (DurableJobRequest.lease_expires_at <= now),
                ),
            )
            # Oldest-due first, then oldest-created — a stable, fair pull order.
            .order_by(DurableJobRequest.available_at, DurableJobRequest.created_at)
            .limit(1)
            # The queue-safety primitive: lock exactly the picked row, and skip (never block on) a row
            # another worker already holds. PostgreSQL-only; the SQLite dialect omits FOR UPDATE.
            .with_for_update(skip_locked=True)
        )
        row = self._session.scalars(stmt).first()
        if row is None:
            # Nothing to claim — end the (empty) transaction so no lock/scope lingers on the connection.
            self._session.commit()
            return None

        # DCA-102: stamp the absolute lifetime ceiling for THIS claim (a reclaim gets a fresh budget),
        # then clamp the first lease window to it — the same bound every later renewal obeys.
        deadline = now + timedelta(seconds=MAX_LEASE_LIFETIME_SECONDS)
        expires = min(now + timedelta(seconds=lease_seconds), deadline)
        row.status = JobRequestStatus.RUNNING.value
        row.lease_owner = worker_id
        row.lease_expires_at = expires
        row.lease_deadline = deadline
        row.lease_version = int(row.lease_version) + 1
        row.attempt_count = int(row.attempt_count) + 1
        row.updated_at = now

        claim = JobClaim(
            job_id=row.id,
            business_id=row.business_id,
            job_type=row.job_type,
            schema_version=int(row.schema_version),
            payload_ref=row.payload_ref,
            payload_digest=row.payload_digest,
            payload_metadata=dict(row.payload_metadata or {}),
            attempt_count=int(row.attempt_count),
            max_attempts=int(row.max_attempts),
            worker_id=worker_id,
            fence_token=int(row.lease_version),
            lease_expires_at=expires,
            lease_deadline=deadline,
        )
        # Commit the short claim: the FOR UPDATE row lock is released HERE, before any handler I/O.
        self._session.commit()
        return claim

    def _current(self, job_id: str) -> DurableJobRequest | None:
        """The job by id WITHIN this tenant, or ``None`` (a cross-tenant id reads as not-found)."""
        row = self._session.get(DurableJobRequest, job_id)
        if row is None or row.business_id != self._business_id:
            return None
        return row

    def holds_current_lease(self, job_id: str, worker_id: str, fence_token: int) -> bool:
        """Read-only fencing check: does ``worker_id`` still hold the job's CURRENT lease at this token?

        True only when the row is ``RUNNING`` and its ``lease_owner`` / ``lease_version`` both still match
        what the worker was handed. A superseded (reclaimed) job has a higher ``lease_version``, so an old
        token returns False — the proof that only one current claim exists even after a lease handover.
        Touches no state."""
        row = self._current(job_id)
        return (
            row is not None
            and row.status == JobRequestStatus.RUNNING.value
            and row.lease_owner == worker_id
            and int(row.lease_version) == int(fence_token)
        )

    def renew(
        self,
        job_id: str,
        worker_id: str,
        fence_token: int,
        *,
        now: datetime,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> JobClaim | None:
        """Heartbeat: extend a still-LIVE lease (DCA-102), fenced on the token and bounded.

        A long-but-live job calls this to keep its claim from lapsing. The renewal is honoured only when
        ALL of the following hold; any failure changes NOTHING and returns ``None`` (the relevant denial),
        so a lapsed/replaced/over-running worker can never keep a claim it no longer legitimately holds:

        - **Fenced on the current lease.** The presented ``(worker_id, fence_token)`` must still be the
          live lease (:meth:`holds_current_lease`). A reclaim bumped the fencing token, so a *replaced*
          worker's token is stale and refused — it cannot renew (or, later, complete) with an old fence.
        - **Live-only.** The lease must not have already lapsed (``now < lease_expires_at``). An *expired*
          worker is refused even if no other worker has reclaimed the row yet: a lapsed lease is
          up-for-grabs, so extending it would risk two live claims. The expired worker must let the job be
          reclaimed, not self-renew.
        - **Within the absolute ceiling.** The lease may not be pushed past the row's ``lease_deadline``
          (the DCA-102 bound stamped at claim time). Once ``now >= lease_deadline`` the heartbeat is
          refused, so a stuck/looping worker cannot hold a claim forever and starve a reclaim.

        On success the lease window is pushed out — by at most ``MAX_LEASE_SECONDS`` and never past
        ``lease_deadline`` — WITHOUT bumping the fencing token (the same claim continues). Renewing keeps a
        claim alive; it still executes nothing (execution stays a disabled future capability)."""
        if not self.holds_current_lease(job_id, worker_id, fence_token):
            # A stale/foreign token touches nothing; leave the transaction clean for the caller.
            self._session.rollback()
            return None
        row = self._current(job_id)
        assert row is not None  # holds_current_lease already proved it within this tenant

        current_expiry = _as_utc(row.lease_expires_at)
        deadline = _as_utc(row.lease_deadline)
        # Live-only bound: an already-lapsed lease is reclaimable, so an expired holder may not renew it.
        if current_expiry is not None and now >= current_expiry:
            self._session.rollback()
            return None
        # Absolute-lifetime bound: never extend once (or past) the claim's hard ceiling.
        if deadline is not None and now >= deadline:
            self._session.rollback()
            return None

        # Per-renewal window is capped, then clamped to the absolute deadline — bounded extension.
        window = min(int(lease_seconds), MAX_LEASE_SECONDS)
        expires = now + timedelta(seconds=window)
        if deadline is not None and expires > deadline:
            expires = deadline
        row.lease_expires_at = expires
        row.updated_at = now
        claim = JobClaim(
            job_id=row.id,
            business_id=row.business_id,
            job_type=row.job_type,
            schema_version=int(row.schema_version),
            payload_ref=row.payload_ref,
            payload_digest=row.payload_digest,
            payload_metadata=dict(row.payload_metadata or {}),
            attempt_count=int(row.attempt_count),
            max_attempts=int(row.max_attempts),
            worker_id=worker_id,
            fence_token=int(row.lease_version),
            lease_expires_at=expires,
            lease_deadline=deadline,
        )
        self._session.commit()
        return claim

    def release(self, job_id: str, worker_id: str, fence_token: int, *, now: datetime) -> bool:
        """Give a still-held claim back to the queue (``QUEUED``, lease cleared), fenced on the token.

        The sanctioned way for a worker to relinquish a job it could not finish so another worker may
        pick it up. Only the current lease holder may release: a stale/foreign ``(worker_id, fence_token)``
        changes NOTHING and returns False (the relevant denial — a lapsed worker whose job was already
        reclaimed cannot reset it). Clearing the lease does not execute, succeed, or fail the job; it only
        makes it claimable again."""
        if not self.holds_current_lease(job_id, worker_id, fence_token):
            self._session.rollback()
            return False
        row = self._current(job_id)
        assert row is not None
        row.status = JobRequestStatus.QUEUED.value
        row.lease_owner = None
        row.lease_expires_at = None
        row.updated_at = now
        self._session.commit()
        return True
