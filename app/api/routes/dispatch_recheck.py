"""DCA-095 — revalidate immediately before host dispatch (the consume/recheck contract).

Key: ``dispatch-recheck`` · source sections 5, 10. Stage: India channel eligibility.

A DCA-094 activation reference proves the DCA's gates were clear for a context *when it was issued*.
State moves: a recipient can withdraw, a permission or source version can roll, the authoritative
suppression view can change. So immediately before a host dispatches, it must RE-ASK the DCA — this
endpoint is that re-ask, and it is a *consume*, not a read:

``POST /v1/tenants/{business_id}/dispatch-recheck/{channel}`` — the host presents the reference, the
CURRENT gate context (current permission state, current suppression snapshot, current provider rule
and the CURRENT permission/source/campaign versions), and a consume-once ``idempotencyKey``. The
endpoint:

1. resolves the contact point WITHIN this business (a cross-tenant/unknown value reads as 404);
2. re-verifies the reference against the CURRENT intended context — a tampered, expired, wrong-tenant,
   wrong-channel, or **version-drifted** reference (the permission/source/campaign version bound at
   issuance no longer matches what the host now holds) is rejected;
3. re-runs the accepted email eligibility evaluation over the CURRENT context — a withdrawal recorded
   since issuance (consent no longer in force, or the contact now suppressed) BLOCKS; an unavailable /
   stale / out-of-scope authority is REVIEW and fails closed;
4. only on a definitive, still-clear grant, **consumes exactly one dispatch slot** keyed on
   ``(business_id, idempotencyKey)`` (:mod:`app.dispatch.consumption`) — a retried or concurrent
   consume of the same dispatch replays that one grant, a genuinely different dispatch under a reused
   key is a 409 conflict.

Fail-closed throughout: a denial writes NOTHING (no consumption, no state change — the rejection
reproduces exactly), and when no activation-reference signing secret is configured the whole
capability is disabled. A grant records that the DCA re-cleared its OWN blockers for exactly one
dispatch; it is NOT recipient consent and NOT a send authorization — the one send pipeline owns and
performs the actual send, which stays a disabled future capability.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ConfigDict, Field

from app.activation.activation_reference import (
    ActivationBinding,
    ActivationReferenceError,
    recipient_digest,
    verify_activation_reference,
)
from app.api.routes.activation_references import IssueReferenceBody
from app.api.routes.eligibility import (
    SessionDep,
    _evaluate_email,
    _normalize_channel,
    _require_tenant,
)
from app.compliance.eligibility_outcome import activation_eligible, classify_email_outcome
from app.compliance.email_eligibility import decide_email_eligibility
from app.core.security import require_service_auth
from app.db.models import ContactChannel, DispatchRecheckConsumption
from app.dispatch.consumption import (
    ConsumptionBinding,
    DispatchConsumptionConflict,
    DispatchConsumptionStore,
    reference_fingerprint,
)

router = APIRouter(
    prefix="/v1/tenants",
    tags=["dispatch-recheck"],
    dependencies=[Depends(require_service_auth)],
)

# Refusal code specific to this surface (token-level codes come from the reference module).
REASON_DISABLED = "dispatch_recheck_disabled"
REASON_KEY_REUSED = "dispatch_recheck_idempotency_key_reused"

# Stable dispatch-decision statuses the host reads uniformly off a 200 response.
STATUS_ALLOWED = "allowed"
STATUS_BLOCKED = "blocked"
STATUS_REVIEW = "review"
STATUS_REFERENCE_REJECTED = "reference_rejected"

_NOTE = (
    "A dispatch recheck re-asserts the DCA's OWN blockers (permission, suppression and version state) "
    "immediately before a host dispatches, and consumes exactly one dispatch slot on a grant. It is "
    "NOT recipient consent and NOT a send authorization — the one send pipeline owns and performs the "
    "actual send, which stays a disabled future capability."
)


def _settings(request: Request):
    return request.app.state.settings


def _reference_secret(request: Request) -> str:
    """The activation-reference signing secret, or a disabled-capability refusal. Fail-closed: without
    it the DCA cannot verify a reference, so the whole recheck is off and nothing is consumed."""
    secret = (getattr(_settings(request), "activation_reference_secret", None) or "").strip()
    if not secret:
        raise HTTPException(status_code=422, detail=REASON_DISABLED)
    return secret


class DispatchRecheckBody(IssueReferenceBody):
    """The presented reference + the CURRENT gate context + a consume-once key.

    Inherits the DCA-094 issue fields, but here they carry the host's CURRENT view at dispatch time:
    ``permissionState`` / ``suppression`` / ``provider`` are re-evaluated, and ``providerName`` /
    ``campaignRevision`` / ``permissionVersion`` / ``sourceVersion`` are the versions the host now holds
    — any that drift from the reference's bound values reject it as stale. ``reference`` is the DCA-094
    token to consume; ``idempotencyKey`` scopes the consume-once to one logical dispatch attempt.
    """

    model_config = ConfigDict(extra="forbid")

    reference: str = Field(min_length=1)
    idempotencyKey: str = Field(min_length=1, max_length=128)


def _binding_from_row(row: DispatchRecheckConsumption) -> dict:
    """The consumed grant's bound context for a response — recipient as a digest, never the raw value."""
    return {
        "businessId": row.business_id,
        "contactPointId": row.contact_point_id,
        "recipientDigest": row.recipient_digest,
        "channel": row.channel,
        "provider": row.provider,
        "campaignRevision": row.campaign_revision,
        "permissionVersion": row.permission_version,
        "sourceVersion": row.source_version,
        "referenceFingerprint": row.reference_fingerprint,
    }


