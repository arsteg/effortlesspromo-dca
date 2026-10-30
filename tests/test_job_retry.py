"""DCA-104 — schedule bounded retries.

DCA-101 leased a job (fencing token); DCA-102 heartbeated a live lease; DCA-103 committed a fenced
success. DCA-104 handles the FAILURE branch: when the worker that holds the current lease reports a
failed attempt, the scheduler classifies it, and — fenced on that same lease — either SCHEDULES a bounded
retry (capped exponential backoff + jitter + provider Retry-After floor) or DEAD-LETTERS the job.

The headline acceptance:
- a PERMANENT policy/auth failure is NEVER retried as a transient — it is dead-lettered immediately, even
  with attempts remaining;
- attempt caps are honoured — a transient failure at ``attempt_count >= max_attempts`` is dead-lettered;
- a retry's next-run time is bounded (capped exponential backoff + jitter) and honours a provider
  Retry-After as a floor;
- a stale/replaced/expired worker cannot fail or retry a job that moved on (each refusal changes nothing).

Scheduling a retry records WHEN a job may next be attempted; it EXECUTES nothing — execution stays a
disabled future capability, the DCA has no send path, and acceptance of a prospect is not recipient consent.

Two layers, matching the DCA-101/102/103 suites:
- **Always-on logic tests** (in-memory SQLite + a seeded RNG) drive the classification + backoff + the
  fenced apply path serially and deterministically.
- **Disposable PostgreSQL** (``DCA_TEST_DATABASE_URL``) re-runs the fenced apply — and the
  failure-vs-reclaim race — against a real Postgres with real commits/rollbacks and the ``FOR UPDATE`` row
  lock, because the acceptance cases are durable row state across short transactions (never SQLite).
  Skips without a URL.

Spin up a throwaway Postgres, e.g.:
    docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55471:5432 postgres:16-alpine
    export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55471/dca"
"""

from __future__ import annotations

import os
import random
from datetime import UTC, datetime, timedelta

import pytest

from app.db.base import Base, make_engine, make_session_factory
from app.db.models import DurableJobRequest, JobRequestStatus
from app.jobs.claim import JobClaimStore
from app.jobs.retry import (
    BASE_BACKOFF_SECONDS,
    JITTER_RATIO,
    MAX_BACKOFF_SECONDS,
    FailureSignal,
    JobErrorNoteContainsPiiError,
    JobFailureCategory,
    JobFailureClass,
    JobRetryScheduler,
    RetryAction,
    RetryScheduleOutcome,
    classify_failure,
    compute_backoff_seconds,
    plan_retry,
)
from app.jobs.store import JobEnqueueRequest, JobRequestStore

BIZ_A = "11111111-1111-1111-1111-11111111d104"
BIZ_B = "22222222-2222-2222-2222-22222222d104"
NOW = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)


def _enqueue(factory, business_id: str, key: str, *, max_attempts: int = 5) -> str:
    with factory() as session:
        row, _ = JobRequestStore(session, business_id).enqueue(
            JobEnqueueRequest(
                job_type="prospect.research",
                idempotency_key=key,
                payload_ref=f"dca-payload://synthetic/{key}",
                max_attempts=max_attempts,
            ),
            now=NOW,
        )
        session.commit()
        return row.id


def _claim(
    factory, business_id: str, worker: str, *, now: datetime = NOW, lease_seconds: int = 600
):
    with factory() as session:
        return JobClaimStore(session, business_id).claim_next(
            worker, now=now, lease_seconds=lease_seconds
        )


def _record(factory, business_id, job_id, worker, fence, signal, *, at, seed: int = 7):
    with factory() as session:
        return JobRetryScheduler(session, business_id).record_failure(
            job_id, worker, fence, signal, now=at, rng=random.Random(seed)
        )


def _row(factory, job_id) -> DurableJobRequest:
    with factory() as session:
        return session.get(DurableJobRequest, job_id)


# --------------------------------------------------------------------------------------------------
# Pure layer: classification + backoff + plan (no DB, fully deterministic).
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "category",
    [
        JobFailureCategory.TIMEOUT,
        JobFailureCategory.NETWORK,
        JobFailureCategory.PROVIDER_UNAVAILABLE,
        JobFailureCategory.RATE_LIMITED,
        JobFailureCategory.UNKNOWN,
    ],
)
def test_transient_categories_classify_transient(category):
    assert classify_failure(category) is JobFailureClass.TRANSIENT


