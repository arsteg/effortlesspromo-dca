"""DCA-098 — deterministic, in-memory EffortlessPromo advertising-audience removal fake for tests.

The only removal-port implementation today: the live HTTP adapter into the platform's advertising-
audience integration is a later story (blocked on DCA-001 B1 — there is no service-to-service contract
into the .NET API yet, and no live advertising-audience integration exists). It is dev/test-only —
production refuses it (:func:`build_removal_port`), mirroring the DCA-096 delivery and DCA-097
cancellation fakes. Every capability answer it produces is flagged ``synthetic`` with a plainly-fake
version, so a test answer can never be mistaken for a real authoritative one. It performs NO upload and
NO send: it models a host-side advertising audience of already-uploaded members and reports, per member,
whether that member was REMOVED (was present), was NOT_PRESENT (already gone / never uploaded), or is
PENDING (unconfirmable right now — the acceptance behaviour of this story).

Knobs make the DCA-098 behaviour and its failure cases testable without a wall clock:

- ``audience`` — the host-side audience: a mapping of ``member_digest -> AudienceMemberState``. A member
  in ``PRESENT`` is removable; a member in ``REMOVED`` or absent is ``NOT_PRESENT``. Seeds the fixture
  with the members that were previously uploaded.
- ``transient_failures`` — member digests whose removal temporarily FAILS at an otherwise-acknowledging
  host; each is reported ``PENDING``, left in the audience, and kept *owed* for retry (the per-member
  failure case). A digest leaves this set to simulate the host recovering.
- ``acknowledges`` — when ``False`` the host does NOT guarantee acknowledgment, so the WHOLE request is
  PENDING and the audience is left UNTOUCHED (the host-level failure case). Not treated as a success.
- ``supports_removal`` — when ``False`` the host offers no removal path at all (also pending).
- ``available`` — when ``False`` the contract simulates being unreachable (fail-closed, pending).

Two things this fake makes visible, matching the acceptance criteria:

- **The withdrawal is authoritative.** Every member named in a request is recorded in an in-memory
  ``_withdrawn`` set the moment the request is made — regardless of the host outcome. :meth:`membership`
  reports such a member as ``withdrawn`` even while it is still ``PRESENT`` on the host (a pending
  removal), so the prior upload never overrides the current withdrawal.
- **Removal failures stay visible and pending.** Any member not confirmed removed is kept in the
  ``_owed`` set and reported ``PENDING``; a request with anything owed is NOT ledgered as complete, so a
  later retry re-attempts it. Only a fully-COMPLETED request is ledgered (idempotent), so a replayed
  completed removal returns the SAME receipt verbatim and never re-reads a now-removed member into a
  different (NOT_PRESENT) answer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from app.advertising.audience_removal import (
    AudienceMemberState,
    RemovalCapabilities,
    RemovalHostAvailability,
    RemovalItemOutcome,
    RemovalItemStatus,
    RemovalReceipt,
    RemovalRequest,
    authorize_removal,
)
from app.compliance.customer_match_eligibility import CUSTOMER_MATCH_CHANNEL

SYNTHETIC_VERSION = "fake-audience-removal-v1"

# The advertising audiences the synthetic EffortlessPromo removal contract speaks for.
DEFAULT_SUPPORTED_AUDIENCES: tuple[str, ...] = (CUSTOMER_MATCH_CHANNEL,)


class AudienceRemovalContractDisabled(RuntimeError):
    """Raised when a removal port is requested where the contract is not wired (production)."""


class FakeEffortlessPromoAudienceRemovalPort:
    """A deterministic :class:`~app.advertising.audience_removal.RemovalPort`. No I/O, no upload, no send.

    Models the EffortlessPromo platform's request-removal contract over an in-memory advertising audience
    of already-uploaded members. ``acknowledges=False`` / ``supports_removal=False`` / ``available=False``
    each make EVERY request PENDING (the host-level failure cases) and leave the audience UNTOUCHED.
    ``transient_failures`` make named members PENDING at an otherwise-acknowledging host (the per-member
    failure case). ``now`` is injected into ``as_of`` so freshness/staleness is testable.

    A fully acknowledged, confirmable request removes each still-``PRESENT`` member (status ``REMOVED``),
    reports an already-gone/never-uploaded member ``NOT_PRESENT``, and reports a transient-failure member
    ``PENDING`` (left in the audience, kept owed). The ``acknowledgment_reference`` is DETERMINISTIC in the
    removal key, and a fully-COMPLETED receipt is stored under that key so a replay returns it verbatim.
    The withdrawal is recorded for every named member regardless of outcome — the prior upload never
    overrides it.
    """

    def __init__(
        self,
        *,
        audience: Mapping[str, AudienceMemberState] | None = None,
        transient_failures: Iterable[str] = (),
        acknowledges: bool = True,
        supports_removal: bool = True,
        available: bool = True,
        supported_audiences: tuple[str, ...] = DEFAULT_SUPPORTED_AUDIENCES,
        now: datetime | None = None,
        version: str = SYNTHETIC_VERSION,
    ) -> None:
        self._audience: dict[str, AudienceMemberState] = dict(audience or {})
        self._transient_failures: set[str] = set(transient_failures)
        self._acknowledges = acknowledges
        self._supports_removal = supports_removal
        self._available = available
        self._supported_audiences = supported_audiences
        self._now = now
        self._version = version
        # The DCA-side authoritative withdrawal set: every member ever named in a request, regardless of
        # whether the host confirmed the removal. The prior upload never overrides this.
        self._withdrawn: set[str] = set()
        # Members whose removal is still owed (requested but not yet confirmed removed) — kept visible.
        self._owed: set[str] = set()
        # Acknowledgment ledger: removal_key -> a fully-COMPLETED receipt already issued. Only a request
        # with nothing left owed is ledgered, so a PENDING request stays retryable.
        self._ledger: dict[str, RemovalReceipt] = {}

    def audience_state(self, member_digest: str) -> AudienceMemberState | None:
        """The host-side state of one member, or ``None`` if the audience never held it."""
        return self._audience.get(member_digest)

    def membership(self, member_digest: str) -> str:
        """The DCA's AUTHORITATIVE view of a member — ``withdrawn`` once a removal has been requested,
        regardless of whether the host still shows it ``present``. This is the acceptance invariant: the
        prior upload (``present`` on the host) never overrides the current withdrawal."""
        if member_digest in self._withdrawn:
            return "withdrawn"
        state = self._audience.get(member_digest)
        if state is AudienceMemberState.PRESENT:
            return "present"
        return "unknown"

    @property
    def withdrawn(self) -> frozenset[str]:
        return frozenset(self._withdrawn)

    @property
    def owed_removals(self) -> frozenset[str]:
        """Members whose removal is still pending (owed) and must be retried — kept visible."""
        return frozenset(self._owed)

    def recover(self, *member_digests: str) -> None:
        """Simulate the host recovering: clear a per-member transient failure so a retry can remove it."""
        self._transient_failures.difference_update(member_digests)

    def capabilities(self, business_id: str) -> RemovalCapabilities:
        """What this fake host advertises for a business. Unavailable ⇒ capability fields are empty and
        the gate refuses on availability regardless of them."""
        now = self._now or datetime.now(UTC)
        if not self._available:
            return RemovalCapabilities(
                business_id=business_id,
                availability=RemovalHostAvailability.UNAVAILABLE,
                version=self._version,
                as_of=now,
                synthetic=True,
            )
        return RemovalCapabilities(
            business_id=business_id,
            availability=RemovalHostAvailability.AVAILABLE,
            version=self._version,
            as_of=now,
            supports_removal=self._supports_removal,
            acknowledges=self._acknowledges,
            supported_audiences=self._supported_audiences,
            synthetic=True,
        )

    def request_removal(self, request: RemovalRequest) -> RemovalReceipt:
        """Request removal of revoked members from the (fake) advertising audience. Fail-closed: a host
        that is unavailable, stale, non-acknowledging, or does not support the audience leaves the WHOLE
        request PENDING with the audience UNTOUCHED — never a silent success. A per-member transient
        failure is PENDING too. Every named member is recorded as withdrawn regardless of outcome (the
        prior upload never overrides it), and any un-confirmed removal is kept owed so the DCA retries.
        Only a fully-COMPLETED request is idempotently ledgered."""
        # The withdrawal is authoritative: record every named member as withdrawn immediately, before and
        # independent of the host outcome. The prior upload can never flip this back to membership.
        for item in request.members:
            self._withdrawn.add(item.member_digest)

        # Replay: a previously COMPLETED request returns its stored receipt verbatim (a PENDING request is
        # never ledgered, so it re-attempts on retry).
        prior = self._ledger.get(request.removal_key)
        if prior is not None:
            return prior

        now = self._now or datetime.now(UTC)
        caps = self.capabilities(request.business_id)
        decision = authorize_removal(caps, request.audience_channel, now=now)

        if not decision.allowed:
            # Host-level failure: the removal is NOT dropped and NOT faked as done. Every member stays
            # owed and is reported PENDING (visible); the audience is left exactly as it was.
            outcomes = tuple(
                RemovalItemOutcome(
                    member_digest=item.member_digest,
                    status=RemovalItemStatus.PENDING,
                    removed=False,
                    pending=True,
                    note=f"removal not confirmable ({decision.reason}); owed, will retry",
                )
                for item in request.members
            )
            for item in request.members:
                self._owed.add(item.member_digest)
            return RemovalReceipt(
                business_id=request.business_id,
                removal_key=request.removal_key,
                audience_channel=request.audience_channel,
                status=RemovalReceipt.PENDING,
                reason=decision.reason,
                acknowledged=False,
                outcomes=outcomes,
                synthetic=True,
            )

        outcomes_list: list[RemovalItemOutcome] = []
        for item in request.members:
            digest = item.member_digest
            if digest in self._transient_failures:
                # Host reachable but this member's removal temporarily failed — keep it owed and visible.
                self._owed.add(digest)
                outcomes_list.append(
                    RemovalItemOutcome(
                        member_digest=digest,
                        status=RemovalItemStatus.PENDING,
                        removed=False,
                        pending=True,
                        note="temporary platform failure removing member; owed, will retry",
                    )
                )
                continue
            state = self._audience.get(digest)
            if state is AudienceMemberState.PRESENT:
                # Actually remove the member from the uploaded audience.
                self._audience[digest] = AudienceMemberState.REMOVED
                self._owed.discard(digest)
                outcomes_list.append(
                    RemovalItemOutcome(
                        member_digest=digest,
                        status=RemovalItemStatus.REMOVED,
                        removed=True,
                        pending=False,
                        note="removed from advertising audience",
                    )
                )
            else:
                # Already removed, or never uploaded — a no-op, reported honestly (never fabricated).
                self._owed.discard(digest)
                outcomes_list.append(
                    RemovalItemOutcome(
                        member_digest=digest,
                        status=RemovalItemStatus.NOT_PRESENT,
                        removed=False,
                        pending=False,
                        note="member not present in advertising audience",
                    )
                )

        outcomes = tuple(outcomes_list)
        has_pending = any(o.pending for o in outcomes)
        if has_pending:
            # Acknowledged, but not everything could be removed — stays PENDING and is NOT ledgered, so a
            # later retry re-attempts the owed members once the host recovers.
            return RemovalReceipt(
                business_id=request.business_id,
                removal_key=request.removal_key,
                audience_channel=request.audience_channel,
                status=RemovalReceipt.PENDING,
                reason=decision.reason,
                acknowledged=True,
                outcomes=outcomes,
                synthetic=True,
            )

        receipt = RemovalReceipt(
            business_id=request.business_id,
            removal_key=request.removal_key,
            audience_channel=request.audience_channel,
            status=RemovalReceipt.COMPLETED,
            reason=decision.reason,
            acknowledged=True,
            outcomes=outcomes,
            acknowledgment_reference=f"synthetic-removal-ack-{request.removal_key}",
            synthetic=True,
        )
        self._ledger[request.removal_key] = receipt
        return receipt


def build_removal_port(settings: Any) -> FakeEffortlessPromoAudienceRemovalPort:
    """The advertising-audience removal port for this deployment.

    Only the dev/test fake exists; the live adapter into the platform's advertising-audience integration
    is unbuilt (DCA-001 B1, and no live advertising-audience integration exists). Production therefore has
    NO removal port — the contract stays disabled until that story ships. Calling this under production
    configuration raises rather than silently handing back a fake (which would risk treating a synthetic
    acknowledgment as a real one — a fail-open this contract forbids, exactly as the DCA-096/097 fakes do).
    """
    if getattr(settings, "is_production", False):
        raise AudienceRemovalContractDisabled(
            "advertising-audience removal contract is disabled in production: the live adapter into the "
            "platform's audience integration is not built yet (DCA-001 B1) and the synthetic fake is "
            "dev/test-only"
        )
    return FakeEffortlessPromoAudienceRemovalPort()
