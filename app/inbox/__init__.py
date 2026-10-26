"""DCA-028 — inbound-event deduplication (inbox / idempotent receiver).

A single sanctioned writer (:class:`~app.inbox.sink.InboxSink`) that stages a durable
:class:`~app.db.models.InboxReceipt` — keyed by the event's external ``(event_source, event_id)`` —
INSIDE the caller's transaction, so handler completion is transactional: a replay of the same event
is rejected at commit (the local change is applied exactly once) and failed processing rolls back
cleanly (the event can be retried safely). This story records the receipt only; no handler
orchestration or relay is performed (those stay disabled).
"""

from app.db.models import InboxStatus
from app.inbox.sink import InboxPayloadError, InboxSink

__all__ = [
    "InboxPayloadError",
    "InboxSink",
    "InboxStatus",
]
