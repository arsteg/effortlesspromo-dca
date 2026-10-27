"""DCA-049 — expire source access on time (effective-date checks + a scheduled expiry transition).

Two layers keep a lapsed source right from being used, and the first is load-bearing:

1. **Effective-date checks (immediate, authoritative).** A source right is in force only *inside* its
   explicit validity window. The platform ceiling's
   :meth:`~app.services.source_approval.SourceApprovalStore.is_in_force` and the terms version's
   :meth:`~app.services.source_terms.SourceTermsStore.permits` both evaluate ``effective_from`` /
   ``expires_at`` against the moment ``now`` the decision is made (DCA-045/046) — so the DCA-048
   preflight denies the instant an approval's review deadline (``expires_at``) passes or before an
   approval's ``effective_from`` arrives, **regardless of the stored status flag**. Nothing has to run
   first: an expired source is unusable the moment its window closes, even before any sweep catches up.

2. **Scheduled expiry transition (durable housekeeping).** :class:`SourceExpirySweep` is the scheduled
   cleanup the worker runs: it finds ACTIVE approvals whose review deadline has passed and ACTIVE terms
   versions whose own window has lapsed at ``now`` and moves each to the terminal ``EXPIRED`` status,
   demoting the terms version a now-lapsed approval had promoted. This makes the lapse a *durable,
   auditable record* (a reader/query sees EXPIRED rather than a stale ACTIVE), but it is NEVER what
   enforces the denial — the effective-date check above already withholds the right. The sweep only
   *records* what the clock already decided.

The sweep is a pure function of ``now``: it transitions only rows that are genuinely past their
window, so it is idempotent (a second run with the same clock transitions nothing) and it can never
promote, widen or re-enable anything — it only ever moves a lapsed right from ACTIVE to EXPIRED. It
never touches a not-yet-effective row (an approval whose ``effective_from`` is still in the future is
not lapsed — it simply is not yet in force, which the effective-date check already handles).

Like the other source stores this is platform CONTROL-PLANE data (a shared registry, not
tenant-scoped), so it is constructed with just the session: there is no business scope and no tenant
audit entry. It stages the transitions into the caller's session and does not commit — the caller
(the worker maintenance pass, or a test) owns the unit of work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    SourceApprovalStatus,
    SourceTerms,
    SourceTermsApproval,
    SourceTermsStatus,
)


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalize a (possibly naive — SQLite) datetime to tz-aware UTC for comparison."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)  # SQLite returns naive datetimes
    return value


@dataclass(frozen=True)
class ExpirySweepResult:
    """What one :meth:`SourceExpirySweep.sweep` transitioned, for an inspectable audit.

    ``expired_approvals`` are ``(source_key, terms_version)`` pairs whose review deadline
    (``expires_at``) had passed and were moved ACTIVE → EXPIRED. ``expired_terms`` are
    ``(source_key, version)`` pairs whose terms version was moved ACTIVE → EXPIRED (either because its
    own window lapsed or because the approval that promoted it did). ``evaluated_at`` is the ``now`` the
    sweep ran against.
    """

    evaluated_at: datetime
    expired_approvals: list[tuple[str, int]] = field(default_factory=list)
    expired_terms: list[tuple[str, int]] = field(default_factory=list)

    @property
    def transitioned(self) -> int:
        """Total rows the sweep moved to EXPIRED (0 when nothing had lapsed — the idempotent case)."""
        return len(self.expired_approvals) + len(self.expired_terms)


class SourceExpirySweep:
    """The scheduled source-expiry transition (DCA-049). Platform control-plane; session-only."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def sweep(self, *, now: datetime | None = None) -> ExpirySweepResult:
        """Move every lapsed ACTIVE approval and terms version to EXPIRED at ``now``; stage only.

        A row is lapsed only when its explicit ``expires_at`` is at/before ``now`` — exactly the
        boundary the effective-date checks (``is_in_force`` / ``permits``) already treat as out of
        force. A not-yet-effective row (``effective_from`` in the future) is left ACTIVE: it is not
        lapsed, merely not yet in force. The sweep stages the status flips into the caller's session
        and returns what it transitioned; the caller owns the commit. Idempotent: rows already EXPIRED
        are not ACTIVE, so a second run at the same ``now`` transitions nothing.
        """
        moment = now or datetime.now(UTC)
        result = ExpirySweepResult(evaluated_at=moment)

        # 1) Expired approvals: an ACTIVE approval whose review deadline (expires_at) has passed. Its
        #    endorsement has lapsed — record the durable EXPIRED state and demote the terms it promoted.
        lapsed_approvals = self._session.scalars(
            select(SourceTermsApproval).where(
                SourceTermsApproval.status == SourceApprovalStatus.ACTIVE.value
            )
        ).all()
        demote_keys: set[tuple[str, int]] = set()
        for approval in lapsed_approvals:
            expires_at = _as_utc(approval.expires_at)
            if expires_at is not None and moment >= expires_at:
                approval.status = SourceApprovalStatus.EXPIRED.value
                result.expired_approvals.append((approval.source_key, approval.terms_version))
                demote_keys.add((approval.source_key, approval.terms_version))

        # 2) Expired terms versions: an ACTIVE terms version whose OWN validity window has lapsed, OR
        #    one a now-expired approval had promoted. Either way its rights are no longer in force, so
        #    the catalog should not keep showing it ACTIVE.
        active_terms = self._session.scalars(
            select(SourceTerms).where(SourceTerms.status == SourceTermsStatus.ACTIVE.value)
        ).all()
        for terms in active_terms:
            expires_at = _as_utc(terms.expires_at)
            own_window_lapsed = expires_at is not None and moment >= expires_at
            approval_lapsed = (terms.source_key, terms.version) in demote_keys
            if own_window_lapsed or approval_lapsed:
                terms.status = SourceTermsStatus.EXPIRED.value
                result.expired_terms.append((terms.source_key, terms.version))

        return result


def run_expiry_sweep(
    session_factory, settings=None, *, now: datetime | None = None
) -> ExpirySweepResult:
    """Open a session, run one :class:`SourceExpirySweep`, COMMIT and return the result.

    The worker-facing entry point (mirrors :func:`app.workers.scheduler.tick_all_tenants`): unlike the
    store, this owns its own unit of work so the scheduled maintenance pass can run it standalone.
    ``settings`` is accepted for a uniform worker signature but is unused — the sweep only makes a
    lapsed right MORE restrictive, so it is safe to run whenever the worker loop is active and is not
    gated behind any feature/source flag.
    """
    with session_factory() as session:
        result = SourceExpirySweep(session).sweep(now=now)
        session.commit()
    return result
