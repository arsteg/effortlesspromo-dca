"""DCA-069 — choose canonical field observations, explainably.

Pins the load-bearing behaviours of :mod:`app.canonical.field_selection` — the pure, deterministic
conclusion layer that selects which observed value of a field is canonical, using evidence
reliability, verified identity, freshness and rights rather than a rigid source-tier rule:

1. **Stale registry data cannot blindly replace a newer verified fact; losing values remain in
   history.** A newer VERIFIED value wins over a stale OBSERVED (registry) value — the opposite of a
   tier rule — and the explanation says so (``verified_over_stale_registry``). Every non-winning
   observation is preserved (nothing is deleted).
2. **Rights gate (the relevant denial).** A value with no rights basis, a lapsed retention window, or
   source terms no longer in force is INELIGIBLE to be canonical even when freshest/most reliable; it
   stays in history with a stable denial reason. When the only otherwise-best value is denied, a lower
   but eligible value wins instead.
3. **Reproducible, pure, no state change.** The same inputs at the same ``now`` always yield the same
   winner and denials; the selection mutates/records nothing.

The selection is pure (no DB), so these run network-free. The read-only observation adapter is
exercised on in-memory SQLite; the real transaction/RLS path for the underlying field-observation
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
from app.canonical import (
    DENIAL_NO_RIGHTS_BASIS,
    DENIAL_RETENTION_LAPSED,
    DENIAL_SOURCE_RIGHTS_NOT_IN_FORCE,
    REASON_FRESHER,
    REASON_HIGHER_RELIABILITY,
    REASON_ONLY_ELIGIBLE,
    REASON_VERIFIED_OVER_STALE_REGISTRY,
    CanonicalCandidate,
    Reliability,
    candidates_from_observations,
    select_canonical,
    select_canonical_by_field,
)
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import FieldObservation
from app.services.field_observation import FieldObservationInput, FieldObservationStore
from app.services.org_observation import ObservationInput, OrgObservationStore
from app.services.source_catalog import SourceCatalogStore, SourceDraftInput
from app.services.source_terms import SourceTermsInput, SourceTermsStore

BIZ_A = "11111111-1111-1111-1111-111111111111"
ACTOR = AuditActor(id="00000000-0000-4000-9000-0000000a0069", type=ActorType.USER)
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
FIELD = "annual_revenue_inr"


def _candidate(
    obs_id: str,
    value: str,
    reliability: Reliability,
    observed_at: datetime,
    **over,
) -> CanonicalCandidate:
    base = dict(
        observation_id=obs_id,
        value=value,
        reliability=reliability,
        observed_at=observed_at,
        field_key=FIELD,
        rights_basis="source_terms:collect+store;purpose=prospecting",
        source_key="mca_master_data",
        source_version=1,
        source_rights_in_force=True,
        retention_until=None,
        recorded_at=observed_at,
    )
    base.update(over)
    return CanonicalCandidate(**base)  # type: ignore[arg-type]


# --- 1) stale registry cannot replace a newer verified fact ---------------------------------------


def test_newer_verified_fact_beats_stale_registry_and_losers_are_preserved():
    """A rigid source-tier rule would let the official registry value win on provenance; here a newer
    VERIFIED fact wins, the explanation records it, and the stale registry value stays in history."""
    stale_registry = _candidate(
        "obs-registry",
        "120000000",
        Reliability.OBSERVED,
        NOW - timedelta(days=400),  # stale
    )
    newer_verified = _candidate(
        "obs-verified",
        "155000000",
        Reliability.VERIFIED,
        NOW - timedelta(days=5),  # newer, independently verified
    )

    result = select_canonical([stale_registry, newer_verified], now=NOW)

    assert result.winner is not None
    assert result.winner.observation_id == "obs-verified"
    assert result.winner.value == "155000000"
    assert REASON_HIGHER_RELIABILITY in result.winner_reasons
    assert REASON_VERIFIED_OVER_STALE_REGISTRY in result.winner_reasons
    # The losing registry value is preserved, not deleted.
    assert result.preserved_observation_ids == ("obs-registry",)
    assert result.mutated_state is False
    # Even in reverse input order the verified fact still wins (pure, order-independent).
    reversed_result = select_canonical([newer_verified, stale_registry], now=NOW)
    assert reversed_result.winner is not None
    assert reversed_result.winner.observation_id == "obs-verified"


def test_freshness_decides_within_a_reliability_tier():
    """Two OBSERVED registry values: the fresher one wins (freshness decides WITHIN a tier), and the
    stale one is preserved."""
    older = _candidate("obs-old", "100", Reliability.OBSERVED, NOW - timedelta(days=200))
    newer = _candidate("obs-new", "130", Reliability.OBSERVED, NOW - timedelta(days=2))

    result = select_canonical([older, newer], now=NOW)

    assert result.winner is not None
    assert result.winner.observation_id == "obs-new"
    assert REASON_FRESHER in result.winner_reasons
    assert result.preserved_observation_ids == ("obs-old",)


def test_inferred_fresh_value_never_outranks_an_observed_stale_one():
    """Reliability is primary: a fresh INFERRED value does not silently override a stale OBSERVED fact
    (lower-reliability, however fresh, never wins on freshness alone)."""
    observed_stale = _candidate(
        "obs-observed", "100", Reliability.OBSERVED, NOW - timedelta(days=90)
    )
    inferred_fresh = _candidate(
        "obs-inferred", "999", Reliability.INFERRED, NOW - timedelta(days=1)
    )

    result = select_canonical([inferred_fresh, observed_stale], now=NOW)

    assert result.winner is not None
    assert result.winner.observation_id == "obs-observed"
    assert REASON_HIGHER_RELIABILITY in result.winner_reasons


# --- 2) rights gate: the relevant denial ----------------------------------------------------------


def test_lapsed_retention_denies_canonical_status_and_a_lesser_eligible_value_wins():
    """The otherwise-best value has a lapsed retention window, so it is INELIGIBLE (a denial) and stays
    in history; a lower but rights-eligible value wins instead."""
    best_but_lapsed = _candidate(
        "obs-lapsed",
        "200",
        Reliability.VERIFIED,
        NOW - timedelta(days=3),
        retention_until=NOW - timedelta(days=1),  # retention has lapsed
    )
    eligible_observed = _candidate(
        "obs-ok",
        "150",
        Reliability.OBSERVED,
        NOW - timedelta(days=30),
        retention_until=NOW + timedelta(days=100),
    )

    result = select_canonical([best_but_lapsed, eligible_observed], now=NOW)

    assert result.winner is not None
    assert result.winner.observation_id == "obs-ok"
    # The denied candidate is preserved with its stable reason.
    denied = {v.candidate.observation_id: v.denial_reason for v in result.denied}
    assert denied == {"obs-lapsed": DENIAL_RETENTION_LAPSED}
    assert "obs-lapsed" in result.preserved_observation_ids


def test_source_rights_not_in_force_and_missing_basis_are_denied():
    revoked = _candidate(
        "obs-revoked", "1", Reliability.VERIFIED, NOW, source_rights_in_force=False
    )
    no_basis = _candidate("obs-nobasis", "2", Reliability.OBSERVED, NOW, rights_basis="   ")

    result = select_canonical([revoked, no_basis], now=NOW)

    assert result.winner is None  # nothing is rights-eligible
    assert result.winner_reasons == ()
    denied = {v.candidate.observation_id: v.denial_reason for v in result.denied}
    assert denied == {
        "obs-revoked": DENIAL_SOURCE_RIGHTS_NOT_IN_FORCE,
        "obs-nobasis": DENIAL_NO_RIGHTS_BASIS,
    }
    # No winner, but every value is still preserved in history.
    assert set(result.preserved_observation_ids) == {"obs-revoked", "obs-nobasis"}


def test_single_eligible_candidate_reports_only_eligible():
    only = _candidate("obs-only", "42", Reliability.OBSERVED, NOW)
    result = select_canonical([only], now=NOW)
    assert result.winner is not None
    assert result.winner.observation_id == "obs-only"
    assert result.winner_reasons == (REASON_ONLY_ELIGIBLE,)
    assert result.preserved_observation_ids == ()


# --- 3) reproducible + grouping + validation ------------------------------------------------------


def test_result_is_reproducible_with_no_state_change():
    a = _candidate("a", "1", Reliability.VERIFIED, NOW - timedelta(days=1))
    b = _candidate("b", "2", Reliability.OBSERVED, NOW - timedelta(days=200))
    first = select_canonical([a, b], now=NOW)
    second = select_canonical([a, b], now=NOW)
    assert first.winner is not None and second.winner is not None
    assert first.winner.observation_id == second.winner.observation_id
    assert first.winner_reasons == second.winner_reasons
    assert first.preserved_observation_ids == second.preserved_observation_ids


def test_mixed_field_keys_is_a_programming_error():
    a = _candidate("a", "1", Reliability.OBSERVED, NOW, field_key="revenue")
    b = _candidate("b", "2", Reliability.OBSERVED, NOW, field_key="segment")
    with pytest.raises(ValueError):
        select_canonical([a, b], now=NOW)


def test_select_by_field_groups_each_field_independently():
    revenue_a = _candidate(
        "rev-a", "1", Reliability.OBSERVED, NOW - timedelta(days=100), field_key="revenue"
    )
    revenue_b = _candidate(
        "rev-b", "2", Reliability.VERIFIED, NOW - timedelta(days=1), field_key="revenue"
    )
    segment = _candidate("seg-a", "mid", Reliability.OBSERVED, NOW, field_key="segment")

    by_field = select_canonical_by_field([revenue_a, revenue_b, segment], now=NOW)

    assert set(by_field) == {"revenue", "segment"}
    assert by_field["revenue"].winner is not None
    assert by_field["revenue"].winner.observation_id == "rev-b"
    assert by_field["segment"].winner is not None
    assert by_field["segment"].winner.observation_id == "seg-a"


# --- read-only adapter over stored observations ---------------------------------------------------


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


def _record(
    store: FieldObservationStore,
    subject: str,
    src: str,
    ver: int,
    value: str,
    observed_at: datetime,
    label: str = "observed",
) -> str:
    obs = store.record(
        FieldObservationInput(
            subject_type="organization",
            subject_id=subject,
            field_key=FIELD,
            field_value=value,
            inference_label=label,
            source_key=src if label == "observed" else "",
            source_version=ver if label == "observed" else None,
            provenance_ref="https://synthetic.registry.example/mca/record/42#revenue",
            observed_at=observed_at,
            scope="nationwide",
            rights_basis="source_terms:collect+store;purpose=prospecting",
        ),
        actor=ACTOR,
    )
    return obs.id


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def test_adapter_builds_candidates_and_marks_verified(session_factory):
    """The read-only adapter maps stored observations onto reliability/rights; a verified id becomes a
    VERIFIED candidate, and selection over the stored history preserves the losers."""
    with session_factory() as session:
        src, ver = _seed_source(session)
        subject = _seed_org_subject(session, BIZ_A)
        store = FieldObservationStore(session, BIZ_A)
        registry_id = _record(store, subject, src, ver, "120000000", NOW - timedelta(days=400))
        verified_id = _record(store, subject, src, ver, "155000000", NOW - timedelta(days=5))
        session.commit()

    with session_factory() as session:
        store = FieldObservationStore(session, BIZ_A)
        observations = store.for_subject("organization", subject)
        candidates = candidates_from_observations(
            observations, verified_observation_ids={verified_id}
        )
        result = select_canonical(candidates, now=NOW)

        assert result.winner is not None
        assert result.winner.observation_id == verified_id
        assert REASON_VERIFIED_OVER_STALE_REGISTRY in result.winner_reasons
        assert result.preserved_observation_ids == (registry_id,)
        # Pure read: both observations are still present after selection (nothing deleted/mutated).
        assert session.query(FieldObservation).count() == 2


def test_adapter_source_rights_predicate_denies_out_of_force_terms(session_factory):
    """A caller predicate can mark a source version's terms as no longer in force, denying any value
    from it — the rights gate, not a source tier."""
    with session_factory() as session:
        src, ver = _seed_source(session)
        subject = _seed_org_subject(session, BIZ_A)
        store = FieldObservationStore(session, BIZ_A)
        _record(store, subject, src, ver, "120000000", NOW - timedelta(days=5))
        session.commit()

    with session_factory() as session:
        store = FieldObservationStore(session, BIZ_A)
        observations = store.for_subject("organization", subject)
        candidates = candidates_from_observations(
            observations, source_rights_in_force=lambda _k, _v: False
        )
        result = select_canonical(candidates, now=NOW)
        assert result.winner is None
        assert all(v.denial_reason == DENIAL_SOURCE_RIGHTS_NOT_IN_FORCE for v in result.denied)


# --- durable path on disposable PostgreSQL --------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")


@pytest.mark.skipif(not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres")
def test_selection_over_postgres_preserves_losing_observations():
    """Over a REAL transaction on disposable PostgreSQL (never SQLite for the durable path): record a
    stale registry fact and a newer verified fact, select the canonical value, and prove the newer
    verified fact wins while the losing registry observation remains in history — unchanged."""
    engine = create_engine(PG_URL, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        factory = make_session_factory(engine)
        with factory() as session:
            src, ver = _seed_source(session)
            subject = _seed_org_subject(session, BIZ_A)
            store = FieldObservationStore(session, BIZ_A)
            registry_id = _record(store, subject, src, ver, "120000000", NOW - timedelta(days=400))
            verified_id = _record(store, subject, src, ver, "155000000", NOW - timedelta(days=5))
            session.commit()

        with factory() as session:
            store = FieldObservationStore(session, BIZ_A)
            observations = store.for_subject("organization", subject)
            candidates = candidates_from_observations(
                observations, verified_observation_ids={verified_id}
            )
            result = select_canonical(candidates, now=NOW)
            assert result.winner is not None
            assert result.winner.observation_id == verified_id
            assert REASON_VERIFIED_OVER_STALE_REGISTRY in result.winner_reasons
            assert registry_id in result.preserved_observation_ids

        with factory() as session:
            # The selection changed nothing: both observations (winner + loser) remain in history.
            assert session.query(FieldObservation).count() == 2
    finally:
        Base.metadata.drop_all(engine)
