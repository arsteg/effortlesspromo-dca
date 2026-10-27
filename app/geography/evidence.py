"""DCA-038 — structured geography and location evidence value objects.

The headline of this story: model geography *structurally* (country, state/UT, city, district,
PIN/service area) and keep the three distinct geographic contexts **separate** so one can never stand
in for another:

- :data:`GeoContext.LEGAL_ADDRESS` — the business's registered/legal address.
- :data:`GeoContext.OPERATIONAL_AREA` — where the business actually operates / its service area.
- :data:`GeoContext.RECIPIENT_LOCATION` — where the *person being targeted* is. This is the only
  context that governs whether DCA personal-data acquisition is in-jurisdiction (India-only).

A business legally registered in India (legal address) that serves Gujarat (operational area) tells
you nothing about whether a specific recipient is in India — so the contexts are kept apart and each
resolves on its own evidence (``app/geography/resolver.py``).

Each :class:`LocationEvidence` item carries a :class:`EvidenceKind`. Kinds split into two buckets:

- **Strong proofs** resolve a *specific country* (a valid Indian PIN, a recognized state/UT, a GST
  state code, an explicitly declared country, a structured postal address with a country).
- **Weak signals** (a ``+91`` phone country code, a ``.in`` domain, a display language, an IP-geo
  guess, an employer's headquarters) never prove the recipient's jurisdiction on their own — the
  product spec is explicit: *do not guess India from +91, .in, language, or an employer's HQ alone*.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import Enum

from app.geography import india


class GeoContext(str, Enum):
    """The three geographic contexts, kept deliberately separate (DCA-038)."""

    LEGAL_ADDRESS = "legal_address"
    OPERATIONAL_AREA = "operational_area"
    RECIPIENT_LOCATION = "recipient_location"


class EvidenceKind(str, Enum):
    """What a piece of location evidence *is*. Strong proofs resolve a country; weak signals don't."""

    # Strong proofs — each can establish a specific country.
    POSTAL_ADDRESS = "postal_address"  # a structured address carrying an explicit country
    PIN_CODE = "pin_code"  # an Indian six-digit PIN
    GST_STATE_CODE = "gst_state_code"  # an Indian GSTIN state code (01-37, 97, 99)
    STATE_UT = "state_ut"  # a recognized Indian state/UT (code or name)
    DECLARED_COUNTRY = "declared_country"  # an explicitly declared/registered country

    # Weak signals — insufficient on their own, never a jurisdiction proof.
    PHONE_COUNTRY_CODE = "phone_country_code"  # e.g. "+91"
    DOMAIN_TLD = "domain_tld"  # e.g. ".in"
    LANGUAGE = "language"  # e.g. "hi"
    IP_GEO = "ip_geo"  # a geolocation guess
    EMPLOYER_HQ = "employer_hq"  # an employer's headquarters country


STRONG_KINDS: frozenset[EvidenceKind] = frozenset(
    {
        EvidenceKind.POSTAL_ADDRESS,
        EvidenceKind.PIN_CODE,
        EvidenceKind.GST_STATE_CODE,
        EvidenceKind.STATE_UT,
        EvidenceKind.DECLARED_COUNTRY,
    }
)
WEAK_KINDS: frozenset[EvidenceKind] = frozenset(EvidenceKind) - STRONG_KINDS


