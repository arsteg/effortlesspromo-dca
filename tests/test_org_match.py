"""DCA-067 — match organizations using strong evidence.

Pins the load-bearing behaviours of :mod:`app.matching.org_match` — the pure, deterministic identity
resolver that sits on top of the DCA-064/065/066 normalizers:

1. **Only a compatible typed registration identifier confirms identity.** Same ``cin``/``llpin`` =>
   same legal entity; same ``gstin`` => same establishment. Either => ``MATCH``.
2. **A conflicting legal-entity identifier refutes identity** and a shared name/domain cannot override
   it (=> ``NO_MATCH``).
3. **Different branches/groups are never collapsed from a shared name/domain alone** — weak-only
   overlap is a ``REVIEW`` candidate, and distinct establishment identifiers stay apart.
4. **Location corroborates/contradicts but never decides alone.**
5. **A denial changes nothing + the result is reproducible** (the resolver is pure).

The resolver is pure (no DB), so these run network-free with no datastore. The read-only observation
adapter is exercised on in-memory SQLite; the real transaction/RLS path for the underlying observation
tables is covered by ``tests/test_org_observation.py`` / ``tests/test_rls.py`` on disposable Postgres.
"""

from __future__ import annotations

from app.audit.sink import ActorType, AuditActor
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import OrgObservation
from app.geography.evidence import EvidenceKind, LocationEvidence
from app.matching import (
    Confidence,
    MatchOutcome,
    SignalKind,
    SignalRelation,
    SignalStrength,
    build_subject,
    match_organizations,
    subject_from_observation,
)
from app.normalization.org_identifiers import _gstin_check_digit
from app.services.org_observation import ExternalIdInput, ObservationInput, OrgObservationStore

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"
ACTOR = AuditActor(id="00000000-0000-4000-9000-0000000a0067", type=ActorType.USER)

# Synthetic, structurally-valid Indian identifiers (no real company).
CIN_ACME = "U72900MH2010PTC123456"
CIN_OTHER = "U72900KA2011PTC654321"
LLPIN_ACME = "AAB-1234"


def _gstin(state: str, pan: str, entity: str = "1") -> str:
    """Build a checksum-valid synthetic GSTIN: ``state`` (2 digits) + ``pan`` (10) + entity + Z + check."""
    body14 = f"{state}{pan}{entity}Z"
    return body14 + _gstin_check_digit(body14)


GSTIN_ACME_MH = _gstin("27", "AAAAA0000A")  # Maharashtra establishment
GSTIN_ACME_KA = _gstin("29", "AAAAA0000A")  # a DIFFERENT establishment (same PAN, another state)


# -- strong-identifier MATCH -------------------------------------------------------------------------


def test_same_legal_entity_identifier_is_a_match() -> None:
    a = build_subject(name="Acme Pvt Ltd", identifiers=[("cin", CIN_ACME)])
    b = build_subject(name="ACME PRIVATE LIMITED", identifiers=[("cin", CIN_ACME.lower())])
    result = match_organizations(a, b)
    assert result.outcome is MatchOutcome.MATCH
    assert result.confidence is Confidence.HIGH
    assert result.relation(SignalKind.LEGAL_ENTITY_IDENTIFIER) is SignalRelation.AGREE
    assert result.auto_merged is False


def test_same_establishment_identifier_is_a_match() -> None:
    a = build_subject(name="Acme", identifiers=[("gstin", GSTIN_ACME_MH)])
    b = build_subject(name="Acme Mumbai branch", identifiers=[("gstin", GSTIN_ACME_MH)])
    result = match_organizations(a, b)
    assert result.outcome is MatchOutcome.MATCH
    assert result.relation(SignalKind.ESTABLISHMENT_IDENTIFIER) is SignalRelation.AGREE


# -- strong-identifier NO_MATCH (the relevant denial) ------------------------------------------------


def test_conflicting_legal_entity_identifier_overrides_name_and_domain() -> None:
    # Same name AND same domain, but DIFFERENT CINs => different registered companies.
    a = build_subject(name="Acme Ltd", domains=["acme.in"], identifiers=[("cin", CIN_ACME)])
    b = build_subject(name="Acme Ltd", domains=["acme.in"], identifiers=[("cin", CIN_OTHER)])
    result = match_organizations(a, b)
    assert result.outcome is MatchOutcome.NO_MATCH
    assert result.confidence is Confidence.HIGH
    assert result.relation(SignalKind.LEGAL_ENTITY_IDENTIFIER) is SignalRelation.CONFLICT
    # The weak signals are still recorded, but they did NOT collapse the two companies.
    assert result.relation(SignalKind.DOMAIN) is SignalRelation.AGREE
    assert result.relation(SignalKind.NAME) is SignalRelation.AGREE
    assert result.auto_merged is False


