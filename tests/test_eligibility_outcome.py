"""DCA-093 — the pure eligibility-outcome classifier (the API's REVIEW/BLOCKED/ELIGIBLE fold).

Unit tests over :mod:`app.compliance.eligibility_outcome`, independent of HTTP: a binary channel
decision folds into the three-state outcome, an indeterminate gate (unavailable/stale/out-of-scope
suppression, or unknown-age consent) is REVIEW rather than a fabricated BLOCKED, and the
``activation_eligible`` invariant holds — ONLY a definitive ELIGIBLE is activation-eligible.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.compliance.eligibility_outcome import (
    EligibilityOutcome,
    activation_eligible,
    classify_email_outcome,
)
from app.compliance.email_eligibility import (
    Freshness,
    PermissionBasis,
    ProviderRule,
    SourceRights,
    decide_email_eligibility,
)
from app.compliance.email_eligibility import (
    EmailEligibilityRequest as EmailGates,
)
from app.consent.permission_state import PermissionState
from app.suppression.contract import ActivationDecision

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def _gates(
    *,
    basis: str = "consent",
    state: str = PermissionState.GRANTED.value,
    permits: bool = True,
    age: timedelta | None = timedelta(days=7),
    suppression: ActivationDecision = ActivationDecision(True, ActivationDecision.OK),
    provider_ok: bool = True,
) -> EmailGates:
    return EmailGates(
        contact_point="x@synthetic.example",
        purpose="marketing_email",
        basis=PermissionBasis(basis=basis, permission_state=state),
        source_rights=SourceRights(permits_email_contact=permits),
        freshness=Freshness(age=age),
        suppression=suppression,
        provider=ProviderRule(allowed=provider_ok),
    )


def _classify(**kw) -> EligibilityOutcome:
    return classify_email_outcome(decide_email_eligibility(_gates(**kw), now=NOW))


def test_all_gates_pass_is_eligible():
    assert _classify() is EligibilityOutcome.ELIGIBLE


def test_no_consent_is_determinate_blocked():
    assert _classify(state=PermissionState.NONE.value) is EligibilityOutcome.BLOCKED


def test_known_too_old_consent_is_blocked():
    assert _classify(age=timedelta(days=4000)) is EligibilityOutcome.BLOCKED


def test_unknown_age_consent_is_review():
    assert _classify(age=None) is EligibilityOutcome.REVIEW


def test_unavailable_suppression_is_review():
    unavailable = ActivationDecision(False, ActivationDecision.LOOKUP_UNAVAILABLE)
    assert _classify(suppression=unavailable) is EligibilityOutcome.REVIEW


def test_stale_suppression_is_review():
    stale = ActivationDecision(False, ActivationDecision.STALE)
    assert _classify(suppression=stale) is EligibilityOutcome.REVIEW


def test_suppressed_contact_is_blocked():
    suppressed = ActivationDecision(False, ActivationDecision.SUPPRESSED)
    assert _classify(suppression=suppressed) is EligibilityOutcome.BLOCKED


def test_any_indeterminate_gate_tips_mixed_denial_to_review():
    # Both a determinate deny (no consent) AND an indeterminate gate (unavailable suppression): the
    # presence of an indeterminacy means we cannot be sure it is a true deny -> REVIEW.
    unavailable = ActivationDecision(False, ActivationDecision.LOOKUP_UNAVAILABLE)
    assert _classify(state=PermissionState.NONE.value, suppression=unavailable) is (
        EligibilityOutcome.REVIEW
    )


def test_activation_eligible_only_for_eligible():
    assert activation_eligible(EligibilityOutcome.ELIGIBLE) is True
    assert activation_eligible(EligibilityOutcome.REVIEW) is False
    assert activation_eligible(EligibilityOutcome.BLOCKED) is False
