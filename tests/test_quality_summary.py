"""DCA-073 — expose separate quality dimensions.

Pins the load-bearing behaviours of :mod:`app.quality.quality_summary` — the pure, deterministic
conclusion layer that grades FIVE independent quality dimensions (identity confidence, freshness,
completeness, deliverability, business fit) and carries the DCA-055 permission decision through on a
SEPARATE eligibility axis:

1. **Quality is strictly separate from eligibility — a high business-fit score NEVER changes a blocked
   permission outcome.** Raising ``business_fit`` from low to the top grade leaves a ``BLOCKED``
   eligibility exactly ``BLOCKED`` (the relevant denial). Eligibility is never derived from a quality
   grade.
2. **Catch-all deliverability remains UNCERTAIN, never "deliverable".** A catch-all / accept-all
   domain is graded ``UNCERTAIN`` even when it "accepts" the address; only a verified mailbox on a
   non-catch-all domain earns ``DELIVERABLE``.
3. **Reproducible, pure, no state change.** The same evidence at the same ``now`` always yields an
   identical summary; the summarizer reads only what it is handed and records / mutates nothing.

The summarizer is pure (no DB), so these run network-free. The read-only observation adapters are
exercised on in-memory SQLite; the durable transaction path for the underlying field-observation
table is covered on a disposable PostgreSQL (``DCA_TEST_DATABASE_URL``) by the test at the end and by
``tests/test_field_observation.py`` / ``tests/test_rls.py`` — never relying on SQLite for the durable
path.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine

from app.audit.sink import ActorType, AuditActor
from app.compliance.policy_engine import PolicyDecision
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import FieldObservation
from app.matching.org_match import Confidence, MatchOutcome
from app.quality import (
    REASON_DELIVERABILITY_CATCH_ALL,
    REASON_DELIVERABILITY_INVALID,
    REASON_DELIVERABILITY_NO_CONTACT_POINT,
    REASON_DELIVERABILITY_VERIFIED,
    REASON_FRESHNESS_FRESH,
    REASON_FRESHNESS_NO_OBSERVATIONS,
    REASON_FRESHNESS_STALE,
    REASON_IDENTITY_AMBIGUOUS,
    REASON_IDENTITY_STRONG,
    REASON_IDENTITY_UNRESOLVED,
    BusinessFitSignal,
    CompletenessSignal,
    Deliverability,
    DeliverabilitySignal,
    Eligibility,
    EligibilityInput,
    FreshnessSignal,
    IdentitySignal,
    QualityDimension,
    QualityEvidence,
    QualityLevel,
    completeness_from_observations,
    freshness_from_observations,
    summarize_quality,
)
from app.services.field_observation import FieldObservationInput, FieldObservationStore
from app.services.org_observation import ObservationInput, OrgObservationStore
from app.services.source_catalog import SourceCatalogStore, SourceDraftInput
from app.services.source_terms import SourceTermsInput, SourceTermsStore

BIZ_A = "11111111-1111-1111-1111-111111111111"
ACTOR = AuditActor(id="00000000-0000-4000-9000-0000000a0073", type=ActorType.USER)
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def _evidence(**over) -> QualityEvidence:
    base = dict(
        identity=IdentitySignal(outcome=MatchOutcome.MATCH, confidence=Confidence.HIGH),
        freshness=FreshnessSignal(observed_ats=(NOW - timedelta(days=5),)),
        completeness=CompletenessSignal(
            expected_fields=frozenset({"a", "b", "c", "d"}),
            present_fields=frozenset({"a", "b", "c", "d"}),
        ),
        deliverability=(DeliverabilitySignal(verified=True),),
        business_fit=BusinessFitSignal(score=0.9),
        eligibility=EligibilityInput(decision=PolicyDecision.ALLOW, reason="allowed_by_rule"),
    )
    base.update(over)
    return QualityEvidence(**base)


# --- acceptance 1: quality is separate from eligibility -------------------------------------------


def test_high_fit_never_changes_a_blocked_permission_outcome():
    """A top business-fit grade cannot flip a BLOCKED eligibility — the two are separate axes."""
    blocked = EligibilityInput(decision=PolicyDecision.DENY, reason="denied_by_rule")

    low_fit = summarize_quality(
        _evidence(business_fit=BusinessFitSignal(score=0.1), eligibility=blocked), now=NOW
    )
    high_fit = summarize_quality(
        _evidence(business_fit=BusinessFitSignal(score=0.99), eligibility=blocked), now=NOW
    )

    # Business fit genuinely changed with the score...
    assert low_fit.business_fit.rating == QualityLevel.LOW.value
    assert high_fit.business_fit.rating == QualityLevel.HIGH.value
    # ...but the blocked permission outcome is identical either way — fit grants nothing.
    assert low_fit.eligibility.eligibility is Eligibility.BLOCKED
    assert high_fit.eligibility.eligibility is Eligibility.BLOCKED
    assert high_fit.eligibility.reason == "denied_by_rule"
    # Eligibility is NOT one of the five quality dimensions.
    assert all(d.dimension is not None for d in high_fit.dimensions())
    assert len(high_fit.dimensions()) == 5
    assert QualityDimension.BUSINESS_FIT in {d.dimension for d in high_fit.dimensions()}


def test_eligibility_passthrough_maps_every_policy_decision():
    for decision, expected in [
        (PolicyDecision.ALLOW, Eligibility.ALLOWED),
        (PolicyDecision.DENY, Eligibility.BLOCKED),
        (PolicyDecision.REVIEW, Eligibility.REVIEW),
        (PolicyDecision.NOT_APPLICABLE, Eligibility.NOT_APPLICABLE),
    ]:
        summary = summarize_quality(
            _evidence(eligibility=EligibilityInput(decision=decision, reason="r")), now=NOW
        )
        assert summary.eligibility.eligibility is expected


def test_eligibility_accepts_string_decision_and_unknown_is_fail_safe():
    assert (
        summarize_quality(
            _evidence(eligibility=EligibilityInput(decision="deny")), now=NOW
        ).eligibility.eligibility
        is Eligibility.BLOCKED
    )
    # No decision supplied, or an unrecognized one, is UNKNOWN (never silently ALLOWED).
    assert (
        summarize_quality(
            _evidence(eligibility=EligibilityInput()), now=NOW
        ).eligibility.eligibility
        is Eligibility.UNKNOWN
    )
    assert (
        summarize_quality(
            _evidence(eligibility=EligibilityInput(decision="bogus")), now=NOW
        ).eligibility.eligibility
        is Eligibility.UNKNOWN
    )


# --- acceptance 2: catch-all deliverability stays uncertain ---------------------------------------


def test_catch_all_deliverability_is_uncertain_not_deliverable():
    """A catch-all / accept-all domain accepts anything, so deliverability stays UNCERTAIN — even when
    the address is 'verified'/accepted by the domain. It is never upgraded to DELIVERABLE."""
    summary = summarize_quality(
        _evidence(deliverability=(DeliverabilitySignal(verified=True, catch_all=True),)), now=NOW
    )
    assert summary.deliverability.rating == Deliverability.UNCERTAIN.value
    assert summary.deliverability.rating != Deliverability.DELIVERABLE.value
    assert summary.deliverability.reason == REASON_DELIVERABILITY_CATCH_ALL


def test_verified_non_catch_all_mailbox_is_deliverable():
    summary = summarize_quality(
        _evidence(deliverability=(DeliverabilitySignal(verified=True, catch_all=False),)), now=NOW
    )
    assert summary.deliverability.rating == Deliverability.DELIVERABLE.value
    assert summary.deliverability.reason == REASON_DELIVERABILITY_VERIFIED


def test_undeliverable_dominates_and_no_contact_is_unknown():
    bounced = summarize_quality(
        _evidence(deliverability=(DeliverabilitySignal(undeliverable=True),)), now=NOW
    )
    assert bounced.deliverability.rating == Deliverability.UNDELIVERABLE.value
    assert bounced.deliverability.reason == REASON_DELIVERABILITY_INVALID

    none = summarize_quality(_evidence(deliverability=()), now=NOW)
    assert none.deliverability.rating == Deliverability.UNKNOWN.value
    assert none.deliverability.reason == REASON_DELIVERABILITY_NO_CONTACT_POINT


def test_undeliverable_beats_a_verified_mailbox_on_a_multi_point_record():
    # Even with a good mailbox present, a hard failure on another point is reported conservatively.
    summary = summarize_quality(
        _evidence(
            deliverability=(
                DeliverabilitySignal(verified=True, catch_all=False),
                DeliverabilitySignal(undeliverable=True),
            )
        ),
        now=NOW,
    )
    assert summary.deliverability.rating == Deliverability.UNDELIVERABLE.value


# --- the remaining dimensions grade independently -------------------------------------------------


def test_identity_confidence_maps_outcome_and_confidence():
    strong = summarize_quality(
        _evidence(identity=IdentitySignal(MatchOutcome.MATCH, Confidence.HIGH)), now=NOW
    )
    assert strong.identity_confidence.rating == QualityLevel.HIGH.value
    assert strong.identity_confidence.reason == REASON_IDENTITY_STRONG

    review = summarize_quality(
        _evidence(identity=IdentitySignal(MatchOutcome.REVIEW, Confidence.MEDIUM)), now=NOW
    )
    assert review.identity_confidence.rating == QualityLevel.LOW.value
    assert review.identity_confidence.reason == REASON_IDENTITY_AMBIGUOUS

    unresolved = summarize_quality(_evidence(identity=IdentitySignal()), now=NOW)
    assert unresolved.identity_confidence.rating == QualityLevel.UNKNOWN.value
    assert unresolved.identity_confidence.reason == REASON_IDENTITY_UNRESOLVED


def test_freshness_grades_on_the_oldest_observation():
    fresh = summarize_quality(
        _evidence(freshness=FreshnessSignal((NOW - timedelta(days=5),))), now=NOW
    )
    assert fresh.freshness.rating == QualityLevel.HIGH.value
    assert fresh.freshness.reason == REASON_FRESHNESS_FRESH

    # One stale field drags the whole record's freshness down (only as fresh as its stalest field).
    stale = summarize_quality(
        _evidence(freshness=FreshnessSignal((NOW - timedelta(days=5), NOW - timedelta(days=500)))),
        now=NOW,
    )
    assert stale.freshness.rating == QualityLevel.LOW.value
    assert stale.freshness.reason == REASON_FRESHNESS_STALE

    aging = summarize_quality(
        _evidence(freshness=FreshnessSignal((NOW - timedelta(days=200),))), now=NOW
    )
    assert aging.freshness.rating == QualityLevel.MEDIUM.value

    unknown = summarize_quality(_evidence(freshness=FreshnessSignal(())), now=NOW)
    assert unknown.freshness.rating == QualityLevel.UNKNOWN.value
    assert unknown.freshness.reason == REASON_FRESHNESS_NO_OBSERVATIONS


def test_completeness_ratio_grades_and_reports_missing_fields():
    partial = summarize_quality(
        _evidence(
            completeness=CompletenessSignal(
                expected_fields=frozenset({"a", "b", "c", "d"}),
                present_fields=frozenset({"a", "b"}),
            )
        ),
        now=NOW,
    )
    assert partial.completeness.rating == QualityLevel.MEDIUM.value
    assert partial.completeness.detail["missing_fields"] == ["c", "d"]

    sparse = summarize_quality(
        _evidence(
            completeness=CompletenessSignal(
                expected_fields=frozenset({"a", "b", "c", "d", "e"}),
                present_fields=frozenset({"a"}),
            )
        ),
        now=NOW,
    )
    assert sparse.completeness.rating == QualityLevel.LOW.value

    unknown = summarize_quality(
        _evidence(completeness=CompletenessSignal(expected_fields=frozenset())), now=NOW
    )
    assert unknown.completeness.rating == QualityLevel.UNKNOWN.value


def test_business_fit_from_score_and_explicit_band():
    assert (
        summarize_quality(
            _evidence(business_fit=BusinessFitSignal(score=0.5)), now=NOW
        ).business_fit.rating
        == QualityLevel.MEDIUM.value
    )
    assert (
        summarize_quality(
            _evidence(business_fit=BusinessFitSignal(band=QualityLevel.HIGH)), now=NOW
        ).business_fit.rating
        == QualityLevel.HIGH.value
    )
    assert (
        summarize_quality(_evidence(business_fit=BusinessFitSignal()), now=NOW).business_fit.rating
        == QualityLevel.UNKNOWN.value
    )


# --- acceptance 3: reproducible, pure, serializable -----------------------------------------------


def test_summary_is_reproducible_with_no_state_change():
    blocked = _evidence(
        business_fit=BusinessFitSignal(score=0.99),
        deliverability=(DeliverabilitySignal(verified=True, catch_all=True),),
        eligibility=EligibilityInput(decision=PolicyDecision.DENY, reason="denied_by_rule"),
    )
    first = summarize_quality(blocked, now=NOW)
    second = summarize_quality(blocked, now=NOW)
    # Frozen dataclasses: value-equality proves the identical result on a repeat call (nothing to mutate).
    assert first == second
    assert first.to_dict() == second.to_dict()
    # The relevant denial + the catch-all uncertainty reproduce exactly.
    assert second.eligibility.eligibility is Eligibility.BLOCKED
    assert second.deliverability.rating == Deliverability.UNCERTAIN.value


def test_to_dict_shape_lists_five_dimensions_and_separate_eligibility():
    summary = summarize_quality(_evidence(), now=NOW)
    payload = summary.to_dict()
    assert set(payload) == {"dimensions", "eligibility"}
    assert set(payload["dimensions"]) == {d.value for d in QualityDimension}
    assert set(payload["eligibility"]) == {"eligibility", "reason"}


# --- read-only adapters over stored field observations --------------------------------------------


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _seed_source(session, key: str = "mca_master_data") -> tuple[str, int]:
    SourceCatalogStore(session).create_draft(
        SourceDraftInput(
            key=key,
            kind="official",
            access_method="api",
            name="MCA Master Data",
            india_coverage={"nationwide": True},
        )
    )
    terms = SourceTermsStore(session).add_version(
        SourceTermsInput(
            source_key=key,
            operations={"collect": True, "store": True},
            purposes=["prospecting"],
            contract_evidence={"reference": "LIC-1", "kind": "licence", "uri": "file://lic"},
        )
    )
    return key, terms.version


def _seed_org_subject(session, business_id: str) -> str:
    org = OrgObservationStore(session, business_id).create(
        ObservationInput(entity_type="legal_entity", display_name="Synthetic Textiles Pvt Ltd"),
        actor=ACTOR,
    )
    return org.id


def _record(store, subject, src, ver, field_key, value, observed_at) -> str:
    obs = store.record(
        FieldObservationInput(
            subject_type="organization",
            subject_id=subject,
            field_key=field_key,
            field_value=value,
            inference_label="observed",
            source_key=src,
            source_version=ver,
            provenance_ref="https://synthetic.registry.example/mca/record/42",
            observed_at=observed_at,
            scope="nationwide",
            rights_basis="source_terms:collect+store;purpose=prospecting",
        ),
        actor=ACTOR,
    )
    return obs.id


def test_adapters_build_freshness_and_completeness_from_stored_observations(session_factory):
    with session_factory() as session:
        src, ver = _seed_source(session)
        subject = _seed_org_subject(session, BIZ_A)
        store = FieldObservationStore(session, BIZ_A)
        _record(
            store,
            subject,
            src,
            ver,
            "legal_name",
            "Synthetic Textiles Pvt Ltd",
            NOW - timedelta(days=10),
        )
        _record(store, subject, src, ver, "state", "KA", NOW - timedelta(days=400))
        session.commit()

    with session_factory() as session:
        store = FieldObservationStore(session, BIZ_A)
        observations = store.for_subject("organization", subject)
        freshness = freshness_from_observations(observations)
        completeness = completeness_from_observations(
            observations, expected_fields={"legal_name", "state", "industry", "website"}
        )
        summary = summarize_quality(
            QualityEvidence(freshness=freshness, completeness=completeness), now=NOW
        )
        # Oldest observation is 400d → stale; 2 of 4 expected present → partial.
        assert summary.freshness.rating == QualityLevel.LOW.value
        assert summary.completeness.rating == QualityLevel.MEDIUM.value
        assert summary.completeness.detail["present_count"] == 2
        # Pure reads only — both observation rows remain, nothing mutated/deleted.
        assert session.query(FieldObservation).count() == 2


# --- durable path on disposable PostgreSQL --------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")


@pytest.mark.skipif(not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres")
def test_adapters_over_postgres_grade_stored_observations():
    """Over a REAL transaction on disposable PostgreSQL (never SQLite for the durable path): record two
    field observations, build the freshness/completeness evidence from them, and prove the summary
    grades as expected while both rows remain in history — unchanged."""
    engine = create_engine(PG_URL, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        factory = make_session_factory(engine)
        with factory() as session:
            src, ver = _seed_source(session)
            subject = _seed_org_subject(session, BIZ_A)
            store = FieldObservationStore(session, BIZ_A)
            _record(
                store,
                subject,
                src,
                ver,
                "legal_name",
                "Synthetic Textiles Pvt Ltd",
                NOW - timedelta(days=10),
            )
            _record(store, subject, src, ver, "state", "KA", NOW - timedelta(days=400))
            session.commit()

        with factory() as session:
            store = FieldObservationStore(session, BIZ_A)
            observations = store.for_subject("organization", subject)
            summary = summarize_quality(
                QualityEvidence(
                    freshness=freshness_from_observations(observations),
                    completeness=completeness_from_observations(
                        observations, expected_fields={"legal_name", "state", "industry", "website"}
                    ),
                    eligibility=EligibilityInput(
                        decision=PolicyDecision.DENY, reason="denied_by_rule"
                    ),
                    business_fit=BusinessFitSignal(score=0.99),
                ),
                now=NOW,
            )
            assert summary.freshness.rating == QualityLevel.LOW.value
            assert summary.completeness.rating == QualityLevel.MEDIUM.value
            # High fit still cannot unblock a blocked permission outcome.
            assert summary.business_fit.rating == QualityLevel.HIGH.value
            assert summary.eligibility.eligibility is Eligibility.BLOCKED

        with factory() as session:
            assert session.query(FieldObservation).count() == 2
    finally:
        Base.metadata.drop_all(engine)
