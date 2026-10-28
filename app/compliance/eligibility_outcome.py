"""DCA-093 — the eligibility-outcome classifier shared by the scoped eligibility endpoint.

Key: ``eligibility-api`` · source sections 5, 10. Stage: India channel eligibility.

The accepted channel evaluators (DCA-087..092) each return a *binary* ``ELIGIBLE`` / ``BLOCKED``
decision, and each exposes STABLE reason codes per gate. This module adds the one small, pure
translation the HTTP contract needs on top of that binary result: it folds a decision's blocker
reasons into a THREE-state :class:`EligibilityOutcome` —

- ``ELIGIBLE`` — every gate passed; the DCA's own blockers are cleared, so the contact point MAY be
  offered to the one send pipeline (which still re-gates the actual send).
- ``BLOCKED`` — a gate was *determinately* denied (consent not in force, suppressed, provider
  prohibited, source rights insufficient, a basis that is not consent, or evidence that is known to be
  too old). The answer is "no", with the reason.
- ``REVIEW`` — a governing gate could NOT be determined from the inputs supplied: the authoritative
  suppression view was unavailable / stale / out of scope, or the permission evidence's age is
  unknown. This mirrors the policy engine's ``REVIEW`` / ``insufficient_facts`` posture — "a rule
  plausibly applies but a required fact is missing, so defer" — rather than fabricating a false deny.

The load-bearing invariant (a DCA-093 acceptance criterion): **REVIEW is not activation permission.**
:func:`activation_eligible` returns ``True`` ONLY for a definitive ``ELIGIBLE``; both ``REVIEW`` and
``BLOCKED`` yield ``False``. And even an ``ELIGIBLE`` activation-eligibility is never recipient consent
and never a send authorization — the one send pipeline owns that and re-gates everything.

Pure and deterministic: it reads nothing and changes nothing, so an outcome (and the activation flag
derived from it) is perfectly reproducible from a decision's reason codes alone.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum

from app.compliance.email_eligibility import EmailEligibilityDecision
from app.suppression.contract import ActivationDecision

# Reason codes that mean "the gate could not be determined from the inputs", NOT "the gate said no".
# A decision whose decisive blocker is one of these is a REVIEW, never a BLOCKED: the authoritative
# answer was missing, so a human/host must resolve it before activation — deferring, not denying.
INDETERMINATE_REASONS: frozenset[str] = frozenset(
    {
        # Suppression: the host's authoritative registry could not give a usable answer.
        ActivationDecision.LOOKUP_UNAVAILABLE,  # registry unreachable / did not answer
        ActivationDecision.STALE,  # answer too old to rely on
        ActivationDecision.CHANNEL_NOT_COVERED,  # snapshot does not speak for this channel
        # Permission freshness: the consent evidence's age is unknown (cannot confirm it is fresh).
        "evidence_stale_unknown_age",
    }
)


class EligibilityOutcome(str, Enum):
    """The three-state outcome the eligibility API reports, folded from a binary channel decision."""

    ELIGIBLE = "eligible"
    BLOCKED = "blocked"
    REVIEW = "review"


def _email_blocker_reasons(decision: EmailEligibilityDecision) -> Iterable[str]:
    """Normalize an email decision's blockers to the reason codes this classifier screens.

    Two gates carry the discriminating signal in the blocker's ``detail`` rather than its headline
    ``reason``:

    - **suppression** always reports the headline ``suppression_blocked``; the ACTUAL suppression
      outcome (the :class:`ActivationDecision` code — unavailable / stale / not-covered vs an actual
      ``contact_point_suppressed``) is in the ``detail``. Yield the detail so an indeterminate lookup
      is told apart from a real listed-suppression deny.
    - **freshness** reports ``evidence_stale`` with a ``detail`` of ``"unknown"`` when the consent age
      is unknown (indeterminate) versus an explicit age string when it is known-too-old (a real deny).
      Rewrite the unknown-age case to a distinct code.
    """
    for blocker in decision.blockers:
        detail = (blocker.detail or "").strip()
        if blocker.gate == "suppression":
            yield detail or blocker.reason
        elif blocker.gate == "freshness" and detail.lower() == "unknown":
            yield "evidence_stale_unknown_age"
        else:
            yield blocker.reason


def classify_email_outcome(decision: EmailEligibilityDecision) -> EligibilityOutcome:
    """Fold a binary email decision into the three-state outcome.

    ``ELIGIBLE`` when the evaluator cleared every gate. Otherwise, if ANY blocking gate was merely
    *indeterminate* (an unavailable/stale/out-of-scope suppression answer, or an unknown-age consent)
    the outcome is ``REVIEW`` — the answer is "undecided, needs review", which is fail-closed for
    activation but is explicitly NOT a deny. Only when every blocker is a determinate deny is the
    outcome ``BLOCKED``.
    """
    if decision.eligible:
        return EligibilityOutcome.ELIGIBLE
    reasons = set(_email_blocker_reasons(decision))
    if reasons & INDETERMINATE_REASONS:
        return EligibilityOutcome.REVIEW
    return EligibilityOutcome.BLOCKED


def activation_eligible(outcome: EligibilityOutcome) -> bool:
    """Whether this outcome clears the DCA's blockers so the contact MAY be offered to the one send
    pipeline. ``True`` ONLY for a definitive ``ELIGIBLE`` — ``REVIEW`` and ``BLOCKED`` are both
    ``False``. This is the "REVIEW is not activation permission" invariant, stated in one place.

    Even when ``True`` this is NOT recipient consent and NOT a send authorization: the one send
    pipeline re-gates the actual send, and acting on it stays a disabled future capability.
    """
    return outcome is EligibilityOutcome.ELIGIBLE
