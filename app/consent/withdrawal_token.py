"""DCA-076 — the self-contained, signed public withdrawal token.

A withdrawal may be exercised two ways (see :mod:`app.services.consent_withdrawal`): by an
AUTHENTICATED business user holding :attr:`~app.identity.permissions.Scope.CONSENT_WITHDRAW`, or by the
SUBJECT themselves through a valid PUBLIC TOKEN — the one-click unsubscribe link, which needs no login.
This module is that token: a pure, deterministic, network-free mint/verify pair, so a link carries its
own proof of authority and can be validated with nothing but the signing secret.

The load-bearing property (the DCA-076 tenant-isolation acceptance criterion): a token BINDS exactly
one scope — ``(business_id, contact_point_id, channel, purpose, brand_key)`` — into the signed payload.
A verifier returns those bound claims; the withdrawal command then acts ONLY on that business's contact
point. So a token minted for Business A's email scope can never withdraw Business B's independent
permission, nor a different scope of A's: the business id and the contact point are inside the signature
and any tampering fails :data:`REASON_BAD_SIGNATURE`. There is no "withdraw all" token and no wildcard.

Design choices, matching the service's fail-closed stance:

- **HMAC-SHA256, constant-time compared.** The signature covers the version tag AND the payload, so
  neither can be swapped. Verification uses :func:`hmac.compare_digest` (no early-exit leak).
- **Self-contained + expiring.** The payload carries ``iat``/``exp`` (epoch seconds); an expired token
  is refused (:data:`REASON_EXPIRED`). No server-side token store is needed — the signature IS the
  state — so this adds no table and no migration.
- **Scope values are normalized** (brand/channel/purpose lower-cased, ids trimmed) BEFORE signing and
  on read, so a verified token's claims line up exactly with the DCA-074 receipt scope.
- **No secret ⇒ disabled.** The caller (the service) only offers the public path when a secret is
  configured; :func:`verify_withdrawal_token` with an empty secret refuses every token
  (:data:`REASON_NO_SECRET`) — a missing contract stays disabled, never fail-open.

The token proves authority to WITHDRAW one scope and nothing else; it is not an authentication token and
grants no read/other write. Acting on the resulting restriction (suppressing a send) stays a disabled
future capability behind the one send pipeline.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime

# The only token version this build mints/accepts. An unknown version is refused rather than guessed,
# so a future format change can never be silently mis-read.
TOKEN_VERSION = "dcaw1"

# Stable refusal codes (mirrors the service/store convention). Each means the token confers no authority.
REASON_NO_SECRET = "withdrawal_token_no_secret"
REASON_INVALID_FORMAT = "withdrawal_token_invalid_format"
REASON_UNSUPPORTED_VERSION = "withdrawal_token_unsupported_version"
REASON_BAD_SIGNATURE = "withdrawal_token_bad_signature"
REASON_MALFORMED_PAYLOAD = "withdrawal_token_malformed_payload"
REASON_EXPIRED = "withdrawal_token_expired"


class WithdrawalTokenError(Exception):
    """A withdrawal token was rejected with a stable ``code``. Raised before any state is touched, so a
    rejected token confers no authority and changes nothing."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WithdrawalTokenClaims:
    """The scope a verified token authorizes a withdrawal of — exactly the DCA-074 scoping dimensions,
    plus the token's own validity window. Normalized, so it lines up with a receipt scope."""

    business_id: str
    contact_point_id: str
    channel: str
    purpose: str
    brand_key: str
    issued_at: datetime
    expires_at: datetime


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _normalize(value: str) -> str:
    return (value or "").strip().lower()


def _sign(secret: str, signing_input: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256
    ).digest()
    return _b64url_encode(digest)


