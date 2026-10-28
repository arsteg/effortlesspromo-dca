"""DCA-088 — validate Indian SMS sender prerequisites (the one composed sender-readiness function).

Key: ``sms-setup`` · source sections 5, 10. Stage: India channel eligibility.

A pure, deterministic Python evaluator that answers ONE question for the Indian SMS channel — *has
the SENDER completed the DLT (TCCCPR) registration prerequisites so a marketing SMS on this route MAY
be offered to the host's one send pipeline?* — by combining the DLT sender-registration facts India's
regime requires before any commercial SMS can be routed:

1. **principal entity** — the Principal Entity (PE) must be registered on a DLT portal and ACTIVE (an
   entity id that is pending / suspended / expired / blacklisted is not usable).
2. **header** — the sender id / header must be registered, ACTIVE, and BOUND to that principal entity.
   A header not tied to the PE is not this sender's header.
3. **content template** — a DLT-registered content template must be registered, ACTIVE, and bound to
   the header. Unregistered content cannot be routed.
4. **consent template** — a consent-template registration is additionally required on the consent-based
   routes (promotional / service-explicit); on transactional / service-implicit it is not applicable.
5. **route / category** — the header's category, the content template's category (and the consent
   template's, when present) must MATCH the requested route. A promotional header cannot carry a
   transactional route, etc.; a mismatch denies the route.
6. **delivery chain** — the registered-telemarketer / aggregator delivery chain must be ACTIVE and
   bound to the principal entity (the operator binding that actually carries the traffic).

Load-bearing guarantees (the DCA-088 acceptance criteria):

- **Missing/mismatched setup denies the route.** Any absent, not-ACTIVE, unbound or category-mismatched
  prerequisite yields ``BLOCKED`` with the gate's stable reason code; ``READY`` requires ALL prerequisites.
- **Setup alone cannot authorize a recipient.** This is SENDER readiness, NOT recipient permission —
  :attr:`SmsSenderReadinessDecision.authorizes_recipient` and ``grants_consent`` are always ``False``.
  Recipient consent is a SEPARATE gate (the DCA-087 recipient-eligibility line): a DLT-ready sender is
  still not permitted to message a given recipient, and a recipient's consent does not make an
  unregistered sender routable. To make that orthogonality explicit, ``recipient_consent_present`` is
  carried for audit only and is NEVER consulted by this decision (as ``email_verified`` / ``fit_band``
  are on the email side).
- **Reproducible, with the relevant denial, changing no state.** The decision is a pure function of its
  structured inputs at a given ``now`` — it reads nothing and writes nothing, so both the ready result
  and every denial are perfectly reproducible with no state change.

This function is deliberately **not wired** into any send, acquisition, worker or HTTP path — nothing
calls it except its own tests and the manual-check scenario. Concluding READY enforces nothing and
sends nothing: the actual SMS send stays a disabled future capability behind the host's one send
pipeline, which re-gates everything (including recipient consent and suppression). Sender readiness
only confirms the DLT prerequisites exist; it is NOT recipient consent and NOT a send authorization.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from app.db.models import RuleChannel

# The one channel this evaluator speaks for. (A sibling channel — email/WhatsApp — is its own story
# with its own sender prerequisites; this story is Indian SMS / DLT only.)
SMS_CHANNEL = RuleChannel.SMS.value


class DltRegistrationStatus(str, Enum):
    """Lifecycle state of a DLT sender-registration artifact (entity / header / template / chain).

    Only :attr:`ACTIVE` (and not past its expiry) is usable; every other state — never registered,
    still pending approval, suspended, lapsed, or blacklisted — is fail-closed and cannot route.
    """

    NONE = "none"  # never registered
    PENDING = "pending"  # submitted, not yet approved
    ACTIVE = "active"  # registered and approved — the only usable state
    SUSPENDED = "suspended"  # temporarily withdrawn
    EXPIRED = "expired"  # registration lapsed
    BLACKLISTED = "blacklisted"  # barred by the regulator / operator


class SmsRoute(str, Enum):
    """The DLT route / category a commercial SMS is sent on.

    The consent-based routes (:attr:`PROMOTIONAL`, :attr:`SERVICE_EXPLICIT`) additionally require a
    registered consent template; :attr:`TRANSACTIONAL` and :attr:`SERVICE_IMPLICIT` do not.
    """

    PROMOTIONAL = "promotional"
    TRANSACTIONAL = "transactional"
    SERVICE_IMPLICIT = "service_implicit"
    SERVICE_EXPLICIT = "service_explicit"


# The routes on which a DLT consent-template registration is an additional prerequisite.
_CONSENT_ROUTES = frozenset({SmsRoute.PROMOTIONAL.value, SmsRoute.SERVICE_EXPLICIT.value})


def requires_consent_template(route: str) -> bool:
    """Whether the given route additionally requires a registered consent template (promo / svc-explicit)."""
    return route.strip().casefold() in _CONSENT_ROUTES


class SmsSenderReadiness(str, Enum):
    """The two outcomes of the composed sender-readiness decision.

    - ``READY`` — every DLT prerequisite is present, ACTIVE, bound and category-matched; the route MAY
      be offered to the one send pipeline (which still re-gates recipient consent, suppression, etc.).
      This is not recipient consent and not a send authorization.
    - ``BLOCKED`` — at least one prerequisite is missing, not ACTIVE, unbound or mismatched. Fail-closed.
    """

    READY = "ready"
    BLOCKED = "blocked"


# Stable, test-asserted reason codes — one per distinct prerequisite that can deny the route.
REASON_OK = "ok"  # READY: every prerequisite satisfied
REASON_PRINCIPAL_ENTITY_UNREGISTERED = "principal_entity_unregistered"  # PE not registered/ACTIVE
REASON_HEADER_NOT_REGISTERED = "header_not_registered"  # header missing/not ACTIVE/unbound
REASON_CONTENT_TEMPLATE_NOT_REGISTERED = (
    "content_template_not_registered"  # content template missing
)
REASON_CONSENT_TEMPLATE_NOT_REGISTERED = (
    "consent_template_not_registered"  # consent template missing
)
REASON_ROUTE_CATEGORY_MISMATCH = "route_category_mismatch"  # header/template category != route
REASON_DELIVERY_CHAIN_INCOMPLETE = (
    "delivery_chain_incomplete"  # TM/aggregator chain missing/unbound
)


def _is_active(status: str, expires_at: datetime | None, now: datetime) -> bool:
    """A registration artifact is usable iff ACTIVE and not past its expiry (fail-closed otherwise)."""
    if status.strip().casefold() != DltRegistrationStatus.ACTIVE.value:
        return False
    if expires_at is None:
        return True
    moment = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    expiry = expires_at if expires_at.tzinfo is not None else expires_at.replace(tzinfo=UTC)
    return expiry > moment


@dataclass(frozen=True)
class PrincipalEntity:
    """The DLT Principal Entity (PE) registration — the sender's root identity on the DLT portal."""

    entity_id: str
    status: str = DltRegistrationStatus.NONE.value
    expires_at: datetime | None = None

    def is_usable(self, now: datetime) -> bool:
        return bool(self.entity_id) and _is_active(self.status, self.expires_at, now)


