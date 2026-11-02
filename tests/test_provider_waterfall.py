"""DCA-148 — route missing fields through a provider waterfall.

Service unit tests (in-memory SQLite — the planner is a pure read with no locking/RLS/transaction to
exercise) pin :class:`~app.services.provider_waterfall.ProviderWaterfallPlanner` and its two
acceptance invariants:

- a field's waterfall STOPS at the first provider that meets the requirement — every cheaper-ranked
  provider after it is ``not_reached``, never called (no redundant next-provider call after success);
- providers that share an upstream origin do NOT count as independent corroboration (two resellers of
  one registry count as one), so a corroboration requirement is only met by DISTINCT upstreams;

plus the relevant denials (a field whose only provider is rights-denied, one with no covering source,
and one whose candidates are all too stale) each reproduce with NO state change, and routing is
tenant-scoped (Business B, unentitled, is denied the same field A can route).

The rights chain itself (DCA-044..048) is covered by ``tests/test_source_preflight.py``; this file pins
the per-field ordered fallback that sits on top of it, reusing the same catalogue/approve/entitle setup.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.db.base import Base, make_engine, make_session_factory
from app.db.models import Source, SourceStatus
from app.services.source_approval import (
    ReviewerPrincipal,
    SourceApprovalReview,
    SourceApprovalStore,
)
from app.services.source_catalog import SourceCatalogStore, SourceDraftInput
from app.services.source_entitlement import (
    BusinessEntitler,
    BusinessSourceEntitlementInput,
    BusinessSourceEntitlementStore,
)
from app.services.source_terms import SourceTermsInput, SourceTermsStore
from app.services.provider_waterfall import (
    DEFAULT_PURPOSE,
    OUTCOME_DENIED_RIGHTS,
    OUTCOME_NO_COVERAGE,
    OUTCOME_NOT_REACHED,
    OUTCOME_SELECTED,
    OUTCOME_SKIPPED_SHARED_UPSTREAM,
    OUTCOME_STALE,
    REASON_ALL_STALE,
    REASON_NO_CANDIDATE,
    REASON_NO_ENTITLED,
    REASON_INSUFFICIENT,
    FieldRequirement,
    ProviderCandidate,
    ProviderWaterfallPlanner,
)

PURPOSE = DEFAULT_PURPOSE
FIELD = "org_identifier"
BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)

ADMIN = BusinessEntitler(subject="00000000-0000-4000-9000-0000000a0148", roles=("owner", "admin"))
OPERATOR = ReviewerPrincipal(subject="op-001", roles=("platform_operator",))


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def _catalogue_approve_enable(session, source_key: str) -> None:
    """Catalogue a source offering ``org_identifier``, record+approve collect terms for the discovery
    purpose, and synthetically ENABLE it so the rights ALLOW path is reachable (in the running service
    nothing promotes a source to ENABLED — a test-only synthetic enable, exactly as DCA-048's tests do).
    """
    SourceCatalogStore(session).create_draft(
        SourceDraftInput(
            key=source_key,
            kind="official",
            access_method="bulk_download",
            trust_tier="official_registry",
            fields=["org_name", FIELD],
            india_coverage={"nationwide": True},
        )
    )
    SourceTermsStore(session).add_version(
        SourceTermsInput(
            source_key=source_key,
            operations={"collect": True},
            purposes=[PURPOSE],
            contract_evidence={"reference": f"{source_key}-TOS", "kind": "terms_of_use"},
        )
    )
    SourceApprovalStore(session).approve(
        source_key,
        1,
        SourceApprovalReview(
            reviewer_id="op-001",
            evidence={"reference": "RIGHTS-REVIEW-2026-11", "kind": "legal_review"},
            reviewed_at=NOW - timedelta(days=1),
            expires_at=NOW + timedelta(days=365),
        ),
        OPERATOR,
    )
    src = session.query(Source).filter(Source.key == source_key).one()
    src.status = SourceStatus.ENABLED.value
    src.enabled = True


def _entitle(session, business_id: str, source_key: str) -> None:
    BusinessSourceEntitlementStore(session, business_id).set_entitlement(
        BusinessSourceEntitlementInput(
            source_key=source_key,
            enabled=True,
            operations={"collect": True},
            purposes=[PURPOSE],
        ),
        ADMIN,
        now=NOW,
    )


# -- acceptance 1: stop at first success; no redundant next-provider call --------------------------


def test_first_provider_success_stops_the_waterfall(session_factory):
    """The cheapest entitled provider satisfies a single-source field; every pricier provider after it
    is NOT_REACHED (never called) — no redundant next-provider call after success."""
    with session_factory() as session:
        _catalogue_approve_enable(session, "cheap")
        _catalogue_approve_enable(session, "pricey")
        _entitle(session, BIZ_A, "cheap")
        _entitle(session, BIZ_A, "pricey")
        session.commit()

    with session_factory() as session:
        planner = ProviderWaterfallPlanner(session, BIZ_A)
        result = planner.route_fields(
            [FieldRequirement(field=FIELD)],
            [
                ProviderCandidate(source_key="pricey", marginal_cost_minor=900, age_days=1),
                ProviderCandidate(source_key="cheap", marginal_cost_minor=100, age_days=10),
            ],
            now=NOW,
        )
    route = result.route_for(FIELD)
    assert route is not None
    assert route.satisfied is True and route.reason is None
    assert route.selected == ("cheap",)  # cheapest first; one source satisfies
    assert route.calls_planned == 1  # exactly one call planned — not two
    assert route.providers_not_reached == ("pricey",)
    outcomes = {c.source_key: c.outcome for c in route.considered}
    assert outcomes == {"cheap": OUTCOME_SELECTED, "pricey": OUTCOME_NOT_REACHED}
    assert result.fully_satisfied is True


def test_route_is_pure_and_reproducible(session_factory):
    """Routing stages/commits nothing: re-running yields the identical result and writes no row."""
    with session_factory() as session:
        _catalogue_approve_enable(session, "cheap")
        _entitle(session, BIZ_A, "cheap")
        session.commit()

    with session_factory() as session:
        planner = ProviderWaterfallPlanner(session, BIZ_A)
        reqs = [FieldRequirement(field=FIELD)]
        cands = [ProviderCandidate(source_key="cheap", marginal_cost_minor=100)]
        first = planner.route_fields(reqs, cands, now=NOW)
        second = planner.route_fields(reqs, cands, now=NOW)
        assert first == second  # deterministic, reproducible
        assert len(session.new) == 0 and len(session.dirty) == 0  # nothing staged


# -- acceptance 2: shared upstream does not count as independent corroboration ---------------------


def test_shared_upstream_is_not_independent_corroboration(session_factory):
    """A 2-independent-source requirement: two providers share an upstream (count once), a third has a
    distinct upstream. The waterfall selects one of the shared pair + the distinct one; the duplicate
    upstream is SKIPPED_SHARED_UPSTREAM and the requirement is met by two DISTINCT origins."""
    with session_factory() as session:
        for key in ("reseller_a", "reseller_b", "independent"):
            _catalogue_approve_enable(session, key)
            _entitle(session, BIZ_A, key)
        session.commit()

    with session_factory() as session:
        planner = ProviderWaterfallPlanner(session, BIZ_A)
        result = planner.route_fields(
            [FieldRequirement(field=FIELD, min_independent_sources=2)],
            [
                # reseller_a and reseller_b both resell the SAME registry (shared upstream).
                ProviderCandidate(
                    source_key="reseller_a", marginal_cost_minor=100, upstream_key="mca"
                ),
                ProviderCandidate(
                    source_key="reseller_b", marginal_cost_minor=200, upstream_key="mca"
                ),
                ProviderCandidate(
                    source_key="independent", marginal_cost_minor=300, upstream_key="gst"
                ),
            ],
            now=NOW,
        )
    route = result.route_for(FIELD)
    assert route is not None
    assert route.satisfied is True and route.reason is None
    assert route.independent_sources == 2
    # Cheapest shared (reseller_a) selected, the duplicate upstream skipped, the distinct one selected.
    assert route.selected == ("reseller_a", "independent")
    outcomes = {c.source_key: c.outcome for c in route.considered}
    assert outcomes["reseller_a"] == OUTCOME_SELECTED
    assert outcomes["reseller_b"] == OUTCOME_SKIPPED_SHARED_UPSTREAM
    assert outcomes["independent"] == OUTCOME_SELECTED


def test_shared_upstream_cannot_alone_meet_a_corroboration_requirement(session_factory):
    """Two providers that share one upstream can never satisfy a 2-independent-source field — the
    second is skipped as a duplicate upstream and the field stays unsatisfied (insufficient)."""
    with session_factory() as session:
        _catalogue_approve_enable(session, "reseller_a")
        _catalogue_approve_enable(session, "reseller_b")
        _entitle(session, BIZ_A, "reseller_a")
        _entitle(session, BIZ_A, "reseller_b")
        session.commit()

    with session_factory() as session:
        planner = ProviderWaterfallPlanner(session, BIZ_A)
        result = planner.route_fields(
            [FieldRequirement(field=FIELD, min_independent_sources=2)],
            [
                ProviderCandidate(
                    source_key="reseller_a", marginal_cost_minor=100, upstream_key="mca"
                ),
                ProviderCandidate(
                    source_key="reseller_b", marginal_cost_minor=200, upstream_key="mca"
                ),
            ],
            now=NOW,
        )
    route = result.route_for(FIELD)
    assert route is not None
    assert route.satisfied is False
    assert route.independent_sources == 1
    assert route.reason == REASON_INSUFFICIENT
    assert route.selected == ("reseller_a",)
    assert result.fully_satisfied is False


# -- the relevant denials (each reproducible with no state change) ---------------------------------


def test_field_with_only_rights_denied_provider_is_unroutable(session_factory):
    """The one covering provider is NOT entitled to the business → rights-denied, selected nothing,
    reason ``no_entitled_provider`` (the relevant denial). Nothing is fetched or staged."""
    with session_factory() as session:
        _catalogue_approve_enable(
            session, "cheap"
        )  # approved at platform, but NOT entitled to BIZ_A
        session.commit()

    with session_factory() as session:
        planner = ProviderWaterfallPlanner(session, BIZ_A)
        result = planner.route_fields(
            [FieldRequirement(field=FIELD)],
            [ProviderCandidate(source_key="cheap", marginal_cost_minor=100)],
            now=NOW,
        )
        assert len(session.new) == 0 and len(session.dirty) == 0  # a denial changes nothing
    route = result.route_for(FIELD)
    assert route is not None
    assert route.satisfied is False
    assert route.selected == ()
    assert route.reason == REASON_NO_ENTITLED
    denied = [c for c in route.considered if c.outcome == OUTCOME_DENIED_RIGHTS]
    assert len(denied) == 1 and denied[0].source_key == "cheap"
    assert denied[0].reason  # carries the stable preflight denial code


def test_field_with_no_covering_source(session_factory):
    """A provider whose catalog source does not offer the field is not a candidate → no_candidate."""
    with session_factory() as session:
        _catalogue_approve_enable(session, "cheap")
        _entitle(session, BIZ_A, "cheap")
        session.commit()

    with session_factory() as session:
        planner = ProviderWaterfallPlanner(session, BIZ_A)
        result = planner.route_fields(
            [FieldRequirement(field="revenue")],  # not in the source's declared fields
            [ProviderCandidate(source_key="cheap", marginal_cost_minor=100)],
            now=NOW,
        )
    route = result.route_for("revenue")
    assert route is not None
    assert route.satisfied is False
    assert route.reason == REASON_NO_CANDIDATE
    assert [c.outcome for c in route.considered] == [OUTCOME_NO_COVERAGE]


def test_all_candidates_too_stale(session_factory):
    """Entitled+covering providers whose data exceeds the freshness bar are STALE → all_candidates_stale."""
    with session_factory() as session:
        _catalogue_approve_enable(session, "cheap")
        _entitle(session, BIZ_A, "cheap")
        session.commit()

    with session_factory() as session:
        planner = ProviderWaterfallPlanner(session, BIZ_A)
        result = planner.route_fields(
            [FieldRequirement(field=FIELD, max_age_days=30)],
            [ProviderCandidate(source_key="cheap", marginal_cost_minor=100, age_days=400)],
            now=NOW,
        )
    route = result.route_for(FIELD)
    assert route is not None
    assert route.satisfied is False
    assert route.reason == REASON_ALL_STALE
    assert [c.outcome for c in route.considered] == [OUTCOME_STALE]


def test_freshness_bar_prefers_a_fresh_provider_over_a_stale_cheaper_one(session_factory):
    """The stale cheap provider is excluded by the freshness bar; the fresher (pricier) one is selected."""
    with session_factory() as session:
        _catalogue_approve_enable(session, "stale_cheap")
        _catalogue_approve_enable(session, "fresh_pricey")
        _entitle(session, BIZ_A, "stale_cheap")
        _entitle(session, BIZ_A, "fresh_pricey")
        session.commit()

    with session_factory() as session:
        planner = ProviderWaterfallPlanner(session, BIZ_A)
        result = planner.route_fields(
            [FieldRequirement(field=FIELD, max_age_days=30)],
            [
                ProviderCandidate(source_key="stale_cheap", marginal_cost_minor=100, age_days=400),
                ProviderCandidate(source_key="fresh_pricey", marginal_cost_minor=900, age_days=5),
            ],
            now=NOW,
        )
    route = result.route_for(FIELD)
    assert route is not None
    assert route.satisfied is True
    assert route.selected == ("fresh_pricey",)
    outcomes = {c.source_key: c.outcome for c in route.considered}
    assert outcomes["stale_cheap"] == OUTCOME_STALE
    assert outcomes["fresh_pricey"] == OUTCOME_SELECTED


# -- tenant isolation ------------------------------------------------------------------------------


def test_routing_is_per_business(session_factory):
    """A and B share the same platform ceiling, but only A is entitled: A routes the field, B is denied."""
    with session_factory() as session:
        _catalogue_approve_enable(session, "cheap")
        _entitle(session, BIZ_A, "cheap")  # only A entitled
        session.commit()

    with session_factory() as session:
        reqs = [FieldRequirement(field=FIELD)]
        cands = [ProviderCandidate(source_key="cheap", marginal_cost_minor=100)]
        route_a = (
            ProviderWaterfallPlanner(session, BIZ_A)
            .route_fields(reqs, cands, now=NOW)
            .route_for(FIELD)
        )
        route_b = (
            ProviderWaterfallPlanner(session, BIZ_B)
            .route_fields(reqs, cands, now=NOW)
            .route_for(FIELD)
        )
    assert route_a is not None and route_a.satisfied is True and route_a.selected == ("cheap",)
    assert (
        route_b is not None and route_b.satisfied is False and route_b.reason == REASON_NO_ENTITLED
    )
