"""DCA-099 — the durable job-request store.

Key: ``job-store`` · source sections 2, 3, 8. Stage: Durable Python execution.

One capability: persist a tenant's *intent* to do a unit of background work as a durable, deduplicated
record, so that intent survives a restart, can be reviewed, and can be deduplicated and audited BEFORE
anything ever runs it. This story persists job requests only — it does NOT execute them. A worker /
claim / retry loop is a separate, currently-disabled future capability; the ``status`` and attempt
columns are scaffolding for it and are never advanced here.

Two load-bearing rules, both fail-closed:

- **One job per logical intent.** ``(business_id, job_type, idempotency_key)`` is unique, and the store
  relies on that constraint as the serialisation point: enqueuing the SAME logical work twice within a
  business collapses to exactly ONE row. A replay whose payload digest matches the recorded job is
  returned verbatim (``created=False``); a *different* payload presented under the same key is a
  :class:`JobRequestConflict` (rejected before any write, so a conflict changes nothing), never silently
  re-homed onto the first job.
- **No raw PII on the row.** The job carries only a ``payload_ref`` (an opaque pointer to where the real
  payload lives) and a one-way ``payload_digest`` of it — never the payload itself. The store refuses an
  enqueue whose reference / metadata contains an email or phone number by shape, so contact data can
  never land in the durable queue.

A job request grants nothing and sends nothing: the DCA has no send path, and acceptance of a prospect
is not recipient consent. Enqueuing work is only a durable record of intent.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.logging import _EMAIL_RE, _MIN_PHONE_DIGITS, _PHONE_CANDIDATE_RE
from app.db.models import DurableJobRequest, JobRequestStatus

# The default retry ceiling stamped onto a new job request. A future worker reads it; nothing here
# advances the attempt count. Kept small and conservative — an explicit per-enqueue value overrides it.
DEFAULT_MAX_ATTEMPTS = 5


def job_payload_digest(payload_ref: str, schema_version: int, payload_metadata: dict | None) -> str:
    """A stable one-way fingerprint of a job's logical payload — never the payload itself.

    Digests the ``(schema_version, payload_ref, sorted metadata)`` descriptor so the SAME logical work
    yields the SAME digest across retries (order-independent for metadata), while any change to the
    referenced payload, its schema version, or its control metadata yields a different digest and is
    treated as a key reuse. Used both to derive a default digest at enqueue time and to decide replay
    vs conflict when a key recurs.
    """
    items = sorted((payload_metadata or {}).items())
    canonical = f"{int(schema_version)}\x1f{payload_ref}\x1f{items!r}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _contains_raw_pii(text: str) -> bool:
    """True if ``text`` carries an email or a phone-shaped run of digits — i.e. raw contact data.

    Reuses the process-wide redactor's email/phone shapes (:mod:`app.core.logging`) so the queue's
    definition of "raw PII" matches the service's logging redaction exactly. Over-rejection is
    preferred to letting contact data into the durable queue.
    """
    if not text:
        return False
    if _EMAIL_RE.search(text):
        return True
    for match in _PHONE_CANDIDATE_RE.finditer(text):
        if sum(ch.isdigit() for ch in match.group()) >= _MIN_PHONE_DIGITS:
            return True
    return False


class PayloadContainsPiiError(Exception):
    """An enqueue carried raw contact data (an email/phone) in its reference or metadata.

    Raised BEFORE any row is staged, so a rejected enqueue leaves no job. The durable queue stores only
    an opaque payload reference + a digest; raw PII belongs behind the reference, never on the row.
    """


class JobRequestConflict(Exception):
    """An idempotency key was reused for a DIFFERENT logical unit of work than it first recorded.

    Raised before any new row is written (or after losing the unique-constraint race, with the duplicate
    rolled back), so a conflict changes nothing. The host must use a fresh key for genuinely new work
    rather than overwrite the recorded job.
    """


@dataclass(frozen=True)
class JobEnqueueRequest:
    """One request to persist a unit of background work (the enqueue contract).

    ``job_type`` + ``idempotency_key`` identify the logical intent within the business (the dedup key).
    ``payload_ref`` is an opaque pointer to the real payload (never raw PII); ``payload_digest`` pins
    that payload for replay/conflict detection — derived from the reference + schema version + metadata
    when not supplied. ``schema_version`` versions the payload contract; ``max_attempts`` / ``available_at``
    are attempt bookkeeping a future worker reads (never advanced in this story).
    """

    job_type: str
    idempotency_key: str
    payload_ref: str
    schema_version: int = 1
    payload_digest: str | None = None
    payload_metadata: dict = field(default_factory=dict)
    max_attempts: int = DEFAULT_MAX_ATTEMPTS

    def resolved_digest(self) -> str:
        """The payload digest to persist — the supplied one, or one derived from the descriptor."""
        if self.payload_digest:
            return self.payload_digest
        return job_payload_digest(self.payload_ref, self.schema_version, self.payload_metadata)


class JobRequestStore:
    """Tenant-scoped reader/writer for the durable job-request queue.

    Bound to a single ``business_id`` (the verified tenant scope). Every read is filtered to that
    business and every write is stamped with it, so a job can never be found or recorded across tenants —
    the same business-scoped discipline every other DCA store uses. The store never commits: it stages +
    flushes into the caller's session so the job and any audit entry commit in one unit of work.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    @property
    def business_id(self) -> str:
        return self._business_id

    def find(self, job_type: str, idempotency_key: str) -> DurableJobRequest | None:
        """The recorded job for this ``(job_type, idempotency_key)`` within this business, or ``None``.

        A key another business recorded is invisible here (the query is business-scoped), exactly like
        every other tenant read in the service."""
        return self._session.scalar(
            select(DurableJobRequest).where(
                DurableJobRequest.business_id == self._business_id,
                DurableJobRequest.job_type == job_type,
                DurableJobRequest.idempotency_key == idempotency_key,
            )
        )

    @staticmethod
    def _matches(row: DurableJobRequest, request: JobEnqueueRequest) -> bool:
        """Does an existing job describe the SAME logical work the caller is now presenting?

        The payload digest pins the exact payload; the job type is already fixed by the lookup. Anything
        else under the same key is different work and must not ride on the first job."""
        return row.payload_digest == request.resolved_digest()

    def enqueue(
        self, request: JobEnqueueRequest, *, now: datetime
    ) -> tuple[DurableJobRequest, bool]:
        """Persist a job request, or return the one already recorded. Returns ``(row, created)``.

        Fail-closed, exactly-once and PII-safe:

        - A reference / metadata carrying raw contact data raises :class:`PayloadContainsPiiError`
          before any write.
        - An existing job whose payload digest matches is **returned** (``created=False``) — no second
          row. A mismatch raises :class:`JobRequestConflict`.
        - Otherwise a new ``QUEUED`` job is staged and flushed. If a concurrent enqueue won the
          unique-constraint race, the :class:`IntegrityError` is caught, this unit of work is rolled
          back, and the winner's row is re-read and returned — so one logical intent yields one job even
          under contention. A surviving mismatch after the race is still a conflict.

        The caller owns the surrounding transaction and commits the returned row with any audit entry in
        the same unit of work (the row is staged + flushed here, never committed)."""
        self._reject_pii(request)

        existing = self.find(request.job_type, request.idempotency_key)
        if existing is not None:
            if not self._matches(existing, request):
                raise JobRequestConflict(request.idempotency_key)
            return existing, False

        row = DurableJobRequest(
            business_id=self._business_id,
            schema_version=int(request.schema_version),
            job_type=request.job_type,
            idempotency_key=request.idempotency_key,
            payload_ref=request.payload_ref,
            payload_digest=request.resolved_digest(),
            payload_metadata=dict(request.payload_metadata),
            status=JobRequestStatus.QUEUED.value,
            attempt_count=0,
            max_attempts=int(request.max_attempts),
            available_at=now,
            last_error=None,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError:
            # A concurrent enqueue of the same logical intent won the unique-constraint race. Roll back
            # our duplicate and return the winner — the intent yields exactly one job regardless of who
            # got there first.
            self._session.rollback()
            winner = self.find(request.job_type, request.idempotency_key)
            if winner is None:  # pragma: no cover - the unique violation implies a row now exists
                raise
            if not self._matches(winner, request):
                raise JobRequestConflict(request.idempotency_key) from None
            return winner, False
        return row, True

    def _reject_pii(self, request: JobEnqueueRequest) -> None:
        # The durable queue stores a pointer + digest, never contact data. Scan the human-controlled
        # fields (the reference and any string metadata) and refuse an enqueue that carries raw PII.
        if _contains_raw_pii(request.payload_ref):
            raise PayloadContainsPiiError("payload_ref must not contain raw contact data")
        for key, value in (request.payload_metadata or {}).items():
            if isinstance(value, str) and _contains_raw_pii(value):
                raise PayloadContainsPiiError(
                    f"payload_metadata[{key!r}] must not contain raw contact data"
                )
