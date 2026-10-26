"""DCA-029 — versioned integration-event envelopes.

One capability: a single, versioned Pydantic contract for every integration event this service
exchanges over the DCA-027 outbox / DCA-028 inbox messaging path, plus the one sanctioned entry
point (:func:`parse_envelope`) that validates an inbound event **before** a receiver touches any
state. The envelope is deliberately thin — it carries only what a consumer needs to route, order,
de-duplicate and scope an event:

- ``schema_version`` — the envelope-contract version. Only :data:`SUPPORTED_ENVELOPE_VERSIONS` are
  accepted; an unknown/unsupported version is rejected so a consumer never mis-reads an event minted
  against a contract it does not understand.
- ``event_id`` — the event's own stable id (the outbox's deterministic id / the inbox's external
  natural key); the de-duplication handle.
- ``business_id`` — the tenant scope the event belongs to (never cross-tenant).
- ``aggregate_version`` — the monotonic version of the aggregate that emitted the event, so a
  consumer can order events and detect gaps / stale replays.
- ``occurred_at`` — when the event happened (its logical timestamp).
- ``payload`` — a **minimized** map of OPAQUE references (ids / row keys) only: screened free of
  secrets and contact data by the shared DCA-012 redaction policy, exactly like the audit / outbox /
  inbox sinks. Candidate acceptance is not recipient consent, and no envelope ever carries contact
  data.

Two acceptance guarantees:

- **Rejected before mutation.** :func:`parse_envelope` is a pure function: it validates and raises on
  a malformed or incompatible event without opening a session or writing anything, so a bad event can
  never reach a receiver's unit of work. (A guarded receiver validates first, then mutates.)
- **No sensitive payload echoed.** Every rejection path raises :class:`EnvelopeError` with a SAFE,
  shape-only message — the version, the offending field *locations*, or a size — never the submitted
  values. Pydantic's own ``ValidationError`` (which embeds the raw input) is caught and summarized to
  field ``loc``/``msg`` pairs with the inputs stripped, and payload contents are screened by the
  DCA-012 policy whose errors name the kind of leak, not the value.

This story defines and validates the contract only. It does NOT wire the envelope into the outbox /
inbox sinks or any transport — that remains a later, still-disabled capability.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.audit import AuditPayloadError, assert_opaque_references, assert_opaque_text

# The envelope-contract version this build mints, and the full set it understands. A consumer accepts
# an event ONLY if its ``schema_version`` is in this set; evolving the contract adds a version here
# (and, if breaking, the migration/translation that lets both be understood) — it never silently
# reinterprets an unknown version. Starts as a single frozen version.
CURRENT_ENVELOPE_VERSION = 1
SUPPORTED_ENVELOPE_VERSIONS: frozenset[int] = frozenset({CURRENT_ENVELOPE_VERSION})

# A hard cap on the serialized payload, so "minimized payload" is enforced, not merely intended: the
# envelope carries opaque references, never a document. Measured over a compact JSON encoding; a
# breach is reported by size only (never by echoing the payload).
MAX_PAYLOAD_BYTES = 8192


class EnvelopeError(Exception):
    """An integration event was refused. Base class for every envelope rejection.

    Always raised with a SAFE, shape-only message (version / field locations / size) — never the
    submitted values — so a rejection can be logged and returned without echoing a sensitive payload.
    """


class UnsupportedEnvelopeVersionError(EnvelopeError):
    """The event declares a ``schema_version`` this build does not support (or none at all).

    Raised before any other validation so an incompatible event is turned away first. The message
    names the offending/supported versions only.
    """


class MalformedEnvelopeError(EnvelopeError):
    """The event is structurally invalid, carries a non-opaque payload, or exceeds the size cap.

    The message describes the *shape* of the problem (missing/invalid field locations, a leak kind, a
    size) — never the submitted values.
    """


class IntegrationEventEnvelope(BaseModel):
    """The versioned, minimized contract for one integration event (producer + consumer symmetric).

    Immutable and closed: ``extra="forbid"`` rejects unknown fields, so an event can only carry the
    agreed envelope. Construct via :func:`parse_envelope` for an inbound (untrusted) event — it adds
    the version gate, the opaque-payload screen and the no-echo rejection — or via :func:`new_envelope`
    to mint a current-version event on the producer side.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # The envelope-contract version (defense in depth: ``parse_envelope`` gates this first, but a
    # model built any other way is still held to a supported version).
    schema_version: int
    # The event's own id — the de-dup handle. Opaque (an id / row key), never contact data.
    event_id: str = Field(min_length=1)
    # The tenant scope. Opaque business id.
    business_id: str = Field(min_length=1)
    # The kind of event (a controlled code, e.g. "candidate.accepted"). Opaque.
    event_type: str = Field(min_length=1)
    # Monotonic version of the emitting aggregate, so a consumer can order / detect stale replays.
    aggregate_version: int = Field(ge=1)
    # When the event logically happened.
    occurred_at: datetime
    # Minimized, opaque references only — screened free of secrets / contact data.
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("schema_version")
    @classmethod
    def _version_supported(cls, value: int) -> int:
        # Not reached for a value Pydantic already rejected as non-int; guards the direct-construction
        # path. The message names versions only — nothing sensitive.
        if value not in SUPPORTED_ENVELOPE_VERSIONS:
            raise ValueError(
                f"unsupported schema_version {value}; "
                f"supported={sorted(SUPPORTED_ENVELOPE_VERSIONS)}"
            )
        return value


