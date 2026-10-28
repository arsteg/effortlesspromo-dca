"""DCA-087 — evaluate email marketing eligibility (the one composed decision function).

Key: ``email-policy`` · source sections 5, 10. Stage: India channel eligibility.

A pure, deterministic Python evaluator that answers ONE question for the email channel — *may a
marketing email to this contact point proceed to the one send pipeline?* — by COMBINING the gates the
earlier accepted stories each built in isolation:

1. **permission / lawful basis** — consent-first. The ONLY accepted basis is explicit, IN-FORCE consent
   for this ``(brand, contact point, email, purpose)`` scope (the DCA-074 receipt / DCA-075 projection).
   There is deliberately **no generic B2B / "legitimate interest" exception**: a claimed non-consent
   basis is rejected, and a contact being verified or high-fit is NOT evidence of permission.
2. **source rights** — the acquisition source's in-force rights (DCA-045/046/047/048 chain) must permit
   USING this address for outbound contact. The DCA never widens past them.
3. **freshness** — the permission evidence must be fresh. A basis collected too long ago cannot be
   relied on (a withdrawal recorded since might be in force), so a stale basis blocks.
4. **suppression** — the host-owned authoritative suppression view (DCA-079/080) must clear the
   ``(contact point, email)`` for activation. Fail-closed: unavailable / stale / not-covered / listed
   all block.
5. **sender-provider rules** — the sending channel provider's acceptable-use restriction can
   **independently deny** even when every other gate passes (``law > provider > product``: a provider
   default only ever NARROWS, never widens).

Load-bearing guarantees (the DCA-087 acceptance criteria):

- **A verified / high-fit email stays BLOCKED without the required evidence.** Verification and fit are
  carried for audit only (like :attr:`SourcePreflight.origin_signals`) and are NEVER consulted by the
  allow decision; absent in-force consent the result is ``BLOCKED`` (``consent_not_in_force``).
- **Provider restrictions can independently deny.** The provider gate is evaluated on its own, so a
  fixture that passes permission / rights / freshness / suppression is still ``BLOCKED`` when the
  provider prohibits (``provider_restricted``).
- **Reproducible, with the relevant denial, changing no state.** The decision is a pure function of its
  structured inputs at a given ``now`` — it reads nothing and writes nothing, so both the eligible
  result and every denial are perfectly reproducible with no state change.

This function is deliberately **not wired** into any send, acquisition, worker or HTTP path — nothing
calls it except its own tests and the manual-check scenario. Concluding ELIGIBLE enforces nothing and
sends nothing: the actual send stays a disabled future capability behind the host's one send pipeline,
which re-gates everything. Eligibility removes the DCA's blockers; it is NOT recipient consent and NOT
a send authorization.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

from app.consent.permission_state import PermissionProjection, PermissionState
from app.db.models import RuleChannel
from app.suppression.contract import (
    ActivationDecision,
    SuppressionSnapshot,
    authorize_activation,
)

# The one channel this evaluator speaks for. (A sibling channel — SMS/WhatsApp — would be its own
# story with its own provider rules; this story is email-only.)
EMAIL_CHANNEL = RuleChannel.EMAIL.value

# Consent-first: the single lawful basis this evaluator accepts for a marketing email. There is no
# generic B2B / legitimate-interest basis — anything else is rejected (``basis_not_consent``).
BASIS_CONSENT = "consent"

# The permission evidence must have been collected no longer ago than this to still be relied on. A
# marketing-consent basis older than this is treated as stale (a withdrawal recorded since might be in
# force); callers may tighten it per rulepack. Independent of the tighter suppression freshness window.
DEFAULT_MAX_EVIDENCE_AGE = timedelta(days=365)


class EmailEligibility(str, Enum):
    """The two outcomes of the composed email decision.

    - ``ELIGIBLE`` — every gate passed; the contact point MAY be offered to the one send pipeline
      (which still re-gates the actual send). This is not recipient consent and not a send authorization.
    - ``BLOCKED`` — at least one gate withheld permission. Fail-closed is the default: anything other
      than an explicit, in-force, fresh consent with source rights, a clear suppression view and no
      provider prohibition is ``BLOCKED``.
    """

    ELIGIBLE = "eligible"
    BLOCKED = "blocked"


# Stable, test-asserted reason codes — one per distinct gate that can withhold eligibility.
REASON_OK = "ok"  # ELIGIBLE: every gate passed
REASON_BASIS_NOT_CONSENT = "basis_not_consent"  # a non-consent basis was claimed (no B2B exception)
REASON_CONSENT_NOT_IN_FORCE = (
    "consent_not_in_force"  # no in-force consent (none/withdrawn/declined)
)
REASON_SOURCE_RIGHTS_INSUFFICIENT = (
    "source_rights_insufficient"  # source may not be used for contact
)
REASON_EVIDENCE_STALE = "evidence_stale"  # the consent basis is too old (or its age is unknown)
REASON_SUPPRESSED = "suppression_blocked"  # the authoritative suppression view did not clear it
REASON_PROVIDER_RESTRICTED = "provider_restricted"  # a sender-provider rule independently denies


@dataclass(frozen=True)
class PermissionBasis:
    """The lawful-basis / permission gate input.

    ``basis`` is the claimed lawful basis; only :data:`BASIS_CONSENT` is accepted (consent-first).
    ``permission_state`` is the DCA-075 :class:`PermissionState` value for the email+purpose scope — only
    ``GRANTED`` means consent is in force. ``scope_channel`` is recorded so a non-email projection cannot
    be mistaken for an email basis.
    """

    basis: str
    permission_state: str
    scope_channel: str = EMAIL_CHANNEL

    @property
    def is_consent(self) -> bool:
        return self.basis.strip().casefold() == BASIS_CONSENT

    @property
    def is_granted(self) -> bool:
        return (
            self.permission_state == PermissionState.GRANTED.value
            and self.scope_channel.strip().casefold() == EMAIL_CHANNEL
        )


@dataclass(frozen=True)
class SourceRights:
    """Whether the acquisition source's in-force rights permit USING this address for outbound contact.

    Resolved elsewhere (the DCA-045/046/047/048 rights chain — platform ceiling narrowed by the
    tenant's entitlement, authorized by a preflight). ``reason`` carries the upstream denial code for
    audit. The DCA never widens past these rights, so absent an explicit permit the gate blocks.
    """

    permits_email_contact: bool
    reason: str = ""


@dataclass(frozen=True)
class Freshness:
    """How fresh the permission evidence is, and the window within which it may still be relied on.

    ``age`` is the elapsed time since the consent was collected (``None`` when it is unknown — which is
    treated as stale, never fresh). ``max_age`` is the freshness window (defaults to
    :data:`DEFAULT_MAX_EVIDENCE_AGE`).
    """

    age: timedelta | None
    max_age: timedelta = DEFAULT_MAX_EVIDENCE_AGE

    @property
    def is_fresh(self) -> bool:
        return self.age is not None and self.age <= self.max_age


@dataclass(frozen=True)
class ProviderRule:
    """A sender / channel-provider acceptable-use restriction that can INDEPENDENTLY deny.

    Providers only ever narrow (``law > provider > product``): a provider prohibition blocks even when
    every other gate passes, but a provider allow can never widen past a withheld permission/right.
    ``reason`` carries the provider's own code (e.g. ``recipient_domain_blocklisted``) for audit.
    """

    allowed: bool
    reason: str = ""


@dataclass(frozen=True)
class EmailEligibilityRequest:
    """The structured inputs to the composed email decision — one value object per gate.

    ``email_verified`` / ``fit_band`` are RECORDED CONTEXT ONLY: they are carried on the decision for
    audit but never influence the allow outcome (the acceptance invariant that verification / fit alone
    never qualify an address). ``purpose`` and ``contact_point`` are provenance for the reported result.
    """

    contact_point: str
    purpose: str
    basis: PermissionBasis
    source_rights: SourceRights
    freshness: Freshness
    suppression: ActivationDecision
    provider: ProviderRule
    email_verified: bool = False
    fit_band: str = ""


@dataclass(frozen=True)
class EligibilityBlocker:
    """One gate that withheld eligibility — its gate name, a stable reason code, and any upstream detail."""

    gate: str
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class EmailEligibilityDecision:
    """The result of the composed email decision — reproducible and auditable, changing no state.

    ``blockers`` lists EVERY gate that withheld eligibility, in a fixed gate order, so a reviewer can
    see that (for example) the provider gate denied on its own. ``decisive`` is the first blocker (the
    headline denial). ``eligible`` is true only when there are no blockers at all.
    """

    decision: EmailEligibility
    contact_point: str
    purpose: str
    channel: str
    blockers: tuple[EligibilityBlocker, ...]
    email_verified: bool
    fit_band: str
    evaluated_at: str

    @property
    def eligible(self) -> bool:
        return self.decision is EmailEligibility.ELIGIBLE

    @property
    def reasons(self) -> tuple[str, ...]:
        """Stable reason codes: ``(ok,)`` when eligible, else one per blocker in gate order."""
        if self.eligible:
            return (REASON_OK,)
        return tuple(b.reason for b in self.blockers)

    @property
    def decisive(self) -> EligibilityBlocker | None:
        return self.blockers[0] if self.blockers else None

    @property
    def grants_consent(self) -> bool:
        """ELIGIBLE is never recipient consent, and never a send authorization. Kept explicit so no
        caller mistakes it for one — the host owns consent and the one send pipeline owns the send."""
        return False


def decide_email_eligibility(
    request: EmailEligibilityRequest, *, now: datetime | None = None
) -> EmailEligibilityDecision:
    """Combine the five gates into one email-marketing eligibility decision — pure, no I/O, no AI.

    Every gate is evaluated (not short-circuited) and each failure appends a blocker in a FIXED gate
    order, so the result shows exactly which gates withheld permission — including the provider gate,
    which can deny independently of the rest. ELIGIBLE requires ALL gates to pass. The function reads
    nothing and changes nothing, so a given ``(request, now)`` always yields the identical result.
    """
    moment = now or datetime.now(UTC)
    blockers: list[EligibilityBlocker] = []

    # 1) Lawful basis — consent-first. A non-consent basis (a generic "B2B" / legitimate-interest claim)
    #    is rejected outright: there is no such exception in this service.
    if not request.basis.is_consent:
        blockers.append(
            EligibilityBlocker("basis", REASON_BASIS_NOT_CONSENT, detail=request.basis.basis)
        )

    # 2) Permission in force — only an explicit, in-force consent for the email scope qualifies. This is
    #    where a verified / high-fit address with no consent evidence is blocked: fit/verification are
    #    recorded on the decision but never consulted here.
    if not request.basis.is_granted:
        blockers.append(
            EligibilityBlocker(
                "permission", REASON_CONSENT_NOT_IN_FORCE, detail=request.basis.permission_state
            )
        )

    # 3) Source rights — the acquisition source must permit using this address for outbound contact.
    if not request.source_rights.permits_email_contact:
        blockers.append(
            EligibilityBlocker(
                "source_rights",
                REASON_SOURCE_RIGHTS_INSUFFICIENT,
                detail=request.source_rights.reason,
            )
        )

    # 4) Freshness — a consent basis collected too long ago (or of unknown age) is stale and blocks.
    if not request.freshness.is_fresh:
        age = request.freshness.age
        detail = "unknown" if age is None else str(age)
        blockers.append(EligibilityBlocker("freshness", REASON_EVIDENCE_STALE, detail=detail))

    # 5) Suppression — the host's authoritative, fresh, in-scope view must clear the contact point.
    if not request.suppression.allowed:
        blockers.append(
            EligibilityBlocker("suppression", REASON_SUPPRESSED, detail=request.suppression.reason)
        )

    # 6) Sender-provider rules — an independent deny. Evaluated on its own so it can block even when
    #    every gate above passes (providers only narrow, never widen).
    if not request.provider.allowed:
        blockers.append(
            EligibilityBlocker(
                "provider", REASON_PROVIDER_RESTRICTED, detail=request.provider.reason
            )
        )

    decision = EmailEligibility.BLOCKED if blockers else EmailEligibility.ELIGIBLE
    return EmailEligibilityDecision(
        decision=decision,
        contact_point=request.contact_point,
        purpose=request.purpose,
        channel=EMAIL_CHANNEL,
        blockers=tuple(blockers),
        email_verified=request.email_verified,
        fit_band=request.fit_band,
        evaluated_at=moment.isoformat(),
    )


# -- convenience builders over the existing gate value objects --------------------------------------
# These adapt already-derived results from the earlier stories into this function's inputs. They add no
# I/O and consult no model; they only translate shapes, so the composition stays pure and reproducible.


def basis_from_projection(
    projection: PermissionProjection | None, *, basis: str = BASIS_CONSENT
) -> PermissionBasis:
    """Build the permission-basis gate input from a DCA-075 :class:`PermissionProjection` (or ``None``).

    A ``None`` projection (no receipts for the scope) maps to ``NONE`` — no permission — so a flag-only
    import that never produced an evidenced receipt is correctly treated as "no consent in force".
    """
    if projection is None:
        return PermissionBasis(basis=basis, permission_state=PermissionState.NONE.value)
    return PermissionBasis(
        basis=basis,
        permission_state=projection.state.value,
        scope_channel=projection.scope.channel,
    )


def freshness_from_projection(
    projection: PermissionProjection | None,
    *,
    now: datetime,
    max_age: timedelta = DEFAULT_MAX_EVIDENCE_AGE,
) -> Freshness:
    """Build the freshness gate input from when the in-force consent was collected.

    Uses the effective event's ``collected_at`` (the subject's action time). When there is no in-force
    event the age is unknown — :class:`Freshness` treats that as stale, so it can never be "fresh".
    """
    collected = projection.collected_at if projection is not None else None
    if collected is None:
        return Freshness(age=None, max_age=max_age)
    collected_utc = collected if collected.tzinfo is not None else collected.replace(tzinfo=UTC)
    now_utc = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    return Freshness(age=now_utc - collected_utc, max_age=max_age)


def evaluate_email_eligibility(
    *,
    contact_point: str,
    purpose: str,
    permission: PermissionProjection | None,
    source_rights: SourceRights,
    suppression_snapshot: SuppressionSnapshot | None,
    provider: ProviderRule,
    now: datetime | None = None,
    max_evidence_age: timedelta = DEFAULT_MAX_EVIDENCE_AGE,
    email_verified: bool = False,
    fit_band: str = "",
) -> EmailEligibilityDecision:
    """Assemble the gate inputs from the existing building blocks, then :func:`decide_email_eligibility`.

    A convenience that derives the permission/freshness gates from a DCA-075 projection and the
    suppression gate from a DCA-079 snapshot (via :func:`authorize_activation`), leaving source-rights
    and provider as already-resolved inputs. Still pure: it opens no DB, calls no adapter and consults no
    model — it only composes value objects the earlier stories produced.
    """
    moment = now or datetime.now(UTC)
    suppression = authorize_activation(
        suppression_snapshot, contact_point, EMAIL_CHANNEL, now=moment
    )
    request = EmailEligibilityRequest(
        contact_point=contact_point,
        purpose=purpose,
        basis=basis_from_projection(permission),
        source_rights=source_rights,
        freshness=freshness_from_projection(permission, now=moment, max_age=max_evidence_age),
        suppression=suppression,
        provider=provider,
        email_verified=email_verified,
        fit_band=fit_band,
    )
    return decide_email_eligibility(request, now=moment)


def reason_codes(decisions: Iterable[EmailEligibilityDecision]) -> list[tuple[str, ...]]:
    """Small helper for scenarios/tests: the reason tuples of a sequence of decisions, in order."""
    return [d.reasons for d in decisions]
