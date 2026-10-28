"""DCA-085 — deterministic, in-memory external-deletion fake for local tests.

The only external-deletion implementation today: the live adapter to a real integration is a later
story. It is dev/test-only — production refuses it (:func:`build_deletion_destination_client` returns
``None``), mirroring the DCA-079 suppression fake and the identity/entitlement/profile fakes. Every
response it produces is flagged ``synthetic``, so a test acknowledgment can never be mistaken for a
real authoritative one.

Three per-destination behaviours make the DCA-085 success path AND its two failure cases testable
without any network or wall clock, by addressing a destination configured to behave each way:

- ``ack`` — the destination acknowledges and returns a deterministic, opaque acknowledgment ref.
- ``unsupported`` — the destination explicitly does not support deletion/removal (recorded, not a
  failure and not a silent success).
- ``timeout`` — the destination does not answer in time: :class:`~app.integration.deletion.DeletionTimeout`
  is raised, so the caller records PENDING (pending reconciliation), never a completed deletion.
- ``reject`` — the destination permanently rejects: :class:`~app.integration.deletion.DeletionRejected`.

The fake is also the seam the reconciliation path is tested through: flipping a destination from
``timeout`` to ``ack`` (or clearing :attr:`heal_after`) models a destination that was briefly
unavailable and later acknowledges the SAME idempotent request — exactly what reconciliation relies on.
Every request it sees is recorded on :attr:`seen` so a test can assert a request was (or was not) re-sent.
"""

from __future__ import annotations

from typing import Any

from app.integration.deletion import (
    SUPPORTED_DESTINATIONS,
    DeletionDispatchRequest,
    DeletionOutcome,
    DeletionRejected,
    DeletionResponse,
    DeletionTimeout,
)

# The default per-destination behaviour of the synthetic fake — one destination of each kind so the
# success path and both failure cases are all exercisable out of the box.
DEFAULT_BEHAVIOURS: dict[str, str] = {
    "crm_mirror": "ack",
    "enrichment_cache": "unsupported",
    "outreach_suppression_feed": "ack",
}

_SYNTHETIC_ACK_PREFIX = "fake-del-ack"


class FakeDeletionDestinationClient:
    """A deterministic :class:`~app.integration.deletion.DeletionDestinationClient`.

    ``behaviours`` maps a destination contract key to one of ``ack`` / ``unsupported`` / ``timeout`` /
    ``reject`` (an unknown destination defaults to ``ack``). A destination set to ``timeout`` raises
    :class:`~app.integration.deletion.DeletionTimeout`; once :attr:`heal_after` sends have been made to
    it (default never), it instead acknowledges — modelling a destination that recovers so a
    reconciliation can succeed on a re-send. Pure: no network, no wall clock. Every response is
    ``synthetic=True``.
    """

    def __init__(
        self,
        behaviours: dict[str, str] | None = None,
        *,
        destinations: tuple[str, ...] = SUPPORTED_DESTINATIONS,
        heal_after: dict[str, int] | None = None,
    ) -> None:
        self._behaviours = dict(behaviours) if behaviours is not None else dict(DEFAULT_BEHAVIOURS)
        self._destinations = tuple(destinations)
        self._heal_after = dict(heal_after or {})
        # Per-(idempotency_key) attempt counter, so a destination can "heal" after N sends and so a
        # test can observe that a reconciliation actually re-sent the same request.
        self._attempts: dict[str, int] = {}
        self.seen: list[DeletionDispatchRequest] = []

    def supported_destinations(self) -> tuple[str, ...]:
        return self._destinations

    def set_behaviour(self, destination: str, behaviour: str) -> None:
        """Change a destination's behaviour (used by a test to model a destination recovering)."""
        self._behaviours[destination] = behaviour

    def dispatch(self, request: DeletionDispatchRequest) -> DeletionResponse:
        self.seen.append(request)
        self._attempts[request.idempotency_key] = self._attempts.get(request.idempotency_key, 0) + 1
        attempt = self._attempts[request.idempotency_key]
        behaviour = self._behaviours.get(request.destination, "ack")

        if behaviour == "timeout":
            heal_after = self._heal_after.get(request.destination)
            if heal_after is not None and attempt >= heal_after:
                behaviour = "ack"  # the destination has recovered — acknowledge the re-send
            else:
                raise DeletionTimeout(
                    f"destination '{request.destination}' did not acknowledge in time"
                )

        if behaviour == "reject":
            raise DeletionRejected(
                f"destination '{request.destination}' rejected the deletion request",
                reason="destination_rejected",
            )

        if behaviour == "unsupported":
            return DeletionResponse(
                outcome=DeletionOutcome.UNSUPPORTED,
                detail_code="deletion_not_supported",
                synthetic=True,
            )

        # Default / "ack": a deterministic, opaque acknowledgment reference (derived from the
        # idempotency key, so a re-send of the same request yields the SAME ack ref).
        return DeletionResponse(
            outcome=DeletionOutcome.ACKNOWLEDGED,
            acknowledgment_ref=f"{_SYNTHETIC_ACK_PREFIX}:{request.idempotency_key}",
            synthetic=True,
        )


def build_deletion_destination_client(settings: Any) -> FakeDeletionDestinationClient | None:
    """The external-deletion client for this deployment, or ``None`` when the contract is disabled.

    Only the dev/test fake exists; the live adapter to a real external integration is unbuilt.
    Production therefore has NO deletion client — the contract stays disabled until that story ships,
    so a propagation request in production fails closed (the route returns 503) rather than quietly
    acknowledging against a synthetic destination (the exact fail-open this story forbids).
    """
    if getattr(settings, "is_production", False):
        return None
    return FakeDeletionDestinationClient()
