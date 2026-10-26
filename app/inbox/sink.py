"""DCA-028 — the DCA inbound-event (inbox) sink.

One capability: deduplicate inbound events with transactional handler completion. The sink stages a
durable :class:`~app.db.models.InboxReceipt` — keyed by the event's external ``(event_source,
event_id)`` natural key — into the *caller's* session, so the receipt commits in the SAME transaction
as the handler's local change it accompanies.

Two guarantees mirror the DCA-026 audit sink and the DCA-027 outbox sink (the platform's
atomic-with-the-caller discipline), and together give the headline acceptance criterion:

- **Atomic with the caller.** :meth:`InboxSink.register` only ``session.add(...)``s the receipt; it
  never commits, flushes, or opens a transaction of its own. The caller commits the handler's local
  change and its receipt together, so:

  * a **replay** re-stages the same receipt, the ``(business_id, event_source, event_id)`` uniqueness
    constraint rejects it at commit, and the caller's rollback takes the handler's change with it —
    the local effect is applied **exactly once**; and
  * **failed processing** rolls back both the partial change and the receipt, leaving nothing behind,
    so the event can be **retried safely**.

- **Opaque payloads only.** Every ``event_source`` / ``event_id`` / ``event_type`` and every
  ``payload`` value is screened by the shared DCA-012 redaction policy (via the audit screens) BEFORE
  anything is staged; a value carrying an email, a phone number, a registered secret, or a sensitive
  key is refused with :class:`InboxPayloadError` and no receipt is created.

:meth:`InboxSink.already_processed` is a fast-path dedup check, scoped to the tenant, so a caller can
skip the handler work entirely on a known replay instead of doing the work only to roll it back.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.audit import AuditPayloadError, assert_opaque_references, assert_opaque_text
from app.db.models import InboxReceipt, InboxStatus
from app.identity.tenant_context import TenantContext


class InboxPayloadError(Exception):
    """An inbox receipt was refused because it carried raw secret or contact data.

    Raised BEFORE the session is touched, so a refused register leaves no row and no partial state.
    Callers must pass opaque references (ids / row keys), never personal data.
    """


class InboxSink:
    """Stage inbound-event receipts into one caller-owned session (idempotent receiver).

    Construct it with the active session and bind the business scope per call or once via
    :meth:`for_context`. The sink never opens, commits, flushes, or rolls back a transaction — that
    is the caller's unit of work, which is exactly what makes the receipt atomic with the handler's
    local change. It performs no I/O: recording the receipt is all this story does.
    """

    def __init__(self, session: Session, *, business_id: str | None = None) -> None:
        self._session = session
        self._business_id = business_id

    @classmethod
    def for_context(cls, session: Session, context: TenantContext) -> InboxSink:
        """Bind the sink to a verified tenant context: scope = the context's business. The scope is
        taken from the context, never from a caller argument, so a receipt can never be attributed to
        a different tenant."""
        return cls(session, business_id=context.business_id)

    def _scope(self, business_id: str | None) -> str:
        effective = business_id or self._business_id
        if not effective:
            raise ValueError("inbox receipt requires a business scope")
        return effective

    def already_processed(
        self, *, event_source: str, event_id: str, business_id: str | None = None
    ) -> InboxReceipt | None:
        """Return the prior receipt for this ``(event_source, event_id)`` within the tenant, or
        ``None`` if the event has not been handled yet.

        A read-only fast path: a caller can short-circuit a replay (skip the handler work entirely)
        rather than staging a duplicate receipt and relying on the commit-time constraint to reject
        it. The lookup is confined to the active business, so another tenant's receipt never matches.
        """
        effective = self._scope(business_id)
        source = (event_source or "").strip()
        ext_id = (event_id or "").strip()
        if not source or not ext_id:
            raise ValueError("inbox lookup requires event_source and event_id")
        return (
            self._session.query(InboxReceipt)
            .filter(
                InboxReceipt.business_id == effective,
                InboxReceipt.event_source == source,
                InboxReceipt.event_id == ext_id,
            )
            .one_or_none()
        )

    def register(
        self,
        *,
        event_source: str,
        event_id: str,
        event_type: str = "",
        payload: dict | None = None,
        business_id: str | None = None,
        at: datetime | None = None,
    ) -> InboxReceipt:
        """Stage one PROCESSED receipt into the caller's session and return the (uncommitted) row.

        ``event_source`` and ``event_id`` are the inbound event's external natural key (both
        required). ``event_type`` is an optional controlled code. Every value, and every ``payload``
        value, must be opaque (screened free of secrets/contact data). The receipt is created
        PROCESSED because completion is transactional: the caller commits it in the SAME unit of work
        as the handler's local change. ``(business_id, event_source, event_id)`` is unique, so a
        replay's receipt is rejected at commit and the handler's change rolls back with it. A missing
        scope/source/id is a programming error (``ValueError``) — the sink is fail-closed.
        """
        effective = self._scope(business_id)
        source = (event_source or "").strip()
        ext_id = (event_id or "").strip()
        event_type = (event_type or "").strip()
        if not source:
            raise ValueError("inbox event_source is required")
        if not ext_id:
            raise ValueError("inbox event_id is required")

        # Screen BEFORE touching the session: a refused payload stages no row. Reuse the single
        # DCA-012 redaction policy (via the audit screens) and surface an inbox-specific error.
        try:
            assert_opaque_text("event_source", source)
            assert_opaque_text("event_id", ext_id)
            if event_type:
                assert_opaque_text("event_type", event_type)
            screened = assert_opaque_references(payload)
        except AuditPayloadError as exc:
            raise InboxPayloadError(str(exc)) from exc

        receipt = InboxReceipt(
            business_id=effective,
            event_source=source,
            event_id=ext_id,
            event_type=event_type,
            status=InboxStatus.PROCESSED.value,
            payload=dict(screened),
            processed_at=at or datetime.now(UTC),
        )
        self._session.add(receipt)
        return receipt

    def mark_processed(self, receipt: InboxReceipt, *, at: datetime | None = None) -> InboxReceipt:
        """Stage a PENDING → PROCESSED transition on an already-persisted receipt (no I/O, no commit).

        Models the lifecycle so a future at-least-once claim/lease capability can complete a
        previously-claimed receipt inside its own transaction. This story never creates a PENDING
        receipt, so no path drives it here."""
        receipt.status = InboxStatus.PROCESSED.value
        receipt.processed_at = at or datetime.now(UTC)
        return receipt