@dataclass(frozen=True)
class Header:
    """A registered sender id / header, its category, and the principal entity it is bound to."""

    header_id: str
    category: str
    status: str = DltRegistrationStatus.NONE.value
    bound_entity_id: str = ""
    expires_at: datetime | None = None

    def is_usable(self, now: datetime) -> bool:
        return bool(self.header_id) and _is_active(self.status, self.expires_at, now)


@dataclass(frozen=True)
class ContentTemplate:
    """A registered DLT content template, its category, and the header it is bound to."""

    template_id: str
    category: str
    status: str = DltRegistrationStatus.NONE.value
    bound_header_id: str = ""
    expires_at: datetime | None = None

    def is_usable(self, now: datetime) -> bool:
        return bool(self.template_id) and _is_active(self.status, self.expires_at, now)


@dataclass(frozen=True)
class ConsentTemplate:
    """A registered DLT consent template (required only on the consent-based routes)."""

    template_id: str
    category: str
    status: str = DltRegistrationStatus.NONE.value
    expires_at: datetime | None = None

    def is_usable(self, now: datetime) -> bool:
        return bool(self.template_id) and _is_active(self.status, self.expires_at, now)


@dataclass(frozen=True)
class DeliveryChain:
    """The registered-telemarketer / aggregator delivery chain that actually carries the traffic.

    Must be ACTIVE and bound to the principal entity (the operator binding); absent it, no route exists.
    """

    telemarketer_id: str
    status: str = DltRegistrationStatus.NONE.value
    bound_entity_id: str = ""
    expires_at: datetime | None = None

    def is_usable(self, now: datetime) -> bool:
        return bool(self.telemarketer_id) and _is_active(self.status, self.expires_at, now)


