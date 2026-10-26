"""DCA-027 — transactional outbox.

A single sanctioned writer (:class:`~app.outbox.sink.OutboxSink`) that stages durable integration
intents (:class:`~app.db.models.OutboxMessage`) INSIDE the caller's transaction, so an outbox intent
can never be committed apart from the local change it accompanies — nor survive its rollback. This
story persists the record and its ``pending``/``delivered`` status only; it performs no remote
delivery (the relay/consumer is a separate, disabled story).
"""

from app.db.models import OutboxStatus, outbox_event_id
from app.outbox.sink import OutboxPayloadError, OutboxSink

__all__ = [
    "OutboxPayloadError",
    "OutboxSink",
    "OutboxStatus",
    "outbox_event_id",
]
