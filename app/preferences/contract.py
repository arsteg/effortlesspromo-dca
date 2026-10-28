"""DCA-089 — the provider-backed SMS recipient-preference / consent screening contract.

Key: ``sms-preference`` · source sections 5, 10. Stage: India channel eligibility.

India's SMS regime layers a *recipient preference* registry on top of the sender's DLT setup: the
TRAI / TCCCPR Do-Not-Disturb (DND) preference registry (the NCPR), exposed through the subscriber's
operator / the DLT platform. A commercial SMS route MUST be scrubbed against this registry — the
"preference/consent screening" a *provider* answers — before a marketing SMS to a recipient may be
offered to the host's one send pipeline.

This module defines ONLY the *contract* the DCA uses to ask that provider "has this recipient
registered a preference that blocks this SMS route right now?": a typed request, an immutable snapshot
carrying the three things an authoritative answer must disclose — its **version**, its **freshness**
(``as_of``) and its **affected scope** (the channels the snapshot speaks for + the registered
preference entries) — a client ``Protocol``, and the deterministic, fail-closed rule that turns a
snapshot into an allow/deny for the requested SMS route.

Fail-closed is the whole point (the DCA-089 acceptance criterion). "Not DND-registered" can only be
concluded from a response that is **available** AND **fresh** AND **covers the SMS channel asked
about**. Any other outcome yields NO permission for the route:

- an **unavailable** response (the preference registry could not be consulted) never clears the route —
  absence of a DND record is not evidence of its absence; the required screening fails closed;
- a **stale** response (scrubbed too long ago) is refused even if it reported no registration, because
  a DND preference recorded since could already be in force;
- a channel the snapshot does not authoritatively cover is refused — a DND answer scoped to SMS says
  **nothing** about email or WhatsApp, so it can never grant permission on another channel.

Two boundaries are kept explicit at the type level:

- **Law vs. product.** This screening is the *legal* preference gate (TRAI/NCPR DND). It is kept
  distinct from the product's consent-first default (the DCA-087/DCA-089 recipient line requires an
  explicit, in-force consent regardless): a clear DND result is NOT recipient consent — the composed
  recipient decision still requires the product consent gate to pass. ``grants_consent`` is always
  ``False``.
- **Channel.** The screening is SMS-only. A DND result does not grant email or WhatsApp permission; a
  snapshot that does not cover a channel refuses it rather than guessing.

Mirrors the DCA-079 suppression contract: shapes here, a deterministic synthetic fake alongside
(``app/preferences/fake.py``), and a live HTTP adapter to the real preference-registry provider
deferred to a later story. The contract stays disabled in production until that adapter ships — the
fake never stands in for it. Deciding the route is CLEAR enforces nothing and sends nothing: the actual
SMS send stays a disabled future capability behind the host's one send pipeline, which re-gates
everything (recipient consent, suppression, sender readiness).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from app.compliance.sms_sender_prerequisites import SMS_CHANNEL, SmsRoute

# DND preferences change often — a newly-registered DND must be honoured quickly — so a scrubbed
# preference snapshot cannot be relied on indefinitely. Operators require scrubbing against a recent
# copy of the register; this default is deliberately short. Callers may tighten it per rulepack.
DEFAULT_MAX_PREFERENCE_AGE = timedelta(days=7)

# The DLT routes on which a recipient preference / DND registration can block the message. The
# transactional / service-implicit routes are exempt from DND scrubbing; the consent-based routes
# (promotional / service-explicit) are the ones a registered preference can withhold.
_DND_APPLICABLE_ROUTES = frozenset({SmsRoute.PROMOTIONAL.value, SmsRoute.SERVICE_EXPLICIT.value})


def dnd_applies_to_route(route: str) -> bool:
    """Whether a registered DND preference can block the given route (promotional / service-explicit)."""
    return route.strip().casefold() in _DND_APPLICABLE_ROUTES


def _norm(value: str) -> str:
    """Case-insensitive, whitespace-trimmed key for matching a contact point / channel / route."""
    return value.strip().casefold()


class PreferenceAvailability(str, Enum):
    """Whether the recipient-preference registry could be consulted for this request.

    - ``AVAILABLE`` — the provider answered; its entries/coverage are authoritative as of ``as_of``.
    - ``UNAVAILABLE`` — the registry could not be reached/answered. The DCA has NO authoritative view,
      so the required screening fails closed: it clears nothing (``entries`` is meaningless here).
    """

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class PreferenceEntry(BaseModel):
    """One recipient's registered SMS preference the provider reports — which routes it blocks.

    ``fully_blocked`` is the "fully DND" registration (every promotional route blocked);
    ``blocked_routes`` are the specific DLT routes/categories the subscriber opted out of. ``since`` is
    when the preference took effect, if the provider discloses it. The DCA never interprets these beyond
    matching the requested route; it only records them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    contact_point: str = Field(min_length=1)
    channel: str = Field(min_length=1)
    fully_blocked: bool = False
    blocked_routes: tuple[str, ...] = ()
    since: datetime | None = None

    def blocks_route(self, route: str) -> bool:
        """Does this preference block the given route? Fully-DND blocks every DND-applicable route."""
        if self.fully_blocked and dnd_applies_to_route(route):
            return True
        target = _norm(route)
        return any(_norm(r) == target for r in self.blocked_routes)


