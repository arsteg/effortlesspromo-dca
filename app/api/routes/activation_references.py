"""DCA-094 — issue and verify short-lived activation references.

Key: ``activation-reference`` · source sections 5, 10. Stage: India channel eligibility.

A thin, authenticated HTTP contract that turns a *definitive ELIGIBLE* answer from the DCA-093
eligibility evaluator into a portable, tamper-resistant PROOF of it — a signed, short-lived activation
reference (:mod:`app.activation.activation_reference`). Two routes, both guarded by ``X-Service-Auth``
exactly like the sibling eligibility route:

- ``POST /v1/tenants/{business_id}/activation-references/{channel}`` — evaluate one of THIS business's
  OWN contact points (reusing the DCA-093 pipeline: tenant-scoped resolution, the pure email evaluator,
  the three-state fold) and, ONLY when the outcome is a definitive ELIGIBLE, mint a reference binding
  the whole activation context (tenant, recipient, channel, purpose, provider, campaign revision, and
  the permission + source versions the decision rested on). A REVIEW/BLOCKED outcome mints NOTHING and
  returns the outcome with its reasons — a reference is never issued for a contact that is not eligible.
- ``POST /v1/tenants/{business_id}/activation-references/{channel}/verify`` — validate a presented
  reference against the signing secret and, optionally, against the exact context the caller intends to
  act on (``expected``). An altered, expired, or wrong-tenant reference is rejected; a reference whose
  bound recipient/channel/etc. disagrees with the caller's intent is rejected.

The reference never widens what eligibility already decided: it is minted only for this business's own
resolved contact point, bound to this path business, and carries a one-way recipient digest rather than
the raw value. It is NOT recipient consent and NOT a send authorization — the one send pipeline owns
and re-gates the actual send, which stays a disabled future capability. Issuing is read-only (it writes
nothing), so a mint and every denial reproduce with no state change. When no signing secret is
configured the whole capability is disabled (fail-closed), never half-served.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.activation.activation_reference import (
    ActivationBinding,
    ActivationReferenceError,
    issue_activation_reference,
    recipient_digest,
    verify_activation_reference,
)
from app.api.routes.eligibility import (
    EligibilityBody,
    SessionDep,
    _evaluate_email,
    _normalize_channel,
    _require_tenant,
)
from app.compliance.eligibility_outcome import activation_eligible, classify_email_outcome
from app.compliance.email_eligibility import decide_email_eligibility
from app.core.security import require_service_auth
from app.db.models import ContactChannel

router = APIRouter(
    prefix="/v1/tenants",
    tags=["activation-references"],
    dependencies=[Depends(require_service_auth)],
)

# Refusal code specific to this surface (token-level codes come from the reference module).
REASON_DISABLED = "activation_references_disabled"


def _settings(request: Request):
    return request.app.state.settings


def _reference_secret(request: Request) -> str:
    """The configured signing secret, or 404-style disabled. Fail-closed: no secret => the whole
    capability is off and no reference is minted or accepted."""
    secret = (getattr(_settings(request), "activation_reference_secret", None) or "").strip()
    if not secret:
        raise HTTPException(status_code=422, detail=REASON_DISABLED)
    return secret


class IssueReferenceBody(EligibilityBody):
    """The eligibility question (DCA-093) PLUS the extra context a reference must bind.

    ``contactPoint`` and the gate context are exactly the DCA-093 fields (resolved within THIS business
    only). The added fields name the sending provider (``providerName`` — distinct from the inherited
    ``provider`` eligibility RULE object), the ``campaignRevision`` the send belongs to, and the
    ``permissionVersion`` / ``sourceVersion`` the eligibility decision rests on — all folded into the
    signed reference so it cannot be re-used for a different provider, revision, or a decision taken
    under different permission/source versions.
    """

    model_config = ConfigDict(extra="forbid")

    providerName: str = Field(
        min_length=1
    )  # the sending provider identity the reference is bound to
    campaignRevision: str = Field(min_length=1)
    permissionVersion: str = Field(min_length=1)
    sourceVersion: str = Field(min_length=1)


class ExpectedBindingBody(BaseModel):
    """The activation context a verifier is about to act on. Every field optional; any that is supplied
    is compared against the signed reference, and a disagreement rejects it (so a caller cannot present
    an authentic reference while intending to contact a different recipient/channel/etc.)."""

    model_config = ConfigDict(extra="forbid")

    businessId: str | None = None
    contactPointId: str | None = None
    recipient: str | None = None  # the raw value the caller intends to use; digested before compare
    channel: str | None = None
    purpose: str | None = None
    provider: str | None = None
    campaignRevision: str | None = None
    permissionVersion: str | None = None
    sourceVersion: str | None = None


class VerifyReferenceBody(BaseModel):
    """A presented reference to validate, optionally against the caller's intended context."""

    model_config = ConfigDict(extra="forbid")

    reference: str = Field(min_length=1)
    expected: ExpectedBindingBody | None = None


