"""DCA-077 — require FRESH evidence for re-consent.

The explicit, pure gate for the one permission transition that re-grants a scope the subject previously
WITHDREW: ``WITHDRAWN -> GRANTED``. It is the conclusion-layer sibling of
:mod:`app.consent.permission_state` (DCA-075, which derives the state in force) and the authority for
the DCA-077 service command (:mod:`app.services.consent_reconsent`). Like its siblings it is a pure,
deterministic, network-free function that reads only the events it is handed and mutates / records /
deletes nothing.

Re-consent is NOT a normal grant. After a withdrawal the only thing that may restore permission is a
*new, valid, affirmative* receipt — a genuinely fresh opt-in by the subject. This gate refuses the
three ways a withdrawn permission could be made to *look* re-granted without one (the DCA-077
acceptance criteria), each with its own stable reason code:

- **Replayed original receipt** — re-presenting the ORIGINAL opt-in receipt (same row, or a copy citing
  the same captured evidence) cannot restore consent. The original evidence was the proof of the grant
  that was later withdrawn; reusing it is not a fresh affirmative act (:data:`REASON_REPLAYED_RECEIPT` /
  :data:`REASON_STALE_EVIDENCE`).
- **Stale import** — a grant whose subject-action time (``collected_at``) is at or BEFORE the in-force
  withdrawal cannot restore consent, no matter when it is imported/recorded. This also follows from the
  DCA-075 ordering rule (an older grant is superseded), and is enforced here explicitly so a stale
  import is refused up front with a re-consent-specific denial (:data:`REASON_NOT_AFTER_WITHDRAWAL`).
- **Provider refresh** — a data provider re-asserting the old subscription is not the subject acting
  again. In receipt terms it carries no NEW captured evidence (it reuses the original proof, or none),
  so it is refused by the fresh-evidence requirement (:data:`REASON_STALE_EVIDENCE` /
  :data:`REASON_MISSING_EVIDENCE`).

A valid re-consent therefore requires ALL of: the scope is currently WITHDRAWN; the candidate is an
affirmative action (grant / confirm); it cites captured evidence; that evidence is FRESH — not cited by
any receipt already in the scope's history; and its ``collected_at`` is STRICTLY AFTER the in-force
withdrawal. Only then does the gate report ``restored`` (:data:`REASON_RECONSENTED`).

The gate reaches a *conclusion*; it authorizes no send and records nothing. The DCA-077 service applies
it before appending the fresh grant, so a refused re-consent stages no row. Acting on the restored
permission (authorizing a send) stays a disabled future capability behind the one send pipeline.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from app.consent.permission_state import (
    PermissionEvent,
    PermissionProjection,
    PermissionState,
    ScopeKey,
    derive_permission_state,
    _as_utc,
)

# Stable reason codes explaining the gate's verdict (mirrors the DCA-074/075/076 convention).
# The one success code: a valid affirmative receipt restored a withdrawn scope.
REASON_RECONSENTED = "reconsented"
# The candidate is not an affirmative action (a withdraw/decline cannot re-grant).
REASON_NOT_AFFIRMATIVE = "reconsent_not_affirmative"
# The scope is not currently WITHDRAWN, so there is nothing to re-consent (a grant onto NONE/GRANTED is
# a first grant / a duplicate, not a re-consent transition).
REASON_NOT_WITHDRAWN = "reconsent_scope_not_withdrawn"
# The candidate cites no captured evidence — never a valid receipt (a provider refresh carries none).
REASON_MISSING_EVIDENCE = "reconsent_missing_evidence"
# The candidate's evidence was already cited by a receipt in this scope's history — it is a replay of
# the original proof, not fresh evidence of a new opt-in (replayed receipt / provider refresh).
REASON_STALE_EVIDENCE = "reconsent_stale_evidence"
# The candidate is literally an already-recorded receipt in this scope's history (true row replay).
REASON_REPLAYED_RECEIPT = "reconsent_replayed_receipt"
# The candidate was collected at or BEFORE the in-force withdrawal — a stale import / same-instant
# replay cannot re-grant (also the DCA-075 ordering rule, surfaced here as a re-consent denial).
REASON_NOT_AFTER_WITHDRAWAL = "reconsent_not_after_withdrawal"


@dataclass(frozen=True)
class ReconsentDecision:
    """The verdict of the re-consent gate for ONE scope (a pure conclusion).

    - ``restored`` — ``True`` only when the candidate is a valid re-consent that re-grants the scope.
    - ``scope`` — the ``(brand, contact_point, channel, purpose)`` the verdict concerns.
    - ``prior_state`` — the permission in force BEFORE the candidate (must be ``WITHDRAWN`` to restore).
    - ``resulting_state`` — the state the candidate would produce: ``GRANTED`` when ``restored``,
      otherwise the unchanged ``prior_state`` (a refused re-consent changes nothing).
    - ``withdrawal_event`` — the in-force withdrawal the candidate must post-date (``None`` when the
      scope was not withdrawn).
    - ``reason`` — the single stable code explaining the verdict.
    - ``mutated_state`` — always ``False``; the gate records and changes nothing.
    """

    restored: bool
    scope: ScopeKey
    prior_state: PermissionState
    resulting_state: PermissionState
    withdrawal_event: PermissionEvent | None
    reason: str
    mutated_state: bool = False


def evaluate_reconsent(
    prior_events: Iterable[PermissionEvent],
    candidate: PermissionEvent,
    *,
    now: datetime | None = None,
) -> ReconsentDecision:
    """Decide whether ``candidate`` is a VALID re-consent restoring a withdrawn scope (pure).

    ``prior_events`` is the scope's existing receipt history (the candidate NOT included); ``candidate``
    is the proposed fresh affirmative receipt. All events — prior and candidate — must concern the same
    scope (a mix is a programming error and raises ``ValueError``). The gate never mutates, records or
    deletes anything; it returns a :class:`ReconsentDecision` and nothing else.

    ``restored`` is ``True`` only when every DCA-077 condition holds:

    1. the candidate is an affirmative action (``grant`` / ``confirm``);
    2. the scope's prior state is ``WITHDRAWN`` (there is a withdrawal to re-consent over);
    3. the candidate cites captured evidence (a non-empty ``evidence_id``);
    4. that evidence is FRESH — not cited by any receipt already in ``prior_events`` (and the candidate
       is not itself an already-recorded receipt);
    5. the candidate's ``collected_at`` is STRICTLY AFTER the in-force withdrawal's ``collected_at``.

    Otherwise ``restored`` is ``False`` with the matching ``REASON_*`` denial and ``resulting_state``
    equal to the unchanged prior state — so a provider refresh, a stale import and a replayed original
    receipt are each refused without any state change.
    """
    prior_list = list(prior_events)

    scopes = {e.scope for e in prior_list} | {candidate.scope}
    if len(scopes) > 1:
        raise ValueError(
            "evaluate_reconsent expects prior events and the candidate for a SINGLE scope; got "
            f"{len(scopes)} distinct scopes"
        )
    scope = candidate.scope

    prior = derive_permission_state(prior_list, now=now)
    withdrawal = prior.effective_event if prior.state is PermissionState.WITHDRAWN else None

    def refuse(reason: str) -> ReconsentDecision:
        return ReconsentDecision(
            restored=False,
            scope=scope,
            prior_state=prior.state,
            resulting_state=prior.state,
            withdrawal_event=withdrawal,
            reason=reason,
        )

    # 1) Only an affirmative action can re-grant; a withdraw/decline is not a re-consent.
    if not candidate.is_positive:
        return refuse(REASON_NOT_AFFIRMATIVE)

    # 2) There must be a withdrawal to re-consent over. A grant onto NONE/GRANTED/DECLINED is a first
    #    grant / duplicate / contradiction — handled by the plain state projection, not this gate.
    if withdrawal is None:
        return refuse(REASON_NOT_WITHDRAWN)

    # 3) A valid receipt must cite captured evidence; a provider refresh with none is refused.
    candidate_evidence = (candidate.evidence_id or "").strip()
    if not candidate_evidence:
        return refuse(REASON_MISSING_EVIDENCE)

    # 4) The candidate must not be a literal replay of a receipt already in history, and its evidence
    #    must be FRESH — not the proof of any prior receipt in this scope (replay / provider refresh).
    prior_receipt_ids = {e.receipt_id for e in prior_list if e.receipt_id}
    if candidate.receipt_id and candidate.receipt_id in prior_receipt_ids:
        return refuse(REASON_REPLAYED_RECEIPT)
    prior_evidence = {
        (e.evidence_id or "").strip() for e in prior_list if (e.evidence_id or "").strip()
    }
    if candidate_evidence in prior_evidence:
        return refuse(REASON_STALE_EVIDENCE)

    # 5) The fresh affirmative act must post-date the in-force withdrawal (strictly). This refuses a
    #    stale import and a same-instant replay (the DCA-075 ordering rule, surfaced explicitly here).
    if _as_utc(candidate.collected_at) <= _as_utc(withdrawal.collected_at):
        return refuse(REASON_NOT_AFTER_WITHDRAWAL)

    return ReconsentDecision(
        restored=True,
        scope=scope,
        prior_state=prior.state,
        resulting_state=PermissionState.GRANTED,
        withdrawal_event=withdrawal,
        reason=REASON_RECONSENTED,
    )


def project_after_reconsent(
    prior_events: Iterable[PermissionEvent],
    candidate: PermissionEvent,
    *,
    now: datetime | None = None,
) -> PermissionProjection:
    """Project the scope's state as it WOULD be after applying ``candidate`` (pure, for inspection).

    Appends the candidate to the prior history and runs :func:`~app.consent.derive_permission_state`.
    This is the independent cross-check that the DCA-075 projection agrees with the gate: for a refused
    re-consent (stale import / same-instant replay) the projection still reports ``WITHDRAWN``, and for
    a valid one it reports ``GRANTED``. It mutates nothing — the candidate is only added to a local
    list.
    """
    return derive_permission_state([*prior_events, candidate], now=now)
