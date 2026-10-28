"""DCA-093 — the scoped channel-eligibility endpoint: one reviewable HTTP capability.

Key: ``eligibility-api`` · source sections 5, 10. Stage: India channel eligibility.

A thin, authenticated HTTP contract around the accepted channel evaluators (DCA-087..092). It lets a
later Web/Admin consumer ask ONE question for a business's OWN contact point — *do the DCA's
compliance gates clear this contact for a channel right now?* — and get back an **expiring** decision
with its reasons and the evidence / version references it rests on. It does not send, acquire, or
mutate anything: concluding ELIGIBLE removes the DCA's blockers only; it is NOT recipient consent and
NOT a send authorization (the one send pipeline owns and re-gates the send, and acting on it stays a
disabled future capability).

Guarded by ``X-Service-Auth`` (fail-closed) exactly like the sibling record/evidence routes, and the
path ``business_id`` is purely the row scope: the contact point is resolved only within THAT business
(``ContactPointStore.resolve_contact`` — there is no global index on the value), so a value another
business recorded is invisible here and reads as 404. This is the load-bearing guarantee: **a caller
cannot evaluate another business's hidden contact.** The gate CONTEXT (permission state, source
rights, the host's suppression snapshot, the sending provider rule) is supplied by the authorized
caller — the DCA never widens past it — and the evaluation is a PURE function of it, so the ELIGIBLE
result and every denial reproduce with no state change.

Only the ``email`` channel is wired today (the channel the manual-check scenario exercises). The other
accepted evaluators stay DISABLED here (``channel_not_enabled``) until their own story wires each one
in — missing contracts stay disabled, never half-served.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.compliance.eligibility_outcome import (
    activation_eligible,
    classify_email_outcome,
)
from app.compliance.email_eligibility import (
    DEFAULT_MAX_EVIDENCE_AGE,
    EMAIL_CHANNEL,
    Freshness,
    PermissionBasis,
    ProviderRule,
    SourceRights,
    decide_email_eligibility,
)
from app.compliance.email_eligibility import (
    EmailEligibilityRequest as EmailGates,
)
from app.consent.permission_state import PermissionState
from app.core.security import require_service_auth
from app.db.models import ContactChannel, TenantAgent
from app.suppression.contract import (
    DEFAULT_MAX_SUPPRESSION_AGE,
    SuppressionAvailability,
    SuppressionEntry,
    SuppressionSnapshot,
    authorize_activation,
)

router = APIRouter(
    prefix="/v1/tenants", tags=["eligibility"], dependencies=[Depends(require_service_auth)]
)

SessionDep = Annotated[Session, Depends(get_session)]

# The channels the accepted evaluators speak for. Only ``email`` is WIRED into this endpoint today;
# the rest are declared so a caller gets a precise "known channel, not yet enabled here" rather than an
# opaque 404, keeping every future capability explicitly disabled until its own story wires it.
WIRED_CHANNELS: frozenset[str] = frozenset({EMAIL_CHANNEL})
KNOWN_CHANNELS: frozenset[str] = frozenset(c.value for c in ContactChannel) | {EMAIL_CHANNEL}

# How long an eligibility decision may be relied on. It rests on the host's authoritative suppression
# view, which itself expires fast (a withdrawal recorded since must be honored), so the decision can
# never outlive that window — the caller must re-ask after it lapses rather than cache a stale clear.
DECISION_TTL = DEFAULT_MAX_SUPPRESSION_AGE


def _require_tenant(session: Session, business_id: str) -> TenantAgent:
    tenant = session.scalar(select(TenantAgent).where(TenantAgent.business_id == business_id))
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant_not_registered")
    return tenant


class SourceRightsBody(BaseModel):
    """Whether the acquisition source's in-force rights permit using this address for outbound contact
    (resolved upstream by the caller — the DCA-045/046/047/048 rights chain). The DCA never widens it."""

    model_config = ConfigDict(extra="forbid")

    permitsEmailContact: bool
    reason: str = ""


class SuppressionBody(BaseModel):
    """The host's authoritative suppression snapshot for this business, as the caller resolved it.

    Carries exactly what an authoritative answer must disclose — ``version``, ``asOf`` (freshness) and
    the affected scope (``coveredChannels`` + the suppressed ``contactPoints``). The endpoint runs the
    real fail-closed :func:`authorize_activation` over it; an unavailable / stale / out-of-scope answer
    yields REVIEW, a listed contact yields BLOCKED. Suppression and consent are the host's to own."""

    model_config = ConfigDict(extra="forbid")

    availability: SuppressionAvailability = SuppressionAvailability.AVAILABLE
    version: str = Field(min_length=1)
    asOf: datetime
    coveredChannels: tuple[str, ...] = ()
    suppressedContactPoints: tuple[str, ...] = ()


