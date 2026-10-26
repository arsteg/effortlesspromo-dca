"""DCA-034 — the transactional-outbox *publisher* (idempotent relay of staged intents).

One capability: drain the PENDING :class:`~app.db.models.OutboxMessage` rows the DCA-027 sink staged
and hand each to the .NET platform through the DCA-032 :class:`~app.clients.platform_transport.
PlatformHttpTransport`, keyed by the message's **stable** event id so a redelivery is the SAME
logical event — never a fresh one. It is the relay mechanism DCA-027 deferred ("a relay/consumer is
a separate, still-disabled story"); it remains **disabled by default** and adds no new send path of
its own — it only rides the one hardened, allowlisted platform transport.

The headline guarantee (the DCA-034 acceptance criterion):

- **A lost acknowledgment preserves a retryable event with the SAME identity.** Each message is
  delivered with its stable ``event_id`` as the ``Idempotency-Key`` (:func:`~app.db.models.
  outbox_event_id`, derived from ``(business_id, event_type, dedup_key)``). If the acknowledgment is
  lost — a transport timeout, an unreachable host, or a retryable 5xx/429 — the row is left exactly
  as it was: ``PENDING``, ``delivered_at`` unset, the **same** ``event_id``. Nothing is deleted,
  re-minted, or given a new key, so the next pass redelivers the identical logical event under the
  identical idempotency key and the platform records it once. The publisher never invents a new
  event on failure.

Acknowledgment tracking is deliberately minimal and durable: only a **2xx** response transitions the
row ``PENDING → DELIVERED`` (stamping ``delivered_at``), committed in the publisher's own unit of
work. Any non-success outcome leaves the row untouched (no state change) and is reported back in a
:class:`PublishResult` so a caller/operator can see *why* without a row ever lying about its state.

Boundary (same stance as the DCA-032 transport it rides): no provider SDKs/keys, no model routing,
no second AI gateway, and no writes to another service's database. The publisher is config-gated OFF
(:meth:`Settings.outbox_publisher_configured`) and no live relay route is wired here — a missing
relay contract stays disabled (B1). In tests and the manual check it is driven against the DCA-033
fake contract server, so nothing leaves the box. Payloads carry only opaque references (screened at
enqueue by the sink and re-screened here by :func:`~app.integration.envelope.new_envelope`); the
outbox never holds contact data and candidate acceptance is not recipient consent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.clients.platform_transport import (
    PlatformHttpTransport,
    PlatformTransportError,
    ResponseClass,
    TransportErrorKind,
    build_platform_transport,
)
from app.db.models import OutboxMessage, OutboxStatus
from app.integration.envelope import new_envelope

log = logging.getLogger("dca.outbox")


class PublishOutcome(str, Enum):
    """The result of attempting to publish one outbox message — a small, stable classification.

    Exactly one value transitions the row's durable state (``DELIVERED``); every other value leaves
    the row ``PENDING`` and unchanged, differing only in *why* the acknowledgment did not arrive so a
    caller can branch (a ``RETRY_*`` outcome is transient and the next pass will try again; ``DENIED``
    is a definitive rejection by the platform that a retry would not fix).
    """

    DELIVERED = "delivered"  # 2xx acknowledgment — row marked DELIVERED (the only state change)
    RETRY_TIMEOUT = "retry_timeout"  # lost ack: transport timeout — stays PENDING, same identity
    RETRY_UNREACHABLE = "retry_unreachable"  # lost ack: host unreachable — stays PENDING
    RETRY_SERVER_ERROR = "retry_server_error"  # 5xx — retryable, stays PENDING
    RETRY_RATE_LIMITED = "retry_rate_limited"  # 429 — retryable, stays PENDING
    DENIED = (
        "denied"  # definitive 4xx (auth/forbidden/client) — stays PENDING, surfaced not retried
    )


_RETRYABLE = frozenset(
    {
        PublishOutcome.RETRY_TIMEOUT,
        PublishOutcome.RETRY_UNREACHABLE,
        PublishOutcome.RETRY_SERVER_ERROR,
        PublishOutcome.RETRY_RATE_LIMITED,
    }
)


@dataclass(frozen=True)
class PublishResult:
    """What happened to one message in a publish pass. Carries only opaque identifiers + a stable
    classification — never a payload value, a credential, or request detail."""

    event_id: str
    event_type: str
    idempotency_key: str
    outcome: PublishOutcome
    status_code: int | None = None
    response_class: str | None = None
    error_kind: str | None = None

    @property
    def delivered(self) -> bool:
        return self.outcome is PublishOutcome.DELIVERED

    @property
    def retryable(self) -> bool:
        return self.outcome in _RETRYABLE


class OutboxPublisherDisabled(Exception):
    """The outbox publisher is not configured (no platform transport / no relay path) — the whole
    capability is off (fail-closed). Mirrors :class:`~app.clients.platform_transport.
    PlatformTransportDisabled`."""


class OutboxPublisher:
    """Deliver PENDING outbox messages idempotently through the platform transport.

    Construct with the (already-configured, allowlisted) platform transport, the relay path to POST
    an event to, and a session factory over the DCA-owned database. The publisher owns its own unit
    of work for the ``DELIVERED`` transition — it never shares the caller's enqueue transaction — so a
    relay failure can never roll back the business change that staged the intent.
    """

    def __init__(
        self,
        transport: PlatformHttpTransport,
        *,
        publish_path: str,
        session_factory: sessionmaker[Session],
        batch_size: int = 50,
    ) -> None:
        path = (publish_path or "").strip()
        if not path:
            # Fail-closed: a publisher with no relay path could never deliver anything.
            raise OutboxPublisherDisabled("outbox relay path is not configured")
        if batch_size <= 0:
            raise ValueError("outbox publish batch_size must be positive")
        self._transport = transport
        self._path = path
        self._session_factory = session_factory
        self._batch_size = batch_size

    def publish_pending(
        self, business_id: str, *, limit: int | None = None, correlation_id: str | None = None
    ) -> list[PublishResult]:
        """Publish up to ``limit`` PENDING messages for ``business_id``, oldest first.

        Scope is mandatory (never a cross-tenant drain): only rows owned by ``business_id`` are read
        and delivered. Each message is attempted independently — a delivered one is committed before
        the next is tried, so a mid-batch lost acknowledgment never undoes an earlier success. Returns
        one :class:`PublishResult` per attempted message, in order.
        """
        if not (business_id or "").strip():
            raise ValueError("outbox publisher requires a business scope")
        batch = self._batch_size if limit is None else min(self._batch_size, max(1, limit))

        with self._session_factory() as session:
            pending = session.scalars(
                select(OutboxMessage)
                .where(
                    OutboxMessage.business_id == business_id,
                    OutboxMessage.status == OutboxStatus.PENDING.value,
                )
                .order_by(OutboxMessage.created_at, OutboxMessage.id)
                .limit(batch)
            ).all()
            return [self._publish_one(session, message, correlation_id) for message in pending]

    def publish_message(
        self, message_id: str, business_id: str, *, correlation_id: str | None = None
    ) -> PublishResult | None:
        """Publish exactly one message by id within ``business_id``. Returns ``None`` if the message
        does not exist for that business or is not PENDING (nothing is attempted or changed)."""
        if not (business_id or "").strip():
            raise ValueError("outbox publisher requires a business scope")
        with self._session_factory() as session:
            message = session.get(OutboxMessage, message_id)
            if (
                message is None
                or message.business_id != business_id
                or message.status != OutboxStatus.PENDING.value
            ):
                return None
            return self._publish_one(session, message, correlation_id)

    # -- internals ---------------------------------------------------------------------------------

    def _publish_one(
        self, session: Session, message: OutboxMessage, correlation_id: str | None
    ) -> PublishResult:
        """Attempt delivery of one message. Commits a DELIVERED transition on a 2xx acknowledgment;
        on any other outcome leaves the row exactly as it was (PENDING, same stable event id)."""
        # The STABLE event id is the idempotency key: a redelivery carries the identical key, so the
        # platform records one logical event however many times a lost ack makes us retry.
        idem = message.event_id
        envelope = new_envelope(
            event_id=message.event_id,
            business_id=message.business_id,
            event_type=message.event_type,
            aggregate_version=message.version,
            occurred_at=message.created_at or datetime.now(UTC),
            payload=dict(message.payload or {}),
        )

        try:
            result = self._transport.request(
                "POST",
                self._path,
                json=envelope.model_dump(mode="json"),
                idempotency_key=idem,
                correlation_id=correlation_id,
            )
        except PlatformTransportError as exc:
            # A lost acknowledgment: no HTTP response arrived. The row is NOT touched — it stays
            # PENDING with the same event id, so the next pass retries under the same idempotency key.
            outcome = (
                PublishOutcome.RETRY_TIMEOUT
                if exc.kind is TransportErrorKind.TIMEOUT
                else PublishOutcome.RETRY_UNREACHABLE
            )
            session.rollback()  # defensive: no write was staged, but never carry work forward
            log.info(
                "outbox publish deferred (transport %s) event_type=%s",
                exc.kind.value,
                message.event_type,
                extra={"correlation_id": correlation_id},
            )
            return PublishResult(
                event_id=message.event_id,
                event_type=message.event_type,
                idempotency_key=idem,
                outcome=outcome,
                error_kind=exc.kind.value,
            )

        outcome = _classify_response(result.response_class)
        if outcome is PublishOutcome.DELIVERED:
            message.status = OutboxStatus.DELIVERED.value
            message.delivered_at = datetime.now(UTC)
            session.commit()
            log.info(
                "outbox event delivered event_type=%s status=%s",
                message.event_type,
                result.status_code,
                extra={"correlation_id": result.correlation_id},
            )
        else:
            # A response arrived but was not a success: retryable (5xx/429) or a definitive denial
            # (4xx). Either way the row keeps its PENDING state and stable id — no state change.
            session.rollback()
            log.info(
                "outbox publish not acknowledged (%s, http %s) event_type=%s",
                outcome.value,
                result.status_code,
                message.event_type,
                extra={"correlation_id": result.correlation_id},
            )
        return PublishResult(
            event_id=message.event_id,
            event_type=message.event_type,
            idempotency_key=idem,
            outcome=outcome,
            status_code=result.status_code,
            response_class=result.response_class.value,
        )


def _classify_response(response_class: ResponseClass) -> PublishOutcome:
    """Map a transport response class to a publish outcome. Only ``SUCCESS`` delivers; 5xx/429 are
    retryable; every other non-success (auth/forbidden/client) is a definitive denial."""
    if response_class is ResponseClass.SUCCESS:
        return PublishOutcome.DELIVERED
    if response_class is ResponseClass.SERVER_ERROR:
        return PublishOutcome.RETRY_SERVER_ERROR
    if response_class is ResponseClass.RATE_LIMITED:
        return PublishOutcome.RETRY_RATE_LIMITED
    return PublishOutcome.DENIED


def build_outbox_publisher(
    settings: Any,
    session_factory: sessionmaker[Session],
    *,
    transport: httpx.BaseTransport | None = None,
    platform_transport: PlatformHttpTransport | None = None,
) -> OutboxPublisher | None:
    """Build the publisher from settings, or ``None`` when the capability is not configured.

    Returning ``None`` (rather than raising) mirrors :func:`~app.clients.platform_transport.
    build_platform_transport`: the publisher is off-by-default, so an unconfigured deployment simply
    has no publisher and the relay capability stays disabled (B1, fail-closed). ``transport`` is the
    httpx transport seam for tests (e.g. the DCA-033 fake contract server); ``platform_transport``
    lets a caller inject an already-built transport instead.
    """
    if not getattr(settings, "outbox_publisher_configured", False):
        return None
    plat = platform_transport or build_platform_transport(settings, transport=transport)
    if plat is None:  # pragma: no cover - outbox_publisher_configured implies transport configured
        return None
    return OutboxPublisher(
        plat,
        publish_path=settings.platform_outbox_publish_path,
        session_factory=session_factory,
        batch_size=settings.outbox_publish_batch_size,
    )


__all__ = [
    "OutboxPublisher",
    "OutboxPublisherDisabled",
    "PublishOutcome",
    "PublishResult",
    "build_outbox_publisher",
]
