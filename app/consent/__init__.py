"""Derive the CURRENT permission state from the versioned consent receipt history (DCA-075).

A pure, deterministic, network-free *conclusion* layer (the sibling of :mod:`app.canonical` and
:mod:`app.quality`) on top of the append-only DCA-074 :class:`~app.db.models.ConsentReceipt` timeline.
For one scope — ``(brand_key, contact_point_id, channel, purpose)`` — it projects the permission in
force *now* from the grant / confirmation / withdrawal / decline events, without touching the history:
an out-of-order or duplicate event can never revive a withdrawn permission, and channel/purpose scopes
stay distinct. It reads only what it is handed and writes / mutates / deletes nothing; acting on a
permission (authorizing a send) stays a disabled future capability.

Public surface:

- :func:`derive_permission_state` / :func:`project_permission_states` — the projection.
- :class:`PermissionProjection` / :class:`PermissionEvent` / :class:`ScopeKey` /
  :class:`PermissionState`.
- :func:`events_from_receipts` — the read-only adapter over stored consent receipts.
- The stable ``REASON_*`` codes and ``ACTION_*`` constants.
"""

from __future__ import annotations

from app.consent.permission_state import (
    ACTION_CONFIRM,
    ACTION_DECLINE,
    ACTION_GRANT,
    ACTION_WITHDRAW,
    REASON_CONFIRMED_IN_FORCE,
    REASON_DECLINED,
    REASON_DUPLICATE_IGNORED,
    REASON_GRANTED_IN_FORCE,
    REASON_LATE_GRANT_SUPERSEDED,
    REASON_NO_EVENTS,
    REASON_REVOCATION_WINS_TIE,
    REASON_WITHDRAWN,
    PermissionEvent,
    PermissionProjection,
    PermissionState,
    ScopeKey,
    derive_permission_state,
    events_from_receipts,
    project_permission_states,
)

__all__ = [
    "ACTION_CONFIRM",
    "ACTION_DECLINE",
    "ACTION_GRANT",
    "ACTION_WITHDRAW",
    "REASON_CONFIRMED_IN_FORCE",
    "REASON_DECLINED",
    "REASON_DUPLICATE_IGNORED",
    "REASON_GRANTED_IN_FORCE",
    "REASON_LATE_GRANT_SUPERSEDED",
    "REASON_NO_EVENTS",
    "REASON_REVOCATION_WINS_TIE",
    "REASON_WITHDRAWN",
    "PermissionEvent",
    "PermissionProjection",
    "PermissionState",
    "ScopeKey",
    "derive_permission_state",
    "events_from_receipts",
    "project_permission_states",
]
