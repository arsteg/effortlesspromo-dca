"""DCA-103 — commit a fenced job result.

Key: ``job-complete`` · source sections 2, 3, 8. Stage: Durable Python execution.

One capability: let the worker that holds a job's CURRENT lease COMMIT a result — flip the running row to
``SUCCEEDED``, clear its lease, and record an opaque result pointer — but ONLY after re-validating, in the
same short transaction as the commit, both (a) that it still holds the current lease (the DCA-101 fencing
token), and (b) that the authorization revisions it relied on while producing the result are still in
force. Either check failing refuses the commit and changes NOTHING (the relevant denial):

- **Fenced on the current lease.** The presented ``(worker_id, fence_token)`` must still be the live lease
  on a ``RUNNING`` row. A reclaim bumped the fencing token (DCA-101), so a *replaced* worker's token is
  stale and refused — it cannot commit a result for a job that has moved on to another worker.
- **Live-only.** The lease must not have already lapsed (``now < lease_expires_at``). An expired lease is
  up-for-grabs (another worker may reclaim it at any moment), so an expired holder may not commit — exactly
  the same live-only discipline the DCA-102 heartbeat uses. The expired worker must let the job be reclaimed.
- **Authorization revisions still in force.** The worker presents the source-rights revisions it operated
  against (``(source_key, terms_version)`` pairs). Each is re-checked against the authoritative
  :meth:`~app.services.source_approval.SourceApprovalStore.is_in_force` at ``now``. If a platform operator
  revoked (DCA-050) or let a relevant approval lapse (DCA-049) since the worker captured it, the rights were
  pulled out from under it and the commit is refused (``source_rights_changed``) — the DCA-050 acceptance
  made concrete at the commit boundary: "an already-running task that captured the old (now-revoked) source
  version is denied when it re-checks before committing what it fetched."

The row is read ``FOR UPDATE`` so the fence/rights re-check and the commit are atomic on PostgreSQL: a
concurrent reclaim (which claims a row only once its lease has lapsed, with ``SKIP LOCKED``) cannot
interleave between the check and the commit — it either waited and then sees ``SUCCEEDED``, or skipped the
locked row. On SQLite the ``FOR UPDATE`` clause is omitted by the dialect, so the completion *logic* is
driven serially (dev/manual-check), but the row-lock isolation is only real on PostgreSQL — which is why
the focused tests run the commit-vs-reclaim race on a disposable PostgreSQL, never SQLite.

**Execution stays a disabled future capability.** Committing a result records the TERMINAL status plus an
opaque result pointer + digest (never raw PII, never the payload/result itself) — it does NOT run the
handler body (:meth:`~app.jobs.handlers.JobHandler.run` still raises ``NotImplementedError`` and no path
here invokes it), and there is no worker loop that drains the queue. A committed result grants nothing and
sends nothing — the DCA has no send path, and acceptance of a prospect is not recipient consent.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import DurableJobRequest, JobRequestStatus
from app.jobs.store import _contains_raw_pii
from app.services.source_approval import SourceApprovalStore


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalise a DB datetime to tz-aware UTC before comparing to an aware ``now``.

    SQLite reads timestamps back NAIVE (dev/manual-check path); PostgreSQL keeps them aware. Treat a naive
    value as UTC so the live-only comparison never raises on a naive-vs-aware mismatch (same discipline as
    :mod:`app.jobs.claim`)."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def job_result_digest(result_ref: str) -> str:
    """A stable one-way fingerprint of a committed result pointer — never the result itself.

    Mirrors :func:`app.jobs.store.job_payload_digest`: the digest pins exactly what was committed so a
    later reader can confirm the stored result without the row ever carrying the result data (or PII)."""
    return hashlib.sha256(result_ref.encode("utf-8")).hexdigest()


class JobCompletionOutcome(str, Enum):
    """Why a :meth:`JobCompletionStore.complete` did (or did not) commit — the inspectable denial reason.

    ``COMMITTED`` is the only success; every other value is a refusal that changed NOTHING:

    - ``NOT_FOUND`` — no such job in this tenant (a cross-tenant id reads as not-found).
    - ``STALE_LEASE`` — the presented ``(worker_id, fence_token)`` is not the current lease (the job was
      reclaimed, so the fencing token moved on), or the row is no longer ``RUNNING``.
    - ``LEASE_EXPIRED`` — the lease has lapsed; a reclaimable job may not be committed by its old holder.
    - ``SOURCE_RIGHTS_CHANGED`` — an authorization revision the worker relied on is no longer in force
      (revoked/lapsed since it was captured); :attr:`JobCompletionResult.detail` names the first one.
    """

    COMMITTED = "committed"
    NOT_FOUND = "not_found"
    STALE_LEASE = "stale_lease"
    LEASE_EXPIRED = "lease_expired"
    SOURCE_RIGHTS_CHANGED = "source_rights_changed"


@dataclass(frozen=True)
class SourceAuthorizationRef:
    """One source-rights revision a worker operated against — a ``(source_key, terms_version)`` pointer.

    This is the authorization the worker captured when it started the job (exactly the "old source version"
    the DCA-050 revocation story talks about). At commit time :meth:`JobCompletionStore.complete` re-checks
    each ref is still in force; a revision that was revoked/lapsed in the meantime refuses the commit."""

    source_key: str
    terms_version: int

    @property
    def label(self) -> str:
        return f"{self.source_key}@v{self.terms_version}"


@dataclass(frozen=True)
class JobCompletionResult:
    """The inert outcome of one completion attempt — the committed result, or a refusal that changed nothing.

    On ``COMMITTED`` it carries the terminal status + the opaque result pointer/digest actually stored; on a
    refusal it carries only the outcome (and, for a rights refusal, which revision lapsed). Holding one runs
    nothing: completion records a terminal status, it does not execute the job."""

    outcome: JobCompletionOutcome
    job_id: str
    business_id: str = ""
    status: str = ""
    fence_token: int = 0
    result_ref: str | None = None
    result_digest: str | None = None
    completed_at: datetime | None = None
    detail: str = ""

    @property
    def committed(self) -> bool:
        return self.outcome is JobCompletionOutcome.COMMITTED


class JobResultContainsPiiError(Exception):
    """A completion carried raw contact data (an email/phone) in its result reference.

    Raised BEFORE any row is touched, so a rejected completion leaves the job exactly as it was. A committed
    result stores only an opaque pointer + digest; raw PII belongs behind the reference, never on the row
    (the same rule the durable queue enforces on ``payload_ref``)."""


class JobCompletionStore:
    """Tenant-scoped, worker-side committer of a fenced job result (DCA-103).

    Bound to one verified ``business_id`` — a job is read/committed only within that business, so a worker
    can never complete another tenant's job (a cross-tenant id reads as not-found). Like
    :class:`~app.jobs.claim.JobClaimStore` it OWNS its transaction: :meth:`complete` locks the row, re-checks
    the fence + the authorization revisions, and commits (or rolls back) the terminal result in one short
    transaction. Nothing here runs a job."""

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id
        self._approvals = SourceApprovalStore(session)

    @property
    def business_id(self) -> str:
        return self._business_id

    def _locked_current(self, job_id: str) -> DurableJobRequest | None:
        """The job by id WITHIN this tenant, locked ``FOR UPDATE`` (a cross-tenant id reads as not-found).

        ``FOR UPDATE`` makes the fence/rights re-check and the commit atomic against a concurrent reclaim on
        PostgreSQL; the SQLite dialect omits the clause (serial dev/manual-check path)."""
        row = self._session.scalars(
            select(DurableJobRequest)
            .where(
                DurableJobRequest.id == job_id,
                DurableJobRequest.business_id == self._business_id,
            )
            .with_for_update()
        ).first()
        return row

    def _first_lapsed_authorization(
        self, authorizations: Sequence[SourceAuthorizationRef], now: datetime
    ) -> SourceAuthorizationRef | None:
        """The first relied-upon revision that is NO LONGER in force at ``now``, or ``None`` if all hold.

        A revision is lapsed if its approval is missing (never approved / wrong version) or not in force
        (revoked or outside its validity window) per the authoritative
        :meth:`~app.services.source_approval.SourceApprovalStore.is_in_force` — so the completion gate never
        disagrees with what the preflight would grant."""
        for ref in authorizations:
            approval = self._approvals.approval_for(ref.source_key, ref.terms_version)
            if approval is None or not self._approvals.is_in_force(approval, now=now):
                return ref
        return None

    def complete(
        self,
        job_id: str,
        worker_id: str,
        fence_token: int,
        *,
        now: datetime,
        result_ref: str | None = None,
        result_digest: str | None = None,
        authorizations: Sequence[SourceAuthorizationRef] = (),
    ) -> JobCompletionResult:
        """Commit a fenced result for a running job, or refuse without changing anything.

        The commit is honoured only when ALL of the following hold; any failure rolls back and returns a
        :class:`JobCompletionResult` whose :attr:`~JobCompletionResult.outcome` names the relevant denial
        (and changes NOTHING), so a stale/replaced worker or one whose source rights changed can never
        commit a result:

        - the job exists in this tenant (else ``NOT_FOUND``);
        - the presented ``(worker_id, fence_token)`` is the current lease on a ``RUNNING`` row (else
          ``STALE_LEASE`` — a reclaim bumped the fencing token);
        - the lease has not lapsed (``now < lease_expires_at``) (else ``LEASE_EXPIRED`` — a reclaimable job
          may not be committed by its old holder);
        - every authorization revision the worker relied on is still in force at ``now`` (else
          ``SOURCE_RIGHTS_CHANGED``, with the lapsed revision in ``detail``).

        On success the row flips ``RUNNING`` → ``SUCCEEDED``, its lease is cleared, and — if a ``result_ref``
        is supplied — the opaque result pointer + its digest (derived when not given) and ``completed_at``
        are recorded. A ``result_ref`` carrying raw contact data raises :class:`JobResultContainsPiiError`
        BEFORE anything is read, so a PII-bearing commit never touches state. Committing a result records the
        terminal status; it still EXECUTES nothing (the handler stays a disabled future capability)."""
        if not worker_id:
            raise ValueError("worker_id must be a non-empty worker identifier")
        # PII is refused before any state is read, so a rejected commit leaves the job untouched.
        if result_ref and _contains_raw_pii(result_ref):
            raise JobResultContainsPiiError("result_ref must not contain raw contact data")

        row = self._locked_current(job_id)
        if row is None:
            # A cross-tenant / unknown id: end the (read-only) transaction so no lock/scope lingers.
            self._session.rollback()
            return JobCompletionResult(JobCompletionOutcome.NOT_FOUND, job_id)

        # 1) Fenced on the current lease: RUNNING, owned by this worker, at this exact fencing token. A
        #    reclaim bumped lease_version (and may have changed the owner), so a replaced worker's token is
        #    stale and refused here — it cannot commit a result for a job that moved on.
        if (
            row.status != JobRequestStatus.RUNNING.value
            or row.lease_owner != worker_id
            or int(row.lease_version) != int(fence_token)
        ):
            self._session.rollback()
            return JobCompletionResult(
                JobCompletionOutcome.STALE_LEASE,
                job_id,
                business_id=row.business_id,
                status=row.status,
                fence_token=int(row.lease_version),
            )

        # 2) Live-only: an already-lapsed lease is reclaimable, so its old holder may not commit a result.
        expiry = _as_utc(row.lease_expires_at)
        if expiry is not None and now >= expiry:
            self._session.rollback()
            return JobCompletionResult(
                JobCompletionOutcome.LEASE_EXPIRED,
                job_id,
                business_id=row.business_id,
                status=row.status,
                fence_token=int(row.lease_version),
            )

        # 3) The authorization revisions the worker relied on must all still be in force. If a relevant
        #    approval was revoked/lapsed since it was captured, the rights were pulled out from under the
        #    worker and the commit is refused (the DCA-050 acceptance, re-checked at the commit boundary).
        lapsed = self._first_lapsed_authorization(authorizations, now)
        if lapsed is not None:
            self._session.rollback()
            return JobCompletionResult(
                JobCompletionOutcome.SOURCE_RIGHTS_CHANGED,
                job_id,
                business_id=row.business_id,
                status=row.status,
                fence_token=int(row.lease_version),
                detail=lapsed.label,
            )

        # All checks passed — commit the terminal result. Record the opaque pointer + digest (never raw
        # data), clear the lease (the job is done; it is not reclaimable), and mark the completion moment.
        digest: str | None = None
        if result_ref:
            digest = result_digest or job_result_digest(result_ref)
        row.status = JobRequestStatus.SUCCEEDED.value
        row.lease_owner = None
        row.lease_expires_at = None
        row.result_ref = result_ref
        row.result_digest = digest
        row.completed_at = now
        row.updated_at = now
        committed = JobCompletionResult(
            JobCompletionOutcome.COMMITTED,
            job_id=row.id,
            business_id=row.business_id,
            status=row.status,
            fence_token=int(row.lease_version),
            result_ref=result_ref,
            result_digest=digest,
            completed_at=now,
        )
        self._session.commit()
        return committed