def _grant_response(
    business_id: str,
    channel: str,
    *,
    row: DispatchRecheckConsumption,
    replayed: bool,
    suppression_version: str,
) -> dict:
    return {
        "businessId": business_id,
        "channel": channel,
        "dispatch": {
            "allowed": True,
            "status": STATUS_ALLOWED,
            "reason": "dispatch_recheck_cleared",
            "authorizesSend": False,
        },
        "consumption": {
            "idempotencyKey": row.idempotency_key,
            "consumed": True,
            "replay": replayed,
            "consumptionId": row.id,
            "consumedAt": row.consumed_at.isoformat(),
        },
        "recheck": {
            "referenceValid": True,
            "referenceReason": None,
            "outcome": "eligible",
            "reasons": [],
            "evidence": {
                "suppressionVersion": suppression_version,
                "permissionVersion": row.permission_version,
                "sourceVersion": row.source_version,
            },
        },
        "binding": _binding_from_row(row),
        "activation": {"eligible": True, "authorizesSend": False, "note": _NOTE},
    }


def _denied_response(
    business_id: str,
    channel: str,
    idempotency_key: str,
    *,
    status: str,
    reason: str,
    reference_valid: bool,
    reference_reason: str | None = None,
    outcome: str | None = None,
    reasons: list[str] | None = None,
    evidence: dict | None = None,
) -> dict:
    """A dispatch denial. Nothing was consumed and nothing was written, so it reproduces with no state
    change — this is the fail-closed path for a withdrawal, a stale version, an unavailable authority,
    or a rejected reference."""
    return {
        "businessId": business_id,
        "channel": channel,
        "dispatch": {
            "allowed": False,
            "status": status,
            "reason": reason,
            "authorizesSend": False,
        },
        "consumption": {
            "idempotencyKey": idempotency_key,
            "consumed": False,
            "replay": False,
        },
        "recheck": {
            "referenceValid": reference_valid,
            "referenceReason": reference_reason,
            "outcome": outcome,
            "reasons": reasons or [],
            "evidence": evidence or {},
        },
        "activation": {"eligible": False, "authorizesSend": False, "note": _NOTE},
    }