@dataclass(frozen=True)
class SmsSenderReadinessRequest:
    """The structured inputs to the composed sender-readiness decision — one value object per prerequisite.

    ``recipient_consent_present`` is RECORDED CONTEXT ONLY: it is carried on the decision for audit but
    never influences the readiness outcome (the acceptance invariant that sender setup and recipient
    permission are orthogonal — setup alone cannot authorize a recipient, and recipient consent cannot
    make an unregistered sender routable).
    """

    route: str
    principal_entity: PrincipalEntity
    header: Header
    content_template: ContentTemplate
    delivery_chain: DeliveryChain
    consent_template: ConsentTemplate | None = None
    recipient_consent_present: bool = False


@dataclass(frozen=True)
class PrerequisiteBlocker:
    """One prerequisite that denied the route — its gate name, a stable reason code, and any detail."""

    gate: str
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class SmsSenderReadinessDecision:
    """The result of the composed sender-readiness decision — reproducible and auditable, changing no state.

    ``blockers`` lists EVERY prerequisite that denied the route, in a fixed gate order, so a reviewer can
    see exactly which DLT setup is missing or mismatched. ``decisive`` is the first blocker (the headline
    denial). ``ready`` is true only when there are no blockers at all.
    """

    decision: SmsSenderReadiness
    route: str
    entity_id: str
    header_id: str
    channel: str
    blockers: tuple[PrerequisiteBlocker, ...]
    recipient_consent_present: bool
    evaluated_at: str

    @property
    def ready(self) -> bool:
        return self.decision is SmsSenderReadiness.READY

    @property
    def reasons(self) -> tuple[str, ...]:
        """Stable reason codes: ``(ok,)`` when ready, else one per blocker in gate order."""
        if self.ready:
            return (REASON_OK,)
        return tuple(b.reason for b in self.blockers)

    @property
    def decisive(self) -> PrerequisiteBlocker | None:
        return self.blockers[0] if self.blockers else None

    @property
    def authorizes_recipient(self) -> bool:
        """Sender readiness is NEVER recipient permission. Kept explicit so no caller mistakes a READY
        sender for being allowed to message a given recipient — that is a separate recipient-consent
        gate owned by the host, re-applied by the one send pipeline."""
        return False

    @property
    def grants_consent(self) -> bool:
        """READY is never recipient consent, and never a send authorization."""
        return False


