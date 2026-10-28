"""DCA-085 — the external deletion/removal contract + the supported-destination port.

Key: ``deletion-remote`` · source sections 5, 10, 11.

A local erasure (DCA-084) removes the DCA's OWN operational data. Some of that data may ALSO have been
handed to a supported external integration — a platform/provider destination. This module defines ONLY
the *contract* the DCA uses to ask such a destination to delete/remove it: a typed, opaque-only request,
the small set of outcomes a destination may return, a client ``Protocol``, and a deterministic synthetic
fake alongside (the only implementation today — the live adapter to a real integration is a later story
and until it ships the contract stays disabled in production).

The contract is deliberately narrow and honest about the three things that make propagation safe:

- **Idempotent.** Every request carries a deterministic :attr:`DeletionDispatchRequest.idempotency_key`
  (derived from the erasure journal id + the destination), so a re-send of the same request is
  de-duplicated by the destination — exactly once end to end. Re-sending a request that already
  succeeded is safe.
- **A timeout is NOT a success.** A destination that does not acknowledge in time raises
  :class:`DeletionTimeout`. The caller records that as *pending reconciliation*, never as a completed
  deletion — treating "we didn't hear back" as "deleted" is the fail-open this story forbids.
- **Unsupported is explicit, not silent.** A destination that cannot delete/remove returns
  :attr:`DeletionOutcome.UNSUPPORTED` — a recorded "cannot be propagated here", never a quiet success.

Like the DCA-079 suppression contract and the DCA-022/037 platform contracts, every payload is OPAQUE
(ids / row keys only — never a contact value, a name, a field value or a secret), and the synthetic
fake is flagged ``synthetic`` so a test response can never be mistaken for a real one.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

# The external integration destinations this build knows how to address. A controlled, opaque set of
# contract keys (never URLs / endpoints / secrets). A destination outside this set is not propagated to
# (the propagator only ever iterates supported destinations), so the surface stays narrow.
SUPPORTED_DESTINATIONS: tuple[str, ...] = (
    "crm_mirror",
    "enrichment_cache",
    "outreach_suppression_feed",
)


class DeletionOutcome(str, Enum):
    """The outcome a destination contract may return for a deletion/removal request.

    - ``ACKNOWLEDGED`` — the destination accepted the deletion and returned an acknowledgment ref.
    - ``UNSUPPORTED`` — the destination contract does not support deletion/removal (for this subject).

    A TIMEOUT is NOT a value here — it is the absence of a response and is raised as
    :class:`DeletionTimeout` so it can never be confused with an acknowledgment. A permanent rejection
    is raised as :class:`DeletionRejected`.
    """

    ACKNOWLEDGED = "acknowledged"
    UNSUPPORTED = "unsupported"


class DeletionContractError(Exception):
    """Base class for a deletion-propagation contract problem. Always raised with a SAFE, shape-only
    message — never the request payload or any provider detail."""


class DeletionContractDisabled(DeletionContractError):
    """The deletion-propagation contract is not wired (e.g. production, where only the synthetic fake
    exists and the live adapter is unbuilt). Raised rather than silently handing back a fake, which
    would risk treating a synthetic acknowledgment as a real one."""


class DeletionTimeout(DeletionContractError):
    """The destination did not acknowledge the deletion in time.

    The caller records this as PENDING (pending reconciliation), never as a completed deletion. A
    later reconciliation re-sends the SAME idempotent request. The message names the destination (an
    opaque contract key) and the fact of a timeout only — never the payload."""


class DeletionRejected(DeletionContractError):
    """The destination explicitly rejected the deletion with a permanent (non-timeout) error.

    Distinct from a timeout so the caller can record FAILED rather than PENDING. ``reason`` is a
    stable, opaque code — never a raw provider message."""

    def __init__(self, message: str, *, reason: str = "destination_rejected") -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class DeletionDispatchRequest:
    """Ask one external destination to delete/remove a subject's data (DCA -> destination).

    Every field is OPAQUE: the business scope, the erasure journal the request answers, the subject's
    opaque ids, the destination contract key, and the DETERMINISTIC idempotency key the destination
    uses to de-duplicate a re-send. It carries NO contact value, name or field value — a destination
    locates its own copy by these references, exactly as the platform handed them over.
    """

    business_id: str
    erasure_journal_id: str
    subject_type: str
    subject_id: str
    destination: str
    idempotency_key: str
    correlation_id: str | None = None


@dataclass(frozen=True)
class DeletionResponse:
    """A destination's response to a deletion request (only for a response that arrived).

    ``outcome`` is ACKNOWLEDGED or UNSUPPORTED. ``acknowledgment_ref`` is the opaque receipt a
    destination returns on an acknowledgment (empty otherwise). ``detail_code`` is a stable opaque
    reason (e.g. why UNSUPPORTED). ``synthetic`` marks a non-production fixture response so it can
    never be mistaken for a real authoritative acknowledgment.
    """

    outcome: DeletionOutcome
    acknowledgment_ref: str = ""
    detail_code: str = ""
    synthetic: bool = False


@runtime_checkable
class DeletionDestinationClient(Protocol):
    """Port the DCA uses to send an idempotent deletion/removal request to a supported destination.

    Synchronous, matching the existing service-to-service client style (identity / entitlement /
    profile / suppression). The only implementation today is the dev/test fake; the live adapter to a
    real external integration is a later story and until it ships the contract stays disabled in
    production.
    """

    def supported_destinations(self) -> tuple[str, ...]:
        """The destinations this client can address (the propagator iterates exactly these)."""
        ...

    def dispatch(self, request: DeletionDispatchRequest) -> DeletionResponse:
        """Send one deletion/removal request. Returns a :class:`DeletionResponse` on an acknowledgment
        or an explicit unsupported state; raises :class:`DeletionTimeout` when the destination did not
        answer in time, or :class:`DeletionRejected` on a permanent rejection."""
        ...


def build_idempotency_key(erasure_journal_id: str, destination: str) -> str:
    """The DETERMINISTIC idempotency key for one (journal, destination) pair.

    Deterministic so a re-send (a retry / a reconciliation) carries the SAME key the first send used,
    which is what lets the destination de-duplicate it. Opaque — it is built only from the journal id
    and the destination contract key, never from any personal data.
    """
    return f"erasure-del:{erasure_journal_id.strip()}:{destination.strip()}"