def _binding_json(binding: ActivationBinding) -> dict:
    """Serialize a bound context for a response — carries the recipient DIGEST, never the raw value."""
    return {
        "businessId": binding.business_id,
        "contactPointId": binding.contact_point_id,
        "recipientDigest": binding.recipient_digest,
        "channel": binding.channel,
        "purpose": binding.purpose,
        "provider": binding.provider,
        "campaignRevision": binding.campaign_revision,
        "permissionVersion": binding.permission_version,
        "sourceVersion": binding.source_version,
    }


@router.post("/{business_id}/activation-references/{channel}")
def issue_reference(
    business_id: str,
    channel: str,
    body: IssueReferenceBody,
    request: Request,
    session: SessionDep,
) -> dict:
    """Evaluate this business's own contact point and, only when definitively ELIGIBLE, mint a
    short-lived reference bound to the whole activation context.

    Mirrors the DCA-093 eligibility route for resolution + evaluation (a cross-tenant/unknown contact
    reads as 404; the evaluator is a pure function of the caller-supplied gate context). A REVIEW or
    BLOCKED outcome returns 200 with ``issued: false`` + the outcome and reasons and mints NOTHING — a
    reference is only ever issued for a contact that is eligible right now (``issued: true``), exactly
    like the DCA-093 endpoint reports non-eligible outcomes without erroring. Read-only: nothing is
    written, so a mint and every non-issuance reproduce with no state change.
    """
    from app.services.contact_point import ContactPointStore

    secret = _reference_secret(request)
    _require_tenant(session, business_id)
    channel = _normalize_channel(channel)

    store = ContactPointStore(session, business_id)
    resolved = store.resolve_contact(ContactChannel.EMAIL.value, body.contactPoint)
    if resolved is None:
        raise HTTPException(status_code=404, detail="contact_point_not_found")

    now = datetime.now(UTC)
    gates, snapshot = _evaluate_email(resolved_contact_point=resolved.value, body=body, now=now)
    decision = decide_email_eligibility(gates, now=now)
    outcome = classify_email_outcome(decision)

    if not activation_eligible(outcome):
        # Not eligible -> NO reference. A reference is a proof of an ELIGIBLE answer; REVIEW/BLOCKED
        # never produce one. Reported as a normal 200 (like the eligibility endpoint) with the outcome
        # and its reasons, and an explicit null reference — nothing was minted.
        return {
            "businessId": business_id,
            "channel": channel,
            "outcome": outcome.value,
            "issued": False,
            "reference": None,
            "reasons": list(decision.reasons),
            "activation": {"eligible": False, "authorizesSend": False},
        }

    binding = ActivationBinding(
        business_id=business_id,
        contact_point_id=resolved.id,
        recipient_digest=recipient_digest(resolved.value),
        channel=channel,
        purpose=decision.purpose,
        provider=body.providerName,
        campaign_revision=body.campaignRevision,
        permission_version=body.permissionVersion,
        source_version=body.sourceVersion,
    )
    ttl = timedelta(seconds=_settings(request).activation_reference_ttl_seconds)
    expires_at = now + ttl
    reference = issue_activation_reference(
        secret, binding=binding, issued_at=now, expires_at=expires_at
    )

    return {
        "businessId": business_id,
        "channel": channel,
        "outcome": outcome.value,
        "issued": True,
        "reference": reference,
        "binding": _binding_json(binding.normalized()),
        "issuedAt": now.isoformat(),
        "expiresAt": expires_at.isoformat(),
        "ttlSeconds": int(ttl.total_seconds()),
        # The evidence/version references the eligibility decision — and thus this reference — rests on.
        "evidence": {
            "suppressionVersion": snapshot.version,
            "suppressionAvailability": snapshot.availability.value,
            "permissionState": gates.basis.permission_state,
            "permissionVersion": body.permissionVersion,
            "sourceVersion": body.sourceVersion,
        },
        # The activation semantics, stated so no consumer can mistake a reference for more than it is.
        "activation": {
            "eligible": True,
            "authorizesSend": False,
            "note": (
                "An activation reference proves the DCA's own blockers were clear for exactly this "
                "context when it was issued. It is short-lived, not standing permission; it is NOT "
                "recipient consent and NOT a send authorization — the one send pipeline owns and "
                "re-gates the send, which stays a disabled future capability."
            ),
        },
    }


