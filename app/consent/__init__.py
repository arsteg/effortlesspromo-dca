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
- :func:`issue_withdrawal_token` / :func:`verify_withdrawal_token` and
  :class:`WithdrawalTokenClaims` — the DCA-076 public one-click-unsubscribe token (pure, signed).
- :func:`evaluate_reconsent` / :func:`project_after_reconsent`, :class:`ReconsentDecision` and the
  ``RECONSENT_*`` reason codes — the DCA-077 fresh-evidence gate for a withdrawn->granted transition
  (the reason constants are re-exported with a ``RECONSENT_`` prefix to keep the package namespace
  distinct from the DCA-075 state-projection ``REASON_*`` codes).
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
from app.consent.reconsent import (
    REASON_MISSING_EVIDENCE as RECONSENT_MISSING_EVIDENCE,
)
from app.consent.reconsent import (
    REASON_NOT_AFFIRMATIVE as RECONSENT_NOT_AFFIRMATIVE,
)
from app.consent.reconsent import (
    REASON_NOT_AFTER_WITHDRAWAL as RECONSENT_NOT_AFTER_WITHDRAWAL,
)
from app.consent.reconsent import (
    REASON_NOT_WITHDRAWN as RECONSENT_NOT_WITHDRAWN,
)
from app.consent.reconsent import (
    REASON_RECONSENTED as RECONSENT_OK,
)
from app.consent.reconsent import (
    REASON_REPLAYED_RECEIPT as RECONSENT_REPLAYED_RECEIPT,
)
from app.consent.reconsent import (
    REASON_STALE_EVIDENCE as RECONSENT_STALE_EVIDENCE,
)
from app.consent.reconsent import (
    ReconsentDecision,
    evaluate_reconsent,
    project_after_reconsent,
)
from app.consent.withdrawal_token import (
    TOKEN_VERSION,
    WithdrawalTokenClaims,
    WithdrawalTokenError,
    issue_withdrawal_token,
    verify_withdrawal_token,
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
    "TOKEN_VERSION",
    "WithdrawalTokenClaims",
    "WithdrawalTokenError",
    "issue_withdrawal_token",
    "verify_withdrawal_token",
    "RECONSENT_OK",
    "RECONSENT_NOT_AFFIRMATIVE",
    "RECONSENT_NOT_WITHDRAWN",
    "RECONSENT_MISSING_EVIDENCE",
    "RECONSENT_STALE_EVIDENCE",
    "RECONSENT_REPLAYED_RECEIPT",
    "RECONSENT_NOT_AFTER_WITHDRAWAL",
    "ReconsentDecision",
    "evaluate_reconsent",
    "project_after_reconsent",
]