def issue_withdrawal_token(
    secret: str,
    *,
    business_id: str,
    contact_point_id: str,
    channel: str,
    purpose: str,
    brand_key: str,
    issued_at: datetime,
    expires_at: datetime,
) -> str:
    """Mint a signed public withdrawal token binding exactly ONE scope (pure, deterministic).

    The payload carries the normalized scope + ``iat``/``exp`` epoch seconds; the returned token is
    ``<version>.<payload_b64url>.<sig_b64url>``. Same inputs + same secret always produce the same
    token (no randomness), so it is fully reproducible. A missing secret or a non-positive validity
    window is a programming error (``ValueError``) — the mint never produces an unsigned or
    already-expired token.
    """
    if not (secret or "").strip():
        raise ValueError("a signing secret is required to mint a withdrawal token")
    if expires_at <= issued_at:
        raise ValueError("a withdrawal token must expire strictly after it is issued")

    payload = {
        "v": TOKEN_VERSION,
        "biz": (business_id or "").strip(),
        "cp": (contact_point_id or "").strip(),
        "ch": _normalize(channel),
        "pur": _normalize(purpose),
        "br": _normalize(brand_key),
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    if not payload["biz"] or not payload["cp"] or not payload["ch"] or not payload["pur"]:
        raise ValueError(
            "a withdrawal token must bind business, contact point, channel and purpose"
        )

    payload_b64 = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    )
    signing_input = f"{TOKEN_VERSION}.{payload_b64}"
    return f"{signing_input}.{_sign(secret, signing_input)}"


def verify_withdrawal_token(
    secret: str, token: str, *, now: datetime | None = None
) -> WithdrawalTokenClaims:
    """Validate a public withdrawal token and return the scope it authorizes, or raise.

    Fail-closed at every step: an empty secret refuses all tokens (:data:`REASON_NO_SECRET`); a token
    that is malformed, carries an unknown version, fails the constant-time signature check, has an
    unreadable payload, or is expired is refused with the matching stable code and confers no
    authority. The signature covers the version AND payload, so a tampered business id / contact point
    / scope is rejected — a token can never be re-pointed at another tenant or another scope.
    """
    moment = now or datetime.now(UTC)
    if not (secret or "").strip():
        raise WithdrawalTokenError(
            REASON_NO_SECRET, "public withdrawal is disabled: no signing secret is configured"
        )

    parts = (token or "").split(".")
    if len(parts) != 3 or not all(parts):
        raise WithdrawalTokenError(REASON_INVALID_FORMAT, "malformed withdrawal token")
    version, payload_b64, provided_sig = parts
    if version != TOKEN_VERSION:
        raise WithdrawalTokenError(
            REASON_UNSUPPORTED_VERSION, f"unsupported withdrawal token version '{version}'"
        )

    expected_sig = _sign(secret, f"{version}.{payload_b64}")
    if not hmac.compare_digest(expected_sig, provided_sig):
        raise WithdrawalTokenError(REASON_BAD_SIGNATURE, "withdrawal token signature is invalid")

    try:
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        business_id = str(payload["biz"]).strip()
        contact_point_id = str(payload["cp"]).strip()
        channel = _normalize(str(payload["ch"]))
        purpose = _normalize(str(payload["pur"]))
        brand_key = _normalize(str(payload.get("br", "")))
        issued_at = datetime.fromtimestamp(int(payload["iat"]), tz=UTC)
        expires_at = datetime.fromtimestamp(int(payload["exp"]), tz=UTC)
    except (ValueError, KeyError, TypeError) as exc:
        raise WithdrawalTokenError(
            REASON_MALFORMED_PAYLOAD, "withdrawal token payload is unreadable"
        ) from exc

    if not business_id or not contact_point_id or not channel or not purpose:
        raise WithdrawalTokenError(
            REASON_MALFORMED_PAYLOAD, "withdrawal token payload is missing a scope dimension"
        )

    if moment >= expires_at:
        raise WithdrawalTokenError(REASON_EXPIRED, "withdrawal token has expired")

    return WithdrawalTokenClaims(
        business_id=business_id,
        contact_point_id=contact_point_id,
        channel=channel,
        purpose=purpose,
        brand_key=brand_key,
        issued_at=issued_at,
        expires_at=expires_at,
    )