@pytest.mark.parametrize(
    "category",
    [
        JobFailureCategory.POLICY_DENIED,
        JobFailureCategory.AUTHORIZATION_REVOKED,
        JobFailureCategory.AUTHENTICATION_FAILED,
        JobFailureCategory.PAYLOAD_INVALID,
        JobFailureCategory.NOT_RETRYABLE,
    ],
)
def test_permanent_categories_classify_permanent(category):
    assert classify_failure(category) is JobFailureClass.PERMANENT


def test_backoff_is_capped_exponential_within_jitter_bounds():
    """Each attempt's delay stays within ``[capped*(1-JITTER_RATIO), capped]`` and the cap is honoured."""
    rng = random.Random(1)
    for attempt in range(1, 15):
        delay = compute_backoff_seconds(attempt, rng=rng)
        exponential = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
        capped = min(exponential, MAX_BACKOFF_SECONDS)
        assert capped * (1 - JITTER_RATIO) - 1e-9 <= delay <= capped + 1e-9
        assert delay <= MAX_BACKOFF_SECONDS + 1e-9


def test_backoff_grows_then_saturates_at_the_cap():
    """The upper bound roughly doubles per attempt until it saturates at MAX_BACKOFF_SECONDS."""

    # Use a max-jitter RNG so every draw hits the top of its window; the sequence should be monotonic
    # non-decreasing and clamp at the cap.
    class _MaxRng(random.Random):
        def random(self) -> float:  # always pick the top of the jitter window
            return 1.0

    rng = _MaxRng()
    delays = [compute_backoff_seconds(a, rng=rng) for a in range(1, 12)]
    assert delays == sorted(delays)  # non-decreasing
    assert delays[-1] == MAX_BACKOFF_SECONDS  # saturated at the cap


def test_retry_after_is_honoured_as_a_floor():
    """A provider Retry-After raises the delay to at least that many seconds (never retry sooner)."""
    rng = random.Random(3)
    # attempt 1's jitter window tops out at ~2s; a 300s Retry-After must dominate.
    delay = compute_backoff_seconds(1, rng=rng, retry_after_seconds=300)
    assert delay == 300


def test_plan_permanent_dead_letters_even_with_attempts_remaining():
    """ACCEPTANCE (pure): a permanent failure is dead-lettered even though 4 of 5 attempts remain."""
    plan = plan_retry(
        FailureSignal(JobFailureCategory.POLICY_DENIED),
        attempt_count=1,
        max_attempts=5,
        now=NOW,
        rng=random.Random(0),
    )
    assert plan.action is RetryAction.DEAD_LETTER_PERMANENT
    assert plan.will_retry is False
    assert plan.next_available_at is None


def test_plan_transient_schedules_until_the_cap_then_dead_letters():
    """ACCEPTANCE (pure): transient retries are scheduled while attempts remain, then the cap dead-letters."""
    sig = FailureSignal(JobFailureCategory.TIMEOUT)
    # attempts 1 and 2 of a 3-attempt budget -> scheduled retries with a future next-run time.
    for attempt in (1, 2):
        plan = plan_retry(
            sig, attempt_count=attempt, max_attempts=3, now=NOW, rng=random.Random(attempt)
        )
        assert plan.action is RetryAction.SCHEDULE_RETRY
        assert plan.next_available_at is not None and plan.next_available_at > NOW
        assert plan.attempts_remaining == 3 - attempt
    # attempt 3 reaches the cap -> dead-lettered (exhausted), honouring the cap.
    plan = plan_retry(sig, attempt_count=3, max_attempts=3, now=NOW, rng=random.Random(9))
    assert plan.action is RetryAction.DEAD_LETTER_EXHAUSTED
    assert plan.will_retry is False


# --------------------------------------------------------------------------------------------------
# Always-on fenced-apply tests (SQLite). The durable-row + race assertions also run on Postgres below.
# --------------------------------------------------------------------------------------------------


