"""DCA-108 — enqueue due recurring schedules exactly once logically.

Key: ``schedule-tick`` · source sections 2, 3, 8. Stage: Durable Python execution.

One capability: turn a business's DUE recurring schedules (the durable DCA-107
:class:`~app.db.models.RecurringSchedule` rows) into enqueued durable jobs (DCA-099
:class:`~app.db.models.DurableJobRequest` rows), **exactly once logically per cycle**, and advance each
fired schedule's persisted ``next_run_at`` so the same cycle is never fired again. This story ENQUEUES
work; it still EXECUTES nothing — there is no worker drain loop here, no handler is resolved or run, and
nothing is sent. The DCA has no send path, and acceptance of a prospect is not recipient consent.

The load-bearing guarantee (the DCA-108 acceptance criteria), fail-closed on two independent axes:

- **Exactly once logically — two scheduler instances do not create duplicate logical ticks.** The enqueue
  for one schedule cycle is keyed on a DETERMINISTIC per-cycle idempotency key —
  :func:`schedule_cycle_key` over ``(schedule_id, fired-boundary-epoch)`` — handed straight to the DCA-099
  durable queue, whose ``(business_id, job_type, idempotency_key)`` uniqueness is THE serialisation point.
  So the exactly-once property does NOT depend on locking at all: two schedulers (or a retried tick) that
  both see the same cycle as due compute the SAME key, and the queue collapses the second enqueue to the
  first job (``created=False``) — one logical tick, one job, never two. On PostgreSQL a second, belt-and-
  suspenders guard is added on top: the due schedules are selected ``FOR UPDATE SKIP LOCKED`` and the
  enqueue + advance + audit commit in ONE short transaction, so a concurrent ticker simply skips a row
  another ticker is mid-tick on (no blocking, no double-advance) rather than relying on the unique
  constraint to catch it after the fact. Either way a double tick yields exactly one job and one advance.

- **Bounded catch-up — missed intervals never produce a backlog storm.** When a scheduler was down and a
  schedule's ``next_run_at`` is several whole intervals in the past, the tick fires the due schedule
  **once** and then advances ``next_run_at`` arithmetically (O(1), no per-interval loop) to the next
  boundary STRICTLY AFTER ``now`` — the whole missed-interval gap is COLLAPSED into that single catch-up
  tick, never replayed as one job per missed interval. This is the documented, deliberately bounded
  catch-up policy: a schedule fires at most once per tick-pass regardless of how long the scheduler was
  down, and then resumes on cadence. The number of jobs one tick can enqueue is therefore bounded by the
  number of distinct due schedules (and the batch cap), never by elapsed time. :attr:`EnqueuedCycle.missed_intervals`
  records how many boundaries a given catch-up collapsed, for inspection.

Precondition FIRST, so a denial changes nothing: only an ``ACTIVE`` schedule whose persisted
``next_run_at`` is at or before ``now`` is a candidate (the ``WHERE`` clause is the precondition). A
not-yet-due schedule and a ``PAUSED`` schedule are skipped untouched — a tick that finds nothing due
enqueues nothing, advances nothing and commits an empty transaction. Tenant isolation is preserved: the
enqueuer is bound to one verified ``business_id`` and every read/write is confined to it (DCA-017 scoping +
DCA-018 RLS beneath it), so a tick can never read, fire or advance another tenant's schedule.

Run-time entitlement / budget enforcement (re-checking DCA-023 / DCA-042 at fire time) and the actual
EXECUTION of an enqueued cycle are deliberately OUT of scope here and stay disabled future capabilities;
this story only persists the intent to run a cycle, exactly once, and moves the clock forward.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.sink import ActorType, AuditActor, AuditSink
from app.db.models import RecurringSchedule, ScheduleStatus
from app.jobs.store import JobEnqueueRequest, JobRequestStore

# The single logical job type every schedule cycle is enqueued under. Opaque to the queue; the specific
# schedule is identified by the deterministic idempotency key (and recorded in the job's metadata), so one
# constant type keeps all schedule-driven work in one dedup namespace without leaking the schedule id into
# a PII-screened field.
SCHEDULE_CYCLE_JOB_TYPE = "schedule.cycle"

# How many DUE schedules one tick-pass fires at most. A tick is a short transaction over a bounded batch so
# it never holds a long scan / many row locks; a scheduler calls it repeatedly to drain a large backlog of
# distinct due schedules. Generous for a normal run, small enough to keep each transaction short.
DEFAULT_TICK_BATCH = 100

# The default opaque identity recorded as the actor of a scheduler tick on the atomic DCA-026 audit entry.
# A tick is a SYSTEM action (a background scheduler), never a user action — there is no user authority to
# check here; the precondition is simply that the schedule is ACTIVE and due.
DEFAULT_SCHEDULER_ACTOR = "dca-scheduler"

# The audited action code for firing a due schedule cycle (a controlled code, like the other writes).
AUDIT_ACTION_SCHEDULE_TICK = "schedule.tick"


def _as_utc(value: datetime) -> datetime:
    """Normalise a datetime to tz-aware UTC (SQLite reads back naive; PostgreSQL stays aware)."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _isoformat(value: datetime) -> str:
    return _as_utc(value).isoformat()


