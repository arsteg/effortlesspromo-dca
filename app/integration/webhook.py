"""DCA-030 — inbound webhook signature verification (transport authentication).

One capability, layered **under** the DCA-029 envelope: before a receiver ever parses an inbound
integration event, it must first prove the event actually came from the platform and was not replayed
or tampered with in transit. This module is the single, sanctioned transport-auth entry point
(:func:`verify_webhook`) that a future inbound route will call *first* — it authenticates the raw
request bytes, not any tenant (per DCA-001 B3 the service-level gate never authorizes a business).

Three properties, exactly as the story asks:

- **Signature verification.** The producer signs the raw body with an HMAC-SHA256 keyed by a shared
  secret; the consumer recomputes it and compares in constant time. A single byte flipped in the body
  (or a wrong/absent signature) fails — the event never reaches the envelope parser.
- **Timestamp tolerance.** The signature covers a producer timestamp, and the consumer rejects any
  event whose timestamp is outside a configured tolerance window, so a validly-signed message captured
  off the wire cannot be replayed later. Staleness is checked **before** the signature so an expired
  event is turned away regardless of how it was signed.
- **Key rotation.** Verification runs against a :class:`WebhookKeyring` of one-or-more active keys, so
  during a rotation **overlap** both the outgoing (old) and incoming (new) key verify. Rotation is:
  add the new key as current, keep the old key in the ring until every in-flight producer has moved,
  then drop it — never a flag-day cutover that would drop a legitimately-signed event.

Boundary: this story adds signature verification + timestamp tolerance + key rotation to the inbound
event transport and nothing else. It defines **no** event-specific business behavior, writes nothing,
opens no session, performs no I/O, and is wired into **no** live route yet — the inbound transport
itself remains a later, still-disabled capability (``webhook_verification_enabled`` is OFF by default
and :meth:`WebhookKeyring.from_settings` returns ``None`` when unconfigured, so the gate fails closed).

Like the rest of the service, every rejection carries a SAFE, shape-only message: it never echoes the
request body, the timestamp, a signature, or (above all) a signing secret.
"""

from __future__ import annotations

import hmac
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - type-only import, avoids a config<-integration runtime cycle
    from app.core.config import Settings

# The signature scheme version this build mints and understands. Baked into the signed content so a
# future scheme change (e.g. a different digest) is a new label, never a silent reinterpretation.
SIGNATURE_VERSION = "v1"

# Default clock-skew / replay window, in seconds. A webhook whose timestamp is further than this from
# the verifier's clock (in either direction) is refused. Mirrors the common 5-minute industry default;
# overridable via settings. Kept deliberately tight — this is a replay guard, not a delivery SLA.
DEFAULT_TIMESTAMP_TOLERANCE_SECONDS = 300

# Hard caps so a hostile header can never balloon work or memory before it is rejected.
_MAX_HEADER_CHARS = 4096
_MAX_SIGNATURES = 16


class WebhookAuthError(Exception):
    """An inbound webhook failed transport authentication. Base class for every rejection.

    Always raised with a SAFE, shape-only message — never the body, timestamp, signature or secret —
    so a rejection can be logged and returned without leaking request content or a signing key.
    """


class WebhookNotConfiguredError(WebhookAuthError):
    """No signing key is configured, so verification fails closed (an empty keyring trusts nothing).

    Raised instead of silently accepting, mirroring the service-auth / token / credential gates: a
    missing contract is never served open.
    """


class MalformedSignatureError(WebhookAuthError):
    """The signature header is missing, unparseable, or carries no usable timestamp/signature.

    The message describes the *shape* of the problem (missing field, bad timestamp) — never the header
    contents.
    """


class StaleWebhookError(WebhookAuthError):
    """The webhook timestamp is outside the tolerance window (too old or too far in the future).

    Checked before the signature so a validly-signed but expired/replayed event is refused regardless
    of its signature. The message names the tolerance (a bound), never the submitted timestamp.
    """


class SignatureMismatchError(WebhookAuthError):
    """No active key reproduced the presented signature — the body was tampered with, the signature is
    forged, or it was signed by a key no longer in the rotation overlap. Generic by design: it never
    reveals which key (if any) was closest, nor echoes the body/signature."""


