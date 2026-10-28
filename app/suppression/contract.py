"""DCA-079 — the authoritative suppression-lookup contract + the platform-suppression port.

Key: ``platform-suppression-port`` · source sections 5, 10, 11.

Suppression and recipient consent are **the host platform's to own**, never the DCA's. The platform is
the single authoritative registry of who must not be contacted (unsubscribes, bounces, complaints,
regulator do-not-contact). This module defines ONLY the *contract* the DCA uses to ask the platform
"is this contact point suppressed for this channel right now?": a typed request, an immutable snapshot
carrying the three things an authoritative answer must disclose — its **version**, its **freshness**
(``as_of``) and its **affected scope** (the channels the snapshot speaks for + the suppressed entries)
— a client ``Protocol``, and the deterministic, fail-closed rule that turns a snapshot into an
allow/deny for *activating* a candidate on a channel.

Fail-closed is the whole point. "Not suppressed" can only be concluded from a response that is
**available** AND **fresh** AND **covers the channel asked about**. Any other outcome yields NO
activation permission:

- an **unavailable** response (the authoritative registry could not be consulted) never permits
  activation — absence of a suppression record is not evidence of its absence;
- a **stale** response (fetched too long ago) is refused even if it reported no suppression, because
  an unsubscribe recorded since could already be in force;
- a channel the snapshot does not authoritatively cover is refused — the DCA does not guess.

Host consent ownership is kept explicit at the type level: a snapshot carries
``host_owns_consent=True`` and an allow decision exposes ``grants_consent == False``. Clearing the
suppression blocker is NOT recipient consent (acceptance of a prospect is not consent either); it only
removes one gate. Acting on an activation — sending anything — stays a disabled future capability
behind the one send pipeline; this module computes and explains the gate, nothing more.

Mirrors the DCA-022 entitlement and DCA-037 platform-profile contracts: shapes here, a deterministic
synthetic fake alongside (``app/suppression/fake.py``), and a live HTTP adapter deferred to a later
story. The contract stays disabled in production until that adapter ships — the fake never stands in
for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

# Suppression is safety-critical and changes often — an unsubscribe must be honored quickly — so the
# freshness window is deliberately tight (far shorter than the 24h entitlement window). A snapshot
# fetched longer ago than this cannot clear a contact point: a withdrawal recorded since might be in
# force and the cached answer would miss it.
DEFAULT_MAX_SUPPRESSION_AGE = timedelta(hours=1)


def _norm(value: str) -> str:
    """Case-insensitive, whitespace-trimmed key for matching a contact point / channel.

    Suppression must match regardless of address casing (``Opted-Out@x`` == ``opted-out@x``); the
    authoritative platform normalizes the same way, so this keeps the DCA's lookup aligned."""
    return value.strip().casefold()


class SuppressionAvailability(str, Enum):
    """Whether the authoritative suppression registry could be consulted for this request.

    - ``AVAILABLE`` — the platform answered; its entries/coverage are authoritative as of ``as_of``.
    - ``UNAVAILABLE`` — the registry could not be reached/answered. The DCA has NO authoritative view,
      so it must fail closed: an unavailable response clears nothing (``entries`` is meaningless here).
    """

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class SuppressionEntry(BaseModel):
    """One suppressed ``(contact_point, channel)`` the platform reports, with why and since-when.

    ``reason`` is a stable, opaque code the platform supplies (e.g. ``unsubscribe`` / ``bounce`` /
    ``complaint``); the DCA never interprets it, only records it. ``since`` is when the suppression took
    effect, if the platform discloses it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contact_point: str = Field(min_length=1)
    channel: str = Field(min_length=1)
    reason: str = ""
    since: datetime | None = None


class SuppressionLookupRequest(BaseModel):
    """Ask the platform for the authoritative suppression view of one business (DCA -> platform).

    Scoped to a ``business_id`` because the DCA only ever acts within a business it has itself
    authorized; the suppression registry itself is host-owned and may span the platform. The optional
    ``correlation_id`` is carried through so the (future) HTTP call and its logs share one id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    business_id: str = Field(min_length=1)
    correlation_id: str | None = None