@dataclass(frozen=True)
class LocationEvidence:
    """One atomic location signal.

    ``value`` is the raw token (a PIN, a state code/name, a phone country code, …). ``country`` is the
    explicitly declared country code and is only meaningful for :data:`EvidenceKind.POSTAL_ADDRESS` /
    :data:`EvidenceKind.DECLARED_COUNTRY` (where the country is a first-class field of the evidence).
    """

    kind: EvidenceKind
    value: str = ""
    country: str = ""

    @property
    def is_strong(self) -> bool:
        return self.kind in STRONG_KINDS

    def proven_country(self) -> str | None:
        """The country this single piece of evidence *proves*, or ``None`` if it proves nothing.

        A weak signal always proves nothing. A malformed strong proof (a bad PIN, an unrecognized
        state, a missing country on an address) also proves nothing rather than guessing.
        """
        if self.kind is EvidenceKind.PIN_CODE:
            return india.INDIA_COUNTRY if india.is_valid_pin(self.value) else None
        if self.kind is EvidenceKind.GST_STATE_CODE:
            return india.INDIA_COUNTRY if india.is_valid_gst_state_code(self.value) else None
        if self.kind is EvidenceKind.STATE_UT:
            return india.INDIA_COUNTRY if india.normalize_state_ut(self.value) else None
        if self.kind is EvidenceKind.DECLARED_COUNTRY:
            return india.normalize_country(self.value) or None
        if self.kind is EvidenceKind.POSTAL_ADDRESS:
            # A structured address only proves a country when it actually carries one.
            return india.normalize_country(self.country) or None
        # Every weak signal: no proof.
        return None


@dataclass
class GeoLocation:
    """A structured place: country, state/UT, district, city, PIN, and an optional service radius.

    This is the structured-geography shape a Web/Admin form or an import would populate. Only the
    fields that are genuine jurisdiction proofs become strong evidence (country, state/UT, PIN). A
    bare city or district name is intentionally NOT a proof: a city name alone is ambiguous
    (e.g. "Hyderabad" exists in more than one country), so it never resolves a jurisdiction by itself.
    """

    country: str = ""
    state_ut: str = ""
    district: str = ""
    city: str = ""
    pin_code: str = ""
    service_area_km: float | None = None
    # Extra atomic signals attached to this place (weak signals like a +91 phone, or more proofs).
    evidence: list[LocationEvidence] = field(default_factory=list)

    def iter_evidence(self) -> Iterator[LocationEvidence]:
        """Decompose the populated structured fields into :class:`LocationEvidence`, then the extras.

        City/district/service-radius are descriptive only and emit no proof evidence (they never, on
        their own, establish a country) — keeping the resolver honest about what is actually evidenced.
        """
        if self.country.strip():
            yield LocationEvidence(
                EvidenceKind.DECLARED_COUNTRY, value=self.country, country=self.country
            )
        if self.state_ut.strip():
            yield LocationEvidence(EvidenceKind.STATE_UT, value=self.state_ut)
        if self.pin_code.strip():
            yield LocationEvidence(EvidenceKind.PIN_CODE, value=self.pin_code)
        yield from self.evidence


@dataclass
class TargetingGeography:
    """The three separate geographic contexts for one targeting evaluation.

    Each context is an independent bag of evidence. They are never merged: the recipient-location
    jurisdiction is resolved from the recipient's own evidence only, so an Indian legal address or
    operational area can never make a foreign/unknown recipient appear in-jurisdiction.
    """

    recipient_location: list[LocationEvidence] = field(default_factory=list)
    legal_address: list[LocationEvidence] = field(default_factory=list)
    operational_area: list[LocationEvidence] = field(default_factory=list)

    @classmethod
    def from_locations(
        cls,
        *,
        recipient_location: GeoLocation | None = None,
        legal_address: GeoLocation | None = None,
        operational_area: GeoLocation | None = None,
    ) -> TargetingGeography:
        """Build from structured :class:`GeoLocation` places, decomposing each into its evidence."""

        def flatten(loc: GeoLocation | None) -> list[LocationEvidence]:
            return list(loc.iter_evidence()) if loc is not None else []

        return cls(
            recipient_location=flatten(recipient_location),
            legal_address=flatten(legal_address),
            operational_area=flatten(operational_area),
        )


def evidence_from_dicts(items: Iterable[dict]) -> list[LocationEvidence]:
    """Build evidence from loosely-typed dicts (the API boundary), ignoring unknown kinds safely."""
    out: list[LocationEvidence] = []
    for item in items:
        try:
            kind = EvidenceKind(str(item.get("kind", "")).strip())
        except ValueError:
            # An unrecognized kind contributes nothing — it can never become a silent proof.
            continue
        out.append(
            LocationEvidence(
                kind=kind,
                value=str(item.get("value", "") or ""),
                country=str(item.get("country", "") or ""),
            )
        )
    return out
