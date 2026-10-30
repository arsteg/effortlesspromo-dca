"""DCA-095 — the idempotent dispatch-slot consumption ledger.

Key: ``dispatch-recheck`` · source sections 5, 10. Stage: India channel eligibility.

A DCA-094 activation reference proves the DCA's gates were clear for a context when it was *issued*.
Immediately before a host dispatches, the DCA-095 recheck re-asserts those gates against CURRENT
state and, on a definitive grant, *consumes* exactly one dispatch slot. This module owns that
consumption: a tenant-scoped, insert-only ledger keyed on ``(business_id, idempotency_key)`` whose
unique constraint is the serialisation point that makes a retried or concurrent consume idempotent —
one logical dispatch records at most one grant, and the database (a real PostgreSQL, not SQLite) is
what enforces it under contention.

Two load-bearing rules, both fail-closed:

- **Grants only persist.** A row is written ONLY for a dispatch the recheck definitively allowed. A
  denied recheck records nothing, so a rejection leaves no state and reproduces with no change.
- **A replay never re-grants.** When a key already carries a consumption, the recorded grant is
  replayed verbatim IF the presented reference/recipient/channel still match it; a *different* dispatch
  presented under the same key is a conflict (:class:`DispatchConsumptionConflict`), never silently
  re-homed onto the first grant.

A consumption records that the DCA re-cleared its OWN blockers for exactly one dispatch. It is NOT
recipient consent and NOT a send authorization — the one send pipeline owns and performs the actual
send, which stays a disabled future capability.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import DispatchRecheckConsumption


def reference_fingerprint(reference: str) -> str:
    """A one-way digest of an activation reference token — never the token itself.

    Bound onto the consumption row so a replay under the same key can confirm the SAME reference is
    being re-presented (a different reference is a key-reuse conflict) without ever storing the token.
    """
    return hashlib.sha256((reference or "").encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ConsumptionBinding:
    """The dimensions a replay must still agree on for a recorded grant to be re-served under its key.

    The reference fingerprint pins the exact token; the recipient digest + channel pin the target.
    Provider / campaign revision / versions are provenance carried onto the stored grant, not matched
    (the reference fingerprint already fixes the whole context the grant rested on).
    """

    reference_fingerprint: str
    recipient_digest: str
    channel: str
    contact_point_id: str = ""
    provider: str = ""
    campaign_revision: str = ""
    permission_version: str = ""
    source_version: str = ""
    suppression_version: str = ""


class DispatchConsumptionConflict(Exception):
    """A consume-once key was reused for a DIFFERENT dispatch than the one it first recorded.

    Raised before any new row is written, so a conflict changes nothing. The HTTP layer collapses it to
    a 409: the host must use a fresh key for a genuinely new dispatch rather than overwrite a grant.
    """


class DispatchConsumptionStore:
    """Tenant-scoped reader/writer for the dispatch-slot consumption ledger.

    Bound to a single ``business_id`` (the path tenant of the service-authenticated recheck). Every
    read is filtered to that business and every write is stamped with it, so a consumption can never be
    found or recorded across tenants — the same ``business_id``-scoped discipline the contact-point and
    reference paths already use on these service routes.
    """

    def __init__(self, session: Session, business_id: str) -> None:
        self._session = session
        self._business_id = business_id

    def find(self, idempotency_key: str) -> DispatchRecheckConsumption | None:
        """The recorded grant for this key within this business, or ``None`` if the slot is unconsumed.

        A key another business recorded is invisible here (the query is business-scoped), exactly like
        every other tenant read in the service."""
        return self._session.scalar(
            select(DispatchRecheckConsumption).where(
                DispatchRecheckConsumption.business_id == self._business_id,
                DispatchRecheckConsumption.idempotency_key == idempotency_key,
            )
        )

    @staticmethod
    def matches(row: DispatchRecheckConsumption, binding: ConsumptionBinding) -> bool:
        """Does an existing grant describe the SAME dispatch the caller is now presenting?

        The reference fingerprint + recipient digest + channel must all line up; anything else under
        the same key is a different dispatch and must not ride on the first grant."""
        return (
            row.reference_fingerprint == binding.reference_fingerprint
            and row.recipient_digest == binding.recipient_digest
            and row.channel == binding.channel
        )

    def consume(
        self, idempotency_key: str, binding: ConsumptionBinding, *, consumed_at: datetime
    ) -> tuple[DispatchRecheckConsumption, bool]:
        """Record a grant for this key, or replay the one already recorded. Returns ``(row, replayed)``.

        Fail-closed and exactly-once:

        - An existing grant whose binding matches is **replayed** (``replayed=True``) — no second row,
          no re-grant. A mismatch raises :class:`DispatchConsumptionConflict`.
        - Otherwise a new grant is staged and flushed. If a concurrent consume won the unique-constraint
          race, the :class:`IntegrityError` is caught, this unit of work is rolled back, and the
          winner's row is re-read and replayed — so one dispatch yields one consumption even under
          contention. A surviving mismatch after the race is still a conflict.

        The caller owns the surrounding transaction and commits the returned row with any audit entry in
        the same unit of work (the row is staged + flushed here, never committed)."""
        existing = self.find(idempotency_key)
        if existing is not None:
            if not self.matches(existing, binding):
                raise DispatchConsumptionConflict(idempotency_key)
            return existing, True

        row = DispatchRecheckConsumption(
            business_id=self._business_id,
            idempotency_key=idempotency_key,
            channel=binding.channel,
            contact_point_id=binding.contact_point_id,
            recipient_digest=binding.recipient_digest,
            reference_fingerprint=binding.reference_fingerprint,
            provider=binding.provider,
            campaign_revision=binding.campaign_revision,
            permission_version=binding.permission_version,
            source_version=binding.source_version,
            suppression_version=binding.suppression_version,
            consumed_at=consumed_at,
        )
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError:
            # A concurrent consume of the same dispatch slot won the race. Roll back our duplicate and
            # replay the winner — the slot is consumed exactly once regardless of who got there first.
            self._session.rollback()
            winner = self.find(idempotency_key)
            if winner is None:  # pragma: no cover - the unique violation implies a row now exists
                raise
            if not self.matches(winner, binding):
                raise DispatchConsumptionConflict(idempotency_key) from None
            return winner, True
        return row, False
