"""DCA-048 — authorize a source operation before fetching (the deterministic preflight gate).

Service unit tests (in-memory SQLite) pin the preflight decision and its load-bearing guarantee:

- the gate composes source status (DCA-044) + the platform ceiling at a moment in time (DCA-045/046)
  + the business entitlement (DCA-047) into ONE deterministic decision with a stable denial reason;
- a DENIED preflight makes ZERO adapter calls — proven with a counted fake adapter;
- official origin (an official trust tier) or a permissive ``robots.txt`` cannot, on their own,
  authorize a fetch the rights chain withholds — the advisory ``origin_signals`` never flip a denial;
- each dimension (unknown source, draft/disabled source, lapsed approval, operation/field/purpose
  denied at platform or not entitled by the business) yields its own stable reason;
- the decision is a pure read: reproducing it changes no state.

The entitlement narrowing and the platform ceiling themselves are covered by
``tests/test_source_entitlement.py``; this file pins the preflight that sits on top of them.
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
from app.services.source_preflight import (
    REASON_MISSING_PURPOSE,
    REASON_MISSING_SOURCE,
    REASON_OPERATION_DENIED_AT_PLATFORM,
    REASON_OPERATION_NOT_ENTITLED,
    REASON_PURPOSE_DENIED_AT_PLATFORM,
    REASON_PURPOSE_NOT_ENTITLED,
    REASON_SOURCE_NOT_ENABLED,
    REASON_SOURCE_NOT_ENTITLED,
    REASON_SOURCE_NOT_FOUND,
    REASON_SOURCE_NOT_PLATFORM_APPROVED,
    REASON_UNKNOWN_OPERATION,
    PreflightRequest,
    SourcePreflight,
)
from app.services.source_terms import SourceTermsInput, SourceTermsStore

SOURCE_KEY = "mca_master_data"
BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)

ADMIN = BusinessEntitler(subject="00000000-0000-4000-9000-0000000a0048", roles=("owner", "admin"))
OPERATOR = ReviewerPrincipal(subject="op-001", roles=("platform_operator",))

# "official origin" + "robots.txt allows crawling" — advisory hints the gate must IGNORE.
OFFICIAL_ROBOTS_OK = {"official_origin": True, "robots_allowed": True, "trust_tier": "official"}


class CountingAdapter:
    """A fake source adapter that COUNTS fetch calls. The preflight must never reach it on denial."""

    key = SOURCE_KEY

    def __init__(self) -> None:
        self.calls = 0

    def fetch(self, icp: dict) -> list:
        self.calls += 1
        return [{"org_name": "Synthetic Co", "source_key": self.key}]


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def _catalogue_and_approve(session, *, source_key: str = SOURCE_KEY) -> None:
    """Catalogue a source (DCA-044), record terms (DCA-045) and approve it (DCA-046) so it is the
    in-force platform ceiling: collect/store/display/export source-wide ('share' DENIED), email
    display-only, purpose 'prospecting'. The catalog entry stays a DISABLED draft (as the service
    never writes ENABLED) — the caller enables it explicitly when the allow path is under test.
    """
    SourceCatalogStore(session).create_draft(
        SourceDraftInput(
            key=source_key,
            kind="official",
            access_method="bulk_download",
            trust_tier="official_registry",
            india_coverage={"nationwide": True},
        )
    )
    SourceTermsStore(session).add_version(
        SourceTermsInput(
            source_key=source_key,
            operations={"collect": True, "store": True, "display": True, "export": True},
            field_rights={"email": {"display": True, "export": False}},
            purposes=["prospecting"],
            contract_evidence={"reference": "MCA-TOS-2026", "kind": "terms_of_use"},
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


def _enable_source(session, *, source_key: str = SOURCE_KEY) -> None:
    """Synthetically flip the catalogued source to ENABLED so the preflight ALLOW path is reachable.

    In the running service nothing promotes a source to ENABLED (that stays a disabled future
    capability), so this is a TEST-ONLY synthetic enable to exercise the allow branch; production
    preflights deny every fetch at the source-status gate.
    """
    source = session.query(Source).filter(Source.key == source_key).one()
    source.status = SourceStatus.ENABLED.value
    source.enabled = True


def _entitle(session, business_id: str, data: BusinessSourceEntitlementInput) -> None:
    BusinessSourceEntitlementStore(session, business_id).set_entitlement(data, ADMIN, now=NOW)


# -- the happy path: a fully-authorized operation allows and the adapter IS called ----------------


def test_fully_authorized_operation_allows_and_fetches(session_factory):
    with session_factory() as session:
        _catalogue_and_approve(session)
        _enable_source(session)
        _entitle(
            session,
            BIZ_A,
            BusinessSourceEntitlementInput(
                source_key=SOURCE_KEY,
                enabled=True,
                operations={"collect": True, "display": True},
                field_rights={"email": {"display": True}},
                purposes=["prospecting"],
            ),
        )
        session.commit()

    with session_factory() as session:
        gate = SourcePreflight(session, BIZ_A)
        adapter = CountingAdapter()
        result = gate.fetch(
            adapter,
            PreflightRequest(
                source_key=SOURCE_KEY,
                operation="collect",
                purpose="prospecting",
                origin_signals=OFFICIAL_ROBOTS_OK,
            ),
            icp={"industries": ["textiles"]},
            now=NOW,
        )
        assert result.decision.allowed is True
        assert result.decision.reason is None
        assert result.adapter_called is True
        assert adapter.calls == 1
        assert result.candidates and result.candidates[0]["source_key"] == SOURCE_KEY
        # The advisory origin signals are recorded for audit, never used for the decision.
        assert result.decision.evidence["ignoredOriginSignals"] == OFFICIAL_ROBOTS_OK


# -- the load-bearing guarantee: a denied preflight makes ZERO adapter calls ----------------------


def test_denied_preflight_makes_zero_adapter_calls(session_factory):
    with session_factory() as session:
        # Approved at the platform level, but the tenant has NO entitlement -> denied by narrowing.
        _catalogue_and_approve(session)
        _enable_source(session)
        session.commit()

    with session_factory() as session:
        gate = SourcePreflight(session, BIZ_A)
        adapter = CountingAdapter()
        result = gate.fetch(
            adapter,
            PreflightRequest(
                source_key=SOURCE_KEY,
                operation="collect",
                purpose="prospecting",
                origin_signals=OFFICIAL_ROBOTS_OK,
            ),
            icp={},
            now=NOW,
        )
        assert result.decision.allowed is False
        assert result.decision.reason == REASON_SOURCE_NOT_ENTITLED
        assert result.adapter_called is False
        assert result.candidates is None
        assert adapter.calls == 0  # the adapter was never reached


def test_official_origin_and_robots_cannot_bypass_rights(session_factory):
    """An official-registry source with a permissive robots.txt, but NO platform approval and NO
    entitlement, is still DENIED — origin/robots alone cannot bypass the rights chain, and the
    adapter is never called."""
    with session_factory() as session:
        # Catalogue + record terms, but DO NOT approve: no in-force platform ceiling. Enable the
        # catalog entry so the ONLY thing missing is the rights chain, not the source status.
        SourceCatalogStore(session).create_draft(
            SourceDraftInput(
                key=SOURCE_KEY,
                kind="official",
                access_method="bulk_download",
                trust_tier="official_registry",
                india_coverage={"nationwide": True},
            )
        )
        SourceTermsStore(session).add_version(
            SourceTermsInput(
                source_key=SOURCE_KEY,
                operations={"collect": True},
                purposes=["prospecting"],
                contract_evidence={"reference": "MCA-TOS-2026", "kind": "terms_of_use"},
            )
        )
        _enable_source(session)
        session.commit()

    with session_factory() as session:
        gate = SourcePreflight(session, BIZ_A)
        adapter = CountingAdapter()
        result = gate.fetch(
            adapter,
            PreflightRequest(
                source_key=SOURCE_KEY,
                operation="collect",
                purpose="prospecting",
                origin_signals=OFFICIAL_ROBOTS_OK,
            ),
            icp={},
            now=NOW,
        )
        assert result.decision.allowed is False
        assert result.decision.reason == REASON_SOURCE_NOT_PLATFORM_APPROVED
        assert result.adapter_called is False
        assert adapter.calls == 0


# -- each dimension yields its own stable reason --------------------------------------------------


def test_unknown_source_is_denied(session_factory):
    with session_factory() as session:
        gate = SourcePreflight(session, BIZ_A)
        decision = gate.authorize(
            PreflightRequest(source_key="nope", operation="collect", purpose="prospecting"),
            now=NOW,
        )
        assert (decision.allowed, decision.reason) == (False, REASON_SOURCE_NOT_FOUND)


def test_missing_source_key_and_purpose_and_unknown_operation(session_factory):
    with session_factory() as session:
        gate = SourcePreflight(session, BIZ_A)
        assert (
            gate.authorize(
                PreflightRequest(source_key="", operation="collect", purpose="prospecting"), now=NOW
            ).reason
            == REASON_MISSING_SOURCE
        )
        assert (
            gate.authorize(
                PreflightRequest(
                    source_key=SOURCE_KEY, operation="teleport", purpose="prospecting"
                ),
                now=NOW,
            ).reason
            == REASON_UNKNOWN_OPERATION
        )
        assert (
            gate.authorize(
                PreflightRequest(source_key=SOURCE_KEY, operation="collect", purpose=""), now=NOW
            ).reason
            == REASON_MISSING_PURPOSE
        )


def test_draft_or_disabled_source_is_not_usable(session_factory):
    with session_factory() as session:
        # Fully approved + entitled, but the catalog entry stays a DISABLED draft (never enabled).
        _catalogue_and_approve(session)
        _entitle(
            session,
            BIZ_A,
            BusinessSourceEntitlementInput(
                source_key=SOURCE_KEY,
                enabled=True,
                operations={"collect": True},
                purposes=["prospecting"],
            ),
        )
        session.commit()

    with session_factory() as session:
        gate = SourcePreflight(session, BIZ_A)
        decision = gate.authorize(
            PreflightRequest(source_key=SOURCE_KEY, operation="collect", purpose="prospecting"),
            now=NOW,
        )
        assert (decision.allowed, decision.reason) == (False, REASON_SOURCE_NOT_ENABLED)


def test_lapsed_platform_approval_denies_at_the_ceiling(session_factory):
    with session_factory() as session:
        _catalogue_and_approve(session)
        _enable_source(session)
        _entitle(
            session,
            BIZ_A,
            BusinessSourceEntitlementInput(
                source_key=SOURCE_KEY,
                enabled=True,
                operations={"collect": True},
                purposes=["prospecting"],
            ),
        )
        session.commit()

    with session_factory() as session:
        gate = SourcePreflight(session, BIZ_A)
        # Evaluate after the approval's one-year window has lapsed: the ceiling is no longer in force.
        decision = gate.authorize(
            PreflightRequest(source_key=SOURCE_KEY, operation="collect", purpose="prospecting"),
            now=NOW + timedelta(days=800),
        )
        assert (decision.allowed, decision.reason) == (False, REASON_SOURCE_NOT_PLATFORM_APPROVED)


def test_operation_denied_at_platform(session_factory):
    with session_factory() as session:
        _catalogue_and_approve(session)  # 'share' is NOT approved at the platform level
        _enable_source(session)
        _entitle(
            session,
            BIZ_A,
            BusinessSourceEntitlementInput(
                source_key=SOURCE_KEY, enabled=True, purposes=["prospecting"]
            ),
        )
        session.commit()

    with session_factory() as session:
        gate = SourcePreflight(session, BIZ_A)
        decision = gate.authorize(
            PreflightRequest(source_key=SOURCE_KEY, operation="share", purpose="prospecting"),
            now=NOW,
        )
        assert (decision.allowed, decision.reason) == (False, REASON_OPERATION_DENIED_AT_PLATFORM)


def test_operation_not_entitled_by_business(session_factory):
    with session_factory() as session:
        _catalogue_and_approve(session)  # 'export' IS approved at platform
        _enable_source(session)
        # ...but the business entitles only 'display' (not 'export') -> narrowed out.
        _entitle(
            session,
            BIZ_A,
            BusinessSourceEntitlementInput(
                source_key=SOURCE_KEY,
                enabled=True,
                operations={"display": True, "export": False},
                purposes=["prospecting"],
            ),
        )
        session.commit()

    with session_factory() as session:
        gate = SourcePreflight(session, BIZ_A)
        decision = gate.authorize(
            PreflightRequest(source_key=SOURCE_KEY, operation="export", purpose="prospecting"),
            now=NOW,
        )
        assert (decision.allowed, decision.reason) == (False, REASON_OPERATION_NOT_ENTITLED)


def test_purpose_denied_at_platform_and_not_entitled(session_factory):
    with session_factory() as session:
        _catalogue_and_approve(session)  # only 'prospecting' is documented at platform
        _enable_source(session)
        _entitle(
            session,
            BIZ_A,
            BusinessSourceEntitlementInput(
                source_key=SOURCE_KEY,
                enabled=True,
                operations={"collect": True},
                purposes=["prospecting"],  # business narrows to 'prospecting' only
            ),
        )
        session.commit()

    with session_factory() as session:
        gate = SourcePreflight(session, BIZ_A)
        # A purpose the platform does not document at all -> platform-level purpose denial.
        denied_platform = gate.authorize(
            PreflightRequest(source_key=SOURCE_KEY, operation="collect", purpose="resale"),
            now=NOW,
        )
        assert (denied_platform.allowed, denied_platform.reason) == (
            False,
            REASON_PURPOSE_DENIED_AT_PLATFORM,
        )


def test_purpose_not_entitled_when_platform_documents_but_business_narrows_out(session_factory):
    with session_factory() as session:
        # Platform documents TWO purposes; the business entitles only one of them.
        SourceCatalogStore(session).create_draft(
            SourceDraftInput(
                key=SOURCE_KEY,
                kind="official",
                access_method="bulk_download",
                trust_tier="official_registry",
                india_coverage={"nationwide": True},
            )
        )
        SourceTermsStore(session).add_version(
            SourceTermsInput(
                source_key=SOURCE_KEY,
                operations={"collect": True},
                purposes=["prospecting", "enrichment"],
                contract_evidence={"reference": "MCA-TOS-2026", "kind": "terms_of_use"},
            )
        )
        SourceApprovalStore(session).approve(
            SOURCE_KEY,
            1,
            SourceApprovalReview(
                reviewer_id="op-001",
                evidence={"reference": "RIGHTS-REVIEW-2026-11", "kind": "legal_review"},
                reviewed_at=NOW - timedelta(days=1),
                expires_at=NOW + timedelta(days=365),
            ),
            OPERATOR,
        )
        _enable_source(session)
        _entitle(
            session,
            BIZ_A,
            BusinessSourceEntitlementInput(
                source_key=SOURCE_KEY,
                enabled=True,
                operations={"collect": True},
                purposes=["prospecting"],  # 'enrichment' documented at platform but NOT entitled
            ),
        )
        session.commit()

    with session_factory() as session:
        gate = SourcePreflight(session, BIZ_A)
        decision = gate.authorize(
            PreflightRequest(source_key=SOURCE_KEY, operation="collect", purpose="enrichment"),
            now=NOW,
        )
        assert (decision.allowed, decision.reason) == (False, REASON_PURPOSE_NOT_ENTITLED)


# -- tenant isolation + reproducibility -----------------------------------------------------------


def test_preflight_is_per_business_and_reproducible(session_factory):
    with session_factory() as session:
        _catalogue_and_approve(session)
        _enable_source(session)
        _entitle(
            session,
            BIZ_A,
            BusinessSourceEntitlementInput(
                source_key=SOURCE_KEY,
                enabled=True,
                operations={"collect": True},
                purposes=["prospecting"],
            ),
        )
        session.commit()

    with session_factory() as session:
        req = PreflightRequest(source_key=SOURCE_KEY, operation="collect", purpose="prospecting")
        # A is entitled -> allowed; B shares the SAME platform ceiling but has no entitlement.
        gate_a = SourcePreflight(session, BIZ_A)
        gate_b = SourcePreflight(session, BIZ_B)
        first = gate_a.authorize(req, now=NOW)
        second = gate_a.authorize(req, now=NOW)  # re-running changes nothing
        assert first.allowed is True and second.allowed is True
        decision_b = gate_b.authorize(req, now=NOW)
        assert (decision_b.allowed, decision_b.reason) == (False, REASON_SOURCE_NOT_ENTITLED)