@router.post("/{business_id}/dispatch-recheck/{channel}")
def dispatch_recheck(
    business_id: str,
    channel: str,
    body: DispatchRecheckBody,
    request: Request,
    session: SessionDep,
) -> dict:
    """Re-validate a reference against CURRENT state and idempotently consume one dispatch slot on a
    grant. See the module docstring for the full contract and fail-closed rules."""
    from app.services.contact_point import ContactPointStore

    secret = _reference_secret(request)
    _require_tenant(session, business_id)
    channel = _normalize_channel(channel)

    store = ContactPointStore(session, business_id)
    resolved = store.resolve_contact(ContactChannel.EMAIL.value, body.contactPoint)
    if resolved is None:
        # Scoped to this business only — another tenant's private value (or an unknown one) is 404.
        raise HTTPException(status_code=404, detail="contact_point_not_found")

    now = datetime.now(UTC)
    digest = recipient_digest(resolved.value)
    fingerprint = reference_fingerprint(body.reference)
    consumption = DispatchConsumptionStore(session, business_id)

    # --- idempotency first: a slot already consumed under this key replays its grant verbatim ---
    existing = consumption.find(body.idempotencyKey)
    if existing is not None:
        replay_binding = ConsumptionBinding(
            reference_fingerprint=fingerprint, recipient_digest=digest, channel=channel
        )
        if not DispatchConsumptionStore.matches(existing, replay_binding):
            # The same consume-once key presented for a DIFFERENT dispatch — never ride on the first
            # grant. The host must use a fresh key for a genuinely new dispatch.
            raise HTTPException(status_code=409, detail=REASON_KEY_REUSED)
        # The dispatch already completed under this key; replay the recorded grant (no re-evaluation,
        # no new row) so a retried consume is exactly-once.
        return _grant_response(
            business_id,
            channel,
            row=existing,
            replayed=True,
            suppression_version=existing.suppression_version,
        )

    # --- fresh slot: authoritative recheck against CURRENT state ---
    # (1) Re-verify the reference against the context the host intends to act on NOW. Any drift in the
    # permission/source/campaign version, provider, recipient, channel, tenant — or a tampered/expired
    # token — rejects it. Build the expected binding from the current request, then verify.
    expected = ActivationBinding(
        business_id=business_id,
        contact_point_id=resolved.id,
        recipient_digest=digest,
        channel=channel,
        purpose=body.purpose,
        provider=body.providerName,
        campaign_revision=body.campaignRevision,
        permission_version=body.permissionVersion,
        source_version=body.sourceVersion,
    )
    try:
        verify_activation_reference(secret, body.reference, now=now, expected=expected)
    except ActivationReferenceError as exc:
        reference_reason = exc.code if exc.detail is None else f"{exc.code}:{exc.detail}"
        return _denied_response(
            business_id,
            channel,
            body.idempotencyKey,
            status=STATUS_REFERENCE_REJECTED,
            reason=reference_reason,
            reference_valid=False,
            reference_reason=reference_reason,
        )

    # (2) Re-run the accepted eligibility evaluation over the CURRENT gate context. A withdrawal now in
    # force (consent not in force, or the contact newly suppressed) BLOCKS; an unavailable/stale/
    # out-of-scope authority is REVIEW and fails closed.
    gates, snapshot = _evaluate_email(resolved_contact_point=resolved.value, body=body, now=now)
    decision = decide_email_eligibility(gates, now=now)
    outcome = classify_email_outcome(decision)
    evidence = {
        "suppressionVersion": snapshot.version,
        "suppressionAvailability": snapshot.availability.value,
        "permissionState": gates.basis.permission_state,
        "permissionVersion": body.permissionVersion,
        "sourceVersion": body.sourceVersion,
    }
    if not activation_eligible(outcome):
        return _denied_response(
            business_id,
            channel,
            body.idempotencyKey,
            status=STATUS_REVIEW if outcome.value == "review" else STATUS_BLOCKED,
            reason=f"recheck_{outcome.value}",
            reference_valid=True,
            outcome=outcome.value,
            reasons=list(decision.reasons),
            evidence=evidence,
        )

    # (3) Still clear -> consume exactly one dispatch slot (idempotent at the DB). A concurrent winner
    # replays; a reused key for a different dispatch is a conflict.
    binding = ConsumptionBinding(
        reference_fingerprint=fingerprint,
        recipient_digest=digest,
        channel=channel,
        contact_point_id=resolved.id,
        provider=body.providerName,
        campaign_revision=body.campaignRevision,
        permission_version=body.permissionVersion,
        source_version=body.sourceVersion,
        suppression_version=snapshot.version,
    )
    try:
        row, replayed = consumption.consume(body.idempotencyKey, binding, consumed_at=now)
    except DispatchConsumptionConflict as exc:
        raise HTTPException(status_code=409, detail=REASON_KEY_REUSED) from exc
    if not replayed:
        session.commit()

    return _grant_response(
        business_id, channel, row=row, replayed=replayed, suppression_version=snapshot.version
    )
