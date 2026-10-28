"""DCA-094 — the pure activation-reference mint/verify pair.

Network-free, deterministic unit tests for :mod:`app.activation.activation_reference`: a reference
binds the whole activation context, is tamper-resistant, is short-lived, and can be re-asserted against
an intended context. No DB, no app — just the signed-token algebra (same shape as the DCA-076 token
tests). The HTTP contract is exercised separately in ``test_activation_reference_api.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.activation.activation_reference import (
    REASON_BAD_SIGNATURE,
    REASON_BINDING_MISMATCH,
    REASON_EXPIRED,
    REASON_INVALID_FORMAT,
    REASON_NO_SECRET,
    REASON_UNSUPPORTED_VERSION,
    ActivationBinding,
    ActivationReferenceError,
    issue_activation_reference,
    recipient_digest,
    verify_activation_reference,
)

SECRET = "activation-reference-signing-secret"
NOW = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)


def _binding(**overrides) -> ActivationBinding:
    base = {
        "business_id": "11111111-1111-1111-1111-111111111111",
        "contact_point_id": "cp-abc",
        "recipient_digest": recipient_digest("owner@acme-synthetic.example"),
        "channel": "email",
        "purpose": "marketing_email",
        "provider": "ses",
        "campaign_revision": "rev-7",
        "permission_version": "perm-v3",
        "source_version": "src-v2",
    }
    base.update(overrides)
    return ActivationBinding(**base)


def _issue(binding: ActivationBinding | None = None, *, ttl_seconds: int = 900) -> str:
    return issue_activation_reference(
        SECRET,
        binding=binding or _binding(),
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=ttl_seconds),
    )


# --------------------------------------------------------------------------------------------------
# Happy path: a fresh reference verifies and returns its exact bound context.
# --------------------------------------------------------------------------------------------------


def test_issued_reference_verifies_and_returns_binding():
    reference = _issue()
    claims = verify_activation_reference(SECRET, reference, now=NOW)
    assert claims.binding == _binding().normalized()
    assert claims.issued_at == NOW
    assert claims.expires_at == NOW + timedelta(seconds=900)


def test_mint_is_deterministic():
    # No randomness: same inputs + same secret always produce the same reference (reproducible).
    assert _issue() == _issue()


def test_recipient_is_bound_as_digest_never_raw():
    reference = _issue()
    # The raw address never appears in the reference; only its one-way digest does.
    assert "owner@acme-synthetic.example" not in reference
    claims = verify_activation_reference(SECRET, reference, now=NOW)
    assert claims.binding.recipient_digest == recipient_digest("owner@acme-synthetic.example")


# --------------------------------------------------------------------------------------------------
# Tamper-resistance: altering ANY bound dimension or the signature is rejected.
# --------------------------------------------------------------------------------------------------


def test_edited_payload_fails_signature():
    reference = _issue()
    version, payload_b64, sig = reference.split(".")
    # Flip a character in the payload (simulating a tampered recipient/channel/etc.).
    tampered_payload = payload_b64[:-1] + ("A" if payload_b64[-1] != "A" else "B")
    tampered = f"{version}.{tampered_payload}.{sig}"
    with pytest.raises(ActivationReferenceError) as exc:
        verify_activation_reference(SECRET, tampered, now=NOW)
    assert exc.value.code == REASON_BAD_SIGNATURE


def test_wrong_secret_fails_signature():
    reference = _issue()
    with pytest.raises(ActivationReferenceError) as exc:
        verify_activation_reference("a-different-secret", reference, now=NOW)
    assert exc.value.code == REASON_BAD_SIGNATURE


def test_unsupported_version_is_rejected():
    reference = _issue()
    _, payload_b64, sig = reference.split(".")
    with pytest.raises(ActivationReferenceError) as exc:
        verify_activation_reference(SECRET, f"dcaarX.{payload_b64}.{sig}", now=NOW)
    assert exc.value.code == REASON_UNSUPPORTED_VERSION


@pytest.mark.parametrize("bad", ["", "a.b", "a.b.c.d", "...", "onlyonepart"])
def test_malformed_reference_is_rejected(bad):
    with pytest.raises(ActivationReferenceError) as exc:
        verify_activation_reference(SECRET, bad, now=NOW)
    assert exc.value.code == REASON_INVALID_FORMAT


# --------------------------------------------------------------------------------------------------
# Short-lived: a reference is refused at and after its expiry — never standing permission.
# --------------------------------------------------------------------------------------------------


def test_expired_reference_is_rejected():
    reference = _issue(ttl_seconds=900)
    with pytest.raises(ActivationReferenceError) as exc:
        verify_activation_reference(SECRET, reference, now=NOW + timedelta(seconds=901))
    assert exc.value.code == REASON_EXPIRED


def test_reference_rejected_exactly_at_expiry():
    reference = _issue(ttl_seconds=900)
    with pytest.raises(ActivationReferenceError) as exc:
        verify_activation_reference(SECRET, reference, now=NOW + timedelta(seconds=900))
    assert exc.value.code == REASON_EXPIRED


def test_reference_valid_just_before_expiry():
    reference = _issue(ttl_seconds=900)
    claims = verify_activation_reference(SECRET, reference, now=NOW + timedelta(seconds=899))
    assert claims.binding.channel == "email"


# --------------------------------------------------------------------------------------------------
# Intended-context re-assertion: a wrong recipient/channel/etc. is rejected even for an authentic token.
# --------------------------------------------------------------------------------------------------


def test_matching_expected_context_passes():
    reference = _issue()
    # Naming the exact context (recipient supplied raw, digested internally) verifies cleanly.
    expected = ActivationBinding(
        business_id="11111111-1111-1111-1111-111111111111",
        contact_point_id="cp-abc",
        recipient_digest=recipient_digest("owner@acme-synthetic.example"),
        channel="email",
        purpose="marketing_email",
        provider="ses",
        campaign_revision="rev-7",
        permission_version="perm-v3",
        source_version="src-v2",
    )
    claims = verify_activation_reference(SECRET, reference, now=NOW, expected=expected)
    assert claims.binding.provider == "ses"


def test_altered_recipient_is_rejected():
    reference = _issue()
    expected = ActivationBinding(
        business_id="",
        contact_point_id="",
        recipient_digest=recipient_digest("someone-else@acme-synthetic.example"),
        channel="",
        purpose="",
        provider="",
        campaign_revision="",
        permission_version="",
        source_version="",
    )
    with pytest.raises(ActivationReferenceError) as exc:
        verify_activation_reference(SECRET, reference, now=NOW, expected=expected)
    assert exc.value.code == REASON_BINDING_MISMATCH
    assert exc.value.detail == "recipient_digest"


def test_altered_channel_is_rejected():
    reference = _issue()
    expected = ActivationBinding(
        business_id="",
        contact_point_id="",
        recipient_digest="",
        channel="whatsapp",
        purpose="",
        provider="",
        campaign_revision="",
        permission_version="",
        source_version="",
    )
    with pytest.raises(ActivationReferenceError) as exc:
        verify_activation_reference(SECRET, reference, now=NOW, expected=expected)
    assert exc.value.code == REASON_BINDING_MISMATCH
    assert exc.value.detail == "channel"


def test_altered_campaign_revision_is_rejected():
    reference = _issue()
    expected = ActivationBinding(
        business_id="",
        contact_point_id="",
        recipient_digest="",
        channel="",
        purpose="",
        provider="",
        campaign_revision="rev-8",
        permission_version="",
        source_version="",
    )
    with pytest.raises(ActivationReferenceError) as exc:
        verify_activation_reference(SECRET, reference, now=NOW, expected=expected)
    assert exc.value.code == REASON_BINDING_MISMATCH
    assert exc.value.detail == "campaign_revision"


def test_empty_expected_dimensions_are_not_asserted():
    # An all-blank expected asserts nothing — the reference still verifies on signature + expiry alone.
    reference = _issue()
    blank = ActivationBinding(
        business_id="",
        contact_point_id="",
        recipient_digest="",
        channel="",
        purpose="",
        provider="",
        campaign_revision="",
        permission_version="",
        source_version="",
    )
    claims = verify_activation_reference(SECRET, reference, now=NOW, expected=blank)
    assert claims.binding == _binding().normalized()


# --------------------------------------------------------------------------------------------------
# Fail-closed + mint invariants.
# --------------------------------------------------------------------------------------------------


def test_no_secret_disables_verification():
    reference = _issue()
    with pytest.raises(ActivationReferenceError) as exc:
        verify_activation_reference("", reference, now=NOW)
    assert exc.value.code == REASON_NO_SECRET


def test_mint_requires_a_secret():
    with pytest.raises(ValueError):
        issue_activation_reference(
            "", binding=_binding(), issued_at=NOW, expires_at=NOW + timedelta(seconds=900)
        )


def test_mint_requires_positive_window():
    with pytest.raises(ValueError):
        issue_activation_reference(SECRET, binding=_binding(), issued_at=NOW, expires_at=NOW)


def test_mint_refuses_incomplete_binding():
    with pytest.raises(ValueError):
        _issue(_binding(provider=""))