class SuppressionSnapshot(BaseModel):
    """An immutable, point-in-time view of the host's authoritative suppression state for a business.

    The three things an authoritative answer must disclose (the DCA-079 "version, freshness, affected
    scope"):

    - ``version`` — the registry version the answer was read at (provenance for audit/repro).
    - ``as_of`` — when the platform produced the answer. This is its freshness; staleness is measured
      from it, not from the DCA's own clock-at-use.
    - affected scope — ``covered_channels`` (the channels this snapshot authoritatively speaks for; a
      channel outside it cannot be cleared) together with ``entries`` (the suppressed contact points).

    ``host_owns_consent`` is always ``True`` and states the ownership boundary at the type level: the
    platform owns suppression and recipient consent; the DCA only reads this snapshot. ``synthetic``
    marks a non-production fixture so a test snapshot can never be mistaken for a real authoritative
    answer. When ``availability`` is ``UNAVAILABLE`` the entry/coverage fields are not authoritative and
    the gate refuses regardless of them.
    """

    model_config = ConfigDict(frozen=True)

    business_id: str = Field(min_length=1)
    availability: SuppressionAvailability
    version: str = Field(min_length=1)
    as_of: datetime
    covered_channels: tuple[str, ...] = ()
    entries: tuple[SuppressionEntry, ...] = ()
    host_owns_consent: bool = True
    synthetic: bool = False

    @property
    def is_available(self) -> bool:
        return self.availability is SuppressionAvailability.AVAILABLE

    def covers_channel(self, channel: str) -> bool:
        """Does this snapshot authoritatively speak for ``channel``? If not, it cannot clear it."""
        target = _norm(channel)
        return any(_norm(c) == target for c in self.covered_channels)

    def is_suppressed(self, contact_point: str, channel: str) -> bool:
        """Is ``(contact_point, channel)`` suppressed per this snapshot? Case-insensitive match.

        Only meaningful on an available snapshot; the gate checks availability/freshness first."""
        cp, ch = _norm(contact_point), _norm(channel)
        return any(_norm(e.contact_point) == cp and _norm(e.channel) == ch for e in self.entries)

    def age(self, *, now: datetime) -> timedelta:
        return now - self.as_of


@runtime_checkable
class PlatformSuppressionClient(Protocol):
    """Port the DCA uses to consult the host's authoritative suppression registry.

    Synchronous, matching the existing service-to-service client style (identity/entitlement/profile).
    The only implementation today is the dev/test fake; the live HTTP adapter is a later story and until
    it ships the contract stays disabled in production.
    """

    def lookup(self, request: SuppressionLookupRequest) -> SuppressionSnapshot: ...


@dataclass(frozen=True)
class ActivationDecision:
    """Whether the authoritative suppression view permits *activating* a contact point on a channel.

    ``reason`` is a stable code for callers/audit. ``grants_consent`` is ALWAYS ``False``: an allow only
    removes the suppression blocker — it is never recipient consent, which the host owns. A caller must
    still hold consent (and everything else) before anything could be sent, and sending itself stays a
    disabled future capability behind the one send pipeline.
    """

    allowed: bool
    reason: str

    # stable reason codes
    OK = "ok"
    LOOKUP_UNAVAILABLE = "suppression_lookup_unavailable"
    STALE = "suppression_lookup_stale"
    CHANNEL_NOT_COVERED = "channel_not_in_authoritative_scope"
    SUPPRESSED = "contact_point_suppressed"

    @property
    def grants_consent(self) -> bool:
        """Clearing suppression is NOT consent. Kept explicit so no caller mistakes an allow for one."""
        return False


def authorize_activation(
    snapshot: SuppressionSnapshot | None,
    contact_point: str,
    channel: str,
    *,
    now: datetime | None = None,
    max_age: timedelta = DEFAULT_MAX_SUPPRESSION_AGE,
) -> ActivationDecision:
    """Decide whether the authoritative suppression view clears ``(contact_point, channel)`` for
    activation. Fail-closed at each step: absent/unavailable → stale → channel-not-covered →
    suppressed → allowed.

    ``now`` defaults to real UTC now; ``max_age`` is the freshness window. The decision is a pure
    function of the snapshot and the moment — it reads nothing and changes nothing, so both the allow
    and every denial are perfectly reproducible.
    """
    moment = now or datetime.now(UTC)

    # Absent or unavailable: the DCA has no authoritative answer. Absence of a suppression record is
    # NOT evidence there is none — refuse.
    if snapshot is None or not snapshot.is_available:
        return ActivationDecision(False, ActivationDecision.LOOKUP_UNAVAILABLE)

    # Stale: fetched too long ago. An unsubscribe recorded since would be missed, so a clean answer can
    # no longer be trusted — refuse even though the snapshot reported no suppression.
    if snapshot.age(now=moment) > max_age:
        return ActivationDecision(False, ActivationDecision.STALE)

    # The snapshot must authoritatively cover the channel being asked about; otherwise the DCA cannot
    # conclude the contact point is clear on it — refuse rather than guess.
    if not snapshot.covers_channel(channel):
        return ActivationDecision(False, ActivationDecision.CHANNEL_NOT_COVERED)

    # The authoritative, fresh, in-scope answer: suppressed means no activation.
    if snapshot.is_suppressed(contact_point, channel):
        return ActivationDecision(False, ActivationDecision.SUPPRESSED)

    # Cleared on suppression grounds only. This is NOT consent (grants_consent is False); it removes one
    # blocker, and acting on it stays a disabled future capability behind the one send pipeline.
    return ActivationDecision(True, ActivationDecision.OK)
