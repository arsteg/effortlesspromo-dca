"""DCA-049 — expire source access on time (effective-date checks + the scheduled expiry sweep).

Service unit tests (in-memory SQLite) pin the acceptance behaviour:

- **An expired source is unusable BEFORE the sweep catches up.** The moment an approval's review
  deadline (``expires_at``) passes, the DCA-048 preflight denies at the platform ceiling — even though
  the approval row is still stored ``active`` because no sweep has run yet. The effective-date check,
  not the status flag, is authoritative.
- **The scheduled expiry sweep durably records the lapse.** :class:`SourceExpirySweep` moves the
  lapsed ACTIVE approval (and the terms version it had promoted) to ``EXPIRED``, so a reader sees the
  durable state. The sweep is idempotent and a controllable clock drives it across the expiry boundary.
- **Failure/negative cases.** An in-force approval is untouched by the sweep and stays usable; a
  not-yet-effective approval (``effective_from`` in the future) is denied by the effective-date check
  yet is NOT expired by the sweep (it has not lapsed, it is merely not yet in force); a terms version
  whose OWN window has lapsed is expired independently of its approval.

The platform ceiling, the entitlement narrowing and the preflight themselves are covered by
``tests/test_source_approval.py`` / ``test_source_entitlement.py`` / ``test_source_preflight.py``;
this file pins the time-based expiry that sits across them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.db.base import Base, make_engine, make_session_factory
from app.db.models import (
    Source,
    SourceApprovalStatus,
    SourceStatus,
    SourceTerms,
    SourceTermsApproval,
    SourceTermsStatus,
)
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
from app.services.source_expiry import SourceExpirySweep, run_expiry_sweep
from app.services.source_preflight import (
    REASON_SOURCE_NOT_PLATFORM_APPROVED,
    PreflightRequest,
    SourcePreflight,
)
from app.services.source_terms import SourceTermsInput, SourceTermsStore

SOURCE_KEY = "mca_master_data"
BIZ_A = "11111111-1111-1111-1111-111111111111"
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
# The approval's explicit review deadline; the controllable clock crosses it in the tests below.
EXPIRES_AT = NOW + timedelta(days=30)

ADMIN = BusinessEntitler(subject="00000000-0000-4000-9000-0000000a0049", roles=("owner", "admin"))
OPERATOR = ReviewerPrincipal(subject="op-001", roles=("platform_operator",))


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def _seed_approved_enabled_entitled(
    session,
    *,
    expires_at: datetime = EXPIRES_AT,
    effective_from: datetime | None = None,
    entitle_at: datetime = NOW,
) -> None:
    """Catalogue + record terms + APPROVE (with an explicit window) + (test-only) ENABLE + entitle A,
    so the DCA-048 preflight ALLOWS inside the window. The approval's ``expires_at`` is the review
    deadline the sweep and the effective-date check both key on."""
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
            operations={"collect": True, "display": True},
            purposes=["prospecting"],
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
            effective_from=effective_from,
            expires_at=expires_at,
        ),
        OPERATOR,
    )
    # Test-only synthetic enable so the allow branch is reachable (the service never writes ENABLED).
    source = session.query(Source).filter(Source.key == SOURCE_KEY).one()
    source.status = SourceStatus.ENABLED.value
    source.enabled = True
    BusinessSourceEntitlementStore(session, BIZ_A).set_entitlement(
        BusinessSourceEntitlementInput(
            source_key=SOURCE_KEY,
            enabled=True,
            operations={"collect": True, "display": True},
            purposes=["prospecting"],
        ),
        ADMIN,
        now=entitle_at,
    )


def _preflight(session) -> PreflightRequest:
    return PreflightRequest(source_key=SOURCE_KEY, operation="collect", purpose="prospecting")


# -- the acceptance criterion: expired is unusable BEFORE the sweep ------------------------------


def test_expired_source_is_unusable_before_the_sweep_runs(session_factory):
    with session_factory() as session:
        _seed_approved_enabled_entitled(session)
        session.commit()

    after_expiry = EXPIRES_AT + timedelta(seconds=1)

    # Inside the window the preflight ALLOWS; the instant past expiry it DENIES — and crucially the
    # approval row is STILL stored 'active' (no sweep has run), proving the effective-date check, not
    # the status flag, makes the source unusable.
    with session_factory() as session:
        gate = SourcePreflight(session, BIZ_A)
        before = gate.authorize(_preflight(session), now=NOW)
        after = gate.authorize(_preflight(session), now=after_expiry)
        stored_status = (
            session.query(SourceTermsApproval)
            .filter(SourceTermsApproval.source_key == SOURCE_KEY)
            .one()
            .status
        )

    assert before.allowed is True
    assert (after.allowed, after.reason) == (False, REASON_SOURCE_NOT_PLATFORM_APPROVED)
    # The sweep has NOT run — the lapse was enforced purely by the time check.
    assert stored_status == SourceApprovalStatus.ACTIVE.value


# -- the scheduled expiry transition records the lapse durably -----------------------------------


def test_sweep_transitions_lapsed_approval_and_its_terms_to_expired(session_factory):
    with session_factory() as session:
        _seed_approved_enabled_entitled(session)
        session.commit()

    after_expiry = EXPIRES_AT + timedelta(seconds=1)

    with session_factory() as session:
        result = SourceExpirySweep(session).sweep(now=after_expiry)
        session.commit()

    assert result.transitioned == 2
    assert result.expired_approvals == [(SOURCE_KEY, 1)]
    assert result.expired_terms == [(SOURCE_KEY, 1)]

    with session_factory() as session:
        approval = (
            session.query(SourceTermsApproval)
            .filter(SourceTermsApproval.source_key == SOURCE_KEY)
            .one()
        )
        terms = session.query(SourceTerms).filter(SourceTerms.source_key == SOURCE_KEY).one()
        assert approval.status == SourceApprovalStatus.EXPIRED.value
        assert terms.status == SourceTermsStatus.EXPIRED.value

        # The preflight remains denied after the durable transition (defence in depth, same reason).
        decision = SourcePreflight(session, BIZ_A).authorize(_preflight(session), now=after_expiry)
        assert (decision.allowed, decision.reason) == (False, REASON_SOURCE_NOT_PLATFORM_APPROVED)


def test_sweep_is_idempotent(session_factory):
    with session_factory() as session:
        _seed_approved_enabled_entitled(session)
        session.commit()

    after_expiry = EXPIRES_AT + timedelta(days=1)

    with session_factory() as session:
        first = SourceExpirySweep(session).sweep(now=after_expiry)
        session.commit()
    with session_factory() as session:
        second = SourceExpirySweep(session).sweep(now=after_expiry)
        session.commit()

    assert first.transitioned == 2
    assert second.transitioned == 0  # nothing left ACTIVE to transition


def test_worker_entrypoint_commits_the_sweep(session_factory):
    """``run_expiry_sweep`` (the worker maintenance entry point) owns its own commit."""
    with session_factory() as session:
        _seed_approved_enabled_entitled(session)
        session.commit()

    result = run_expiry_sweep(session_factory, now=EXPIRES_AT + timedelta(days=1))
    assert result.transitioned == 2

    with session_factory() as session:
        approval = (
            session.query(SourceTermsApproval)
            .filter(SourceTermsApproval.source_key == SOURCE_KEY)
            .one()
        )
        assert approval.status == SourceApprovalStatus.EXPIRED.value


# -- negative / failure cases --------------------------------------------------------------------


def test_sweep_leaves_an_in_force_approval_untouched(session_factory):
    """An approval well inside its window is not lapsed — the sweep must not expire it, and it stays
    usable."""
    with session_factory() as session:
        _seed_approved_enabled_entitled(session)
        session.commit()

    within_window = NOW + timedelta(days=1)  # far before EXPIRES_AT

    with session_factory() as session:
        result = SourceExpirySweep(session).sweep(now=within_window)
        session.commit()
    assert result.transitioned == 0

    with session_factory() as session:
        approval = (
            session.query(SourceTermsApproval)
            .filter(SourceTermsApproval.source_key == SOURCE_KEY)
            .one()
        )
        assert approval.status == SourceApprovalStatus.ACTIVE.value
        decision = SourcePreflight(session, BIZ_A).authorize(_preflight(session), now=within_window)
        assert decision.allowed is True


def test_not_yet_effective_approval_is_denied_but_not_expired_by_the_sweep(session_factory):
    """An approval whose ``effective_from`` is still in the future is DENIED by the effective-date
    check (not yet in force), but it has NOT lapsed, so the sweep must leave it ACTIVE."""
    effective_from = NOW + timedelta(days=5)
    expires_at = NOW + timedelta(days=40)
    with session_factory() as session:
        # Entitle at a moment the approval IS in force (after effective_from) so the narrowing is
        # recorded; the preflight is then evaluated BEFORE effective_from in the assertion below.
        _seed_approved_enabled_entitled(
            session,
            effective_from=effective_from,
            expires_at=expires_at,
            entitle_at=effective_from + timedelta(days=1),
        )
        session.commit()

    before_effective = NOW + timedelta(days=1)  # before effective_from, well before expiry

    with session_factory() as session:
        # Effective-date check denies: the approval is not yet in force.
        decision = SourcePreflight(session, BIZ_A).authorize(
            _preflight(session), now=before_effective
        )
        assert (decision.allowed, decision.reason) == (False, REASON_SOURCE_NOT_PLATFORM_APPROVED)

    with session_factory() as session:
        result = SourceExpirySweep(session).sweep(now=before_effective)
        session.commit()
    assert result.transitioned == 0  # not lapsed — merely not yet effective

    with session_factory() as session:
        approval = (
            session.query(SourceTermsApproval)
            .filter(SourceTermsApproval.source_key == SOURCE_KEY)
            .one()
        )
        assert approval.status == SourceApprovalStatus.ACTIVE.value


def test_sweep_expires_terms_whose_own_window_lapsed_independently(session_factory):
    """A terms version with its OWN earlier expiry is expired by the sweep even while the approval
    that promoted it is still in force (two independent deadlines)."""
    terms_expiry = NOW + timedelta(days=10)
    approval_expiry = NOW + timedelta(days=365)
    with session_factory() as session:
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
                expires_at=terms_expiry,
            )
        )
        SourceApprovalStore(session).approve(
            SOURCE_KEY,
            1,
            SourceApprovalReview(
                reviewer_id="op-001",
                evidence={"reference": "RIGHTS-REVIEW-2026-11", "kind": "legal_review"},
                reviewed_at=NOW - timedelta(days=1),
                expires_at=approval_expiry,
            ),
            OPERATOR,
        )
        session.commit()

    # Past the terms' own window but still inside the approval's window.
    at = terms_expiry + timedelta(days=1)
    with session_factory() as session:
        result = SourceExpirySweep(session).sweep(now=at)
        session.commit()

    assert result.expired_terms == [(SOURCE_KEY, 1)]
    assert result.expired_approvals == []  # the approval itself has not lapsed

    with session_factory() as session:
        terms = session.query(SourceTerms).filter(SourceTerms.source_key == SOURCE_KEY).one()
        approval = (
            session.query(SourceTermsApproval)
            .filter(SourceTermsApproval.source_key == SOURCE_KEY)
            .one()
        )
        assert terms.status == SourceTermsStatus.EXPIRED.value
        assert approval.status == SourceApprovalStatus.ACTIVE.value