def schedule_cycle_key(schedule_id: str, fired_at: datetime) -> str:
    """The DETERMINISTIC durable-queue idempotency key for ONE cycle of ONE schedule.

    Keyed on ``(schedule_id, fired-boundary-epoch-seconds)`` so every ticker that fires the SAME due
    boundary of the SAME schedule computes the SAME key — which is exactly what makes the enqueue
    exactly-once-logically: the DCA-099 queue's uniqueness collapses a second enqueue under this key to the
    first job. The schedule id is opaque and the epoch is an integer, so the key is stable across retries
    and process restarts. This key goes into ``idempotency_key`` (NOT a PII-screened field), never into the
    payload reference/metadata.
    """
    return f"schedule:{schedule_id}:{int(_as_utc(fired_at).timestamp())}"


@dataclass(frozen=True)
class EnqueuedCycle:
    """An inert record of ONE schedule cycle a tick acted on — never a runnable.

    Carries only opaque / non-identifying fields: the schedule + tenant, the opaque ``schedule_key``, the
    enqueued job id and whether THIS tick created it (``False`` when an earlier/concurrent tick already
    enqueued the same cycle — the exactly-once collapse), the boundary that fired, the deterministic cycle
    key, how many missed boundaries the catch-up collapsed, and the before/after ``next_run_at``. It never
    exposes contact data. Holding one runs nothing.
    """

    schedule_id: str
    business_id: str
    schedule_key: str
    job_id: str
    job_created: bool
    fired_at: datetime
    cycle_key: str
    missed_intervals: int
    prior_next_run_at: datetime
    new_next_run_at: datetime


@dataclass(frozen=True)
class ScheduleTickReport:
    """The inert outcome of one :meth:`DueScheduleEnqueuer.tick` pass.

    Lists every due schedule fired in the pass (empty when none were due — the tick then changed nothing),
    scoped to the one business the enqueuer is bound to. A reviewer reads the counts and the per-cycle
    :class:`EnqueuedCycle` records to confirm each due schedule enqueued exactly one job and advanced its
    ``next_run_at``, and that a repeat tick added nothing.
    """

    business_id: str
    ticked_at: datetime
    enqueued: tuple[EnqueuedCycle, ...] = field(default_factory=tuple)

    @property
    def total(self) -> int:
        return len(self.enqueued)

    @property
    def created_count(self) -> int:
        """How many cycles THIS pass actually enqueued a new job for (vs collapsed onto an existing one)."""
        return sum(1 for c in self.enqueued if c.job_created)

    @property
    def changed(self) -> bool:
        """True iff the tick fired at least one due schedule."""
        return bool(self.enqueued)


