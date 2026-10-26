"""DCA-029 — versioned integration-event envelopes.

A single, versioned Pydantic contract (:class:`~app.integration.envelope.IntegrationEventEnvelope`)
for every integration event exchanged over the DCA-027 outbox / DCA-028 inbox messaging path, and
the one sanctioned entry point (:func:`~app.integration.envelope.parse_envelope`) that validates an
inbound event — version gate, opaque/minimized-payload screen, structural check — BEFORE a receiver
touches any state, rejecting a malformed or incompatible event without echoing its payload. This
story defines and validates the contract only; wiring it into the sinks/transport stays a later,
disabled capability.
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

__all__ = [
    "CURRENT_ENVELOPE_VERSION",
    "MAX_PAYLOAD_BYTES",
    "SUPPORTED_ENVELOPE_VERSIONS",
    "EnvelopeError",
    "IntegrationEventEnvelope",
    "MalformedEnvelopeError",
    "UnsupportedEnvelopeVersionError",
    "new_envelope",
    "parse_envelope",
]
