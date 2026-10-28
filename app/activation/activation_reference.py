"""DCA-094 — the self-contained, signed, short-lived activation reference.

Key: ``activation-reference`` · source sections 5, 10. Stage: India channel eligibility.

The DCA-093 eligibility endpoint answers a *binary, expiring* question — do the DCA's compliance
gates clear THIS business's contact point for a channel right now? This module turns a *definitive
ELIGIBLE* answer into a portable, tamper-resistant PROOF of that answer: a signed activation
reference. It is a pure, deterministic, network-free mint/verify pair (the exact shape of the DCA-076
withdrawal token), so a reference carries its own authority and is validated with nothing but the
signing secret — no server-side token store, no table, no migration.

The load-bearing properties (the DCA-094 acceptance criteria):

- **It binds the WHOLE activation context.** The signed payload fixes every dimension that makes an
  eligibility answer meaningful: the ``business_id`` (tenant), the ``recipient`` (contact-point id +
  a one-way digest of the value — never the raw value), the ``channel``, the ``purpose``, the sending
  ``provider``, the ``campaign_revision``, and the ``permission_version`` + ``source_version`` the
  decision rested on. Change ANY of them and the HMAC no longer matches — a reference can never be
  re-pointed at another tenant, recipient, channel or campaign revision.
- **It is tamper-resistant.** HMAC-SHA256 over the version tag AND the payload, compared in constant
  time (:func:`hmac.compare_digest`). An edited payload fails :data:`REASON_BAD_SIGNATURE`.
- **It is short-lived.** The payload carries ``iat``/``exp`` epoch seconds; past ``exp`` it is refused
  (:data:`REASON_EXPIRED`). A reference is a snapshot, never standing permission.
- **Verification can re-assert the intended context.** A consumer that is about to act names the
  context it believes it is acting on (``expected``); any dimension that disagrees with the signed
  reference is refused (:data:`REASON_BINDING_MISMATCH`, the offending dimension in ``detail``). So a
  caller that lies about the recipient or channel is rejected even if the token itself is authentic.
- **No secret ⇒ disabled.** An empty secret refuses every reference (:data:`REASON_NO_SECRET`) and
  the mint refuses to produce one — a missing contract stays disabled, never fail-open.

A valid reference proves the DCA's OWN blockers were clear at ``iat`` for exactly this context, and
nothing more. It is NOT recipient consent and NOT a send authorization: the one send pipeline owns and
re-gates the actual send, which stays a disabled future capability.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime

# The only reference version this build mints/accepts. An unknown version is refused rather than
# guessed, so a future format change can never be silently mis-read.
REFERENCE_VERSION = "dcaar1"

# Stable refusal codes (mirrors the service/token convention). Each means the reference confers no
# authority — it is rejected before any state is read or touched.
REASON_NO_SECRET = "activation_reference_no_secret"
REASON_INVALID_FORMAT = "activation_reference_invalid_format"
REASON_UNSUPPORTED_VERSION = "activation_reference_unsupported_version"
REASON_BAD_SIGNATURE = "activation_reference_bad_signature"
REASON_MALFORMED_PAYLOAD = "activation_reference_malformed_payload"
REASON_EXPIRED = "activation_reference_expired"
REASON_BINDING_MISMATCH = "activation_reference_binding_mismatch"


class ActivationReferenceError(Exception):
    """An activation reference was rejected with a stable ``code``. Raised before anything is acted
    on, so a rejected reference confers no authority and changes nothing. ``detail`` names the
    offending dimension on a binding mismatch (never a raw recipient value)."""

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


def recipient_digest(value: str) -> str:
    """A one-way, non-reversible digest of a recipient contact value — never the raw value.

    The reference binds this digest (not the address) so a recipient tamper is detectable while the
    reference stays safe to log or relay. Returns ``""`` for an empty value (the mint rejects that)."""
    token = (value or "").strip().lower()
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _norm(value: str) -> str:
    """Lower-case + trim a scope dimension so a reference lines up with normalized decision inputs."""
    return (value or "").strip().lower()


@dataclass(frozen=True)
class ActivationBinding:
    """The fully-qualified activation context a reference is bound to.

    Every field is part of the signature. ``recipient_digest`` is the one-way digest of the contact
    value (use :func:`recipient_digest`); the raw value never enters the token. Scope dimensions are
    normalized on construction so an ``expected`` binding compares equal to a minted one regardless of
    the caller's casing/whitespace.
    """

    business_id: str
    contact_point_id: str
    recipient_digest: str
    channel: str
    purpose: str
    provider: str
    campaign_revision: str
    permission_version: str
    source_version: str

    def normalized(self) -> ActivationBinding:
        """Return a copy with every dimension normalized the way the mint/verify path stores them."""
        return ActivationBinding(
            business_id=(self.business_id or "").strip(),
            contact_point_id=(self.contact_point_id or "").strip(),
            recipient_digest=(self.recipient_digest or "").strip().lower(),
            channel=_norm(self.channel),
            purpose=_norm(self.purpose),
            provider=_norm(self.provider),
            campaign_revision=(self.campaign_revision or "").strip(),
            permission_version=(self.permission_version or "").strip(),
            source_version=(self.source_version or "").strip(),
        )

    def _require_complete(self) -> ActivationBinding:
        """Normalize and assert every dimension is present — an under-specified reference is a
        programming error, never silently minted with a blank binding dimension."""
        norm = self.normalized()
        missing = [
            name
            for name, value in (
                ("business_id", norm.business_id),
                ("contact_point_id", norm.contact_point_id),
                ("recipient_digest", norm.recipient_digest),
                ("channel", norm.channel),
                ("purpose", norm.purpose),
                ("provider", norm.provider),
                ("campaign_revision", norm.campaign_revision),
                ("permission_version", norm.permission_version),
                ("source_version", norm.source_version),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                "an activation reference must bind every dimension; missing: " + ", ".join(missing)
            )
        return norm


# The payload keys, paired with the binding attribute each carries. Kept in one place so the mint,
# the verify, and the mismatch check can never drift apart.
_BINDING_FIELDS: tuple[tuple[str, str], ...] = (
    ("biz", "business_id"),
    ("cp", "contact_point_id"),
    ("rcp", "recipient_digest"),
    ("ch", "channel"),
    ("pur", "purpose"),
    ("prov", "provider"),
    ("crev", "campaign_revision"),
    ("pver", "permission_version"),
    ("sver", "source_version"),
)


@dataclass(frozen=True)
class ActivationReferenceClaims:
    """A verified reference's bound context plus its validity window. Normalized, so it lines up with
    the eligibility decision inputs it was minted from."""

    binding: ActivationBinding
    issued_at: datetime
    expires_at: datetime


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(secret: str, signing_input: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256
    ).digest()
    return _b64url_encode(digest)


def issue_activation_reference(
    secret: str,
    *,
    binding: ActivationBinding,
    issued_at: datetime,
    expires_at: datetime,
) -> str:
    """Mint a signed activation reference binding exactly ONE activation context (pure, deterministic).

    The payload carries the normalized binding + ``iat``/``exp`` epoch seconds; the returned reference
    is ``<version>.<payload_b64url>.<sig_b64url>``. Same inputs + same secret always produce the same
    reference (no randomness), so it is fully reproducible. A missing secret, an incomplete binding, or
    a non-positive validity window is a programming error (``ValueError``) — the mint never produces an
    unsigned, under-bound, or already-expired reference.
    """
    if not (secret or "").strip():
        raise ValueError("a signing secret is required to mint an activation reference")
    if expires_at <= issued_at:
        raise ValueError("an activation reference must expire strictly after it is issued")

    norm = binding._require_complete()
    payload: dict[str, object] = {"v": REFERENCE_VERSION}
    for key, attr in _BINDING_FIELDS:
        payload[key] = getattr(norm, attr)
    payload["iat"] = int(issued_at.timestamp())
    payload["exp"] = int(expires_at.timestamp())

    payload_b64 = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    )
    signing_input = f"{REFERENCE_VERSION}.{payload_b64}"
    return f"{signing_input}.{_sign(secret, signing_input)}"


def verify_activation_reference(
    secret: str,
    reference: str,
    *,
    now: datetime | None = None,
    expected: ActivationBinding | None = None,
) -> ActivationReferenceClaims:
    """Validate an activation reference and return its bound context, or raise.

    Fail-closed at every step: an empty secret refuses all references (:data:`REASON_NO_SECRET`); a
    reference that is malformed, carries an unknown version, fails the constant-time signature check,
    has an unreadable payload, or is expired is refused with the matching stable code and confers no
    authority. The signature covers the version AND the whole binding, so a tampered tenant / recipient
    / channel / campaign revision / version is rejected — a reference can never be re-pointed.

    When ``expected`` is supplied (the context a consumer is about to act on), each NON-EMPTY dimension
    on it is compared against the signed binding AFTER signature + expiry pass; any disagreement is
    refused with :data:`REASON_BINDING_MISMATCH` and the offending dimension in ``detail``. A dimension
    left empty on ``expected`` is simply not asserted. So a caller that presents an authentic reference
    but lies about the recipient or channel it intends to use is still rejected.
    """
    moment = now or datetime.now(UTC)
    if not (secret or "").strip():
        raise ActivationReferenceError(
            REASON_NO_SECRET, "activation references are disabled: no signing secret is configured"
        )

    parts = (reference or "").split(".")
    if len(parts) != 3 or not all(parts):
        raise ActivationReferenceError(REASON_INVALID_FORMAT, "malformed activation reference")
    version, payload_b64, provided_sig = parts
    if version != REFERENCE_VERSION:
        raise ActivationReferenceError(
            REASON_UNSUPPORTED_VERSION, f"unsupported activation reference version '{version}'"
        )

    expected_sig = _sign(secret, f"{version}.{payload_b64}")
    if not hmac.compare_digest(expected_sig, provided_sig):
        raise ActivationReferenceError(
            REASON_BAD_SIGNATURE, "activation reference signature is invalid"
        )

    try:
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        binding = ActivationBinding(
            **{attr: str(payload[key]) for key, attr in _BINDING_FIELDS}
        ).normalized()
        issued_at = datetime.fromtimestamp(int(payload["iat"]), tz=UTC)
        expires_at = datetime.fromtimestamp(int(payload["exp"]), tz=UTC)
    except (ValueError, KeyError, TypeError) as exc:
        raise ActivationReferenceError(
            REASON_MALFORMED_PAYLOAD, "activation reference payload is unreadable"
        ) from exc

    if any(not getattr(binding, attr) for _, attr in _BINDING_FIELDS):
        raise ActivationReferenceError(
            REASON_MALFORMED_PAYLOAD, "activation reference payload is missing a bound dimension"
        )

    if moment >= expires_at:
        raise ActivationReferenceError(REASON_EXPIRED, "activation reference has expired")

    if expected is not None:
        want = expected.normalized()
        for _, attr in _BINDING_FIELDS:
            want_val = getattr(want, attr)
            if want_val and want_val != getattr(binding, attr):
                raise ActivationReferenceError(
                    REASON_BINDING_MISMATCH,
                    f"activation reference does not match the intended {attr}",
                    detail=attr,
                )

    return ActivationReferenceClaims(binding=binding, issued_at=issued_at, expires_at=expires_at)
