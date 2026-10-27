"""DCA-038 — validate Indian targeting evidence: the stateless service API.

A structured HTTP contract (for later Web/Admin consumers) over the deterministic geography resolver
(:mod:`app.geography`). A caller submits the three *separate* geographic contexts — recipient location,
legal address, operational area — each as structured geography (country, state/UT, city, district,
PIN, service area) and/or atomic evidence items, and receives:

- the **recipient jurisdiction** resolution (``in`` / ``foreign`` / ``unresolved``), and
- the India-only **targeting decision** (allowed only when the recipient resolves to India), plus each
  context's own resolution so a reviewer can see legal address / operational area are kept separate.

This route is **stateless and read-only** — it touches no database and changes nothing — so a result
and its denial are fully reproducible with no side effects. It is ``X-Service-Auth``-gated
(fail-closed) like every other service route; it holds no tenant data, so it carries no business scope.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.security import require_service_auth
from app.geography import (
    STRONG_KINDS,
    EvidenceKind,
    GeoLocation,
    JurisdictionResolution,
    TargetingDecision,
    TargetingGeography,
    evidence_from_dicts,
    validate_targeting,
)
from app.geography import india as india_ref

# The strong-proof evidence kinds (by value) — advertised on the reference route.
_STRONG_VALUES = frozenset(k.value for k in STRONG_KINDS)

router = APIRouter(
    prefix="/v1/targeting", tags=["targeting"], dependencies=[Depends(require_service_auth)]
)


class EvidenceBody(BaseModel):
    kind: str
    value: str = ""
    country: str = ""


class LocationBody(BaseModel):
    """Structured geography for one context. Every field is optional — an empty body is 'no evidence'."""

    country: str = ""
    stateUt: str = ""
    district: str = ""
    city: str = ""
    pinCode: str = ""
    serviceAreaKm: float | None = None
    evidence: list[EvidenceBody] = Field(default_factory=list)


class TargetingValidateBody(BaseModel):
    recipientLocation: LocationBody | None = None
    legalAddress: LocationBody | None = None
    operationalArea: LocationBody | None = None


def _to_location(body: LocationBody | None) -> GeoLocation | None:
    if body is None:
        return None
    return GeoLocation(
        country=body.country,
        state_ut=body.stateUt,
        district=body.district,
        city=body.city,
        pin_code=body.pinCode,
        service_area_km=body.serviceAreaKm,
        evidence=evidence_from_dicts(e.model_dump() for e in body.evidence),
    )


def _resolution_view(resolution: JurisdictionResolution) -> dict:
    return {
        "status": resolution.status.value,
        "reasonCode": resolution.reason_code,
        "country": resolution.country,
        "provenCountries": list(resolution.proven_countries),
        "hadWeakSignal": resolution.had_weak_signal,
        "detail": resolution.detail,
    }


def _structured_view(body: LocationBody | None) -> dict | None:
    """Field-level validation of the structured recipient geography (normalized + validity flags)."""
    if body is None:
        return None
    state_code = india_ref.normalize_state_ut(body.stateUt) if body.stateUt.strip() else None
    return {
        "country": india_ref.normalize_country(body.country),
        "stateUt": state_code,
        "stateUtRecognized": (bool(state_code) if body.stateUt.strip() else None),
        "stateUtName": india_ref.state_ut_name(state_code) if state_code else None,
        "city": body.city.strip(),
        "district": body.district.strip(),
        "pinCode": body.pinCode.strip(),
        "pinValid": (india_ref.is_valid_pin(body.pinCode) if body.pinCode.strip() else None),
        "serviceAreaKm": body.serviceAreaKm,
    }


def _decision_view(decision: TargetingDecision, body: TargetingValidateBody) -> dict:
    return {
        "allowed": decision.allowed,
        "reasonCode": decision.reason_code,
        # The recipient jurisdiction governs the gate; the other two are reported but kept separate.
        "recipient": _resolution_view(decision.recipient),
        "legalAddress": _resolution_view(decision.legal_address),
        "operationalArea": _resolution_view(decision.operational_area),
        "notes": list(decision.notes),
        "recipientStructured": _structured_view(body.recipientLocation),
    }


@router.post("/validate")
def validate_evidence(body: TargetingValidateBody) -> dict:
    """Validate recipient targeting evidence against the India-only jurisdiction rules.

    Stateless: no row is read or written, so the result and any denial are fully reproducible.
    """
    geography: TargetingGeography = TargetingGeography.from_locations(
        recipient_location=_to_location(body.recipientLocation),
        legal_address=_to_location(body.legalAddress),
        operational_area=_to_location(body.operationalArea),
    )
    decision = validate_targeting(geography)
    return _decision_view(decision, body)


@router.get("/reference")
def targeting_reference() -> dict:
    """The controlled geography vocabulary a consumer validates against (static reference data)."""
    return {
        "country": india_ref.INDIA_COUNTRY,
        "statesAndUts": india_ref.STATES_AND_UTS,
        "evidenceKinds": {
            "strong": sorted(k.value for k in EvidenceKind if k.value in _STRONG_VALUES),
            "weak": sorted(k.value for k in EvidenceKind if k.value not in _STRONG_VALUES),
        },
    }
