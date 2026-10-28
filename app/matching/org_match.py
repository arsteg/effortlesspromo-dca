"""DCA-067 — match organizations using STRONG evidence (pure, deterministic identity resolution).

This is the "identity resolution" layer the normalization siblings (DCA-064 names/domains, DCA-065
typed identifiers, DCA-066 phones) each deliberately deferred: those modules derive repeatable search
keys and typed, structurally-validated identifiers but record and merge *nothing*, because deciding
that two observations are the SAME organization is an evidence decision, not a normalization side
effect. :func:`match_organizations` makes that decision — and only that decision.

Like its siblings it is pure, network-free, deterministic Python: no model, no DB write, no HTTP, no
registry lookup. It compares two :class:`OrgMatchSubject` bundles of already-normalized evidence and
returns an :class:`OrgMatchResult`. It NEVER mutates anything and NEVER merges two observations: a
``MATCH`` is an auditable *conclusion* a later (disabled) capability could act on, never an automatic
collapse (``auto_merged`` is always ``False``).

The evidence hierarchy (the DCA-067 acceptance criteria):

- **Only a compatible typed registration identifier is strong enough to confirm identity.** Two
  subjects that agree on a legal-entity identifier (the SAME ``cin`` or ``llpin``) are the SAME legal
  entity; two that agree on an establishment identifier (the SAME ``gstin``) are the SAME
  establishment. Either yields ``MATCH``.

- **A conflicting legal-entity identifier refutes identity.** Two subjects that each carry a
  legal-entity identifier of the same scheme with DIFFERENT values are DIFFERENT registered companies
  (``NO_MATCH``) — and a shared name or domain can never override that.

- **Different branches/groups are never collapsed from a shared name/domain alone.** A shared company
  name and/or a shared corporate domain is a WEAK signal: many branches of one group and even
  unrelated firms share a name or a domain. A weak-only overlap is a ``REVIEW`` candidate — surfaced
  for a human / further evidence — never a confirmed ``MATCH``. DISTINCT establishment identifiers
  (two different ``gstin``s) are recorded as a *distinguishing* signal so same-name establishments in
  different registrations stay apart.

- **Location evidence corroborates or contradicts; it never decides alone.** A shared resolved country
  / Indian state/UT raises a weak candidate's confidence; a conflicting one lowers it. Location on its
  own (no name/domain anchor) is far too weak to even propose a candidate, so it never creates one and
  never flips the categorical outcome — that stays driven by identifiers (strong) and name/domain
  (weak).

- **A denial changes nothing.** The function is pure, so both a positive result and the relevant denial
  (``NO_MATCH`` on a conflicting identifier, or the refusal to collapse branches to ``REVIEW``) are
  perfectly reproducible and leave no state behind. Building a subject from raw strings
  (:func:`build_subject`) runs them through the existing normalizers and COLLECTS any refusal as a
  :class:`OrgMatchDenial` rather than letting a malformed value silently masquerade as evidence.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from app.geography import india
from app.geography.evidence import EvidenceKind, LocationEvidence
from app.normalization.domains import NormalizedDomain, normalize_domain
from app.normalization.errors import NormalizationError
from app.normalization.names import NormalizedName, normalize_company_name
from app.normalization.org_identifiers import (
    ENTITY_CLASS_LEGAL_ENTITY,
    ValidatedOrgIdentifier,
    validate_org_identifier,
)

if TYPE_CHECKING:
    from app.db.models import OrgObservation
    from app.services.org_observation import OrgObservationStore


class MatchOutcome(str, Enum):
    """The categorical identity-resolution decision for a pair of organization observations.

    - ``MATCH`` — strong evidence (a compatible typed registration identifier) confirms the SAME
      organization. A conclusion only: it never merges the observations.
    - ``NO_MATCH`` — strong evidence (a conflicting legal-entity identifier) proves they are DIFFERENT
      registered organizations; a shared name/domain cannot override it.
    - ``REVIEW`` — only WEAK evidence overlaps (a shared name and/or domain, optionally corroborated by
      location): a candidate to surface for a human / further evidence, never an automatic match.
    - ``INSUFFICIENT`` — no match-worthy evidence overlaps (nothing, or only a location signal with no
      name/domain anchor), so not even a candidate is proposed.
    """

    MATCH = "match"
    NO_MATCH = "no_match"
    REVIEW = "review"
    INSUFFICIENT = "insufficient"


class Confidence(str, Enum):
    """How strongly the evidence supports the outcome. ``HIGH`` is reserved for a strong-identifier
    ``MATCH`` / ``NO_MATCH``; a ``REVIEW`` candidate is at most ``MEDIUM`` (never promoted on
    name/domain/location alone); ``INSUFFICIENT`` is ``NONE``."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