@dataclass(frozen=True)
class WebhookKey:
    """One signing key: a stable, non-sensitive ``key_id`` plus the secret VALUE (injected
    out-of-band, never logged). ``key_id`` lets an operator reason about a rotation; it is a hint only
    and is never trusted to *select* the key — verification tries every active key regardless."""

    key_id: str
    secret: str

    def __post_init__(self) -> None:
        if not self.key_id.strip():
            raise ValueError("webhook key_id is required")
        if not self.secret:
            raise ValueError("webhook signing secret is required")


@dataclass(frozen=True)
class VerifiedWebhook:
    """The result of a successful verification: which active key matched and the (trusted, now-proven)
    producer timestamp. Carries nothing sensitive — safe to log / audit."""

    key_id: str
    timestamp: datetime


class WebhookKeyring:
    """An ordered set of active signing keys. The first key is *current* (used to sign); every key in
    the ring is accepted on verify, which is exactly what lets an old and a new key both work during a
    rotation overlap. A key id must be unique within the ring. An empty ring is not constructible —
    callers get a disabled (``None``) keyring from :meth:`from_settings` and fail closed instead.
    """

    def __init__(self, keys: Sequence[WebhookKey]) -> None:
        if not keys:
            raise ValueError("a webhook keyring requires at least one key")
        seen: set[str] = set()
        for key in keys:
            if key.key_id in seen:
                raise ValueError(f"duplicate webhook key_id '{key.key_id}' in the keyring")
            seen.add(key.key_id)
        self._keys: tuple[WebhookKey, ...] = tuple(keys)

    @property
    def current(self) -> WebhookKey:
        """The key new outbound signatures are minted with (the first / newest key)."""
        return self._keys[0]

    @property
    def keys(self) -> tuple[WebhookKey, ...]:
        return self._keys

    @classmethod
    def from_settings(cls, settings: Settings) -> WebhookKeyring | None:
        """Build the keyring from configuration, or ``None`` when webhook verification is disabled.

        Fail-closed: returns ``None`` unless ``webhook_verification_enabled`` is set AND at least a
        current signing secret is present — so an unconfigured or half-configured deployment never
        verifies open. The current key comes first; an optional *previous* key (kept through a
        rotation overlap) follows, so a message signed by either verifies until the previous key is
        removed from config. Secrets are VALUES injected out-of-band; config only names which key is
        current (``webhook_current_key_id`` / ``webhook_previous_key_id``).
        """
        if not settings.webhook_verification_enabled:
            return None
        current_secret = (settings.webhook_signing_secret or "").strip()
        if not current_secret:
            return None
        keys = [
            WebhookKey(
                key_id=(settings.webhook_current_key_id or "current").strip() or "current",
                secret=current_secret,
            )
        ]
        previous_secret = (settings.webhook_previous_signing_secret or "").strip()
        if previous_secret:
            keys.append(
                WebhookKey(
                    key_id=(settings.webhook_previous_key_id or "previous").strip() or "previous",
                    secret=previous_secret,
                )
            )
        return cls(keys)


def _signed_content(timestamp: int, body: bytes) -> bytes:
    """The canonical bytes an HMAC is computed over: ``<unix_ts>.<raw_body>``.

    Binding the timestamp into the signed content is what makes the replay window tamper-proof: an
    attacker cannot change the timestamp without invalidating the signature, so the staleness check
    cannot be bypassed by rewriting the ``t`` field.
    """
    return f"{timestamp}.".encode() + body


def _expected_signature(key: WebhookKey, timestamp: int, body: bytes) -> str:
    return hmac.new(
        key.secret.encode("utf-8"), _signed_content(timestamp, body), sha256
    ).hexdigest()


def sign_body(keyring: WebhookKeyring, body: bytes, *, timestamp: datetime) -> str:
    """Produce the signature header value for ``body`` using the keyring's CURRENT key.

    The producer-side counterpart of :func:`verify_webhook`, so a test (and a future sender) share one
    scheme. The header is a compact, order-independent list of ``name=value`` pairs::

        t=<unix_ts>,k=<key_id>,v1=<hex_hmac_sha256>

    ``k`` records which key signed (an operator hint); ``t`` is the signed timestamp; ``v1`` is the
    hex HMAC over ``<t>.<body>``.
    """
    ts = int(timestamp.timestamp())
    key = keyring.current
    sig = _expected_signature(key, ts, body)
    return f"t={ts},k={key.key_id},{SIGNATURE_VERSION}={sig}"