class DueScheduleEnqueuer:
    """Tenant-scoped scheduler that fires a business's DUE recurring schedules exactly once (DCA-108).

    Bound to one verified ``business_id`` — every read and write is confined to that business, so a tick
    can never fire or advance another tenant's schedule (the same discipline as every other DCA store).
    Like the DCA-101/106 claim/recovery stores it OWNS its transaction: :meth:`tick` selects a bounded
    batch of due schedules with ``FOR UPDATE SKIP LOCKED``, enqueues + advances + audits them, and COMMITS
    the short transaction — releasing every row lock before returning. Nothing here runs a job.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    @property
    def business_id(self) -> str:
        return self._business_id

    def tick(
        self,
        *,
        now: datetime,
        limit: int = DEFAULT_TICK_BATCH,
        scheduler_id: str = DEFAULT_SCHEDULER_ACTOR,
    ) -> ScheduleTickReport:
        """Fire this business's due schedules exactly once, or change nothing if none are due.

        A due schedule is an ``ACTIVE`` row whose persisted ``next_run_at <= now`` (the precondition — a
        not-yet-due or ``PAUSED`` schedule is never selected, so it is left exactly as it is). The tick
        selects up to ``limit`` such rows (oldest-due first) with ``FOR UPDATE SKIP LOCKED`` — so a
        concurrent ticker skips, never blocks on, a row this one is mid-tick on, and two tickers never
        fire the same schedule in the same pass — and for each:

        - enqueues ONE durable job under the deterministic per-cycle key (:func:`schedule_cycle_key`): the
          DCA-099 queue collapses a replay/concurrent enqueue of the same cycle to the one job, so the tick
          is exactly-once-logically even without the row lock;
        - advances ``next_run_at`` to the next boundary strictly after ``now`` (bounded catch-up: whole
          missed intervals are collapsed into this single tick, computed arithmetically, never replayed);
        - stamps an atomic DCA-026 audit entry (opaque references only) in the SAME unit of work.

        The short transaction is COMMITTED here; the returned :class:`ScheduleTickReport` is inert. A tick
        that finds nothing due enqueues nothing, advances nothing, and commits an empty transaction.
        Enqueuing a cycle records the intent to run it; it EXECUTES nothing — execution stays a disabled
        future capability.
        """
        batch = max(1, int(limit))
        stmt = (
            select(RecurringSchedule)
            .where(
                RecurringSchedule.business_id == self._business_id,
                RecurringSchedule.status == ScheduleStatus.ACTIVE.value,
                RecurringSchedule.next_run_at <= now,
            )
            # Oldest-due first, then oldest-created — a stable, fair fire order.
            .order_by(RecurringSchedule.next_run_at, RecurringSchedule.created_at)
            .limit(batch)
            # Lock exactly the due rows, and skip (never block on) a row another ticker already holds.
            # PostgreSQL-only; the SQLite dialect omits FOR UPDATE (serial dev / manual-check path). The
            # deterministic cycle key keeps the tick exactly-once even where this lock is a no-op.
            .with_for_update(skip_locked=True)
        )
        rows = self._session.scalars(stmt).all()

        job_store = JobRequestStore(self._session, self._business_id)
        audit = AuditSink(
            self._session,
            business_id=self._business_id,
            actor=AuditActor(id=scheduler_id, type=ActorType.SYSTEM),
        )

        enqueued: list[EnqueuedCycle] = []
        for row in rows:
            fired = _as_utc(row.next_run_at)
            cycle_key = schedule_cycle_key(row.id, fired)

            # Enqueue exactly one job for THIS cycle. The deterministic key makes a concurrent/replayed
            # enqueue collapse to the one job (created=False) — exactly once logically.
            job, created = job_store.enqueue(
                JobEnqueueRequest(
                    job_type=SCHEDULE_CYCLE_JOB_TYPE,
                    idempotency_key=cycle_key,
                    # Opaque pointer — the schedule_key is a dotted logical name, never contact data. The
                    # schedule id / cycle epoch live in the (un-screened) idempotency key and in integer
                    # metadata below, never in this screened reference.
                    payload_ref=f"dca-schedule://cycle/{row.schedule_key}",
                    payload_metadata={
                        "scheduleKey": row.schedule_key,
                        "icpVersion": int(row.icp_version),
                        "profileVersion": (
                            None if row.profile_version is None else int(row.profile_version)
                        ),
                        "intervalSeconds": int(row.interval_seconds),
                        # Integer epoch of the fired boundary — an int, so it is NOT a PII-screened string
                        # yet still pins which cycle this job stands for.
                        "cycleEpoch": int(fired.timestamp()),
                    },
                ),
                now=now,
            )

            # Bounded catch-up: advance to the next boundary STRICTLY AFTER now, collapsing any whole
            # missed intervals into this single tick (O(1) — never a per-interval loop / backlog storm).
            new_next, missed = _advance(fired, _as_utc(now), int(row.interval_seconds))
            row.next_run_at = new_next
            row.updated_at = now

            audit.record(
                action=AUDIT_ACTION_SCHEDULE_TICK,
                reason=f"fired due recurring schedule '{row.schedule_key}'",
                references={
                    "scheduleId": row.id,
                    "scheduleKey": row.schedule_key,
                    "jobId": job.id,
                    "jobCreated": created,
                    "cycleEpoch": int(fired.timestamp()),
                    "missedIntervals": missed,
                    "icpVersion": int(row.icp_version),
                    "profileVersion": row.profile_version,
                    "priorNextRunAt": _isoformat(fired),
                    "newNextRunAt": _isoformat(new_next),
                },
            )

            enqueued.append(
                EnqueuedCycle(
                    schedule_id=row.id,
                    business_id=row.business_id,
                    schedule_key=row.schedule_key,
                    job_id=job.id,
                    job_created=created,
                    fired_at=fired,
                    cycle_key=cycle_key,
                    missed_intervals=missed,
                    prior_next_run_at=fired,
                    new_next_run_at=new_next,
                )
            )

        # Commit the short tick: every FOR UPDATE row lock is released HERE, before returning. An empty
        # batch commits an empty transaction (nothing was due) and leaves every row untouched.
        self._session.commit()
        return ScheduleTickReport(
            business_id=self._business_id,
            ticked_at=now,
            enqueued=tuple(enqueued),
        )


def _advance(fired: datetime, now: datetime, interval_seconds: int) -> tuple[datetime, int]:
    """The next boundary strictly after ``now``, plus how many whole missed boundaries were collapsed.

    ``fired`` is the due boundary (``fired <= now``). The bounded catch-up policy fires the schedule once
    and jumps to the next boundary strictly greater than ``now``, collapsing every whole interval in the
    gap rather than replaying one job per missed interval. Computed arithmetically (one floor-division),
    so a long outage is O(1) and never a per-interval loop. Returns ``(new_next_run_at, missed_intervals)``
    where ``missed_intervals`` is the number of boundaries between the fired one and ``now`` that were
    skipped (0 when the schedule was fired on time).
    """
    interval = timedelta(seconds=interval_seconds)
    elapsed = now - fired
    # Smallest k >= 1 such that fired + k*interval > now. ``elapsed // interval`` is the number of whole
    # intervals already elapsed since the fired boundary; one more lands strictly past now.
    steps = (elapsed // interval) + 1
    missed = (
        steps - 1
    )  # whole boundaries between the fired one and now, all collapsed into this tick
    return fired + steps * interval, missed
