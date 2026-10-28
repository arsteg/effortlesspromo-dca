"""DCA-089 — evaluate SMS recipient preferences (the one composed recipient-eligibility decision).

Key: ``sms-preference`` · source sections 5, 10. Stage: India channel eligibility.

A pure, deterministic Python evaluator that answers ONE question for the Indian SMS channel — *may a
marketing SMS to this recipient proceed to the one send pipeline?* — by combining TWO deliberately
distinct gates:

1. **consent-first product default** (PRODUCT) — the product requires an explicit, in-force recipient
   consent for the SMS scope regardless of what the law alone would permit. This is stricter than, and
   kept SEPARATE from, the legal DND gate: being absent from the DND register does NOT substitute for
   consent. The only accepted basis is ``consent`` (there is no generic B2B / "legitimate interest"
   exception), and only a ``GRANTED`` permission state on the SMS scope qualifies.
2. **preference / DND screening** (LAW) — the provider-backed TRAI/NCPR preference registry (DCA-089
   ``app.preferences`` contract) must CLEAR the route: available, fresh, covering the SMS channel, and
   with no registered preference that blocks the requested route. This is the legal gate; it is
   evaluated on its own and fails closed when the required screening is unavailable / stale / off-scope.

Load-bearing guarantees (the DCA-089 acceptance criteria):

- **Unavailable required screening fails closed.** When the preference provider could not be consulted
  (or the answer is stale / does not cover SMS), the decision is ``BLOCKED`` with the screening gate's
  stable reason code — never a silent allow. Absence of a DND record is not evidence of its absence.
- **DND results do not grant email or WhatsApp permission.** This decision is SMS-scoped: its channel
  is always ``sms`` and :meth:`SmsRecipientEligibilityDecision.permits_channel` is ``False`` for every
  other channel. The screening snapshot is likewise SMS-scoped, so a clear DND answer can never clear
  email or WhatsApp — the contract refuses a channel it does not cover.
- **Consent-first product default kept distinct from law.** The two gates are independent and BOTH must
  pass: a recipient who is not on DND (law clear) is still ``BLOCKED`` without product consent, and a
  consented recipient who IS on DND is still ``BLOCKED``. Clearing DND is never recipient consent.
- **Reproducible, with the relevant denial, changing no state.** The decision is a pure function of its
  structured inputs at a given ``now`` — it reads nothing and writes nothing, so both the eligible
  result and every denial are perfectly reproducible with no state change.

This function is deliberately **not wired** into any send, acquisition, worker or HTTP path — nothing
calls it except its own tests and the manual-check scenario. Concluding ELIGIBLE enforces nothing and
sends nothing: the actual SMS send stays a disabled future capability behind the host's one send
pipeline, which re-gates everything. Eligibility removes the DCA's recipient-preference blockers; it is
NOT recipient consent authority and NOT a send authorization.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

from app.compliance.sms_sender_prerequisites import SMS_CHANNEL
from app.consent.permission_state import PermissionProjection, PermissionState
from app.preferences.contract import (
    DEFAULT_MAX_PREFERENCE_AGE,
    PreferenceScreeningDecision,
    PreferenceScreeningSnapshot,
    screen_preferences,
)

# Consent-first: the single lawful/product basis this evaluator accepts for a marketing SMS. There is
# no generic B2B / legitimate-interest basis — anything else is rejected (``basis_not_consent``). This
# is the PRODUCT default, kept distinct from the legal DND gate below.
BASIS_CONSENT = "consent"


class SmsRecipientEligibility(str, Enum):
    """The two outcomes of the composed recipient decision.

    - ``ELIGIBLE`` — both gates passed; the recipient MAY be offered to the one send pipeline on SMS
      (which still re-gates the actual send). This is not recipient-consent authority and not a send
      authorization.
    - ``BLOCKED`` — at least one gate withheld permission. Fail-closed is the default: anything other
      than an explicit, in-force SMS consent AND a clear, available, fresh, in-scope DND screening is
      ``BLOCKED``.
    """

    ELIGIBLE = "eligible"
    BLOCKED = "blocked"


# Stable, test-asserted reason codes. The product gate owns the first two; the screening (law) gate
# bubbles up its own codes from :class:`PreferenceScreeningDecision`.
REASON_OK = "ok"  # ELIGIBLE: both gates passed
REASON_BASIS_NOT_CONSENT = "basis_not_consent"  # a non-consent basis was claimed (no B2B exception)
REASON_PRODUCT_CONSENT_NOT_IN_FORCE = (
    "product_consent_not_in_force"  # no in-force SMS consent (none/withdrawn/declined)
)


@dataclass(frozen=True)
class ProductConsent:
    """The consent-first PRODUCT gate input — distinct from the legal DND screening.

    ``basis`` is the claimed basis; only :data:`BASIS_CONSENT` is accepted. ``permission_state`` is the
    DCA-075 :class:`PermissionState` value for the SMS+purpose scope — only ``GRANTED`` means consent is
    in force. ``scope_channel`` is recorded so a non-SMS projection cannot be mistaken for an SMS basis.
    """

    basis: str
    permission_state: str
    scope_channel: str = SMS_CHANNEL

    @property
    def is_consent(self) -> bool:
        return self.basis.strip().casefold() == BASIS_CONSENT

    @property
    def is_granted(self) -> bool:
        return (
            self.permission_state == PermissionState.GRANTED.value
            and self.scope_channel.strip().casefold() == SMS_CHANNEL
        )


@dataclass(frozen=True)
class SmsRecipientEligibilityRequest:
    """The structured inputs to the composed recipient decision — one value object per gate.

    ``consent`` is the PRODUCT gate; ``screening`` is the already-computed LAW gate (a
    :class:`PreferenceScreeningDecision` from :func:`app.preferences.contract.screen_preferences`).
    ``contact_point``, ``purpose`` and ``route`` are provenance for the reported result.
    """

    contact_point: str
    purpose: str
    route: str
    consent: ProductConsent
    screening: PreferenceScreeningDecision


@dataclass(frozen=True)
class RecipientEligibilityBlocker:
    """One gate that withheld eligibility — its gate name, a stable reason code, and any upstream detail."""

    gate: str
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class SmsRecipientEligibilityDecision:
    """The result of the composed recipient decision — reproducible and auditable, changing no state.

    ``blockers`` lists EVERY gate that withheld eligibility, in a fixed gate order, so a reviewer can
    see that (for example) the DND screening denied while consent was in force, or that consent was
    missing while DND was clear. ``decisive`` is the first blocker (the headline denial). ``eligible``
    is true only when there are no blockers at all. The decision is ALWAYS SMS-scoped.
    """

    decision: SmsRecipientEligibility
    contact_point: str
    purpose: str
    route: str
    channel: str
    blockers: tuple[RecipientEligibilityBlocker, ...]
    evaluated_at: str

    @property
    def eligible(self) -> bool:
        return self.decision is SmsRecipientEligibility.ELIGIBLE

    @property
    def reasons(self) -> tuple[str, ...]:
        """Stable reason codes: ``(ok,)`` when eligible, else one per blocker in gate order."""
        if self.eligible:
            return (REASON_OK,)
        return tuple(b.reason for b in self.blockers)

    @property
    def decisive(self) -> RecipientEligibilityBlocker | None:
        return self.blockers[0] if self.blockers else None

    @property
    def grants_consent(self) -> bool:
        """ELIGIBLE is never recipient-consent authority, and never a send authorization. Kept explicit
        so no caller mistakes it for one — the host owns consent and the one send pipeline owns the send."""
        return False

    def permits_channel(self, channel: str) -> bool:
        """This decision authorises nothing beyond the SMS channel it evaluated.

        Only ``True`` when ELIGIBLE AND ``channel`` is the SMS channel. ALWAYS ``False`` for email /
        WhatsApp: the SMS recipient-preference decision (and the DND screening underneath it) grants no
        permission on another channel (the DCA-089 cross-channel invariant)."""
        return self.eligible and channel.strip().casefold() == self.channel.strip().casefold()


def decide_sms_recipient_eligibility(
    request: SmsRecipientEligibilityRequest, *, now: datetime | None = None
) -> SmsRecipientEligibilityDecision:
    """Combine the consent-first product gate and the DND screening gate into one SMS recipient-eligibility
    decision — pure, no I/O, no AI.

    Both gates are evaluated (not short-circuited) and each failure appends a blocker in a FIXED gate
    order, so the result shows exactly which gate(s) withheld permission — the product consent default
    and the legal DND screening are independent, and either can deny alone. ELIGIBLE requires BOTH to
    pass. The function reads nothing and changes nothing, so a given ``(request, now)`` always yields the
    identical result. ELIGIBLE is SMS-scoped and is NOT recipient consent authority.
    """
    moment = now or datetime.now(UTC)
    blockers: list[RecipientEligibilityBlocker] = []

    # 1) Consent-first PRODUCT default — a non-consent basis (a generic "B2B" / legitimate-interest
    #    claim) is rejected outright: there is no such exception in this service.
    if not request.consent.is_consent:
        blockers.append(
            RecipientEligibilityBlocker(
                "basis", REASON_BASIS_NOT_CONSENT, detail=request.consent.basis
            )
        )

    # 2) Product consent in force — only an explicit, in-force consent for the SMS scope qualifies. A
    #    recipient who is merely not on DND (law clear) is still blocked here: consent is the product
    #    default, kept distinct from the legal DND gate below.
    if not request.consent.is_granted:
        blockers.append(
            RecipientEligibilityBlocker(
                "product_consent",
                REASON_PRODUCT_CONSENT_NOT_IN_FORCE,
                detail=request.consent.permission_state,
            )
        )

    # 3) Preference / DND screening (LAW) — the provider-backed registry must clear the route. Evaluated
    #    on its own so it can deny even when consent is in force; fails closed when the required
    #    screening is unavailable / stale / off-scope.
    if not request.screening.allowed:
        blockers.append(
            RecipientEligibilityBlocker(
                "preference_screening",
                request.screening.reason,
                detail=request.screening.detail,
            )
        )

    decision = SmsRecipientEligibility.BLOCKED if blockers else SmsRecipientEligibility.ELIGIBLE
    return SmsRecipientEligibilityDecision(
        decision=decision,
        contact_point=request.contact_point,
        purpose=request.purpose,
        route=request.route,
        channel=SMS_CHANNEL,
        blockers=tuple(blockers),
        evaluated_at=moment.isoformat(),
    )


# -- convenience builders over the existing gate value objects --------------------------------------
# These adapt already-derived results from the earlier stories into this function's inputs. They add no
# I/O and consult no model; they only translate shapes, so the composition stays pure and reproducible.


def consent_from_projection(
    projection: PermissionProjection | None, *, basis: str = BASIS_CONSENT
) -> ProductConsent:
    """Build the product-consent gate input from a DCA-075 :class:`PermissionProjection` (or ``None``).

    A ``None`` projection (no receipts for the scope) maps to ``NONE`` — no permission — so a flag-only
    import that never produced an evidenced receipt is correctly treated as "no consent in force".
    """
    if projection is None:
        return ProductConsent(basis=basis, permission_state=PermissionState.NONE.value)
    return ProductConsent(
        basis=basis,
        permission_state=projection.state.value,
        scope_channel=projection.scope.channel,
    )


def evaluate_sms_recipient_eligibility(
    *,
    contact_point: str,
    purpose: str,
    route: str,
    permission: PermissionProjection | None,
    screening_snapshot: PreferenceScreeningSnapshot | None,
    now: datetime | None = None,
    max_preference_age: timedelta = DEFAULT_MAX_PREFERENCE_AGE,
) -> SmsRecipientEligibilityDecision:
    """Assemble the gate inputs from the existing building blocks, then
    :func:`decide_sms_recipient_eligibility`.

    A convenience that derives the product-consent gate from a DCA-075 projection and the DND screening
    gate from a DCA-089 preference snapshot (via :func:`screen_preferences`). Still pure: it opens no DB,
    calls no adapter and consults no model — it only composes value objects the earlier stories
    produced.
    """
    moment = now or datetime.now(UTC)
    screening = screen_preferences(
        screening_snapshot,
        contact_point,
        route,
        channel=SMS_CHANNEL,
        now=moment,
        max_age=max_preference_age,
    )
    request = SmsRecipientEligibilityRequest(
        contact_point=contact_point,
        purpose=purpose,
        route=route,
        consent=consent_from_projection(permission),
        screening=screening,
    )
    return decide_sms_recipient_eligibility(request, now=moment)


def reason_codes(decisions: Iterable[SmsRecipientEligibilityDecision]) -> list[tuple[str, ...]]:
    """Small helper for scenarios/tests: the reason tuples of a sequence of decisions, in order."""
    return [d.reasons for d in decisions]