@pytest.fixture
def factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def test_transient_failure_schedules_a_bounded_retry(factory):
    """A current holder's transient failure -> FAILED, lease cleared, next-run pushed out, note recorded."""
    job_id = _enqueue(factory, BIZ_A, "job-transient", max_attempts=5)
    claim = _claim(factory, BIZ_A, "worker-1")
    assert claim is not None and claim.fence_token == 1

    at = NOW + timedelta(seconds=5)
    result = _record(
        factory,
        BIZ_A,
        job_id,
        "worker-1",
        claim.fence_token,
        FailureSignal(JobFailureCategory.TIMEOUT, error_note="upstream_timeout"),
        at=at,
    )
    assert result.scheduled_retry is True
    assert result.outcome is RetryScheduleOutcome.RETRY_SCHEDULED
    assert result.failure_class is JobFailureClass.TRANSIENT
    assert result.next_available_at is not None and result.next_available_at > at

    row = _row(factory, job_id)
    assert row.status == JobRequestStatus.FAILED.value
    assert row.lease_owner is None and row.lease_expires_at is None
    assert row.lease_version == 1  # fencing token is not bumped by a failure record
    assert row.attempt_count == 1  # DCA-101 bumped it at claim; record_failure does not re-bump
    assert row.last_error == "upstream_timeout"
    # available_at was pushed to the future (the scheduled next-run time).
    assert row.available_at.replace(tzinfo=UTC) > at


def test_permanent_failure_is_dead_lettered_not_retried(factory):
    """ACCEPTANCE: a permanent policy failure is dead-lettered immediately, NOT retried, with attempts left."""
    job_id = _enqueue(factory, BIZ_A, "job-permanent", max_attempts=5)
    claim = _claim(factory, BIZ_A, "worker-1")

    result = _record(
        factory,
        BIZ_A,
        job_id,
        "worker-1",
        claim.fence_token,
        FailureSignal(JobFailureCategory.POLICY_DENIED, error_note="rulepack_denied"),
        at=NOW + timedelta(seconds=5),
    )
    assert result.dead_lettered is True
    assert result.outcome is RetryScheduleOutcome.DEAD_LETTERED_PERMANENT
    assert result.failure_class is JobFailureClass.PERMANENT

    row = _row(factory, job_id)
    assert row.status == JobRequestStatus.DEAD_LETTER.value  # parked, not FAILED (no retry)
    assert (
        row.attempt_count == 1 and row.max_attempts == 5
    )  # 4 attempts remained but it is NOT retried
    assert row.lease_owner is None
    assert row.last_error == "rulepack_denied"


def test_transient_failure_at_the_cap_is_dead_lettered(factory):
    """ACCEPTANCE: a transient failure at the attempt cap is dead-lettered (the cap is honoured)."""
    # max_attempts=1: the single claim bumps attempt_count to 1, so the first transient failure is at cap.
    job_id = _enqueue(factory, BIZ_A, "job-exhausted", max_attempts=1)
    claim = _claim(factory, BIZ_A, "worker-1")
    assert claim is not None and claim.attempt_count == 1

    result = _record(
        factory,
        BIZ_A,
        job_id,
        "worker-1",
        claim.fence_token,
        FailureSignal(JobFailureCategory.NETWORK, error_note="conn_reset"),
        at=NOW + timedelta(seconds=5),
    )
    assert result.dead_lettered is True
    assert result.outcome is RetryScheduleOutcome.DEAD_LETTERED_EXHAUSTED

    row = _row(factory, job_id)
    assert row.status == JobRequestStatus.DEAD_LETTER.value
    assert row.attempt_count == 1 and row.max_attempts == 1


def test_retry_after_floors_the_scheduled_next_run(factory):
    """A provider Retry-After floors the next-run time even when the backoff window is tiny."""
    job_id = _enqueue(factory, BIZ_A, "job-retryafter", max_attempts=5)
    claim = _claim(factory, BIZ_A, "worker-1")
    at = NOW + timedelta(seconds=5)
    result = _record(
        factory,
        BIZ_A,
        job_id,
        "worker-1",
        claim.fence_token,
        FailureSignal(
            JobFailureCategory.RATE_LIMITED, error_note="throttled", retry_after_seconds=120
        ),
        at=at,
    )
    assert result.scheduled_retry is True
    assert result.delay_seconds == 120  # the Retry-After floor dominates attempt-1's small window
    assert result.next_available_at == at + timedelta(seconds=120)
    row = _row(factory, job_id)
    assert row.available_at.replace(tzinfo=UTC) == at + timedelta(seconds=120)