class PreferenceScreeningRequest(BaseModel):
    """Ask the provider for the authoritative preference view of one recipient (DCA -> provider).

    Scoped to a ``business_id`` because the DCA only ever acts within a business it has itself
    authorized; the preference registry itself is operator / DLT-platform owned. ``contact_point`` is
    the recipient phone the scrub is about. The optional ``correlation_id`` is carried through so the
    (future) HTTP call and its logs share one id.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    business_id: str = Field(min_length=1)
    contact_point: str = Field(min_length=1)
    channel: str = SMS_CHANNEL
    correlation_id: str | None = None


class PreferenceScreeningSnapshot(BaseModel):
    """An immutable, point-in-time view of the provider's recipient-preference state for a business.

    The three things an authoritative answer must disclose:

    - ``version`` — the register version the answer was read at (provenance for audit/repro).
    - ``as_of`` — when the provider produced the answer. Staleness is measured from it.
    - affected scope — ``covered_channels`` (the channels this snapshot authoritatively speaks for; a
      channel outside it cannot be cleared — an SMS scrub says nothing about email/WhatsApp) together
      with ``entries`` (the registered preferences).

    ``provider`` names the preference-registry provider that answered (provenance; "provider-backed").
    ``synthetic`` marks a non-production fixture so a test snapshot can never be mistaken for a real
    authoritative answer. When ``availability`` is ``UNAVAILABLE`` the entry/coverage fields are not
    authoritative and the gate refuses regardless of them.
    """

    model_config = ConfigDict(frozen=True)

    business_id: str = Field(min_length=1)
    availability: PreferenceAvailability
    provider: str = Field(min_length=1)
    version: str = Field(min_length=1)
    as_of: datetime
    covered_channels: tuple[str, ...] = ()
    entries: tuple[PreferenceEntry, ...] = ()
    synthetic: bool = False

    @property
    def is_available(self) -> bool:
        return self.availability is PreferenceAvailability.AVAILABLE

    def covers_channel(self, channel: str) -> bool:
        """Does this snapshot authoritatively speak for ``channel``? If not, it cannot clear it."""
        target = _norm(channel)
        return any(_norm(c) == target for c in self.covered_channels)

    def registered_block(
        self, contact_point: str, channel: str, route: str
    ) -> PreferenceEntry | None:
        """The preference entry that blocks ``(contact_point, channel)`` on ``route``, if any.

        Only meaningful on an available snapshot; the gate checks availability/freshness first."""
        cp, ch = _norm(contact_point), _norm(channel)
        for entry in self.entries:
            if (
                _norm(entry.contact_point) == cp
                and _norm(entry.channel) == ch
                and entry.blocks_route(route)
            ):
                return entry
        return None

    def age(self, *, now: datetime) -> timedelta:
        return now - self.as_of


@runtime_checkable
class PreferenceScreeningClient(Protocol):
    """Port the DCA uses to consult the recipient-preference registry provider.

    Synchronous, matching the existing service-to-service client style (identity/entitlement/profile/
    suppression). The only implementation today is the dev/test fake; the live HTTP adapter to the real
    provider is a later story and until it ships the contract stays disabled in production.
    """

    def screen(self, request: PreferenceScreeningRequest) -> PreferenceScreeningSnapshot: ...


@dataclass(frozen=True)
class PreferenceScreeningDecision:
    """Whether the provider-backed preference view CLEARS an SMS route for a recipient.

    ``reason`` is a stable code for callers/audit. ``grants_consent`` is ALWAYS ``False``: clearing the
    DND/preference blocker is the legal gate only — it is never recipient consent (the product's
    consent-first default is a separate gate) and never a send authorization. A clear result is scoped
    to the ``channel`` screened; it grants nothing on any other channel.
    """

    allowed: bool
    reason: str
    channel: str
    route: str
    provider: str = ""
    detail: str = ""

    # stable reason codes
    OK = "ok"
    UNAVAILABLE = "preference_screening_unavailable"
    STALE = "preference_screening_stale"
    CHANNEL_NOT_SCREENED = "channel_not_in_screening_scope"
    DND_REGISTERED = "recipient_dnd_registered"

    @property
    def grants_consent(self) -> bool:
        """Clearing DND is NOT consent, and NOT a send authorization. Kept explicit so no caller
        mistakes a clear preference result for product consent — a separate, consent-first gate owns
        that, and the host's one send pipeline owns the send."""
        return False

    def permits_channel(self, channel: str) -> bool:
        """A clear preference result authorises nothing beyond the channel it screened.

        Only ``True`` when the route is cleared AND ``channel`` is the SMS channel this screening spoke
        for. It is ALWAYS ``False`` for email / WhatsApp: a DND result does not grant permission on
        another channel (the DCA-089 cross-channel invariant)."""
        return self.allowed and _norm(channel) == _norm(self.channel)