class SignalKind(str, Enum):
    """Which evidence dimension a :class:`MatchSignal` came from."""

    LEGAL_ENTITY_IDENTIFIER = "legal_entity_identifier"
    ESTABLISHMENT_IDENTIFIER = "establishment_identifier"
    DOMAIN = "domain"
    NAME = "name"
    LOCATION = "location"


class SignalStrength(str, Enum):
    """Whether a signal is strong enough to decide identity on its own (``STRONG``) or merely a
    clustering hint (``WEAK``)."""

    STRONG = "strong"
    WEAK = "weak"


class SignalRelation(str, Enum):
    """What the signal says about the pair.

    - ``AGREE`` — the two subjects share this evidence (same identifier value / host / name key /
      location).
    - ``CONFLICT`` — they carry comparable evidence of this kind that DISAGREES (different legal-entity
      identifiers; disjoint resolved countries/states).
    - ``DISTINCT`` — they carry establishment identifiers that differ: not a conflict (they may be
      branches of one group) but a reason NOT to collapse them on name/domain.
    - ``CORROBORATE`` — a weak, supporting agreement that strengthens (never creates) a candidate.
    """

    AGREE = "agree"
    CONFLICT = "conflict"
    DISTINCT = "distinct"
    CORROBORATE = "corroborate"


@dataclass(frozen=True)
class MatchSignal:
    """One piece of evidence that contributed to the decision, with its strength and relation.

    ``detail`` is an opaque, human-readable summary (scheme + whether values agreed, a shared host, a
    shared state code). It carries company facts (a domain, a state) only — never contact data.
    """

    kind: SignalKind
    strength: SignalStrength
    relation: SignalRelation
    detail: str


@dataclass(frozen=True)
class OrgMatchDenial:
    """A raw input :func:`build_subject` could not normalize/validate, captured rather than raised.

    ``field`` names the input bucket (``name`` / ``domain`` / ``identifier``); ``code`` is the stable
    :class:`~app.normalization.errors.NormalizationError` code; ``detail`` echoes the offending input.
    Collecting (not raising) keeps a malformed value from silently becoming evidence while still letting
    the valid evidence be matched.
    """

    field: str
    code: str
    detail: str


@dataclass(frozen=True)
class OrgMatchSubject:
    """One organization's already-normalized evidence bundle — the unit :func:`match_organizations`
    compares. Build one with :func:`build_subject` (from raw strings) or
    :func:`subject_from_observation` (from a stored, tenant-scoped observation).

    Nothing here is tenant state: a subject is a value object, and matching two subjects records and
    merges nothing. ``denials`` surfaces any raw input that failed to normalize when the subject was
    built.
    """

    name: NormalizedName | None = None
    domains: tuple[NormalizedDomain, ...] = ()
    identifiers: tuple[ValidatedOrgIdentifier, ...] = ()
    location: tuple[LocationEvidence, ...] = ()
    denials: tuple[OrgMatchDenial, ...] = ()

    def proven_countries(self) -> frozenset[str]:
        """Countries this subject's STRONG location evidence (and identifiers) actually prove."""
        countries: set[str] = set()
        for ev in self.location:
            proven = ev.proven_country()
            if proven:
                countries.add(proven)
        # A validated Indian identifier (any scheme) places the legal entity/establishment in India.
        if self.identifiers:
            countries.add(india.INDIA_COUNTRY)
        return frozenset(countries)

    def state_codes(self) -> frozenset[str]:
        """The recognized Indian state/UT codes this subject's evidence pins (from STATE_UT /
        GST_STATE_CODE location evidence and from identifiers that embed a jurisdiction)."""
        codes: set[str] = set()
        for ev in self.location:
            if ev.kind is EvidenceKind.STATE_UT:
                resolved = india.normalize_state_ut(ev.value)
                if resolved:
                    codes.add(resolved)
            elif ev.kind is EvidenceKind.GST_STATE_CODE and india.is_valid_gst_state_code(ev.value):
                # A GST numeric state code is kept in its own code space (prefixed) so it is only ever
                # compared like-with-like and never mistaken for an ISO state/UT code.
                codes.add(f"gst:{ev.value}")
        for ident in self.identifiers:
            if ident.state_code:
                resolved = india.normalize_state_ut(ident.state_code)
                if resolved:
                    codes.add(resolved)
        return frozenset(codes)