class ProviderRuleBody(BaseModel):
    """A sending-provider acceptable-use rule that can INDEPENDENTLY deny (providers only narrow)."""

    model_config = ConfigDict(extra="forbid")

    allowed: bool
    reason: str = ""


class EligibilityBody(BaseModel):
    """The structured eligibility question for one of this business's OWN contact points on a channel.

    ``contactPoint`` names a value this business privately recorded; it is resolved within the business
    only, so another business's value reads as not-found. The remaining fields are the host-resolved
    gate context (permission / source rights / suppression / provider). Recorded-only context
    (``emailVerified`` / ``fitBand``) is carried onto the result for audit but NEVER qualifies it.
    """

    model_config = ConfigDict(extra="forbid")

    contactPoint: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    basis: str = "consent"
    permissionState: PermissionState = PermissionState.NONE
    permissionScopeChannel: str = EMAIL_CHANNEL
    consentCollectedAt: datetime | None = None
    maxEvidenceAgeDays: int | None = Field(default=None, ge=1)
    sourceRights: SourceRightsBody
    suppression: SuppressionBody
    provider: ProviderRuleBody
    emailVerified: bool = False
    fitBand: str = ""


def _normalize_channel(channel: str) -> str:
    token = (channel or "").strip().lower()
    if token not in KNOWN_CHANNELS:
        # An unknown channel reads as not-found, like an unknown route — never an oracle.
        raise HTTPException(status_code=404, detail="unknown_channel")
    if token not in WIRED_CHANNELS:
        # A known channel whose evaluator is not wired into this endpoint yet: explicitly disabled.
        raise HTTPException(status_code=422, detail="channel_not_enabled")
    return token


def _evaluate_email(
    *, resolved_contact_point: str, body: EligibilityBody, now: datetime
) -> tuple[EmailGates, SuppressionSnapshot]:
    """Assemble the email gate value objects from the request body + the resolved contact point, run
    the host's fail-closed suppression authorization, then the pure composed email decision."""
    max_age = (
        timedelta(days=body.maxEvidenceAgeDays)
        if body.maxEvidenceAgeDays is not None
        else DEFAULT_MAX_EVIDENCE_AGE
    )
    if body.consentCollectedAt is None:
        freshness = Freshness(age=None, max_age=max_age)
    else:
        collected = body.consentCollectedAt
        collected_utc = collected if collected.tzinfo is not None else collected.replace(tzinfo=UTC)
        freshness = Freshness(age=now - collected_utc, max_age=max_age)

    as_of = body.suppression.asOf
    as_of_utc = as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=UTC)
    snapshot = SuppressionSnapshot(
        business_id="_eligibility_query",  # scope label only; the lookup is over the supplied snapshot
        availability=body.suppression.availability,
        version=body.suppression.version,
        as_of=as_of_utc,
        covered_channels=tuple(body.suppression.coveredChannels),
        entries=tuple(
            SuppressionEntry(contact_point=cp, channel=EMAIL_CHANNEL)
            for cp in body.suppression.suppressedContactPoints
        ),
    )
    suppression = authorize_activation(snapshot, resolved_contact_point, EMAIL_CHANNEL, now=now)

    gates = EmailGates(
        contact_point=resolved_contact_point,
        purpose=body.purpose,
        basis=PermissionBasis(
            basis=body.basis,
            permission_state=body.permissionState.value,
            scope_channel=body.permissionScopeChannel,
        ),
        source_rights=SourceRights(
            permits_email_contact=body.sourceRights.permitsEmailContact,
            reason=body.sourceRights.reason,
        ),
        freshness=freshness,
        suppression=suppression,
        provider=ProviderRule(allowed=body.provider.allowed, reason=body.provider.reason),
        email_verified=body.emailVerified,
        fit_band=body.fitBand,
    )
    return gates, snapshot