def _safe_validation_summary(exc: ValidationError) -> str:
    """Summarize a Pydantic ``ValidationError`` to field ``loc``/``msg`` pairs, stripping the raw
    inputs (and any context values) so a malformed submission's values are never echoed."""
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(piece) for piece in err.get("loc", ())) or "(envelope)"
        # ``msg`` describes the expectation ("Field required", "Input should be a valid integer");
        # ``input``/``ctx`` (which may hold the submitted value) are deliberately omitted.
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return "; ".join(parts) or "invalid integration event"


def _assert_payload_minimized(payload: Any) -> dict[str, Any]:
    """Screen the payload: opaque references only, within the size cap. Returns it on success.

    Raises :class:`MalformedEnvelopeError` (never echoing values) if the payload is not a mapping,
    carries a secret / contact value, or exceeds :data:`MAX_PAYLOAD_BYTES`.
    """
    try:
        screened = assert_opaque_references(payload)
    except AuditPayloadError as exc:
        # These messages name the leak KIND / field, not the value — safe to surface.
        raise MalformedEnvelopeError(str(exc)) from exc
    try:
        size = len(json.dumps(screened, separators=(",", ":"), default=str).encode("utf-8"))
    except (TypeError, ValueError) as exc:  # pragma: no cover - opaque screen already enforces JSON
        raise MalformedEnvelopeError("payload is not JSON-serializable") from exc
    if size > MAX_PAYLOAD_BYTES:
        raise MalformedEnvelopeError(
            f"payload exceeds the minimized-size cap ({size} > {MAX_PAYLOAD_BYTES} bytes)"
        )
    return screened


def parse_envelope(raw: Any) -> IntegrationEventEnvelope:
    """Validate one inbound (untrusted) integration event and return the typed envelope.

    The sanctioned entry point for a receiver: it is a pure function — it opens no session and writes
    nothing — so a rejection happens strictly BEFORE any mutation. Order is deliberate:

    1. the event must be a mapping;
    2. the ``schema_version`` gate runs FIRST, so an unknown/unsupported version is turned away before
       any other work (:class:`UnsupportedEnvelopeVersionError`);
    3. free-text ids and the payload are screened for secrets / contact data and the payload size cap
       BEFORE the model is built, so a non-opaque value is rejected with a safe message and never
       flows into a Pydantic error that would embed it;
    4. structural validation builds the model; any Pydantic error is summarized to field locations
       with the submitted inputs stripped (:class:`MalformedEnvelopeError`).

    Every rejection raises an :class:`EnvelopeError` whose message is shape-only — never the payload.
    """
    if not isinstance(raw, dict):
        raise MalformedEnvelopeError("integration event must be a JSON object")

    # (2) Version gate FIRST — an incompatible event is incompatible regardless of the rest.
    version = raw.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise UnsupportedEnvelopeVersionError(
            "integration event schema_version is missing or not an integer; "
            f"supported={sorted(SUPPORTED_ENVELOPE_VERSIONS)}"
        )
    if version not in SUPPORTED_ENVELOPE_VERSIONS:
        raise UnsupportedEnvelopeVersionError(
            f"unsupported integration event schema_version {version}; "
            f"supported={sorted(SUPPORTED_ENVELOPE_VERSIONS)}"
        )

    # (3) Opaque screen on free-text keys + the payload, BEFORE the model is built, so a sensitive
    #     value is refused with a safe message and never reaches a Pydantic error that embeds inputs.
    try:
        for field in ("event_id", "event_type", "business_id"):
            value = raw.get(field)
            if isinstance(value, str):
                assert_opaque_text(field, value)
    except AuditPayloadError as exc:
        raise MalformedEnvelopeError(str(exc)) from exc
    if "payload" in raw and raw["payload"] is not None:
        _assert_payload_minimized(raw["payload"])

    # (4) Structural validation — strip inputs from any Pydantic error before surfacing it.
    try:
        return IntegrationEventEnvelope.model_validate(raw)
    except ValidationError as exc:
        # Re-raised as a safe, no-echo MalformedEnvelopeError (inputs stripped from the summary).
        raise MalformedEnvelopeError(_safe_validation_summary(exc)) from exc


def new_envelope(
    *,
    event_id: str,
    business_id: str,
    event_type: str,
    aggregate_version: int,
    occurred_at: datetime,
    payload: dict[str, Any] | None = None,
) -> IntegrationEventEnvelope:
    """Mint a CURRENT-version envelope on the producer side (payload screened minimized/opaque).

    A convenience so a producer and a consumer share one contract: it stamps
    :data:`CURRENT_ENVELOPE_VERSION`, screens the payload like :func:`parse_envelope`, and raises the
    same safe :class:`MalformedEnvelopeError` on a non-opaque / oversized payload.
    """
    screened = _assert_payload_minimized(payload or {})
    return IntegrationEventEnvelope(
        schema_version=CURRENT_ENVELOPE_VERSION,
        event_id=event_id,
        business_id=business_id,
        event_type=event_type,
        aggregate_version=aggregate_version,
        occurred_at=occurred_at,
        payload=screened,
    )