def screen_preferences(
    snapshot: PreferenceScreeningSnapshot | None,
    contact_point: str,
    route: str,
    *,
    channel: str = SMS_CHANNEL,
    now: datetime | None = None,
    max_age: timedelta = DEFAULT_MAX_PREFERENCE_AGE,
) -> PreferenceScreeningDecision:
    """Decide whether the provider-backed preference view clears ``(contact_point, channel)`` on
    ``route``. Fail-closed at each step: absent/unavailable → stale → channel-not-covered → registered →
    cleared.

    ``now`` defaults to real UTC now; ``max_age`` is the freshness window. The decision is a pure
    function of the snapshot and the moment — it reads nothing and changes nothing, so both the clear
    and every denial are perfectly reproducible with no state change.
    """
    moment = now or datetime.now(UTC)
    provider = snapshot.provider if snapshot is not None else ""

    # Absent or unavailable: the required screening could not be consulted. Absence of a DND record is
    # NOT evidence there is none — fail closed.
    if snapshot is None or not snapshot.is_available:
        return PreferenceScreeningDecision(
            False, PreferenceScreeningDecision.UNAVAILABLE, channel, route, provider=provider
        )

    # Stale: scrubbed too long ago. A DND preference recorded since would be missed, so a clean answer
    # can no longer be trusted — refuse even though the snapshot reported no registration.
    if snapshot.age(now=moment) > max_age:
        return PreferenceScreeningDecision(
            False,
            PreferenceScreeningDecision.STALE,
            channel,
            route,
            provider=provider,
            detail=str(snapshot.age(now=moment)),
        )

    # The snapshot must authoritatively cover the channel being asked about; a scrub scoped to SMS
    # cannot clear another channel — refuse rather than guess (this is what keeps a DND result from
    # ever granting email / WhatsApp permission).
    if not snapshot.covers_channel(channel):
        return PreferenceScreeningDecision(
            False,
            PreferenceScreeningDecision.CHANNEL_NOT_SCREENED,
            channel,
            route,
            provider=provider,
        )

    # The authoritative, fresh, in-scope answer: a registered preference that blocks this route denies.
    block = snapshot.registered_block(contact_point, channel, route)
    if block is not None:
        detail = "fully_blocked" if block.fully_blocked else ",".join(block.blocked_routes)
        return PreferenceScreeningDecision(
            False,
            PreferenceScreeningDecision.DND_REGISTERED,
            channel,
            route,
            provider=provider,
            detail=detail,
        )

    # Cleared on preference grounds only. This is the legal gate, NOT consent (grants_consent is False),
    # and it is scoped to this channel; acting on it stays a disabled future capability behind the one
    # send pipeline.
    return PreferenceScreeningDecision(
        True, PreferenceScreeningDecision.OK, channel, route, provider=provider
    )
