"""Transactional outbox: the writer (DCA-027) and the idempotent publisher (DCA-034).

- :class:`~app.outbox.sink.OutboxSink` (DCA-027) is the single sanctioned *writer*: it stages durable
  integration intents (:class:`~app.db.models.OutboxMessage`) INSIDE the caller's transaction, so an
  outbox intent can never be committed apart from the local change it accompanies — nor survive its
  rollback.
- :class:`~app.outbox.publisher.OutboxPublisher` (DCA-034) is the *relay*: it drains PENDING messages
  and delivers each through the DCA-032 platform transport keyed by the message's STABLE event id
  (the Idempotency-Key), so a lost acknowledgment is retried as the SAME logical event — never a
  fresh key. It owns its own unit of work and only a 2xx acknowledgment marks a row DELIVERED. It is
  config-gated OFF and rides the one hardened platform transport (no new send path).
"""

from app.db.models import OutboxStatus, outbox_event_id
from app.outbox.publisher import (
    OutboxPublisher,
    OutboxPublisherDisabled,
    PublishOutcome,
    PublishResult,
    build_outbox_publisher,
)
from app.outbox.sink import OutboxPayloadError, OutboxSink

__all__ = [
    "OutboxPayloadError",
    "OutboxPublisher",
    "OutboxPublisherDisabled",
    "OutboxSink",
    "OutboxStatus",
    "PublishOutcome",
    "PublishResult",
    "build_outbox_publisher",
    "outbox_event_id",
]
