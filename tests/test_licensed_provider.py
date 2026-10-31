"""DCA-125 — enrich an organization from ONE licensed provider (the third concrete source adapter).

Pins the load-bearing behaviours of :mod:`app.sources.licensed_provider` — the organization-only
licensed-provider adapter that enriches an organization observation the business already holds with
fields fetched from a PAID/licensed B2B provider, behind the DCA-122 adapter contract and the DCA-048
rights gate, WITH the paid call flowing through the DCA-113 budget reservation + DCA-115 provider-call
lifecycle + DCA-114 reconciliation. **No real/paid provider call is ever made** — the only client is the
synthetic fake (:class:`SyntheticLicensedProviderClient`), which performs no network call:

1. **Approved + budgeted fetch records provenanced observations + a reconciled, tracked spend.** An
   approved+entitled source with budget headroom reserves the estimate, records a provider call that
   reaches SUCCEEDED, settles the hold at the reported actual cost (unused headroom released), and
   records the permitted company fields as observed :class:`FieldObservation` rows.
2. **Organization-only.** A personal/contact field the provider returns is DROPPED, never recorded.
3. **Unentitled is denied and does nothing.** A rights denial reserves no budget, makes no provider call
   and writes no observation / provenance / ledger row.
4. **Over-budget is denied and does nothing.** A reservation that exceeds a cap is refused BEFORE the
   call, so no provider call is made and nothing is spent or written.
5. **An UNCERTAIN outcome preserves the hold and records nothing.** A dispatched-then-unconfirmed call is
   kept distinct from a confirmed failure: the hold is preserved (awaiting the DCA-116 resolver) and no
   data is trusted/recorded; a FAILED call releases the hold in full.
6. **Idempotent re-fetch.** Re-running the same fetch key is a deterministic no-op (no second
   reservation, no second provider call, no duplicate facts).
7. **Tenant isolation.** Two businesses fetch independently; neither sees the other's provenance.

The adapter logic is unit-tested on in-memory SQLite; the real transaction / RLS path is exercised on a
disposable PostgreSQL (``DCA_TEST_DATABASE_URL``). The RLS/migration paths for the table are covered by
``tests/test_rls.py`` and ``tests/test_migrations.py``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine

from app.audit.sink import ActorType, AuditActor
from app.cost.reserve import REASON_PER_RUN_EXCEEDED
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import (
    AuditEvent,
    BudgetReservation,
    ContactPoint,
    FieldObservation,
    LicensedProviderFetch,
    PersonObservation,
    ProviderCallRecord,
    ProviderCallStatus,
    ReservationStatus,
    Source,
    SourceStatus,
)
from app.services.budget_store import BudgetLimitInput, BudgetLimitStore
from app.services.org_observation import ObservationInput, OrgObservationStore
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
from app.services.source_preflight import REASON_SOURCE_NOT_ENTITLED
from app.services.source_terms import SourceTermsInput, SourceTermsStore
from app.sources.licensed_provider import (
    LICENSED_PROVIDER_CAPABILITY,
    PERMITTED_ORG_FIELDS,
    REASON_SUBJECT_NOT_FOUND,
    LicensedProviderActor,
    LicensedProviderAdapter,
    LicensedProviderAuthorizationError,
    LicensedProviderError,
    LicensedProviderFetchRequest,
    _is_contact_value,
)
from app.sources.licensed_provider_contract import LicensedProviderOutcome
from app.sources.licensed_provider_fake import (
    SYNTHETIC_PROVIDER,
    SyntheticLicensedProviderClient,
)

SOURCE_KEY = "synthetic_b2b_provider"
BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"
NOW = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)  # 14:30 IST → day 2026-10-31, month 2026-10
ACTOR = AuditActor(id="00000000-0000-4000-9000-0000000a0125", type=ActorType.USER)

ADMIN = LicensedProviderActor(
    subject="00000000-0000-4000-9000-0000000a0125", roles=("owner", "admin")
)
VIEWER = LicensedProviderActor(subject="00000000-0000-4000-9000-0000000v0125", roles=("viewer",))
ENTITLER = BusinessEntitler(
    subject="00000000-0000-4000-9000-0000000a0125", roles=("owner", "admin")
)
BUDGET_ACTOR = AuditActor(id="00000000-0000-4000-9000-0000000b0125", type=ActorType.USER)
OPERATOR = ReviewerPrincipal(subject="op-001", roles=("platform_operator",))

ESTIMATE_MINOR = 2_000  # ₹20.00 reserved up front; the fake reports ₹12.00 actual on success


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def _catalogue_approve_enable(session, *, source_key: str = SOURCE_KEY) -> None:
    """Catalogue (DCA-044), record terms (DCA-045), approve (DCA-046) and TEST-ONLY enable a licensed
    provider source so it is the in-force platform ceiling for collect/prospecting. Enabling is
    synthetic: the running service never promotes a source to ENABLED (a disabled future capability)."""
    SourceCatalogStore(session).create_draft(
        SourceDraftInput(
            key=source_key,
            kind="commercial",
            access_method="api",
            trust_tier="licensed_provider",
            india_coverage={"nationwide": True},
        )
    )
    SourceTermsStore(session).add_version(
        SourceTermsInput(
            source_key=source_key,
            operations={"collect": True, "store": True},
            purposes=["prospecting"],
            contract_evidence={"reference": "LICENSE-2026", "kind": "data_license"},
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


def _entitle(session, business_id: str, *, source_key: str = SOURCE_KEY) -> None:
    BusinessSourceEntitlementStore(session, business_id).set_entitlement(
        BusinessSourceEntitlementInput(
            source_key=source_key,
            enabled=True,
            operations={"collect": True},
            purposes=["prospecting"],
        ),
        ENTITLER,
        now=NOW,
    )


def _seed_budget(
    session, business_id, *, daily=100_000, monthly=1_000_000, per_run=100_000
) -> None:
    BudgetLimitStore(session, business_id).save(
        BudgetLimitInput(
            daily_cap_minor=daily, monthly_cap_minor=monthly, per_run_cap_minor=per_run
        ),
        BUDGET_ACTOR,
    )


def _seed_subject(session, business_id: str, *, name: str = "Synthetic B2B Target Pvt Ltd") -> str:
    obs = OrgObservationStore(session, business_id).create(
        ObservationInput(entity_type="legal_entity", display_name=name), actor=ACTOR, now=NOW
    )
    session.flush()
    return obs.id


def _request(subject_id: str, **overrides) -> LicensedProviderFetchRequest:
    base = {
        "subject_observation_id": subject_id,
        "source_key": SOURCE_KEY,
        "source_version": 1,
        "purpose": "prospecting",
        "org_ref": "CIN-U72900MH2026PTC000125",
        "fetch_key": "fetch-1",
        "estimated_cost_minor": ESTIMATE_MINOR,
        "scope": "MH",
    }
    base.update(overrides)
    return LicensedProviderFetchRequest(**base)


# --- pure helpers ----------------------------------------------------------------------------------


def test_capability_is_organization_only():
    assert LICENSED_PROVIDER_CAPABILITY.is_organization_only is True
    assert LICENSED_PROVIDER_CAPABILITY.handles_personal_contacts is False
    assert "founder_email" not in PERMITTED_ORG_FIELDS


def test_contact_value_detection():
    assert _is_contact_value("founder@synthetic.example") is True
    assert _is_contact_value("+919876543210") is True
    assert _is_contact_value("Information Technology") is False


# --- 1/2) approved + budgeted fetch records facts, reconciles spend, drops contacts ----------------


def test_approved_fetch_records_facts_and_reconciles_spend(session_factory):
    with session_factory() as session:
        _catalogue_approve_enable(session)
        _entitle(session, BIZ_A)
        _seed_budget(session, BIZ_A)
        subject_id = _seed_subject(session, BIZ_A)
        session.commit()

    with session_factory() as session:
        adapter = LicensedProviderAdapter(session, BIZ_A, SyntheticLicensedProviderClient())
        result = adapter.fetch(_request(subject_id), ADMIN, now=NOW)
        assert result.authorized is True and result.skipped is False
        assert result.phase == "call"
        assert result.outcome == "succeeded" and result.call_status == "succeeded"
        # Five permitted company fields recorded; one personal field (founder_email) dropped.
        assert {k for k, _ in result.recorded} == {
            "legal_name",
            "industry",
            "employee_count",
            "annual_revenue_band",
            "hq_state",
        }
        assert result.fields_recorded == 5
        assert result.contact_fields_dropped == 1
        # Bounded, tracked spend: reserved ₹20.00, settled at the reported ₹12.00, ₹8.00 released.
        assert result.reserved_minor == ESTIMATE_MINOR
        assert result.settled_minor == 1_200 and result.spent_minor == 1_200
        assert result.released_minor == 800
        session.commit()

    with session_factory() as session:
        # Five observed field observations citing the provider-call provenance; NO contact rows.
        fields = session.query(FieldObservation).all()
        assert len(fields) == 5
        assert all(f.subject_id == subject_id for f in fields)
        assert all(f.inference_label == "observed" for f in fields)
        assert all(f.source_key == SOURCE_KEY and f.source_version == 1 for f in fields)
        assert all(f.scope == "MH" for f in fields)
        assert all(f.provenance_ref.startswith("licensed:") for f in fields)
        assert session.query(PersonObservation).count() == 0
        assert session.query(ContactPoint).count() == 0

        # The provider call is a reconciled, tracked SUCCEEDED lifecycle record linked to the hold.
        call = session.query(ProviderCallRecord).one()
        assert call.status == ProviderCallStatus.SUCCEEDED.value
        assert call.dispatched is True and call.cost_possible is True
        assert call.total_tokens == 240

        # The reservation is settled at the actual cost (the unused headroom released).
        reservation = session.query(BudgetReservation).one()
        assert reservation.id == call.reservation_id
        assert reservation.status == ReservationStatus.SETTLED.value
        assert reservation.settled_amount_minor == 1_200
        assert reservation.overspend_minor == 0

        # One run-level provenance row with the cost + outcome attribution.
        prov = session.query(LicensedProviderFetch).one()
        assert prov.subject_id == subject_id and prov.source_key == SOURCE_KEY
        assert prov.outcome == "succeeded" and prov.call_status == "succeeded"
        assert prov.fields_recorded == 5 and prov.contact_fields_dropped == 1
        assert prov.reserved_minor == ESTIMATE_MINOR and prov.settled_minor == 1_200
        assert prov.needs_review is False


def test_over_run_cost_is_clamped_and_flagged(session_factory):
    """A reported actual cost ABOVE the reserved hold is clamped to the hold and flagged as overspend —
    never charged past the cap the reserve path admitted it under."""
    with session_factory() as session:
        _catalogue_approve_enable(session)
        _entitle(session, BIZ_A)
        _seed_budget(session, BIZ_A)
        subject_id = _seed_subject(session, BIZ_A)
        session.commit()

    with session_factory() as session:
        # The provider reports ₹30.00 actual, above the ₹20.00 reserved estimate.
        client = SyntheticLicensedProviderClient(cost_minor=3_000)
        result = LicensedProviderAdapter(session, BIZ_A, client).fetch(
            _request(subject_id), ADMIN, now=NOW
        )
        assert result.settled_minor == ESTIMATE_MINOR  # clamped to the reserved hold
        session.commit()

    with session_factory() as session:
        reservation = session.query(BudgetReservation).one()
        assert reservation.status == ReservationStatus.SETTLED.value
        assert reservation.settled_amount_minor == ESTIMATE_MINOR
        assert reservation.overspend_minor == 1_000  # ₹10.00 over-run, flagged, never charged


# --- 3) a rights denial does nothing ---------------------------------------------------------------


def test_unentitled_fetch_is_denied_and_does_nothing(session_factory):
    with session_factory() as session:
        # Approved + enabled at the platform level, but the business has NO entitlement -> denied.
        _catalogue_approve_enable(session)
        _seed_budget(session, BIZ_A)
        subject_id = _seed_subject(session, BIZ_A)
        session.commit()

    with session_factory() as session:
        result = LicensedProviderAdapter(session, BIZ_A, SyntheticLicensedProviderClient()).fetch(
            _request(subject_id), ADMIN, now=NOW
        )
        assert result.authorized is False
        assert result.phase == "rights"
        assert result.reason == REASON_SOURCE_NOT_ENTITLED
        assert result.reservation_id is None and result.provider_call_id is None
        session.rollback()

    with session_factory() as session:
        # A denial changed nothing: no reservation, no provider call, no fields, no provenance.
        assert session.query(BudgetReservation).count() == 0
        assert session.query(ProviderCallRecord).count() == 0
        assert session.query(FieldObservation).count() == 0
        assert session.query(LicensedProviderFetch).count() == 0
        # Only the two seed audits remain (budget-limit save + subject seed); the denied fetch added
        # no audit of its own (the rights preflight is a pure read).
        assert session.query(AuditEvent).count() == 2


# --- 4) over-budget is denied BEFORE the call and does nothing -------------------------------------


def test_over_budget_fetch_is_denied_with_no_call_and_no_spend(session_factory):
    with session_factory() as session:
        _catalogue_approve_enable(session)
        _entitle(session, BIZ_A)
        # A per-run cap far below the estimate: the reservation cannot be admitted.
        _seed_budget(session, BIZ_A, per_run=500)
        subject_id = _seed_subject(session, BIZ_A)
        session.commit()

    with session_factory() as session:
        result = LicensedProviderAdapter(session, BIZ_A, SyntheticLicensedProviderClient()).fetch(
            _request(subject_id), ADMIN, now=NOW
        )
        assert result.authorized is False
        assert result.phase == "budget"
        assert result.reason == REASON_PER_RUN_EXCEEDED
        assert result.provider_call_id is None and result.spent_minor == 0
        session.rollback()

    with session_factory() as session:
        # No reservation was staged, NO provider call was made, nothing was spent or recorded.
        assert session.query(BudgetReservation).count() == 0
        assert session.query(ProviderCallRecord).count() == 0
        assert session.query(FieldObservation).count() == 0
        assert session.query(LicensedProviderFetch).count() == 0


# --- 5) uncertain / failed outcome handling --------------------------------------------------------


def test_uncertain_outcome_preserves_the_hold_and_records_nothing(session_factory):
    with session_factory() as session:
        _catalogue_approve_enable(session)
        _entitle(session, BIZ_A)
        _seed_budget(session, BIZ_A)
        subject_id = _seed_subject(session, BIZ_A)
        session.commit()

    with session_factory() as session:
        client = SyntheticLicensedProviderClient(outcome=LicensedProviderOutcome.UNCERTAIN)
        result = LicensedProviderAdapter(session, BIZ_A, client).fetch(
            _request(subject_id), ADMIN, now=NOW
        )
        assert result.outcome == "uncertain" and result.needs_review is True
        assert result.fields_recorded == 0
        # The full hold is preserved (counts in full), nothing settled, nothing released.
        assert result.settled_minor is None
        assert result.spent_minor == ESTIMATE_MINOR and result.released_minor == 0
        session.commit()

    with session_factory() as session:
        call = session.query(ProviderCallRecord).one()
        assert call.status == ProviderCallStatus.UNCERTAIN.value and call.cost_possible is True
        reservation = session.query(BudgetReservation).one()
        assert reservation.status == ReservationStatus.UNCERTAIN.value
        assert session.query(FieldObservation).count() == 0  # no data trusted/recorded
        prov = session.query(LicensedProviderFetch).one()
        assert prov.outcome == "uncertain" and prov.needs_review is True
        assert prov.settled_minor is None


def test_failed_outcome_releases_the_hold_and_records_nothing(session_factory):
    with session_factory() as session:
        _catalogue_approve_enable(session)
        _entitle(session, BIZ_A)
        _seed_budget(session, BIZ_A)
        subject_id = _seed_subject(session, BIZ_A)
        session.commit()

    with session_factory() as session:
        client = SyntheticLicensedProviderClient(outcome=LicensedProviderOutcome.FAILED)
        result = LicensedProviderAdapter(session, BIZ_A, client).fetch(
            _request(subject_id), ADMIN, now=NOW
        )
        assert result.outcome == "failed"
        assert result.fields_recorded == 0
        # A confirmed no-call failure frees the whole hold: nothing spent, the estimate released.
        assert result.settled_minor == 0 and result.spent_minor == 0
        assert result.released_minor == ESTIMATE_MINOR
        session.commit()

    with session_factory() as session:
        call = session.query(ProviderCallRecord).one()
        assert call.status == ProviderCallStatus.FAILED.value and call.cost_possible is False
        reservation = session.query(BudgetReservation).one()
        assert reservation.status == ReservationStatus.RELEASED.value
        assert session.query(FieldObservation).count() == 0


# --- 6) idempotent re-fetch ------------------------------------------------------------------------


def test_idempotent_refetch_is_a_no_op(session_factory):
    with session_factory() as session:
        _catalogue_approve_enable(session)
        _entitle(session, BIZ_A)
        _seed_budget(session, BIZ_A)
        subject_id = _seed_subject(session, BIZ_A)
        session.commit()

    with session_factory() as session:
        LicensedProviderAdapter(session, BIZ_A, SyntheticLicensedProviderClient()).fetch(
            _request(subject_id), ADMIN, now=NOW
        )
        session.commit()

    with session_factory() as session:
        again = LicensedProviderAdapter(session, BIZ_A, SyntheticLicensedProviderClient()).fetch(
            _request(subject_id), ADMIN, now=NOW
        )
        assert again.skipped is True
        assert again.outcome == "succeeded"
        assert again.fields_recorded == 0  # nothing new recorded on the replay
        session.commit()

    with session_factory() as session:
        # Unchanged: still exactly one reservation, one provider call, five fields, one provenance row.
        assert session.query(BudgetReservation).count() == 1
        assert session.query(ProviderCallRecord).count() == 1
        assert session.query(FieldObservation).count() == 5
        assert session.query(LicensedProviderFetch).count() == 1


# --- 7) authority + request-shape refusals change nothing ------------------------------------------


def test_viewer_is_refused_before_any_state(session_factory):
    with session_factory() as session:
        _catalogue_approve_enable(session)
        _entitle(session, BIZ_A)
        _seed_budget(session, BIZ_A)
        subject_id = _seed_subject(session, BIZ_A)
        session.commit()

    with session_factory() as session:
        with pytest.raises(LicensedProviderAuthorizationError):
            LicensedProviderAdapter(session, BIZ_A, SyntheticLicensedProviderClient()).fetch(
                _request(subject_id), VIEWER, now=NOW
            )
        session.rollback()

    with session_factory() as session:
        assert session.query(BudgetReservation).count() == 0
        assert session.query(ProviderCallRecord).count() == 0
        assert session.query(LicensedProviderFetch).count() == 0


def test_unknown_subject_is_refused_before_any_state(session_factory):
    with session_factory() as session:
        _catalogue_approve_enable(session)
        _entitle(session, BIZ_A)
        _seed_budget(session, BIZ_A)
        _seed_subject(session, BIZ_A)
        session.commit()

    with session_factory() as session:
        with pytest.raises(LicensedProviderError) as exc:
            LicensedProviderAdapter(session, BIZ_A, SyntheticLicensedProviderClient()).fetch(
                _request("does-not-exist"), ADMIN, now=NOW
            )
        assert exc.value.code == REASON_SUBJECT_NOT_FOUND
        session.rollback()

    with session_factory() as session:
        assert session.query(LicensedProviderFetch).count() == 0
        assert session.query(BudgetReservation).count() == 0


def test_a_subject_of_another_business_reads_as_not_found(session_factory):
    with session_factory() as session:
        _catalogue_approve_enable(session)
        _entitle(session, BIZ_A)
        _entitle(session, BIZ_B)
        _seed_budget(session, BIZ_A)
        _seed_budget(session, BIZ_B)
        b_subject = _seed_subject(session, BIZ_B)
        session.commit()

    with session_factory() as session:
        with pytest.raises(LicensedProviderError) as exc:
            LicensedProviderAdapter(session, BIZ_A, SyntheticLicensedProviderClient()).fetch(
                _request(b_subject), ADMIN, now=NOW
            )
        assert exc.value.code == REASON_SUBJECT_NOT_FOUND


# --- 8) tenant isolation ---------------------------------------------------------------------------


def test_two_businesses_fetch_independently(session_factory):
    with session_factory() as session:
        _catalogue_approve_enable(session)
        _entitle(session, BIZ_A)
        _entitle(session, BIZ_B)
        _seed_budget(session, BIZ_A)
        _seed_budget(session, BIZ_B)
        a_subject = _seed_subject(session, BIZ_A)
        b_subject = _seed_subject(session, BIZ_B)
        session.commit()

    with session_factory() as session:
        LicensedProviderAdapter(session, BIZ_A, SyntheticLicensedProviderClient()).fetch(
            _request(a_subject), ADMIN, now=NOW
        )
        LicensedProviderAdapter(session, BIZ_B, SyntheticLicensedProviderClient()).fetch(
            _request(b_subject), ADMIN, now=NOW
        )
        session.commit()

    with session_factory() as session:
        a = LicensedProviderAdapter(session, BIZ_A, SyntheticLicensedProviderClient()).fetches_for(
            SOURCE_KEY, a_subject
        )
        b = LicensedProviderAdapter(session, BIZ_B, SyntheticLicensedProviderClient()).fetches_for(
            SOURCE_KEY, b_subject
        )
        assert len(a) == 1 and len(b) == 1
        assert a[0].business_id == BIZ_A and b[0].business_id == BIZ_B
        # Neither business sees the other's fetch provenance.
        assert (
            LicensedProviderAdapter(session, BIZ_A, SyntheticLicensedProviderClient()).fetches_for(
                SOURCE_KEY, b_subject
            )
            == []
        )


def test_store_never_commits(session_factory):
    with session_factory() as session:
        _catalogue_approve_enable(session)
        _entitle(session, BIZ_A)
        _seed_budget(session, BIZ_A)
        subject_id = _seed_subject(session, BIZ_A)
        session.commit()

    with session_factory() as session:
        LicensedProviderAdapter(session, BIZ_A, SyntheticLicensedProviderClient()).fetch(
            _request(subject_id), ADMIN, now=NOW
        )
        session.rollback()  # the adapter never commits — a rollback undoes the whole run

    with session_factory() as session:
        assert session.query(LicensedProviderFetch).count() == 0
        assert session.query(BudgetReservation).count() == 0
        assert session.query(ProviderCallRecord).count() == 0
        assert session.query(FieldObservation).count() == 0


def test_default_provider_is_the_synthetic_fake(session_factory):
    with session_factory() as session:
        _catalogue_approve_enable(session)
        _entitle(session, BIZ_A)
        _seed_budget(session, BIZ_A)
        subject_id = _seed_subject(session, BIZ_A)
        session.commit()

    with session_factory() as session:
        result = LicensedProviderAdapter(session, BIZ_A, SyntheticLicensedProviderClient()).fetch(
            _request(subject_id), ADMIN, now=NOW
        )
        assert result.provider == SYNTHETIC_PROVIDER
        session.commit()


# --- 9) Postgres-backed: the real transaction / RLS path -------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")


@pytest.mark.skipif(not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres")
def test_fetch_over_postgres():
    """Exercise the real approved-fetch + idempotent re-fetch path on a disposable PostgreSQL with real
    transactions (reserve → provider-call lifecycle → reconcile → observations) — never SQLite for the
    durable write path."""
    engine = create_engine(PG_URL, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        factory = make_session_factory(engine)
        with factory() as session:
            _catalogue_approve_enable(session)
            _entitle(session, BIZ_A)
            _seed_budget(session, BIZ_A)
            subject_id = _seed_subject(session, BIZ_A)
            session.commit()

        with factory() as session:
            result = LicensedProviderAdapter(
                session, BIZ_A, SyntheticLicensedProviderClient()
            ).fetch(_request(subject_id), ADMIN, now=NOW)
            assert result.fields_recorded == 5 and result.settled_minor == 1_200
            session.commit()

        with factory() as session:
            again = LicensedProviderAdapter(
                session, BIZ_A, SyntheticLicensedProviderClient()
            ).fetch(_request(subject_id), ADMIN, now=NOW)
            assert again.skipped is True
            session.commit()

        with factory() as session:
            assert session.query(FieldObservation).count() == 5
            assert session.query(BudgetReservation).count() == 1
            assert session.query(ProviderCallRecord).count() == 1
            assert session.query(LicensedProviderFetch).count() == 1
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
