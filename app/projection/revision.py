"""DCA-031 — revision-aware projection processing.

:class:`RevisionProjector` is the single sanctioned writer of the DCA-031 projection tables
(:class:`~app.db.models.RevisionProjection` + :class:`~app.db.models.RevisionPendingEvent`). Given an
aggregate's identity and the ``aggregate_version`` an integration event carries, it decides exactly
one of three outcomes and stages the matching state change into the *caller's* session (it never
commits, flushes a transaction of its own, opens one, or does any I/O — the caller owns the unit of
work, like the DCA-026/027/028 sinks):

- **APPLIED** — the event is the next contiguous revision (``version == applied_version + 1``, or the
  very first at ``version == 1``). Its state becomes the projection, ``applied_version`` advances, and
  any buffered successor revisions that are now contiguous are reconciled (applied in order).
- **STALE** — the event's revision is ``<= applied_version``: an older (or duplicate) revision that
  must NOT overwrite the newer projection. Nothing is mutated — the denial is reproducible with no
  state change.
- **GAP** — the event's revision is ``> applied_version + 1``: the revisions in between are missing
  and their state is unknown, so the event is NOT applied over the gap. It is buffered
  (:class:`~app.db.models.RevisionPendingEvent`) so the gap is **visible** and the event can be
  applied in order once the missing revision arrives. The projection is marked ``GAPPED``.

Concurrency: the projection row is read ``FOR UPDATE`` so two concurrent deliveries for the same
aggregate serialize — the second sees the first's committed advance and cannot lose an update or
resurrect a stale revision. ``FOR UPDATE`` is a no-op on SQLite (dev/test); the behaviour that
depends on it is verified on a disposable PostgreSQL.

All opaque-reference discipline from the other durable writers applies: ``aggregate_type`` /
``aggregate_id`` / ``event_id`` and every ``state`` value are screened by the shared DCA-012
redaction policy BEFORE anything is staged; a value carrying a secret or contact datum is refused
with :class:`RevisionPayloadError` and no row is created.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import AuditPayloadError, assert_opaque_references, assert_opaque_text
from app.db.models import RevisionPendingEvent, RevisionProjection, RevisionStatus
from app.identity.tenant_context import TenantContext


class RevisionError(Exception):
    """Base class for a refused revision apply. Carries only shape/version detail, never state."""


class RevisionPayloadError(RevisionError):
    """A revision was refused because its identity or state carried raw secret or contact data.

    Raised BEFORE the session is touched, so a refused apply leaves no row and no partial state.
    Callers must pass opaque references (ids / row keys), never personal data.
    """


class RevisionDecision(str, Enum):
    """The outcome of a single :meth:`RevisionProjector.apply`."""

    APPLIED = "applied"
    STALE = "stale"
    GAP = "gap"


@dataclass(frozen=True)
class RevisionOutcome:
    """What one apply decided, with enough detail to reproduce and audit it (no state echoed).

    ``applied_version`` is the projection's version *after* the call (unchanged on STALE/GAP).
    ``reconciled`` lists any buffered revisions that became contiguous and were applied by this call.
    ``missing`` lists the still-missing revisions below the highest buffered one (the visible gap).
    """

    decision: RevisionDecision
    aggregate_type: str
    aggregate_id: str
    incoming_version: int
    applied_version: int
    reconciled: tuple[int, ...] = ()
    missing: tuple[int, ...] = ()
    reason: str = ""

    @property
    def applied(self) -> bool:
        return self.decision is RevisionDecision.APPLIED

    @property
    def stale(self) -> bool:
        return self.decision is RevisionDecision.STALE

    @property
    def gap(self) -> bool:
        return self.decision is RevisionDecision.GAP


@dataclass
class _Target:
    """The validated, opaque-screened inputs for one apply."""

    business_id: str
    aggregate_type: str
    aggregate_id: str
    version: int
    state: dict
    event_id: str
    occurred_at: datetime | None = field(default=None)


class RevisionProjector:
    """Apply revision-ordered projection events into one caller-owned session.

    Construct it with the active session and bind the business scope per call or once via
    :meth:`for_context`. Every read/write is confined to that business; the scope is taken from the
    verified context, never a caller argument, so a projection can never be attributed to or read
    across another tenant.
    """

    def __init__(self, session: Session, *, business_id: str | None = None) -> None:
        self._session = session
        self._business_id = business_id

    @classmethod
    def for_context(cls, session: Session, context: TenantContext) -> RevisionProjector:
        """Bind the projector to a verified tenant context (scope = the context's business)."""
        return cls(session, business_id=context.business_id)

    # -- public read helpers (tenant-scoped; make the current state and any gap inspectable) --------

    def projection(
        self, *, aggregate_type: str, aggregate_id: str, business_id: str | None = None
    ) -> RevisionProjection | None:
        """Return the projection row for an aggregate within the tenant, or ``None`` if unseen."""
        scope = self._scope(business_id)
        return self._session.scalars(
            self._by_aggregate(
                RevisionProjection, scope, aggregate_type.strip(), aggregate_id.strip()
            )
        ).one_or_none()

    def pending(
        self, *, aggregate_type: str, aggregate_id: str, business_id: str | None = None
    ) -> list[RevisionPendingEvent]:
        """Return the buffered out-of-order revisions for an aggregate, ordered by version."""
        scope = self._scope(business_id)
        stmt = self._by_aggregate(
            RevisionPendingEvent, scope, aggregate_type.strip(), aggregate_id.strip()
        ).order_by(RevisionPendingEvent.aggregate_version)
        return list(self._session.scalars(stmt).all())

    def gaps(
        self, *, aggregate_type: str, aggregate_id: str, business_id: str | None = None
    ) -> list[int]:
        """Return the still-missing revisions below the highest buffered one — the visible gap.

        Computed, never guessed: the versions in ``(applied_version, max buffered]`` for which no
        event has arrived. An empty list means there is no gap (or nothing buffered).
        """
        scope = self._scope(business_id)
        atype, aid = aggregate_type.strip(), aggregate_id.strip()
        proj = self._session.scalars(
            self._by_aggregate(RevisionProjection, scope, atype, aid)
        ).one_or_none()
        applied = proj.applied_version if proj is not None else 0
        return self._missing_versions(scope, atype, aid, applied)

    # -- the one write path -------------------------------------------------------------------------

    def apply(
        self,
        *,
        aggregate_type: str,
        aggregate_id: str,
        version: int,
        state: dict | None = None,
        event_id: str = "",
        occurred_at: datetime | None = None,
        business_id: str | None = None,
    ) -> RevisionOutcome:
        """Apply one projected revision and return the decision (APPLIED / STALE / GAP).

        ``version`` is the aggregate's ``aggregate_version`` (as carried by the DCA-029 envelope);
        it must be a positive integer. ``state`` is the opaque reference map this revision projects.
        Nothing is committed — the caller commits the returned decision's staged change in its own
        unit of work. A missing scope/type/id or a non-positive version is a programming error
        (``ValueError``); a non-opaque identity/state is a :class:`RevisionPayloadError`. Neither
        stages a row.
        """
        target = self._validate(
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            version=version,
            state=state,
            event_id=event_id,
            occurred_at=occurred_at,
            business_id=business_id,
        )

        proj = self._locked_projection(target)
        if proj is None:
            return self._apply_first(target)

        applied = proj.applied_version
        if target.version <= applied:
            # STALE — an older or duplicate revision. Do NOT overwrite; mutate nothing.
            return RevisionOutcome(
                decision=RevisionDecision.STALE,
                aggregate_type=target.aggregate_type,
                aggregate_id=target.aggregate_id,
                incoming_version=target.version,
                applied_version=applied,
                reason=(
                    f"stale revision {target.version} <= applied {applied}; "
                    "newer projection preserved"
                ),
            )
        if target.version == applied + 1:
            self._advance(proj, target.version, target.state, target.event_id, target.occurred_at)
            reconciled = self._reconcile(proj)
            return RevisionOutcome(
                decision=RevisionDecision.APPLIED,
                aggregate_type=target.aggregate_type,
                aggregate_id=target.aggregate_id,
                incoming_version=target.version,
                applied_version=proj.applied_version,
                reconciled=tuple(reconciled),
                reason=(
                    f"applied revision {target.version}"
                    + (f"; reconciled buffered {list(reconciled)}" if reconciled else "")
                ),
            )
        # GAP — revision arrived ahead of a missing predecessor. Buffer it; never apply over a gap.
        self._buffer(target)
        proj.status = RevisionStatus.GAPPED.value
        missing = self._missing_versions(
            target.business_id, target.aggregate_type, target.aggregate_id, applied
        )
        return RevisionOutcome(
            decision=RevisionDecision.GAP,
            aggregate_type=target.aggregate_type,
            aggregate_id=target.aggregate_id,
            incoming_version=target.version,
            applied_version=applied,
            missing=tuple(missing),
            reason=f"gap: revision {target.version} buffered; missing {missing}",
        )

    # -- internals ----------------------------------------------------------------------------------

    def _scope(self, business_id: str | None) -> str:
        effective = business_id or self._business_id
        if not effective:
            raise ValueError("revision projection requires a business scope")
        return effective

    def _validate(
        self,
        *,
        aggregate_type: str,
        aggregate_id: str,
        version: int,
        state: dict | None,
        event_id: str,
        occurred_at: datetime | None,
        business_id: str | None,
    ) -> _Target:
        scope = self._scope(business_id)
        atype = (aggregate_type or "").strip()
        aid = (aggregate_id or "").strip()
        eid = (event_id or "").strip()
        if not atype:
            raise ValueError("revision aggregate_type is required")
        if not aid:
            raise ValueError("revision aggregate_id is required")
        # A bool is an int in Python; reject it explicitly so ``True`` is never a version.
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError("revision version must be a positive integer")

        # Screen BEFORE touching the session: a refused value stages no row. Reuse the single
        # DCA-012 redaction policy (via the audit screens) and surface a revision-specific error.
        try:
            assert_opaque_text("aggregate_type", atype)
            assert_opaque_text("aggregate_id", aid)
            if eid:
                assert_opaque_text("event_id", eid)
            screened = assert_opaque_references(state)
        except AuditPayloadError as exc:
            raise RevisionPayloadError(str(exc)) from exc

        return _Target(
            business_id=scope,
            aggregate_type=atype,
            aggregate_id=aid,
            version=version,
            state=dict(screened),
            event_id=eid,
            occurred_at=occurred_at,
        )

    def _by_aggregate(self, model, scope: str, atype: str, aid: str):
        return select(model).where(
            model.business_id == scope,
            model.aggregate_type == atype,
            model.aggregate_id == aid,
        )

    def _locked_projection(self, target: _Target) -> RevisionProjection | None:
        # FOR UPDATE serialises concurrent deliveries for the SAME aggregate (no-op on SQLite). Two
        # racing applies against one projection cannot both read a stale applied_version.
        stmt = self._by_aggregate(
            RevisionProjection, target.business_id, target.aggregate_type, target.aggregate_id
        ).with_for_update()
        return self._session.scalars(stmt).one_or_none()

    def _apply_first(self, target: _Target) -> RevisionOutcome:
        if target.version == 1:
            proj = RevisionProjection(
                business_id=target.business_id,
                aggregate_type=target.aggregate_type,
                aggregate_id=target.aggregate_id,
                applied_version=1,
                state=dict(target.state),
                status=RevisionStatus.CURRENT.value,
                last_event_id=target.event_id,
                last_occurred_at=target.occurred_at,
            )
            self._session.add(proj)
            return RevisionOutcome(
                decision=RevisionDecision.APPLIED,
                aggregate_type=target.aggregate_type,
                aggregate_id=target.aggregate_id,
                incoming_version=1,
                applied_version=1,
                reason="applied revision 1 (first)",
            )
        # First event for the aggregate is already ahead of revision 1 — a gap from the start. Anchor
        # the aggregate at applied_version 0 (nothing applied) and buffer the event; do NOT guess the
        # missing revisions 1..version-1.
        proj = RevisionProjection(
            business_id=target.business_id,
            aggregate_type=target.aggregate_type,
            aggregate_id=target.aggregate_id,
            applied_version=0,
            state={},
            status=RevisionStatus.GAPPED.value,
            last_event_id="",
        )
        self._session.add(proj)
        self._buffer(target)
        missing = list(range(1, target.version))
        return RevisionOutcome(
            decision=RevisionDecision.GAP,
            aggregate_type=target.aggregate_type,
            aggregate_id=target.aggregate_id,
            incoming_version=target.version,
            applied_version=0,
            missing=tuple(missing),
            reason=f"gap: first event is revision {target.version}; missing {missing}",
        )

    def _buffer(self, target: _Target) -> RevisionPendingEvent:
        # Idempotent: a re-delivery of the same out-of-order revision is not double-buffered.
        existing = self._session.scalars(
            self._by_aggregate(
                RevisionPendingEvent, target.business_id, target.aggregate_type, target.aggregate_id
            ).where(RevisionPendingEvent.aggregate_version == target.version)
        ).one_or_none()
        if existing is not None:
            return existing
        pending = RevisionPendingEvent(
            business_id=target.business_id,
            aggregate_type=target.aggregate_type,
            aggregate_id=target.aggregate_id,
            aggregate_version=target.version,
            event_id=target.event_id,
            state=dict(target.state),
            occurred_at=target.occurred_at,
        )
        self._session.add(pending)
        return pending

    def _advance(
        self,
        proj: RevisionProjection,
        version: int,
        state: dict,
        event_id: str,
        occurred_at: datetime | None,
    ) -> None:
        proj.applied_version = version
        proj.state = dict(state)
        proj.last_event_id = event_id
        proj.last_occurred_at = occurred_at

    def _reconcile(self, proj: RevisionProjection) -> list[int]:
        """Apply any buffered revisions that are now contiguous, in order, and update the status.

        After an advance to version N, a buffered N+1 (then N+2, …) can finally be applied — the gap
        it was waiting on is filled. Each is applied and removed from the buffer. Returns the
        reconciled versions; the projection ends ``CURRENT`` iff nothing remains buffered.
        """
        reconciled: list[int] = []
        while True:
            nxt = self._session.scalars(
                self._by_aggregate(
                    RevisionPendingEvent, proj.business_id, proj.aggregate_type, proj.aggregate_id
                ).where(RevisionPendingEvent.aggregate_version == proj.applied_version + 1)
            ).one_or_none()
            if nxt is None:
                break
            next_version = nxt.aggregate_version
            self._advance(proj, next_version, nxt.state, nxt.event_id, nxt.occurred_at)
            self._session.delete(nxt)
            reconciled.append(next_version)
        remaining = self._pending_count(proj.business_id, proj.aggregate_type, proj.aggregate_id)
        proj.status = RevisionStatus.GAPPED.value if remaining else RevisionStatus.CURRENT.value
        return reconciled

    def _pending_count(self, scope: str, atype: str, aid: str) -> int:
        # autoflush flushes staged deletes before this read, so it reflects the post-reconcile buffer.
        return len(
            self._session.scalars(self._by_aggregate(RevisionPendingEvent, scope, atype, aid)).all()
        )

    def _missing_versions(self, scope: str, atype: str, aid: str, applied: int) -> list[int]:
        pending_versions = sorted(
            self._session.scalars(
                self._by_aggregate(RevisionPendingEvent, scope, atype, aid).with_only_columns(
                    RevisionPendingEvent.aggregate_version
                )
            ).all()
        )
        if not pending_versions:
            return []
        present = set(pending_versions)
        top = pending_versions[-1]
        return [v for v in range(applied + 1, top + 1) if v not in present]
