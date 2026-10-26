"""DCA-027 — the DCA transactional-outbox sink.

One capability: stage a durable :class:`~app.db.models.OutboxMessage` — an integration *intent* to be
relayed later — into the *caller's* session so it is committed in the SAME transaction as the local
state change it accompanies. This story persists the record only; it performs NO remote delivery
(a relay/consumer is a separate, still-disabled story).

Two guarantees mirror the DCA-026 audit sink (and the platform's atomic-audit rule, SEC-001/BR-122):

- **Atomic with the caller.** :meth:`OutboxSink.enqueue` only ``session.add(...)``s the row; it never
  commits, flushes, or opens a transaction of its own. The caller commits the local change and its
  outbox intent together, so a failed unit of work leaves NEITHER — rolling back a local change also
  removes its outbox intent.
- **Opaque payloads only.** Every ``event_type`` / ``dedup_key`` / ``payload`` value is screened by
  the shared DCA-012 redaction policy BEFORE anything is staged; a value carrying an email, a phone
  number, a registered secret, or a sensitive key is refused with :class:`OutboxPayloadError` and no
  row is created. Candidate acceptance is not recipient consent and the outbox holds no contact data.

Stable event ids + uniqueness make intents idempotent: the event id is derived deterministically from
``(business_id, event_type, dedup_key)`` (:func:`~app.db.models.outbox_event_id`), and
``(business_id, event_id)`` is unique — so a duplicate logical intent re-derives the same id and is
rejected by the database rather than silently doubled.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.audit import AuditPayloadError, assert_opaque_references, assert_opaque_text
from app.db.models import OutboxMessage, OutboxStatus, outbox_event_id
from app.identity.tenant_context import TenantContext


class OutboxPayloadError(Exception):
    """An outbox payload was refused because it carried raw secret or contact data.

    Raised BEFORE the session is touched, so a refused enqueue leaves no row and no partial state.
    Callers must pass opaque references (ids / row keys), never personal data.
    """


class OutboxSink:
    """Stage transactional outbox messages into one caller-owned session.

    Construct it with the active session and bind the business scope per call or once via
    :meth:`for_context`. The sink never opens, commits, flushes, or rolls back a transaction — that
    is the caller's unit of work, which is exactly what makes the outbox intent atomic with the local
    change. It also performs no I/O: persisting the intent is all this story does.
    """

    def __init__(self, session: Session, *, business_id: str | None = None) -> None:
        self._session = session
        self._business_id = business_id

    @classmethod
    def for_context(cls, session: Session, context: TenantContext) -> OutboxSink:
        """Bind the sink to a verified tenant context: scope = the context's business. The scope is
        taken from the context, never from a caller argument, so an intent can never be attributed to
        a different tenant."""
        return cls(session, business_id=context.business_id)

    def enqueue(
        self,
        *,
        event_type: str,
        dedup_key: str,
        payload: dict | None = None,
        version: int = 1,
        business_id: str | None = None,
    ) -> OutboxMessage:
        """Stage one PENDING outbox message into the caller's session and return the (uncommitted) row.

        ``event_type`` is a required controlled code (e.g. ``"candidate.accepted"``); ``dedup_key`` is
        the caller's logical-intent key (e.g. a row id + action). Both, and every ``payload`` value,
        must be opaque (screened free of secrets/contact data). The stable ``event_id`` is derived
        from ``(business, event_type, dedup_key)`` and ``(business_id, event_id)`` is unique, so the
        same logical intent cannot be staged twice. A missing scope/type/key is a programming error
        (``ValueError``) — the sink is fail-closed and will not invent one.
        """
        effective_business_id = business_id or self._business_id
        if not event_type or not event_type.strip():
            raise ValueError("outbox event_type is required")
        if not dedup_key or not dedup_key.strip():
            raise ValueError("outbox dedup_key is required")
        if not effective_business_id:
            raise ValueError("outbox message requires a business scope")
        if version < 1:
            raise ValueError("outbox version must be a positive integer")

        event_type = event_type.strip()
        dedup_key = dedup_key.strip()

        # Screen BEFORE touching the session: a refused payload stages no row. Reuse the single
        # DCA-012 redaction policy (via the audit screens) and surface an outbox-specific error.
        try:
            assert_opaque_text("event_type", event_type)
            assert_opaque_text("dedup_key", dedup_key)
            screened = assert_opaque_references(payload)
        except AuditPayloadError as exc:
            raise OutboxPayloadError(str(exc)) from exc

        message = OutboxMessage(
            business_id=effective_business_id,
            event_id=outbox_event_id(effective_business_id, event_type, dedup_key),
            event_type=event_type,
            dedup_key=dedup_key,
            version=version,
            payload=dict(screened),
            status=OutboxStatus.PENDING.value,
        )
        self._session.add(message)
        return message

    def mark_delivered(
        self, message: OutboxMessage, *, at: datetime | None = None
    ) -> OutboxMessage:
        """Stage a PENDING → DELIVERED transition on an already-persisted message (no I/O, no commit).

        Models the lifecycle so a later relay story can reconcile a hand-off inside its own
        transaction. This story never calls it from a send path — there is no send path here."""
        message.status = OutboxStatus.DELIVERED.value
        message.delivered_at = at or datetime.now(UTC)
        return message