def _parse_header(header: str) -> tuple[int, list[str]]:
    """Parse a signature header into ``(timestamp, [signatures])``.

    Tolerant of ordering, whitespace and extra fields (e.g. the ``k`` key-id hint). Raises
    :class:`MalformedSignatureError` — shape-only — if the timestamp is missing/non-integer or no
    ``v1`` signature is present. The header length is capped first so a hostile value cannot force
    unbounded parsing.
    """
    if not header or not header.strip():
        raise MalformedSignatureError("missing webhook signature header")
    if len(header) > _MAX_HEADER_CHARS:
        raise MalformedSignatureError("webhook signature header exceeds the maximum length")

    timestamp: int | None = None
    signatures: list[str] = []
    for part in header.split(","):
        name, sep, value = part.strip().partition("=")
        if not sep:
            continue  # ignore a malformed fragment rather than echo it
        name = name.strip()
        value = value.strip()
        if name == "t":
            try:
                timestamp = int(value)
            except ValueError as exc:
                raise MalformedSignatureError(
                    "webhook signature timestamp is not an integer"
                ) from exc
        elif name == SIGNATURE_VERSION and value:
            if len(signatures) >= _MAX_SIGNATURES:
                raise MalformedSignatureError(
                    "webhook signature header carries too many signatures"
                )
            signatures.append(value)
        # Any other field (e.g. the `k` hint, or a future scheme version) is ignored here.

    if timestamp is None:
        raise MalformedSignatureError("webhook signature header is missing the timestamp (t)")
    if not signatures:
        raise MalformedSignatureError(
            f"webhook signature header carries no {SIGNATURE_VERSION} signature"
        )
    return timestamp, signatures


def _assert_fresh(timestamp: int, now: datetime, tolerance_seconds: int) -> None:
    """Reject a timestamp outside the ±tolerance window. Names the tolerance only, never the value."""
    if tolerance_seconds < 0:
        raise ValueError("webhook timestamp tolerance must be non-negative")
    skew = abs(int(now.timestamp()) - timestamp)
    if skew > tolerance_seconds:
        raise StaleWebhookError(
            "webhook timestamp is outside the allowed tolerance "
            f"of {tolerance_seconds}s (replay/skew guard)"
        )


def _matches_any_key(keys: Iterable[WebhookKey], timestamp: int, body: bytes, provided: list[str]):
    """Return the first key whose expected signature equals one of the ``provided`` signatures, or
    ``None``. Every comparison is constant-time (``hmac.compare_digest``); we check all keys so a
    rotation overlap accepts either the old or the new key's signature."""
    for key in keys:
        expected = _expected_signature(key, timestamp, body)
        for candidate in provided:
            if hmac.compare_digest(expected, candidate):
                return key
    return None


def verify_webhook(
    *,
    keyring: WebhookKeyring | None,
    header: str | None,
    body: bytes,
    now: datetime | None = None,
    tolerance_seconds: int = DEFAULT_TIMESTAMP_TOLERANCE_SECONDS,
) -> VerifiedWebhook:
    """Authenticate one inbound webhook's raw ``body`` against its signature ``header``.

    The sanctioned transport-auth entry point: a receiver calls this FIRST, before it parses the
    DCA-029 envelope or touches any state. It is a pure function — no session, no write, no I/O — so a
    rejection happens strictly before anything is mutated. Order is deliberate:

    1. **fail-closed** if no keyring is configured (``None`` ⇒ verification disabled);
    2. parse the header (missing/garbled ⇒ :class:`MalformedSignatureError`);
    3. **timestamp tolerance** (stale/future ⇒ :class:`StaleWebhookError`) — checked BEFORE the
       signature so a validly-signed but expired/replayed event is still refused;
    4. **signature** against every active key (none match ⇒ :class:`SignatureMismatchError`), so a
       tampered body is rejected and a rotation overlap accepts either the old or the new key.

    Returns a :class:`VerifiedWebhook` (the matched key id + the proven timestamp) on success. Every
    rejection raises a :class:`WebhookAuthError` whose message is shape-only — never the body,
    timestamp, signature or secret.
    """
    if keyring is None:
        raise WebhookNotConfiguredError("webhook verification is not configured")

    timestamp, signatures = _parse_header(header or "")
    _assert_fresh(timestamp, now or datetime.now(UTC), tolerance_seconds)

    matched = _matches_any_key(keyring.keys, timestamp, body, signatures)
    if matched is None:
        raise SignatureMismatchError("webhook signature does not match any active signing key")

    return VerifiedWebhook(
        key_id=matched.key_id,
        timestamp=datetime.fromtimestamp(timestamp, tz=UTC),
    )