@router.post("/{business_id}/eligibility/{channel}")
def evaluate_eligibility(
    business_id: str, channel: str, body: EligibilityBody, session: SessionDep
) -> dict:
    """Evaluate one of THIS business's contact points for a channel and return an expiring decision.

    Steps: the business must be registered; the channel must be wired; the contact point must resolve
    WITHIN this business (a cross-tenant / unknown value reads as 404 — the "cannot evaluate another
    business's hidden contact" guarantee). The accepted channel evaluator then runs as a pure function
    of the caller-supplied gate context, and the binary decision is folded into a three-state outcome:
    REVIEW (indeterminate — a required authoritative fact was missing) is explicitly NOT activation
    permission, and even ELIGIBLE is not recipient consent or a send authorization. Read-only: nothing
    is written, so the result and every denial reproduce with no state change.
    """
    from app.services.contact_point import ContactPointStore

    _require_tenant(session, business_id)
    channel = _normalize_channel(channel)

    store = ContactPointStore(session, business_id)
    resolved = store.resolve_contact(ContactChannel.EMAIL.value, body.contactPoint)
    if resolved is None:
        # Scoped to this business only — another tenant's private value (or an unknown one) is
        # invisible here and reads exactly like not-found, never surfaced.
        raise HTTPException(status_code=404, detail="contact_point_not_found")

    now = datetime.now(UTC)
    gates, snapshot = _evaluate_email(resolved_contact_point=resolved.value, body=body, now=now)
    decision = decide_email_eligibility(gates, now=now)
    outcome = classify_email_outcome(decision)

    expires_at = now + DECISION_TTL
    return {
        "businessId": business_id,
        "channel": channel,
        # Echo the business's OWN stored, normalized value — never a value the caller could smuggle in
        # for another tenant (the resolve step already proved it belongs to this business).
        "contactPoint": resolved.value,
        "contactPointId": resolved.id,
        "purpose": decision.purpose,
        "outcome": outcome.value,
        "reasons": list(decision.reasons),
        "blockers": [
            {"gate": b.gate, "reason": b.reason, "detail": b.detail} for b in decision.blockers
        ],
        "decisive": (
            None
            if decision.decisive is None
            else {
                "gate": decision.decisive.gate,
                "reason": decision.decisive.reason,
                "detail": decision.decisive.detail,
            }
        ),
        # The activation semantics, stated so no consumer can mistake the decision for more than it is.
        "activation": {
            # True ONLY for a definitive ELIGIBLE — REVIEW and BLOCKED are both False. The contact MAY
            # then be offered to the one send pipeline; it is not itself an activation.
            "eligible": activation_eligible(outcome),
            # ALWAYS False: eligibility is never recipient consent and never a send authorization.
            "authorizesSend": False,
            "note": (
                "Eligibility removes the DCA's own blockers only. REVIEW and BLOCKED never permit "
                "activation, and even ELIGIBLE is not recipient consent or a send authorization — the "
                "one send pipeline owns and re-gates the send, which stays a disabled capability."
            ),
        },
        # Context recorded for audit but NEVER consulted by the decision (the acceptance invariant).
        "recordedContext": {
            "emailVerified": decision.email_verified,
            "fitBand": decision.fit_band,
        },
        # The evidence / version references the decision rests on — provenance for a reviewer to repro.
        "evidence": {
            "suppressionVersion": snapshot.version,
            "suppressionAvailability": snapshot.availability.value,
            "suppressionAsOf": snapshot.as_of.isoformat(),
            "suppressionCoveredChannels": list(snapshot.covered_channels),
            "permissionState": gates.basis.permission_state,
            "permissionScopeChannel": gates.basis.scope_channel,
            "consentCollectedAt": (
                body.consentCollectedAt.isoformat() if body.consentCollectedAt is not None else None
            ),
            "sourceRightsReason": gates.source_rights.reason,
            "providerReason": gates.provider.reason,
        },
        "evaluatedAt": decision.evaluated_at,
        # The decision EXPIRES: it cannot be trusted past the suppression freshness window it rests on.
        "expiresAt": expires_at.isoformat(),
        "ttlSeconds": int(DECISION_TTL.total_seconds()),
    }
