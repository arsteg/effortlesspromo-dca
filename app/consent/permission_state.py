"""DCA-075 — derive the CURRENT permission state from the versioned receipt/withdrawal history.

This is the *conclusion* sibling of :mod:`app.services.consent_receipt` (DCA-074), modelled on
:mod:`app.canonical` / :mod:`app.quality`: a pure, deterministic, network-free layer that sits on top
of the append-only :class:`~app.db.models.ConsentReceipt` timeline and PROJECTS the permission that is
in force *now* for a scope — without touching the event history. It reads only the events it is handed,
and it records, mutates and deletes nothing: every grant / confirmation / withdrawal / decline stays in
the store, so the permission timeline remains fully reviewable and the projection is reproducible.

A permission is scoped, never a global flag, so the unit of projection is one **scope** —
``(brand_key, contact_point_id, channel, purpose)`` — exactly the DCA-074 scoping dimensions.

The load-bearing guarantees (the DCA-075 acceptance criteria):

- **Out-of-order and duplicate events cannot revive a withdrawn permission.** The current state is the
  effect of the event that is latest *by the time the subject acted* (``collected_at``), NOT by the
  order the rows happened to be recorded. So a delayed old grant — recorded (``recorded_at``) AFTER a
  withdrawal but collected BEFORE it — is superseded and does not revive permission
  (:data:`REASON_LATE_GRANT_SUPERSEDED`), and a duplicate of an earlier event collapses to the same
  state (:data:`REASON_DUPLICATE_IGNORED`). At the SAME ``collected_at`` a revocation (withdraw /
  decline) always beats a positive event, so a same-instant replay can never re-grant
  (:data:`REASON_REVOCATION_WINS_TIE`).

- **Channel and purpose scopes stay distinct.** Events are grouped by the full scope key, so a grant
  for one purpose never satisfies another purpose, and a grant on one channel never satisfies another.
  :func:`project_permission_states` keeps each scope's projection independent.

- **Reproducible, with the relevant denial.** The projection is a pure function of the supplied events
  at a given ``now``, so both the in-force state and the "withdrawn, not revived" denial are perfectly
  reproducible with no state to change. Acting on a permission (authorizing a send) stays a disabled
  future capability behind the one send pipeline; this layer only computes and explains the state.

Retention: this layer faithfully reflects whatever history it is handed. Retention policy governs which
receipt rows are *kept* (a separate, disabled purge capability) — it is never applied by silently
dropping events here, because that could change a derived state without an audit trail. The projection
over the retained history is always exact.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.db.models import ConsentReceipt

# The permission actions a receipt can record (mirrors app.db.models.ConsentAction values, kept as
# plain strings so this conclusion layer takes no ORM import at runtime).
ACTION_GRANT = "grant"
ACTION_CONFIRM = "confirm"
ACTION_WITHDRAW = "withdraw"
ACTION_DECLINE = "decline"

# Positive actions assert a permission is in force; negative actions revoke / refuse it.
_POSITIVE_ACTIONS: frozenset[str] = frozenset({ACTION_GRANT, ACTION_CONFIRM})
_NEGATIVE_ACTIONS: frozenset[str] = frozenset({ACTION_WITHDRAW, ACTION_DECLINE})

# Reason codes explaining the derived state (stable, reviewable).
REASON_NO_EVENTS = "no_events"
REASON_GRANTED_IN_FORCE = "granted_in_force"
REASON_CONFIRMED_IN_FORCE = "confirmed_in_force"
REASON_WITHDRAWN = "withdrawn"
REASON_DECLINED = "declined"
# The load-bearing acceptance guards:
# a positive event exists but is OLDER than the in-force revocation, so it does not revive permission.
REASON_LATE_GRANT_SUPERSEDED = "late_grant_superseded"
# a revocation at the SAME collected_at as a positive event wins (a same-instant replay can't re-grant).
REASON_REVOCATION_WINS_TIE = "revocation_wins_tie"
# a duplicate of an already-seen event (same action + same collected_at) is ignored.
REASON_DUPLICATE_IGNORED = "duplicate_ignored"


class PermissionState(str, Enum):
    """The permission in force for a scope *now*, derived from its event history.

    - ``GRANTED`` — the latest effective event is a grant / confirmation: permission is in force.
    - ``WITHDRAWN`` — the latest effective event is a withdrawal: permission is NOT in force.
    - ``DECLINED`` — the latest effective event is a decline: permission was never given.
    - ``NONE`` — no events for this scope: there is no permission (fail-closed).

    Only ``GRANTED`` means a send could be permitted on compliance grounds — and acting on it stays a
    disabled future capability regardless.
    """

    GRANTED = "granted"
    WITHDRAWN = "withdrawn"
    DECLINED = "declined"
    NONE = "none"


def _as_utc(moment: datetime) -> datetime:
    """Normalize to a tz-aware UTC datetime. SQLite returns naive datetimes, so a naive value is
    assumed to already be UTC (matching the store's convention) rather than compared tz-naively."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


@dataclass(frozen=True)
class ScopeKey:
    """The full scope a permission lives in — the DCA-074 scoping dimensions.

    Channel and purpose are BOTH part of the key, so permission on one channel/purpose never leaks to
    another (the acceptance criterion that scopes stay distinct)."""

    brand_key: str
    contact_point_id: str
    channel: str
    purpose: str


@dataclass(frozen=True)
class PermissionEvent:
    """One receipt as the projection sees it — a value object, never tenant state.

    - ``receipt_id`` — the id of the backing :class:`~app.db.models.ConsentReceipt` (never mutated).
    - ``action`` — ``grant`` | ``confirm`` | ``withdraw`` | ``decline``.
    - ``collected_at`` — WHEN THE SUBJECT ACTED. This is the ordering authority: the latest event by
      ``collected_at`` decides the state, so an out-of-order recording cannot change the outcome.
    - ``recorded_at`` — when the row was written locally (``ConsentReceipt.created_at``). A late, stable
      tiebreak only; it never overrides ``collected_at``.
    - ``evidence_id`` — the captured proof the backing receipt cites (``ConsentReceipt.evidence_id``),
      empty when the event cites none (a withdrawal may). It does NOT affect the state projection; it is
      read by the DCA-077 re-consent gate to require FRESH evidence for a withdrawn→granted transition.
    """

    receipt_id: str
    brand_key: str
    contact_point_id: str
    channel: str
    purpose: str
    action: str
    collected_at: datetime
    recorded_at: datetime | None = None
    evidence_id: str = ""

    @property
    def scope(self) -> ScopeKey:
        return ScopeKey(self.brand_key, self.contact_point_id, self.channel, self.purpose)

    @property
    def is_positive(self) -> bool:
        return self.action in _POSITIVE_ACTIONS

    @property
    def is_negative(self) -> bool:
        return self.action in _NEGATIVE_ACTIONS


@dataclass(frozen=True)
class PermissionProjection:
    """The current permission state for ONE scope, derived from its event history (pure).

    - ``scope`` — the ``(brand, contact_point, channel, purpose)`` this projection is about.
    - ``state`` / ``granted`` — the permission in force now (``granted`` is ``state is GRANTED``).
    - ``effective_event`` — the event that decided the state (``None`` when there are no events).
    - ``applied_event_ids`` — the distinct events considered, oldest-effective first (duplicates
      excluded); the LAST is the effective one.
    - ``superseded_event_ids`` — applied events that an later event overrode (incl. a late grant that
      did not revive a withdrawal); preserved in history, they simply do not decide the state.
    - ``duplicate_event_ids`` — events dropped as duplicates of an already-seen event; preserved too.
    - ``reasons`` — stable codes explaining the state and any acceptance-relevant denial.
    - ``mutated_state`` — always ``False``; a projection is a pure conclusion.
    """

    scope: ScopeKey
    state: PermissionState
    effective_event: PermissionEvent | None
    applied_event_ids: tuple[str, ...]
    superseded_event_ids: tuple[str, ...]
    duplicate_event_ids: tuple[str, ...]
    reasons: tuple[str, ...]
    mutated_state: bool = False

    @property
    def granted(self) -> bool:
        return self.state is PermissionState.GRANTED

    @property
    def effective_action(self) -> str:
        return self.effective_event.action if self.effective_event is not None else ""

    @property
    def collected_at(self) -> datetime | None:
        return self.effective_event.collected_at if self.effective_event is not None else None


def _order_key(event: PermissionEvent) -> tuple[float, int, float, str]:
    """A total, deterministic ordering key — ASCENDING, so the LAST element is the effective event.

    Priority: the subject's action time (``collected_at``) first — so the genuinely latest event wins
    regardless of the order rows were recorded. At an EQUAL ``collected_at`` a negative action sorts
    AFTER a positive one (polarity rank 1 vs 0), so a revocation beats a same-instant grant and a replay
    can never re-grant. Remaining ties fall to the recorded time then the stable receipt id, so the
    result is fully reproducible (positive-vs-positive or negative-vs-negative ties don't change state).
    """
    recorded = event.recorded_at
    recorded_ts = _as_utc(recorded).timestamp() if recorded is not None else float("-inf")
    polarity = 1 if event.is_negative else 0
    return (_as_utc(event.collected_at).timestamp(), polarity, recorded_ts, event.receipt_id)


def _state_for(action: str) -> PermissionState:
    if action == ACTION_GRANT:
        return PermissionState.GRANTED
    if action == ACTION_CONFIRM:
        return PermissionState.GRANTED
    if action == ACTION_WITHDRAW:
        return PermissionState.WITHDRAWN
    if action == ACTION_DECLINE:
        return PermissionState.DECLINED
    # An unknown action is treated fail-closed (no permission); the writer already rejects unknowns.
    return PermissionState.NONE


def _reasons_for(
    effective: PermissionEvent, superseded: list[PermissionEvent], duplicates_present: bool
) -> tuple[str, ...]:
    """Explain the in-force state and the acceptance-relevant denial for one scope."""
    reasons: list[str] = []
    if effective.action == ACTION_GRANT:
        reasons.append(REASON_GRANTED_IN_FORCE)
    elif effective.action == ACTION_CONFIRM:
        reasons.append(REASON_CONFIRMED_IN_FORCE)
    elif effective.action == ACTION_WITHDRAW:
        reasons.append(REASON_WITHDRAWN)
    elif effective.action == ACTION_DECLINE:
        reasons.append(REASON_DECLINED)

    # The load-bearing guard: the state is a revocation, yet a positive (grant/confirm) event exists in
    # history — it was superseded and did NOT revive permission.
    if effective.is_negative and any(e.is_positive for e in superseded):
        reasons.append(REASON_LATE_GRANT_SUPERSEDED)

    # A revocation that won over a positive event recorded at the SAME instant (same-time replay guard).
    eff_collected = _as_utc(effective.collected_at)
    if effective.is_negative and any(
        e.is_positive and _as_utc(e.collected_at) == eff_collected for e in superseded
    ):
        reasons.append(REASON_REVOCATION_WINS_TIE)

    if duplicates_present:
        reasons.append(REASON_DUPLICATE_IGNORED)
    return tuple(reasons)


def derive_permission_state(
    events: Iterable[PermissionEvent], *, now: datetime | None = None
) -> PermissionProjection:
    """Project the current permission state for ONE scope from its event history (pure, explainable).

    All events must concern the same scope (a mix is a programming error and raises ``ValueError`` —
    use :func:`project_permission_states` for a mixed set). Duplicates (same action AND same
    ``collected_at``) collapse to a single applied event. The effective event is the latest by
    :func:`_order_key`; its action yields the state. Mutates nothing; deletes nothing.

    ``now`` is accepted for signature symmetry with :func:`project_permission_states` (and reserved for
    a future retention-horizon report); the state itself is a pure function of the supplied events.
    """
    del now  # the projection does not depend on wall-clock time, only on the events' own timestamps
    items = list(events)

    scopes = {e.scope for e in items}
    if len(scopes) > 1:
        raise ValueError(
            "derive_permission_state expects events for a single scope; got "
            f"{len(scopes)} distinct scopes (use project_permission_states)"
        )
    scope = next(iter(scopes), ScopeKey("", "", "", ""))

    # Deterministic order first, so "already seen" is stable and duplicates are the LATER-ordered copies.
    ordered = sorted(items, key=_order_key)

    applied: list[PermissionEvent] = []
    duplicate_ids: list[str] = []
    seen_signatures: set[tuple[str, float]] = set()
    for event in ordered:
        signature = (event.action, _as_utc(event.collected_at).timestamp())
        if signature in seen_signatures:
            # A duplicate of an already-seen event — same action at the same instant. It cannot change
            # the state; keep it preserved in history but out of the applied timeline.
            duplicate_ids.append(event.receipt_id)
            continue
        seen_signatures.add(signature)
        applied.append(event)

    if not applied:
        return PermissionProjection(
            scope=scope,
            state=PermissionState.NONE,
            effective_event=None,
            applied_event_ids=(),
            superseded_event_ids=(),
            duplicate_event_ids=tuple(duplicate_ids),
            reasons=(REASON_NO_EVENTS,),
        )

    effective = applied[-1]
    superseded = applied[:-1]
    return PermissionProjection(
        scope=scope,
        state=_state_for(effective.action),
        effective_event=effective,
        applied_event_ids=tuple(e.receipt_id for e in applied),
        superseded_event_ids=tuple(e.receipt_id for e in superseded),
        duplicate_event_ids=tuple(duplicate_ids),
        reasons=_reasons_for(effective, superseded, bool(duplicate_ids)),
    )


def project_permission_states(
    events: Iterable[PermissionEvent], *, now: datetime | None = None
) -> dict[ScopeKey, PermissionProjection]:
    """Group events by scope and project each scope's current permission independently (pure).

    This is how channel/purpose scopes stay distinct: a grant for one ``(channel, purpose)`` never
    appears in another scope's history, so it can never satisfy a different channel or purpose. Returns
    a dict keyed by :class:`ScopeKey`. Mutates nothing; deletes nothing.
    """
    moment = now or datetime.now(UTC)
    by_scope: dict[ScopeKey, list[PermissionEvent]] = {}
    for event in events:
        by_scope.setdefault(event.scope, []).append(event)
    return {scope: derive_permission_state(group, now=moment) for scope, group in by_scope.items()}


# -- read-only adapter over stored consent receipts -------------------------------------------------


def events_from_receipts(receipts: Iterable["ConsentReceipt"]) -> list[PermissionEvent]:
    """Build :class:`PermissionEvent` value objects from stored DCA-074 receipts (read-only).

    The caller supplies the receipts already read within its tenant scope (e.g.
    ``ConsentReceiptStore.for_contact_point(...)`` or ``.receipts()``), so this never widens scope and
    reads only what it is handed. ``collected_at`` (the subject's action time) becomes the ordering
    authority and ``created_at`` the stable recorded-time tiebreak. Builds value objects only — it
    stages, mutates and deletes nothing.
    """
    return [
        PermissionEvent(
            receipt_id=r.id,
            brand_key=r.brand_key,
            contact_point_id=r.contact_point_id,
            channel=r.channel,
            purpose=r.purpose,
            action=r.action,
            collected_at=r.collected_at,
            recorded_at=r.created_at,
            evidence_id=r.evidence_id or "",
        )
        for r in receipts
    ]