# -- subject builders --------------------------------------------------------------------------------


def build_subject(
    *,
    name: str | None = None,
    domains: Iterable[str] = (),
    identifiers: Iterable[tuple[str, str]] = (),
    location: Iterable[LocationEvidence] = (),
) -> OrgMatchSubject:
    """Build an :class:`OrgMatchSubject` from RAW strings, running each through its existing normalizer.

    ``identifiers`` are ``(scheme, value)`` pairs validated by DCA-065
    (:func:`~app.normalization.org_identifiers.validate_org_identifier`). Any input that fails to
    normalize/validate is COLLECTED as an :class:`OrgMatchDenial` (never raised), so a malformed value
    cannot masquerade as evidence and the refusal is visible and reproducible. Pure — mutates nothing.
    """
    denials: list[OrgMatchDenial] = []

    normalized_name: NormalizedName | None = None
    if name is not None and name.strip():
        try:
            normalized_name = normalize_company_name(name)
        except NormalizationError as exc:
            denials.append(OrgMatchDenial(field="name", code=exc.code, detail=exc.detail or name))

    normalized_domains: list[NormalizedDomain] = []
    for raw in domains:
        try:
            normalized_domains.append(normalize_domain(raw))
        except NormalizationError as exc:
            denials.append(OrgMatchDenial(field="domain", code=exc.code, detail=exc.detail or raw))

    validated_ids: list[ValidatedOrgIdentifier] = []
    for scheme, value in identifiers:
        try:
            validated_ids.append(validate_org_identifier(scheme, value))
        except NormalizationError as exc:
            denials.append(
                OrgMatchDenial(field="identifier", code=exc.code, detail=exc.detail or value)
            )

    return OrgMatchSubject(
        name=normalized_name,
        domains=tuple(normalized_domains),
        identifiers=tuple(validated_ids),
        location=tuple(location),
        denials=tuple(denials),
    )


# -- the decision ------------------------------------------------------------------------------------


# The typed-identifier schemes whose DISAGREEMENT proves two DIFFERENT registered organizations.
# A GSTIN is deliberately excluded: two different GSTINs are two establishments (possibly of one
# legal-entity group), not two different organizations — so they must never produce NO_MATCH.
_LEGAL_ENTITY_CLASS = ENTITY_CLASS_LEGAL_ENTITY


def _identifiers_by_scheme(
    subject: OrgMatchSubject,
) -> dict[str, set[str]]:
    by_scheme: dict[str, set[str]] = {}
    for ident in subject.identifiers:
        by_scheme.setdefault(ident.scheme, set()).add(ident.normalized)
    return by_scheme


def _entity_class(subject: OrgMatchSubject, scheme: str) -> str:
    for ident in subject.identifiers:
        if ident.scheme == scheme:
            return ident.entity_class
    return ""


def _identifier_signals(
    a: OrgMatchSubject, b: OrgMatchSubject
) -> tuple[list[MatchSignal], bool, bool]:
    """Compare typed identifiers. Returns (signals, has_strong_agreement, has_legal_entity_conflict)."""
    signals: list[MatchSignal] = []
    has_agreement = False
    has_conflict = False

    a_by = _identifiers_by_scheme(a)
    b_by = _identifiers_by_scheme(b)
    for scheme in sorted(set(a_by) & set(b_by)):
        values_a, values_b = a_by[scheme], b_by[scheme]
        entity_class = _entity_class(a, scheme)
        is_legal_entity = entity_class == _LEGAL_ENTITY_CLASS
        kind = (
            SignalKind.LEGAL_ENTITY_IDENTIFIER
            if is_legal_entity
            else SignalKind.ESTABLISHMENT_IDENTIFIER
        )
        shared = values_a & values_b
        if shared:
            has_agreement = True
            signals.append(
                MatchSignal(
                    kind=kind,
                    strength=SignalStrength.STRONG,
                    relation=SignalRelation.AGREE,
                    detail=f"{scheme}: same registration identifier",
                )
            )
        elif is_legal_entity:
            # Both carry a legal-entity id of this scheme and they disagree -> different companies.
            has_conflict = True
            signals.append(
                MatchSignal(
                    kind=kind,
                    strength=SignalStrength.STRONG,
                    relation=SignalRelation.CONFLICT,
                    detail=f"{scheme}: different legal-entity identifiers",
                )
            )
        else:
            # Different establishment identifiers (e.g. two GSTINs): not a conflict (they may be
            # branches of one group), but a reason NOT to collapse them on name/domain alone.
            signals.append(
                MatchSignal(
                    kind=kind,
                    strength=SignalStrength.STRONG,
                    relation=SignalRelation.DISTINCT,
                    detail=f"{scheme}: distinct establishment registrations",
                )
            )
    return signals, has_agreement, has_conflict


