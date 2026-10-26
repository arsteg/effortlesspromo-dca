"""Integration event transport contracts.

- **DCA-029** — a single, versioned Pydantic contract
  (:class:`~app.integration.envelope.IntegrationEventEnvelope`) for every integration event exchanged
  over the DCA-027 outbox / DCA-028 inbox messaging path, and the one sanctioned entry point
  (:func:`~app.integration.envelope.parse_envelope`) that validates an inbound event — version gate,
  opaque/minimized-payload screen, structural check — BEFORE a receiver touches any state.
- **DCA-030** — inbound webhook signature verification
  (:func:`~app.integration.webhook.verify_webhook`): the transport-auth gate a receiver runs FIRST
  (before the envelope parser) to prove the raw bytes came from the platform and were not replayed or
  tampered with — HMAC signature + timestamp tolerance + key-rotation overlap.

Both define/validate contracts only; wiring them into the sinks and a live inbound route stays a
later, still-disabled capability.
"""

from app.integration.envelope import (
    CURRENT_ENVELOPE_VERSION,
    MAX_PAYLOAD_BYTES,
    SUPPORTED_ENVELOPE_VERSIONS,
    EnvelopeError,
    IntegrationEventEnvelope,
    MalformedEnvelopeError,
    UnsupportedEnvelopeVersionError,
    new_envelope,
    parse_envelope,
)
from app.integration.webhook import (
    DEFAULT_TIMESTAMP_TOLERANCE_SECONDS,
    SIGNATURE_VERSION,
    MalformedSignatureError,
    SignatureMismatchError,
    StaleWebhookError,
    VerifiedWebhook,
    WebhookAuthError,
    WebhookKey,
    WebhookKeyring,
    WebhookNotConfiguredError,
    sign_body,
    verify_webhook,
)

__all__ = [
    "CURRENT_ENVELOPE_VERSION",
    "DEFAULT_TIMESTAMP_TOLERANCE_SECONDS",
    "MAX_PAYLOAD_BYTES",
    "SIGNATURE_VERSION",
    "SUPPORTED_ENVELOPE_VERSIONS",
    "EnvelopeError",
    "IntegrationEventEnvelope",
    "MalformedEnvelopeError",
    "MalformedSignatureError",
    "SignatureMismatchError",
    "StaleWebhookError",
    "UnsupportedEnvelopeVersionError",
    "VerifiedWebhook",
    "WebhookAuthError",
    "WebhookKey",
    "WebhookKeyring",
    "WebhookNotConfiguredError",
    "new_envelope",
    "parse_envelope",
    "sign_body",
    "verify_webhook",
]