# -- weak-only overlap stays a REVIEW candidate ------------------------------------------------------


def test_shared_domain_alone_is_a_review_candidate_not_a_match() -> None:
    a = build_subject(name="Acme North", domains=["acme.in"])
    b = build_subject(name="Acme South", domains=["acme.in"])
    result = match_organizations(a, b)
    assert result.outcome is MatchOutcome.REVIEW
    assert result.confidence is Confidence.LOW
    domain = next(s for s in result.signals if s.kind is SignalKind.DOMAIN)
    assert domain.strength is SignalStrength.WEAK


def test_shared_name_alone_is_a_review_candidate_not_a_match() -> None:
    a = build_subject(name="Acme Technologies")
    b = build_subject(name="acme technologies")
    result = match_organizations(a, b)
    assert result.outcome is MatchOutcome.REVIEW
    assert result.confidence is Confidence.LOW


def test_shared_name_and_domain_is_medium_review() -> None:
    a = build_subject(name="Acme", domains=["acme.in"])
    b = build_subject(name="ACME", domains=["acme.in"])
    result = match_organizations(a, b)
    assert result.outcome is MatchOutcome.REVIEW
    assert result.confidence is Confidence.MEDIUM


def test_distinct_establishments_with_shared_name_and_domain_are_not_collapsed() -> None:
    # Two branches of a group: same name, same corporate domain, but DIFFERENT GSTINs. They must stay
    # a review candidate (distinguishable), never an automatic match.
    a = build_subject(name="Acme", domains=["acme.in"], identifiers=[("gstin", GSTIN_ACME_MH)])
    b = build_subject(name="Acme", domains=["acme.in"], identifiers=[("gstin", GSTIN_ACME_KA)])
    result = match_organizations(a, b)
    assert result.outcome is MatchOutcome.REVIEW
    assert result.relation(SignalKind.ESTABLISHMENT_IDENTIFIER) is SignalRelation.DISTINCT


# -- location corroborates / contradicts, never decides ----------------------------------------------


def test_location_corroboration_promotes_low_to_medium() -> None:
    mh = [LocationEvidence(EvidenceKind.STATE_UT, value="MH")]
    a = build_subject(name="Acme", location=mh)
    b = build_subject(name="Acme", location=mh)
    result = match_organizations(a, b)
    assert result.outcome is MatchOutcome.REVIEW
    assert result.confidence is Confidence.MEDIUM  # name (low) + same state corroboration
    assert result.relation(SignalKind.LOCATION) is SignalRelation.CORROBORATE


def test_location_conflict_keeps_candidate_low_but_does_not_flip_outcome() -> None:
    a = build_subject(
        name="Acme", domains=["acme.in"], location=[LocationEvidence(EvidenceKind.STATE_UT, "MH")]
    )
    b = build_subject(
        name="Acme", domains=["acme.in"], location=[LocationEvidence(EvidenceKind.STATE_UT, "KA")]
    )
    result = match_organizations(a, b)
    # name + domain would be MEDIUM, but the conflicting state knocks it back to LOW...
    assert result.outcome is MatchOutcome.REVIEW
    assert result.confidence is Confidence.LOW
    assert result.relation(SignalKind.LOCATION) is SignalRelation.CONFLICT


def test_location_alone_is_too_weak_to_propose_a_candidate() -> None:
    mh = [LocationEvidence(EvidenceKind.STATE_UT, value="MH")]
    a = build_subject(name="Alpha", location=mh)
    b = build_subject(name="Beta", location=mh)
    result = match_organizations(a, b)
    assert result.outcome is MatchOutcome.INSUFFICIENT
    assert result.confidence is Confidence.NONE


# -- insufficient / cross-class identifiers ----------------------------------------------------------


def test_no_shared_evidence_is_insufficient() -> None:
    a = build_subject(name="Alpha", domains=["alpha.in"], identifiers=[("cin", CIN_ACME)])
    b = build_subject(name="Beta", domains=["beta.in"], identifiers=[("gstin", GSTIN_ACME_KA)])
    result = match_organizations(a, b)
    assert result.outcome is MatchOutcome.INSUFFICIENT


def test_cross_class_identifiers_do_not_confirm_or_refute() -> None:
    # One has a legal-entity id, the other only an establishment id (different levels, not comparable):
    # the shared name keeps them a review candidate rather than a match or a conflict.
    a = build_subject(name="Acme", identifiers=[("cin", CIN_ACME)])
    b = build_subject(name="Acme", identifiers=[("gstin", GSTIN_ACME_KA)])
    result = match_organizations(a, b)
    assert result.outcome is MatchOutcome.REVIEW