def _domain_signal(a: OrgMatchSubject, b: OrgMatchSubject) -> MatchSignal | None:
    hosts_a = {d.ascii for d in a.domains if d.ascii}
    hosts_b = {d.ascii for d in b.domains if d.ascii}
    shared = hosts_a & hosts_b
    if not shared:
        return None
    return MatchSignal(
        kind=SignalKind.DOMAIN,
        strength=SignalStrength.WEAK,
        relation=SignalRelation.AGREE,
        detail=f"shared host: {sorted(shared)[0]}",
    )


def _name_signal(a: OrgMatchSubject, b: OrgMatchSubject) -> MatchSignal | None:
    if a.name is None or b.name is None:
        return None
    if a.name.match_key and a.name.match_key == b.name.match_key:
        return MatchSignal(
            kind=SignalKind.NAME,
            strength=SignalStrength.WEAK,
            relation=SignalRelation.AGREE,
            detail="shared name match key",
        )
    return None


def _location_signal(a: OrgMatchSubject, b: OrgMatchSubject) -> MatchSignal | None:
    """A corroborating or conflicting location signal (never decisive on its own)."""
    states_a, states_b = a.state_codes(), b.state_codes()
    if states_a and states_b:
        shared_states = states_a & states_b
        if shared_states:
            return MatchSignal(
                kind=SignalKind.LOCATION,
                strength=SignalStrength.WEAK,
                relation=SignalRelation.CORROBORATE,
                detail=f"shared state/UT: {sorted(shared_states)[0]}",
            )
        return MatchSignal(
            kind=SignalKind.LOCATION,
            strength=SignalStrength.WEAK,
            relation=SignalRelation.CONFLICT,
            detail="different states/UTs",
        )
    countries_a, countries_b = a.proven_countries(), b.proven_countries()
    if countries_a and countries_b:
        shared_countries = countries_a & countries_b
        if shared_countries:
            return MatchSignal(
                kind=SignalKind.LOCATION,
                strength=SignalStrength.WEAK,
                relation=SignalRelation.CORROBORATE,
                detail=f"shared country: {sorted(shared_countries)[0]}",
            )
        return MatchSignal(
            kind=SignalKind.LOCATION,
            strength=SignalStrength.WEAK,
            relation=SignalRelation.CONFLICT,
            detail="different proven countries",
        )
    return None


@dataclass(frozen=True)
class OrgMatchResult:
    """The identity-resolution decision for a pair of subjects.

    ``outcome`` is the categorical decision; ``confidence`` grades the evidence; ``signals`` lists every
    evidence dimension that contributed (strong agreements/conflicts, weak name/domain/location hints).
    ``auto_merged`` is ALWAYS ``False`` — a ``MATCH`` is a conclusion, never an automatic collapse of
    two observations (merging stays a disabled future capability).
    """

    outcome: MatchOutcome
    confidence: Confidence
    signals: tuple[MatchSignal, ...] = ()
    auto_merged: bool = False

    def relation(self, kind: SignalKind) -> SignalRelation | None:
        """The relation recorded for one evidence dimension, or ``None`` if it did not contribute."""
        for signal in self.signals:
            if signal.kind is kind:
                return signal.relation
        return None