def test_replaced_worker_cannot_schedule_a_retry_after_reclaim(factory):
    """ACCEPTANCE: a stale worker whose job was reclaimed cannot fail/retry it — nothing changes."""
    job_id = _enqueue(factory, BIZ_A, "job-reclaimed", max_attempts=5)
    first = _claim(factory, BIZ_A, "worker-1", lease_seconds=60)
    assert first is not None and first.fence_token == 1

    later = NOW + timedelta(seconds=120)
    second = _claim(factory, BIZ_A, "worker-2", now=later, lease_seconds=60)
    assert second is not None and second.fence_token == 2

    result = _record(
        factory,
        BIZ_A,
        job_id,
        "worker-1",
        first.fence_token,
        FailureSignal(JobFailureCategory.TIMEOUT),
        at=later + timedelta(seconds=1),
    )
    assert result.changed is False
    assert result.outcome is RetryScheduleOutcome.STALE_LEASE

    row = _row(factory, job_id)
    assert row.status == JobRequestStatus.RUNNING.value  # still worker-2's live claim
    assert row.lease_owner == "worker-2" and row.lease_version == 2
    assert row.last_error is None


def test_expired_holder_cannot_schedule_a_retry(factory):
    """A lapsed (reclaimable) lease may not be failed/retried by its old holder, even before a reclaim."""
    job_id = _enqueue(factory, BIZ_A, "job-expired", max_attempts=5)
    claim = _claim(factory, BIZ_A, "worker-1", lease_seconds=60)
    result = _record(
        factory,
        BIZ_A,
        job_id,
        "worker-1",
        claim.fence_token,
        FailureSignal(JobFailureCategory.TIMEOUT),
        at=NOW + timedelta(seconds=120),
    )
    assert result.changed is False
    assert result.outcome is RetryScheduleOutcome.LEASE_EXPIRED
    row = _row(factory, job_id)
    assert row.status == JobRequestStatus.RUNNING.value  # untouched, still reclaimable
    assert row.lease_owner == "worker-1" and row.lease_version == 1


def test_foreign_worker_cannot_fail_a_live_lease(factory):
    """Fenced on owner: a different worker id cannot fail someone else's live claim."""
    job_id = _enqueue(factory, BIZ_A, "job-foreign", max_attempts=5)
    claim = _claim(factory, BIZ_A, "worker-1")
    result = _record(
        factory,
        BIZ_A,
        job_id,
        "worker-2",
        claim.fence_token,
        FailureSignal(JobFailureCategory.TIMEOUT),
        at=NOW + timedelta(seconds=5),
    )
    assert result.changed is False and result.outcome is RetryScheduleOutcome.STALE_LEASE
    row = _row(factory, job_id)
    assert row.status == JobRequestStatus.RUNNING.value and row.lease_owner == "worker-1"


def test_cross_tenant_job_cannot_be_failed(factory):
    """Tenant isolation: a worker in business B cannot fail business A's job (reads not-found)."""
    job_id = _enqueue(factory, BIZ_A, "job-tenant", max_attempts=5)
    claim = _claim(factory, BIZ_A, "worker-1")
    result = _record(
        factory,
        BIZ_B,
        job_id,
        "worker-1",
        claim.fence_token,
        FailureSignal(JobFailureCategory.TIMEOUT),
        at=NOW,
    )
    assert result.changed is False and result.outcome is RetryScheduleOutcome.NOT_FOUND
    row = _row(factory, job_id)
    assert row.status == JobRequestStatus.RUNNING.value and row.lease_owner == "worker-1"


def test_error_note_with_raw_pii_is_refused_before_any_change(factory):
    """An error note carrying raw contact data is refused before the row is touched (PII-safe)."""
    job_id = _enqueue(factory, BIZ_A, "job-pii", max_attempts=5)
    claim = _claim(factory, BIZ_A, "worker-1")
    with pytest.raises(JobErrorNoteContainsPiiError):
        _record(
            factory,
            BIZ_A,
            job_id,
            "worker-1",
            claim.fence_token,
            FailureSignal(
                JobFailureCategory.TIMEOUT, error_note="failed emailing person@example.com"
            ),
            at=NOW,
        )
    row = _row(factory, job_id)
    assert row.status == JobRequestStatus.RUNNING.value  # untouched
    assert row.last_error is None


def test_empty_worker_id_is_rejected(factory):
    job_id = _enqueue(factory, BIZ_A, "job-noworker", max_attempts=5)
    claim = _claim(factory, BIZ_A, "worker-1")
    with pytest.raises(ValueError):
        _record(
            factory,
            BIZ_A,
            job_id,
            "",
            claim.fence_token,
            FailureSignal(JobFailureCategory.TIMEOUT),
            at=NOW,
        )