def decide_sms_sender_readiness(
    request: SmsSenderReadinessRequest, *, now: datetime | None = None
) -> SmsSenderReadinessDecision:
    """Combine the DLT sender prerequisites into one SMS sender-readiness decision — pure, no I/O, no AI.

    Every prerequisite is evaluated (not short-circuited) and each failure appends a blocker in a FIXED
    gate order, so the result shows exactly which DLT setup is missing or mismatched. READY requires ALL
    prerequisites. The function reads nothing and changes nothing, so a given ``(request, now)`` always
    yields the identical result. READY confirms the sender is registered — it does NOT authorize any
    recipient (``recipient_consent_present`` is recorded for audit but never consulted).
    """
    moment = now or datetime.now(UTC)
    route = request.route.strip().casefold()
    entity_id = request.principal_entity.entity_id
    blockers: list[PrerequisiteBlocker] = []

    # 1) Principal entity — the PE must be registered and ACTIVE on a DLT portal.
    if not request.principal_entity.is_usable(moment):
        blockers.append(
            PrerequisiteBlocker(
                "principal_entity",
                REASON_PRINCIPAL_ENTITY_UNREGISTERED,
                detail=request.principal_entity.status,
            )
        )

    # 2) Header — registered, ACTIVE, and bound to THIS principal entity.
    header_bound = bool(entity_id) and request.header.bound_entity_id == entity_id
    if not (request.header.is_usable(moment) and header_bound):
        detail = (
            "unbound_entity"
            if request.header.is_usable(moment) and not header_bound
            else request.header.status
        )
        blockers.append(PrerequisiteBlocker("header", REASON_HEADER_NOT_REGISTERED, detail=detail))

    # 3) Content template — registered, ACTIVE, and bound to the header.
    content_bound = (
        bool(request.header.header_id)
        and request.content_template.bound_header_id == request.header.header_id
    )
    if not (request.content_template.is_usable(moment) and content_bound):
        detail = (
            "unbound_header"
            if request.content_template.is_usable(moment) and not content_bound
            else request.content_template.status
        )
        blockers.append(
            PrerequisiteBlocker(
                "content_template", REASON_CONTENT_TEMPLATE_NOT_REGISTERED, detail=detail
            )
        )

    # 4) Consent template — an additional prerequisite ONLY on the consent-based routes; not applicable
    #    on transactional / service-implicit (so its absence there is not a blocker).
    if requires_consent_template(route):
        consent = request.consent_template
        if consent is None or not consent.is_usable(moment):
            detail = "missing" if consent is None else consent.status
            blockers.append(
                PrerequisiteBlocker(
                    "consent_template", REASON_CONSENT_TEMPLATE_NOT_REGISTERED, detail=detail
                )
            )

    # 5) Route / category match — the header's, content template's (and consent template's, when
    #    present) categories must all equal the requested route. Evaluated independently of usability so
    #    a correctly-registered-but-wrong-category artifact is still caught.
    mismatches: list[str] = []
    if request.header.category.strip().casefold() != route:
        mismatches.append(f"header={request.header.category}")
    if request.content_template.category.strip().casefold() != route:
        mismatches.append(f"content={request.content_template.category}")
    if (
        request.consent_template is not None
        and request.consent_template.category.strip().casefold() != route
    ):
        mismatches.append(f"consent={request.consent_template.category}")
    if mismatches:
        blockers.append(
            PrerequisiteBlocker(
                "route_category",
                REASON_ROUTE_CATEGORY_MISMATCH,
                detail=f"route={request.route}; " + ", ".join(mismatches),
            )
        )

    # 6) Delivery chain — the registered-telemarketer / aggregator chain must be ACTIVE and bound to the PE.
    chain_bound = bool(entity_id) and request.delivery_chain.bound_entity_id == entity_id
    if not (request.delivery_chain.is_usable(moment) and chain_bound):
        detail = (
            "unbound_entity"
            if request.delivery_chain.is_usable(moment) and not chain_bound
            else request.delivery_chain.status
        )
        blockers.append(
            PrerequisiteBlocker("delivery_chain", REASON_DELIVERY_CHAIN_INCOMPLETE, detail=detail)
        )

    decision = SmsSenderReadiness.BLOCKED if blockers else SmsSenderReadiness.READY
    return SmsSenderReadinessDecision(
        decision=decision,
        route=request.route,
        entity_id=entity_id,
        header_id=request.header.header_id,
        channel=SMS_CHANNEL,
        blockers=tuple(blockers),
        recipient_consent_present=request.recipient_consent_present,
        evaluated_at=moment.isoformat(),
    )


def reason_codes(decisions: Iterable[SmsSenderReadinessDecision]) -> list[tuple[str, ...]]:
    """Small helper for scenarios/tests: the reason tuples of a sequence of decisions, in order."""
    return [d.reasons for d in decisions]