def match_organizations(a: OrgMatchSubject, b: OrgMatchSubject) -> OrgMatchResult:
    """Decide whether two organization subjects are the same, using strong evidence first.

    Deterministic and pure (mutates nothing, merges nothing). Resolution order:

    1. A compatible typed registration identifier that AGREES (same ``cin`` / ``llpin`` -> same legal
       entity; same ``gstin`` -> same establishment) => ``MATCH`` (``HIGH``).
    2. Otherwise, a legal-entity identifier that CONFLICTS (different ``cin`` / ``llpin``) => ``NO_MATCH``
       (``HIGH``) — a shared name/domain can never override it.
    3. Otherwise, a shared name and/or domain => ``REVIEW`` candidate (``LOW``/``MEDIUM``), raised or
       lowered by any location corroboration/conflict but never promoted past ``MEDIUM`` or flipped to a
       match — so different branches/groups are never collapsed from a shared name/domain alone.
    4. Otherwise => ``INSUFFICIENT`` (``NONE``).
    """
    id_signals, has_agreement, has_conflict = _identifier_signals(a, b)
    domain_signal = _domain_signal(a, b)
    name_signal = _name_signal(a, b)
    location_signal = _location_signal(a, b)

    # 1) Strong identifier agreement confirms identity.
    if has_agreement:
        signals = _collect(id_signals, domain_signal, name_signal, location_signal)
        return OrgMatchResult(MatchOutcome.MATCH, Confidence.HIGH, signals)

    # 2) Conflicting legal-entity identifier refutes identity (name/domain cannot override it).
    if has_conflict:
        signals = _collect(id_signals, domain_signal, name_signal, location_signal)
        return OrgMatchResult(MatchOutcome.NO_MATCH, Confidence.HIGH, signals)

    # 3) Weak evidence only — a candidate for review, anchored by name and/or domain.
    weak_anchors = [s for s in (domain_signal, name_signal) if s is not None]
    if weak_anchors:
        confidence = Confidence.MEDIUM if len(weak_anchors) >= 2 else Confidence.LOW
        if location_signal is not None:
            if location_signal.relation is SignalRelation.CONFLICT:
                confidence = Confidence.LOW
            elif (
                location_signal.relation is SignalRelation.CORROBORATE
                and confidence is Confidence.LOW
            ):
                confidence = Confidence.MEDIUM
        signals = _collect(id_signals, domain_signal, name_signal, location_signal)
        return OrgMatchResult(MatchOutcome.REVIEW, confidence, signals)

    # 4) Nothing match-worthy overlaps (a lone location signal is too weak to propose a candidate).
    signals = _collect(id_signals, domain_signal, name_signal, location_signal)
    return OrgMatchResult(MatchOutcome.INSUFFICIENT, Confidence.NONE, signals)


def _collect(
    id_signals: Sequence[MatchSignal], *optional: MatchSignal | None
) -> tuple[MatchSignal, ...]:
    out: list[MatchSignal] = list(id_signals)
    out.extend(s for s in optional if s is not None)
    return tuple(out)


# -- read-only, tenant-scoped adapter over stored observations --------------------------------------


# The DCA-059 external-id schemes that map onto a DCA-065 validated identifier (the only ones whose
# structure/type this matcher can verify). Other stored schemes (pan/lei/tan/duns/platform_org_id) are
# left out of strong-identifier comparison here — matching never treats an unverifiable scheme as proof.
_MATCHABLE_SCHEMES = frozenset({"cin", "llpin", "gstin"})


def subject_from_observation(
    store: "OrgObservationStore",
    observation: "OrgObservation",
) -> OrgMatchSubject:
    """Build a subject from ONE stored, tenant-scoped :class:`~app.db.models.OrgObservation`.

    ``store`` is an :class:`~app.services.org_observation.OrgObservationStore` already bound to the
    observation's business, so every read stays within that tenant — matching is a WITHIN-business
    candidate-resolution aid and never reaches across tenants. Read-only: it stages/commits nothing.

    Only external ids whose scheme DCA-065 can structurally validate (``cin`` / ``llpin`` / ``gstin``)
    become strong identifiers; an id that fails validation is dropped from the strong comparison (it was
    already validated at write time, but matching re-checks rather than trusting a stored string blindly).
    The observation's ``state_code`` contributes a STATE_UT location signal.
    """
    name_text = observation.display_name or ""
    identifiers: list[tuple[str, str]] = []
    for mapping in store.external_ids_of(observation.id):
        if mapping.scheme in _MATCHABLE_SCHEMES:
            identifiers.append((mapping.scheme, mapping.value))

    location: list[LocationEvidence] = []
    if observation.state_code:
        location.append(LocationEvidence(EvidenceKind.STATE_UT, value=observation.state_code))

    return build_subject(name=name_text, identifiers=identifiers, location=location)