def test_a_dead_lettered_job_cannot_be_failed_again(factory):
    """Terminality: once DEAD_LETTER the row is no longer RUNNING, so a second record is refused."""
    job_id = _enqueue(factory, BIZ_A, "job-twice", max_attempts=5)
    claim = _claim(factory, BIZ_A, "worker-1")
    first = _record(
        factory,
        BIZ_A,
        job_id,
        "worker-1",
        claim.fence_token,
        FailureSignal(JobFailureCategory.POLICY_DENIED, error_note="denied"),
        at=NOW,
    )
    assert first.dead_lettered is True
    second = _record(
        factory,
        BIZ_A,
        job_id,
        "worker-1",
        claim.fence_token,
        FailureSignal(JobFailureCategory.TIMEOUT, error_note="later"),
        at=NOW + timedelta(seconds=1),
    )
    assert second.changed is False and second.outcome is RetryScheduleOutcome.STALE_LEASE
    row = _row(factory, job_id)
    assert row.last_error == "denied"  # the dead-letter note is intact


# --------------------------------------------------------------------------------------------------
# The same contract — and the failure-vs-reclaim race — on a disposable PostgreSQL.
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
    try:
        yield make_session_factory(engine)
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@requires_pg
def test_transient_failure_schedules_retry_postgres(pg_factory):
    """Happy-path scheduled retry against real Postgres with the FOR UPDATE lock."""
    job_id = _enqueue(pg_factory, BIZ_A, "job-transient-pg", max_attempts=5)
    claim = _claim(pg_factory, BIZ_A, "worker-1")
    at = NOW + timedelta(seconds=5)
    result = _record(
        pg_factory,
        BIZ_A,
        job_id,
        "worker-1",
        claim.fence_token,
        FailureSignal(JobFailureCategory.TIMEOUT, error_note="upstream_timeout"),
        at=at,
    )
    assert result.scheduled_retry is True
    row = _row(pg_factory, job_id)
    assert row.status == JobRequestStatus.FAILED.value
    assert row.lease_owner is None
    assert _as_aware(row.available_at) > at


@requires_pg
def test_permanent_failure_dead_letters_postgres(pg_factory):
    """ACCEPTANCE on real Postgres: a permanent failure dead-letters and is not retried."""
    job_id = _enqueue(pg_factory, BIZ_A, "job-permanent-pg", max_attempts=5)
    claim = _claim(pg_factory, BIZ_A, "worker-1")
    result = _record(
        pg_factory,
        BIZ_A,
        job_id,
        "worker-1",
        claim.fence_token,
        FailureSignal(JobFailureCategory.AUTHORIZATION_REVOKED, error_note="rights_revoked"),
        at=NOW + timedelta(seconds=5),
    )
    assert result.outcome is RetryScheduleOutcome.DEAD_LETTERED_PERMANENT
    row = _row(pg_factory, job_id)
    assert row.status == JobRequestStatus.DEAD_LETTER.value and row.attempt_count == 1


@requires_pg
def test_reclaimed_then_stale_failure_refused_postgres(pg_factory):
    """ACCEPTANCE on real Postgres: after a genuine reclaim (worker-2), worker-1's stale failure is refused
    by the fence and nothing changes — the stale worker cannot stomp worker-2's claim."""
    job_id = _enqueue(pg_factory, BIZ_A, "job-reclaimed-pg", max_attempts=5)
    first = _claim(pg_factory, BIZ_A, "worker-1", lease_seconds=60)
    assert first is not None and first.fence_token == 1
    later = NOW + timedelta(seconds=120)
    second = _claim(pg_factory, BIZ_A, "worker-2", now=later, lease_seconds=60)
    assert second is not None and second.fence_token == 2
    result = _record(
        pg_factory,
        BIZ_A,
        job_id,
        "worker-1",
        first.fence_token,
        FailureSignal(JobFailureCategory.TIMEOUT),
        at=later + timedelta(seconds=1),
    )
    assert result.changed is False and result.outcome is RetryScheduleOutcome.STALE_LEASE
    row = _row(pg_factory, job_id)
    assert row.status == JobRequestStatus.RUNNING.value
    assert row.lease_owner == "worker-2" and row.lease_version == 2


def _as_aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value