def test_llpin_agreement_is_a_match() -> None:
    a = build_subject(name="Acme LLP", identifiers=[("llpin", LLPIN_ACME)])
    b = build_subject(name="acme llp", identifiers=[("llpin", "AAB1234")])
    assert match_organizations(a, b).outcome is MatchOutcome.MATCH


# -- builder collects denials; malformed values never become evidence --------------------------------


def test_build_subject_collects_denials_without_raising() -> None:
    subject = build_subject(
        name="\x00\x00",  # non-blank but holds no normalizable content -> refused
        domains=["not a domain"],  # malformed -> refused
        identifiers=[("gstin", "BADGSTIN")],  # malformed -> refused
    )
    assert subject.name is None
    assert subject.domains == ()
    assert subject.identifiers == ()
    fields = {d.field for d in subject.denials}
    assert fields == {"name", "domain", "identifier"}


def test_malformed_identifier_does_not_fabricate_a_match() -> None:
    a = build_subject(name="Acme", identifiers=[("cin", "GARBAGE")])
    b = build_subject(name="Acme", identifiers=[("cin", "GARBAGE")])
    result = match_organizations(a, b)
    # Both CINs were refused (collected as denials), so there is NO strong identifier to match on —
    # only the shared name remains, i.e. a weak review candidate, never a confirmed match.
    assert result.outcome is MatchOutcome.REVIEW
    assert all(d.field == "identifier" for d in a.denials)


# -- purity / reproducibility ------------------------------------------------------------------------


def test_result_is_reproducible_and_symmetric() -> None:
    a = build_subject(name="Acme", domains=["acme.in"], identifiers=[("cin", CIN_ACME)])
    b = build_subject(name="Acme", domains=["acme.in"], identifiers=[("cin", CIN_OTHER)])
    first = match_organizations(a, b)
    second = match_organizations(a, b)
    reversed_ = match_organizations(b, a)
    assert first.outcome is second.outcome is reversed_.outcome is MatchOutcome.NO_MATCH
    assert first.confidence is second.confidence is reversed_.confidence


# -- read-only, tenant-scoped observation adapter ----------------------------------------------------


def _session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _record(store: OrgObservationStore, session, name: str, gstin: str) -> OrgObservation:
    obs = store.create(
        ObservationInput(
            entity_type="legal_entity",
            display_name=name,
            external_ids=[ExternalIdInput(scheme="gstin", value=gstin)],
        ),
        ACTOR,
    )
    session.flush()
    return obs


def test_subject_from_observation_matches_via_each_tenants_own_store() -> None:
    # The SAME real GSTIN legitimately recorded by two businesses (the mapping is per-business). Each
    # subject is built through that business's OWN scoped store — neither store reads the other's rows —
    # yet the pure matcher still concludes the two observations describe the same establishment.
    factory = _session_factory()
    with factory() as session:
        store_a = OrgObservationStore(session, BIZ_A)
        store_b = OrgObservationStore(session, BIZ_B)
        obs_a = _record(store_a, session, "Acme Pvt Ltd", GSTIN_ACME_MH)
        obs_b = _record(store_b, session, "Acme (Mumbai)", GSTIN_ACME_MH)

        # Tenant isolation: A's store cannot see B's observation.
        assert store_a.get(obs_b.id) is None

        subj_a = subject_from_observation(store_a, obs_a)
        subj_b = subject_from_observation(store_b, obs_b)

    result = match_organizations(subj_a, subj_b)
    assert result.outcome is MatchOutcome.MATCH
    assert result.relation(SignalKind.ESTABLISHMENT_IDENTIFIER) is SignalRelation.AGREE
    # No write occurred: the adapter is read-only and the matcher merges nothing.
    assert result.auto_merged is False


def test_observation_adapter_reads_state_from_the_row() -> None:
    factory = _session_factory()
    with factory() as session:
        store = OrgObservationStore(session, BIZ_A)
        obs = store.create(
            ObservationInput(
                entity_type="establishment",
                display_name="Acme Maharashtra",
                parent_observation_id=_record(store, session, "Acme Parent", GSTIN_ACME_KA).id,
                state_code="MH",
                external_ids=[ExternalIdInput(scheme="gstin", value=GSTIN_ACME_MH)],
            ),
            ACTOR,
        )
        session.flush()
        subject = subject_from_observation(store, obs)
    assert subject.state_codes() == frozenset({"MH"})