@router.post("/{business_id}/activation-references/{channel}/verify")
def verify_reference(
    business_id: str,
    channel: str,
    body: VerifyReferenceBody,
    request: Request,
) -> dict:
    """Validate a presented reference, optionally against the exact context the caller intends to act
    on. The path ``business_id`` and ``channel`` are themselves asserted against the signed binding, so
    a reference minted for another tenant or channel is rejected here even without an ``expected`` body.

    An altered (signature-failing) or expired reference is refused; a reference whose bound dimensions
    disagree with the caller's intent is refused with the offending dimension. A valid reference's bound
    context is returned (recipient as a digest, never the raw value). This neither reads nor writes any
    tenant data — it is a pure signature/binding check — so it changes no state.
    """
    secret = _reference_secret(request)
    channel = _normalize_channel(channel)

    expected = _expected_binding(business_id, channel, body.expected)
    try:
        claims = verify_activation_reference(
            secret, body.reference, now=datetime.now(UTC), expected=expected
        )
    except ActivationReferenceError as exc:
        # Every rejection collapses to a 422 whose message is the stable reason code, with the
        # offending dimension appended on a binding mismatch (``<code>:<dimension>`` — dimension names
        # are never PII). The reference confers no authority and nothing was acted on.
        message = exc.code if exc.detail is None else f"{exc.code}:{exc.detail}"
        raise HTTPException(status_code=422, detail=message) from exc

    return {
        "valid": True,
        "binding": _binding_json(claims.binding),
        "issuedAt": claims.issued_at.isoformat(),
        "expiresAt": claims.expires_at.isoformat(),
        "activation": {"eligible": True, "authorizesSend": False},
    }


def _expected_binding(
    business_id: str, channel: str, expected: ExpectedBindingBody | None
) -> ActivationBinding:
    """Fold the path scope and the caller's optional intended context into an
    :class:`ActivationBinding` to assert against the signed reference. Dimensions the caller does not
    name are left empty (and so are not asserted). The path ``business_id`` is ALWAYS pinned — a
    reference minted for another tenant can never be verified here. ``channel`` defaults to the path
    channel but a caller-supplied ``expected.channel`` overrides it (so the caller can assert the exact
    channel it intends to act on). The raw ``recipient`` is digested before comparison so the raw value
    never leaves the caller.
    """
    exp = expected or ExpectedBindingBody()
    return ActivationBinding(
        business_id=business_id,  # path business is authoritative — always asserted
        contact_point_id=exp.contactPointId or "",
        recipient_digest=recipient_digest(exp.recipient) if exp.recipient else "",
        channel=exp.channel
        or channel,  # path channel by default; caller may pin a different intent
        purpose=exp.purpose or "",
        provider=exp.provider or "",
        campaign_revision=exp.campaignRevision or "",
        permission_version=exp.permissionVersion or "",
        source_version=exp.sourceVersion or "",
    )
