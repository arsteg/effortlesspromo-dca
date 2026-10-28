"""DCA-090 — evaluate WhatsApp eligibility (the one composed WhatsApp-marketing decision function).

Key: ``whatsapp-policy`` · source sections 5, 10. Stage: India channel eligibility.

A pure, deterministic Python evaluator that answers ONE question for the WhatsApp channel — *may a
MARKETING (promotional) WhatsApp message to this recipient proceed to the one send pipeline?* — by
combining TWO deliberately distinct concern-families that WhatsApp's Business policy keeps separate:

**A) marketing permission (the AUTHORITY to send promotional content at all).** Consent-first:

1. **basis** — the only accepted lawful/product basis is explicit ``consent``. There is deliberately
   **no generic B2B / "legitimate interest" exception** — any other basis is rejected.
2. **opt-in in force** — an explicit, in-force marketing opt-in on the WhatsApp scope (the DCA-074
   receipt / DCA-075 projection ``GRANTED`` state). Anything else (none / withdrawn / declined) blocks.
3. **contact origin** — the number must have come from a qualifying OPT-IN origin. A **public phone**
   (scraped / listed) and an **inbound service inquiry** (the user messaged in for support) are
   explicitly NON-authorizing: neither, alone, is opt-in to future promotional messages.

**B) message-delivery constraints (the FORMAT/WINDOW mechanics — SEPARATE from permission).** A
promotional WhatsApp message is deliverable only via either:

4. an **approved marketing template** (a pre-approved Message Template of the ``marketing`` category,
   in the ``APPROVED`` state), OR the recipient's open **24-hour service window** (opened by their last
   inbound message, within which a free-form session message may ride).

Load-bearing guarantees (the DCA-090 acceptance criteria):

- **A public phone or inbound service inquiry alone cannot authorize future promotional messages.** The
  contact-origin gate rejects both origins, and the opt-in gate independently requires an in-force
  marketing consent — so a number known only from a public listing or a support DM stays ``BLOCKED`` for
  marketing. Crucially, an inbound service inquiry **opens the service window** (satisfying the *delivery*
  constraint), yet the message is still ``BLOCKED`` on the marketing-permission family: the
  approved-template / service-window constraints are kept SEPARATE from marketing permission and can
  never substitute for it (``window open`` ≠ ``opted in``).
- **Reproducible, with the relevant denial, changing no state.** The decision is a pure function of its
  structured inputs at a given ``now`` — it reads nothing and writes nothing, so both the eligible result
  and every denial are perfectly reproducible with no state change.

This function is deliberately **not wired** into any send, acquisition, worker or HTTP path — nothing
calls it except its own tests and the manual-check scenario. Concluding ELIGIBLE enforces nothing and
sends nothing: the actual WhatsApp send stays a disabled future capability behind the host's one send
pipeline, which re-gates everything (recipient consent, suppression, source rights, the live BSP). This
is NOT recipient consent and NOT a send authorization.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

from app.consent.permission_state import PermissionProjection, PermissionState
from app.db.models import RuleChannel

# The one channel this evaluator speaks for. (A sibling channel — email/SMS — is its own story with its
# own gates; this story is WhatsApp-only.)
WHATSAPP_CHANNEL = RuleChannel.WHATSAPP.value

# Consent-first: the single lawful/product basis this evaluator accepts for a marketing WhatsApp
# message. There is no generic B2B / legitimate-interest basis — anything else is rejected.
BASIS_CONSENT = "consent"

# The default WhatsApp customer-care ("service") window: 24 hours from the recipient's last inbound
# message. Within it a free-form session message may ride; outside it, only an approved template.
DEFAULT_SERVICE_WINDOW = timedelta(hours=24)


class WhatsAppContactOrigin(str, Enum):
    """How this WhatsApp number entered the system — the contact-origin gate's input.

    Only :attr:`EXPLICIT_OPT_IN` is a qualifying marketing opt-in origin. The two deliberately
    NON-authorizing origins are the acceptance invariant of this story:

    - ``PUBLIC_PHONE`` — a scraped / publicly listed number. Being reachable is not permission.
    - ``INBOUND_SERVICE_INQUIRY`` — the user messaged in for support. That opens the service window
      (a *delivery* mechanic) but is NOT opt-in to future promotional messages.
    - ``UNKNOWN`` — unrecorded origin: fail-closed (treated as non-opt-in).
    """

    EXPLICIT_OPT_IN = "explicit_opt_in"
    PUBLIC_PHONE = "public_phone"
    INBOUND_SERVICE_INQUIRY = "inbound_service_inquiry"
    UNKNOWN = "unknown"


# The origins that count as a qualifying marketing opt-in. Everything else — including a public phone
# and an inbound service inquiry — is non-authorizing and fails the contact-origin gate.
OPT_IN_ORIGINS = frozenset({WhatsAppContactOrigin.EXPLICIT_OPT_IN.value})


def origin_is_opt_in(origin: str) -> bool:
    """Whether a contact origin qualifies as a marketing opt-in (only an explicit opt-in does)."""
    return origin.strip().casefold() in OPT_IN_ORIGINS


class TemplateStatus(str, Enum):
    """Lifecycle state of a WhatsApp Message Template. Only :attr:`APPROVED` is usable; everything
    else — never submitted, pending review, rejected, paused for quality, or disabled — fails closed."""

    NONE = "none"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    PAUSED = "paused"
    DISABLED = "disabled"


class WhatsAppCategory(str, Enum):
    """The WhatsApp template / message category. This evaluator decides a ``MARKETING`` (promotional)
    message; a template must match that category to carry it."""

    MARKETING = "marketing"
    UTILITY = "utility"
    AUTHENTICATION = "authentication"
    SERVICE = "service"


class WhatsAppEligibility(str, Enum):
    """The two outcomes of the composed WhatsApp decision.

    - ``ELIGIBLE`` — both families passed: marketing permission is in force AND a delivery mechanism
      (approved template or open service window) exists. The recipient MAY be offered to the one send
      pipeline (which still re-gates the actual send). Not recipient consent, not a send authorization.
    - ``BLOCKED`` — at least one gate withheld. Fail-closed is the default.
    """

    ELIGIBLE = "eligible"
    BLOCKED = "blocked"


# The two gate families, carried on every blocker so the separation is explicit and testable: marketing
# PERMISSION (the authority) versus the message-delivery CONSTRAINT (format/window mechanics).
FAMILY_MARKETING_PERMISSION = "marketing_permission"
FAMILY_MESSAGE_CONSTRAINT = "message_constraint"

# Stable, test-asserted reason codes — one per distinct gate that can withhold eligibility.
REASON_OK = "ok"  # ELIGIBLE: every gate passed
REASON_BASIS_NOT_CONSENT = "basis_not_consent"  # a non-consent basis was claimed (no B2B exception)
REASON_OPT_IN_NOT_IN_FORCE = (
    "opt_in_not_in_force"  # no in-force WhatsApp marketing opt-in (none/withdrawn/declined)
)
REASON_CONTACT_ORIGIN_NOT_OPT_IN = (
    "contact_origin_not_opt_in"  # public phone / inbound inquiry / unknown — not an opt-in origin
)
REASON_APPROVED_TEMPLATE_REQUIRED = (
    "approved_template_required"  # no approved marketing template AND the service window is closed
)

# The delivery path a satisfied message-constraint gate took (empty when the constraint is unmet).
DELIVERY_APPROVED_TEMPLATE = "approved_template"
DELIVERY_SERVICE_WINDOW = "service_window"


@dataclass(frozen=True)
class MarketingOptIn:
    """The marketing-permission family input: basis + in-force opt-in + contact origin.

    ``basis`` is the claimed lawful/product basis (only :data:`BASIS_CONSENT` accepted).
    ``permission_state`` is the DCA-075 :class:`PermissionState` value for the WhatsApp+purpose scope —
    only ``GRANTED`` means an opt-in is in force. ``contact_origin`` is the :class:`WhatsAppContactOrigin`
    value. ``scope_channel`` is recorded so a non-WhatsApp projection cannot be mistaken for one.
    """

    basis: str
    permission_state: str
    contact_origin: str
    scope_channel: str = WHATSAPP_CHANNEL

    @property
    def is_consent(self) -> bool:
        return self.basis.strip().casefold() == BASIS_CONSENT

    @property
    def is_granted(self) -> bool:
        return (
            self.permission_state == PermissionState.GRANTED.value
            and self.scope_channel.strip().casefold() == WHATSAPP_CHANNEL
        )

    @property
    def origin_qualifies(self) -> bool:
        return origin_is_opt_in(self.contact_origin)


@dataclass(frozen=True)
class ApprovedTemplate:
    """A WhatsApp Message Template — the template delivery path for a promotional message.

    Usable for a marketing send only when it is ``APPROVED`` and its category matches ``marketing``. A
    missing / pending / rejected / paused / disabled template, or one of the wrong category, cannot carry
    promotional content.
    """

    template_id: str = ""
    category: str = WhatsAppCategory.MARKETING.value
    status: str = TemplateStatus.NONE.value
    language: str = ""

    def is_usable_for(self, category: str) -> bool:
        return (
            bool(self.template_id)
            and self.status.strip().casefold() == TemplateStatus.APPROVED.value
            and self.category.strip().casefold() == category.strip().casefold()
        )


@dataclass(frozen=True)
class ServiceWindow:
    """The 24-hour WhatsApp customer-care window, opened by the recipient's last inbound message.

    ``last_inbound_at`` is when the recipient last messaged in (``None`` ⇒ never ⇒ window closed).
    ``duration`` is the window length (default :data:`DEFAULT_SERVICE_WINDOW`). The window is a *delivery*
    mechanic ONLY — an open window never substitutes for marketing permission (the DCA-090 separation).
    """

    last_inbound_at: datetime | None = None
    duration: timedelta = DEFAULT_SERVICE_WINDOW

    def is_open(self, now: datetime) -> bool:
        if self.last_inbound_at is None:
            return False
        last = (
            self.last_inbound_at
            if self.last_inbound_at.tzinfo is not None
            else self.last_inbound_at.replace(tzinfo=UTC)
        )
        moment = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        elapsed = moment - last
        return timedelta(0) <= elapsed <= self.duration


@dataclass(frozen=True)
class WhatsAppEligibilityRequest:
    """The structured inputs to the composed WhatsApp decision — one value object per family.

    ``opt_in`` is the marketing-permission family; ``template`` + ``service_window`` are the separate
    message-delivery-constraint family. ``message_category`` is the category being sent (this story
    decides ``marketing``). ``contact_point`` and ``purpose`` are provenance for the reported result.
    """

    contact_point: str
    purpose: str
    opt_in: MarketingOptIn
    template: ApprovedTemplate
    service_window: ServiceWindow
    message_category: str = WhatsAppCategory.MARKETING.value


@dataclass(frozen=True)
class EligibilityBlocker:
    """One gate that withheld eligibility — its family, gate name, a stable reason code, and detail."""

    family: str
    gate: str
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class WhatsAppEligibilityDecision:
    """The result of the composed WhatsApp decision — reproducible and auditable, changing no state.

    ``blockers`` lists EVERY gate that withheld eligibility, in a fixed order (permission family first,
    then the message-constraint family), each tagged with its :attr:`EligibilityBlocker.family` so a
    reviewer can see that (for example) the message could be *delivered* via an open window yet was
    blocked on marketing permission. ``decisive`` is the first blocker. ``eligible`` is true only when
    there are no blockers at all. ``delivery_path`` records which delivery mechanism cleared the
    message-constraint family (empty when it was unmet). The decision is ALWAYS WhatsApp-scoped.
    """

    decision: WhatsAppEligibility
    contact_point: str
    purpose: str
    channel: str
    message_category: str
    contact_origin: str
    delivery_path: str
    blockers: tuple[EligibilityBlocker, ...]
    evaluated_at: str

    @property
    def eligible(self) -> bool:
        return self.decision is WhatsAppEligibility.ELIGIBLE

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
    def marketing_permission_blockers(self) -> tuple[EligibilityBlocker, ...]:
        """The withheld gates in the marketing-PERMISSION family (basis / opt-in / contact-origin)."""
        return tuple(b for b in self.blockers if b.family == FAMILY_MARKETING_PERMISSION)

    @property
    def message_constraint_blockers(self) -> tuple[EligibilityBlocker, ...]:
        """The withheld gates in the message-delivery-CONSTRAINT family (template / service window)."""
        return tuple(b for b in self.blockers if b.family == FAMILY_MESSAGE_CONSTRAINT)

    @property
    def marketing_permission_granted(self) -> bool:
        """Whether the marketing-permission family fully passed (opt-in + qualifying origin + consent
        basis). This is the AUTHORITY to send promotional content; it is independent of whether a
        delivery mechanism exists, so an open service window never flips it to ``True``."""
        return not self.marketing_permission_blockers

    @property
    def message_delivery_available(self) -> bool:
        """Whether a delivery mechanism exists (an approved marketing template or an open service
        window). SEPARATE from permission: this can be ``True`` (e.g. an inbound inquiry opened the
        window) while :attr:`marketing_permission_granted` is ``False``."""
        return not self.message_constraint_blockers

    @property
    def grants_consent(self) -> bool:
        """ELIGIBLE is never recipient consent, and never a send authorization. Kept explicit so no
        caller mistakes it for one — the host owns consent and the one send pipeline owns the send."""
        return False

    def permits_channel(self, channel: str) -> bool:
        """This decision authorises nothing beyond the WhatsApp channel it evaluated. Only ``True`` when
        ELIGIBLE AND ``channel`` is WhatsApp — always ``False`` for email / SMS."""
        return self.eligible and channel.strip().casefold() == self.channel.strip().casefold()


def decide_whatsapp_eligibility(
    request: WhatsAppEligibilityRequest, *, now: datetime | None = None
) -> WhatsAppEligibilityDecision:
    """Combine the marketing-permission family and the message-delivery-constraint family into one
    WhatsApp decision — pure, no I/O, no AI.

    Every gate is evaluated (not short-circuited) and each failure appends a blocker in a FIXED order,
    permission family first. ELIGIBLE requires ALL gates to pass. The two families are independent: the
    message-delivery constraint can be satisfied (e.g. an open service window) while marketing permission
    is withheld — satisfying delivery NEVER grants permission. The function reads nothing and changes
    nothing, so a given ``(request, now)`` always yields the identical result.
    """
    moment = now or datetime.now(UTC)
    category = request.message_category
    blockers: list[EligibilityBlocker] = []

    # -- A) marketing PERMISSION family (the authority to send promotional content) ------------------

    # 1) Basis — consent-first. A non-consent basis (a generic "B2B" / legitimate-interest claim) is
    #    rejected outright: there is no such exception in this service.
    if not request.opt_in.is_consent:
        blockers.append(
            EligibilityBlocker(
                FAMILY_MARKETING_PERMISSION,
                "basis",
                REASON_BASIS_NOT_CONSENT,
                detail=request.opt_in.basis,
            )
        )

    # 2) Opt-in in force — only an explicit, in-force marketing opt-in on the WhatsApp scope qualifies.
    if not request.opt_in.is_granted:
        blockers.append(
            EligibilityBlocker(
                FAMILY_MARKETING_PERMISSION,
                "opt_in",
                REASON_OPT_IN_NOT_IN_FORCE,
                detail=request.opt_in.permission_state,
            )
        )

    # 3) Contact origin — a public phone or an inbound service inquiry (or an unknown origin) is NOT an
    #    opt-in origin. This is the DCA-090 invariant: being reachable, or having messaged in for
    #    support, never authorizes future promotional messages.
    if not request.opt_in.origin_qualifies:
        blockers.append(
            EligibilityBlocker(
                FAMILY_MARKETING_PERMISSION,
                "contact_origin",
                REASON_CONTACT_ORIGIN_NOT_OPT_IN,
                detail=request.opt_in.contact_origin,
            )
        )

    # -- B) message-delivery CONSTRAINT family (format/window — SEPARATE from permission) ------------

    # 4) Delivery mechanism — a promotional message needs EITHER an approved marketing template OR an
    #    open 24h service window. This is a delivery mechanic; an open window here does NOT (and must
    #    not) substitute for the marketing permission above.
    delivery_path = ""
    if request.template.is_usable_for(category):
        delivery_path = DELIVERY_APPROVED_TEMPLATE
    elif request.service_window.is_open(moment):
        delivery_path = DELIVERY_SERVICE_WINDOW
    else:
        blockers.append(
            EligibilityBlocker(
                FAMILY_MESSAGE_CONSTRAINT,
                "message_constraint",
                REASON_APPROVED_TEMPLATE_REQUIRED,
                detail=f"template={request.template.status}; service_window=closed",
            )
        )

    decision = WhatsAppEligibility.BLOCKED if blockers else WhatsAppEligibility.ELIGIBLE
    return WhatsAppEligibilityDecision(
        decision=decision,
        contact_point=request.contact_point,
        purpose=request.purpose,
        channel=WHATSAPP_CHANNEL,
        message_category=category,
        contact_origin=request.opt_in.contact_origin,
        delivery_path=delivery_path,
        blockers=tuple(blockers),
        evaluated_at=moment.isoformat(),
    )


# -- convenience builders over the existing gate value objects --------------------------------------
# These adapt already-derived results from the earlier stories into this function's inputs. They add no
# I/O and consult no model; they only translate shapes, so the composition stays pure and reproducible.


def opt_in_from_projection(
    projection: PermissionProjection | None,
    contact_origin: str,
    *,
    basis: str = BASIS_CONSENT,
) -> MarketingOptIn:
    """Build the marketing-opt-in gate input from a DCA-075 :class:`PermissionProjection` (or ``None``).

    A ``None`` projection (no receipts for the scope) maps to ``NONE`` — no permission — so a flag-only
    import that never produced an evidenced receipt is correctly treated as "no opt-in in force". The
    ``contact_origin`` is carried through unchanged and gated on its own.
    """
    if projection is None:
        return MarketingOptIn(
            basis=basis,
            permission_state=PermissionState.NONE.value,
            contact_origin=contact_origin,
        )
    return MarketingOptIn(
        basis=basis,
        permission_state=projection.state.value,
        contact_origin=contact_origin,
        scope_channel=projection.scope.channel,
    )


def evaluate_whatsapp_eligibility(
    *,
    contact_point: str,
    purpose: str,
    permission: PermissionProjection | None,
    contact_origin: str,
    template: ApprovedTemplate | None = None,
    service_window: ServiceWindow | None = None,
    message_category: str = WhatsAppCategory.MARKETING.value,
    now: datetime | None = None,
) -> WhatsAppEligibilityDecision:
    """Assemble the gate inputs from the existing building blocks, then
    :func:`decide_whatsapp_eligibility`.

    A convenience that derives the opt-in gate from a DCA-075 projection + the recorded contact origin,
    leaving the approved template and service window as already-resolved facts (an absent template /
    window defaults to a fail-closed empty one). Still pure: it opens no DB, calls no adapter and
    consults no model — it only composes value objects the earlier stories produced.
    """
    request = WhatsAppEligibilityRequest(
        contact_point=contact_point,
        purpose=purpose,
        opt_in=opt_in_from_projection(permission, contact_origin),
        template=template or ApprovedTemplate(),
        service_window=service_window or ServiceWindow(),
        message_category=message_category,
    )
    return decide_whatsapp_eligibility(request, now=now)


def reason_codes(decisions: Iterable[WhatsAppEligibilityDecision]) -> list[tuple[str, ...]]:
    """Small helper for scenarios/tests: the reason tuples of a sequence of decisions, in order."""
    return [d.reasons for d in decisions]
