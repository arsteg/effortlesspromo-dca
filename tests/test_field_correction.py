"""DCA-070 — protect user-verified field corrections.

Pins the load-bearing behaviours of the authorized, versioned correction store
(:mod:`app.services.field_correction`) and the pure correction overlay
(:mod:`app.canonical.correction_overlay`) that gives a protected correction precedence over the
DCA-069 provider selection:

1. **A refresh does not overwrite a protected correction.** After a user corrects a field, recording a
   NEWER provider observation (a "refresh") changes the DCA-069 provider winner — yet the overlay keeps
   the user's protected value canonical, and the explanation records
   ``refresh_did_not_overwrite``. Every observation (and the correction) stays in history. An
   UNprotected correction is the failure case: the provider refresh wins instead.
2. **A correction stays business-private.** Business B has no correction for A's subject/field (its
   overlay returns the provider value), a cross-tenant by-id read is not-found, and B cannot even
   attach a correction to A's subject (resolves as not-found, stages nothing).
3. **Authorized + attributed (the relevant denial).** Only a business user holding
   :attr:`~app.identity.permissions.Scope.FIELD_CORRECT` may record a correction; a read-only viewer
   or a platform operator is refused BEFORE the session is mutated, so a denial stages no row and no
   audit entry. The raw corrected value is never written to the audit trail.
4. **Versioned, insert-only history.** Each correction inserts a new version at ``max(version)+1``;
   prior versions are preserved and the active correction is the highest version.

The store logic is unit-tested on in-memory SQLite; the real transaction / RLS path is exercised on a
disposable PostgreSQL (``DCA_TEST_DATABASE_URL``) by the test at the end — never relying on SQLite for
that. The RLS/migration paths for the table are covered by ``tests/test_rls.py`` and
``tests/test_migrations.py``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine

from app.audit.sink import ActorType, AuditActor
from app.canonical import (
    ORIGIN_PROVIDER_SELECTION,
    ORIGIN_USER_CORRECTION,
    REASON_CORRECTION_NOT_PROTECTED,
    REASON_NO_USER_CORRECTION,
    REASON_REFRESH_DID_NOT_OVERWRITE,
    REASON_USER_CORRECTION_PROTECTED,
    apply_user_correction,
    candidates_from_observations,
    correction_from_row,
    select_canonical,
)
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import AuditEvent, FieldCorrection, FieldObservation
from app.identity.permissions import PLATFORM_OPERATOR_ROLE
from app.services.field_correction import (
    AUDIT_ACTION_RECORD_CORRECTION,
    REASON_MISSING_CORRECTED_VALUE,
    REASON_MISSING_FIELD_KEY,
    REASON_NOT_AUTHORIZED,
    REASON_SUBJECT_NOT_FOUND,
    FieldCorrectionAuthorizationError,
    FieldCorrectionError,
    FieldCorrectionInput,
    FieldCorrector,
    FieldCorrectionStore,
)
from app.services.field_observation import FieldObservationInput, FieldObservationStore
from app.services.org_observation import ObservationInput, OrgObservationStore
from app.services.source_catalog import SourceCatalogStore, SourceDraftInput
from app.services.source_terms import SourceTermsInput, SourceTermsStore

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"
ACTOR = AuditActor(id="00000000-0000-4000-9000-0000000a0070", type=ActorType.USER)
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
FIELD = "annual_revenue_inr"

# An authorized acting business user, a read-only viewer, and a platform operator (no business scope).
USER = FieldCorrector(subject="00000000-0000-4000-9000-0000000c0070", roles=("user",))
VIEWER = FieldCorrector(subject="00000000-0000-4000-9000-0000000c0071", roles=("viewer",))
OPERATOR = FieldCorrector(
    subject="00000000-0000-4000-9000-0000000c0072", roles=(PLATFORM_OPERATOR_ROLE,)
)


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


def _observe(store, subject, src, ver, value, observed_at) -> str:
    """Record one provider OBSERVED field observation, return its id."""
    obs = store.record(
        FieldObservationInput(
            subject_type="organization",
            subject_id=subject,
            field_key=FIELD,
            field_value=value,
            inference_label="observed",
            source_key=src,
            source_version=ver,
            provenance_ref="https://synthetic.registry.example/mca/record/42#revenue",
            observed_at=observed_at,
            scope="nationwide",
            rights_basis="source_terms:collect+store;purpose=prospecting",
        ),
        actor=ACTOR,
    )
    return obs.id


def _provider_selection(session, business_id: str, subject: str):
    """Run the DCA-069 provider selection over the stored observation history for one subject/field."""
    observations = FieldObservationStore(session, business_id).for_subject("organization", subject)
    candidates = candidates_from_observations(observations)
    return select_canonical(candidates, now=NOW)


# --- 1) a refresh does not overwrite a protected correction ---------------------------------------


def test_protected_correction_wins_over_a_newer_provider_observation_after_refresh(session_factory):
    """The acceptance case: the user corrects a field (protected), then a NEWER provider observation
    arrives (a refresh that changes the DCA-069 provider winner) — the business still sees the user's
    corrected value, with ``refresh_did_not_overwrite`` recorded, and nothing is deleted."""
    with session_factory() as session:
        src, ver = _seed_source(session)
        subject = _seed_org_subject(session, BIZ_A)
        obs_store = FieldObservationStore(session, BIZ_A)
        # Initial provider value, then the user's authoritative correction.
        _observe(obs_store, subject, src, ver, "120000000", NOW - timedelta(days=30))
        correction = FieldCorrectionStore(session, BIZ_A).record(
            FieldCorrectionInput(
                subject_type="organization",
                subject_id=subject,
                field_key=FIELD,
                corrected_value="155000000",
                reason="verified_by_customer",
            ),
            corrector=USER,
            now=NOW - timedelta(days=10),
        )
        session.commit()
        correction_id = correction.id

    # Provider selection BEFORE a refresh already differs from the correction (120M vs 155M).
    with session_factory() as session:
        before = _provider_selection(session, BIZ_A, subject)
        assert before.winner is not None and before.winner.value == "120000000"

    # A refresh: a newer provider observation lands — its value becomes the new provider winner.
    # (The source catalog is platform-shared, already seeded above — reuse src/ver, never re-seed.)
    with session_factory() as session:
        obs_store = FieldObservationStore(session, BIZ_A)
        _observe(obs_store, subject, src, ver, "999000000", NOW - timedelta(days=1))
        session.commit()

    with session_factory() as session:
        provider = _provider_selection(session, BIZ_A, subject)
        # The refresh DID change the raw provider winner...
        assert provider.winner is not None and provider.winner.value == "999000000"

        active = FieldCorrectionStore(session, BIZ_A).active_for_field(
            "organization", subject, FIELD
        )
        outcome = apply_user_correction(provider, correction_from_row(active))

        # ...but the business still sees the user's protected value.
        assert outcome.value == "155000000"
        assert outcome.origin == ORIGIN_USER_CORRECTION
        assert outcome.refresh_protected is True
        assert REASON_USER_CORRECTION_PROTECTED in outcome.reasons
        assert REASON_REFRESH_DID_NOT_OVERWRITE in outcome.reasons
        assert outcome.correction is not None and outcome.correction.correction_id == correction_id
        assert outcome.mutated_state is False

        # Nothing was deleted: two provider observations + one correction all remain in history.
        assert session.query(FieldObservation).count() == 2
        assert session.query(FieldCorrection).count() == 1


def test_unprotected_correction_does_not_override_a_refresh(session_factory):
    """The failure case for protection: a correction the user explicitly recorded as UNprotected does
    NOT win over a provider refresh — the provider selection governs and the reason says so."""
    with session_factory() as session:
        src, ver = _seed_source(session)
        subject = _seed_org_subject(session, BIZ_A)
        obs_store = FieldObservationStore(session, BIZ_A)
        _observe(obs_store, subject, src, ver, "999000000", NOW - timedelta(days=1))
        FieldCorrectionStore(session, BIZ_A).record(
            FieldCorrectionInput(
                subject_type="organization",
                subject_id=subject,
                field_key=FIELD,
                corrected_value="155000000",
                protected_from_refresh=False,
            ),
            corrector=USER,
            now=NOW,
        )
        session.commit()

    with session_factory() as session:
        provider = _provider_selection(session, BIZ_A, subject)
        active = FieldCorrectionStore(session, BIZ_A).active_for_field(
            "organization", subject, FIELD
        )
        outcome = apply_user_correction(provider, correction_from_row(active))
        assert outcome.value == "999000000"  # the provider refresh wins
        assert outcome.origin == ORIGIN_PROVIDER_SELECTION
        assert outcome.refresh_protected is False
        assert REASON_CORRECTION_NOT_PROTECTED in outcome.reasons


def test_no_correction_lets_the_provider_selection_govern(session_factory):
    with session_factory() as session:
        src, ver = _seed_source(session)
        subject = _seed_org_subject(session, BIZ_A)
        _observe(FieldObservationStore(session, BIZ_A), subject, src, ver, "120000000", NOW)
        session.commit()

    with session_factory() as session:
        provider = _provider_selection(session, BIZ_A, subject)
        outcome = apply_user_correction(provider, correction_from_row(None))
        assert outcome.value == "120000000"
        assert outcome.origin == ORIGIN_PROVIDER_SELECTION
        assert REASON_NO_USER_CORRECTION in outcome.reasons


# --- 2) a correction stays business-private -------------------------------------------------------


def test_correction_is_business_private_b_sees_the_provider_value(session_factory):
    """A protects a field; B observes the SAME field for its OWN subject. B has no correction, so B's
    overlay returns B's provider value — the same field shows corrected in A and provider in B."""
    with session_factory() as session:
        src, ver = _seed_source(session)
        subject_a = _seed_org_subject(session, BIZ_A)
        subject_b = _seed_org_subject(session, BIZ_B)
        # A observes + corrects; B only observes (its own subject).
        _observe(FieldObservationStore(session, BIZ_A), subject_a, src, ver, "120000000", NOW)
        _observe(FieldObservationStore(session, BIZ_B), subject_b, src, ver, "120000000", NOW)
        FieldCorrectionStore(session, BIZ_A).record(
            FieldCorrectionInput(
                subject_type="organization",
                subject_id=subject_a,
                field_key=FIELD,
                corrected_value="155000000",
            ),
            corrector=USER,
            now=NOW,
        )
        session.commit()

    with session_factory() as session:
        # A sees the corrected value.
        provider_a = _provider_selection(session, BIZ_A, subject_a)
        active_a = FieldCorrectionStore(session, BIZ_A).active_for_field(
            "organization", subject_a, FIELD
        )
        outcome_a = apply_user_correction(provider_a, correction_from_row(active_a))
        assert outcome_a.value == "155000000"
        assert outcome_a.origin == ORIGIN_USER_CORRECTION

        # B has no correction for its subject/field — the overlay returns B's provider value.
        active_b = FieldCorrectionStore(session, BIZ_B).active_for_field(
            "organization", subject_b, FIELD
        )
        assert active_b is None
        provider_b = _provider_selection(session, BIZ_B, subject_b)
        outcome_b = apply_user_correction(provider_b, correction_from_row(active_b))
        assert outcome_b.value == "120000000"
        assert outcome_b.origin == ORIGIN_PROVIDER_SELECTION


def test_cross_tenant_correction_id_reads_as_not_found(session_factory):
    with session_factory() as session:
        src, ver = _seed_source(session)
        subject_a = _seed_org_subject(session, BIZ_A)
        _observe(FieldObservationStore(session, BIZ_A), subject_a, src, ver, "120000000", NOW)
        correction = FieldCorrectionStore(session, BIZ_A).record(
            FieldCorrectionInput(
                subject_type="organization",
                subject_id=subject_a,
                field_key=FIELD,
                corrected_value="155000000",
            ),
            corrector=USER,
            now=NOW,
        )
        session.commit()
        correction_id = correction.id

    with session_factory() as session:
        store_b = FieldCorrectionStore(session, BIZ_B)
        assert store_b.get(correction_id) is None  # A's id reads exactly like not-found for B


def test_business_b_cannot_attach_a_correction_to_a_subject_and_stages_nothing(session_factory):
    """B cannot overwrite/record a correction on A's subject: the subject resolves as not-found in B,
    the create is refused before the session is mutated, and no row is staged."""
    with session_factory() as session:
        src, ver = _seed_source(session)
        subject_a = _seed_org_subject(session, BIZ_A)
        _observe(FieldObservationStore(session, BIZ_A), subject_a, src, ver, "120000000", NOW)
        session.commit()

    with session_factory() as session:
        store_b = FieldCorrectionStore(session, BIZ_B)
        with pytest.raises(FieldCorrectionError) as exc:
            store_b.record(
                FieldCorrectionInput(
                    subject_type="organization",
                    subject_id=subject_a,  # owned by A
                    field_key=FIELD,
                    corrected_value="1",
                ),
                corrector=USER,
                now=NOW,
            )
        assert exc.value.code == REASON_SUBJECT_NOT_FOUND
        session.rollback()

    with session_factory() as session:
        assert session.query(FieldCorrection).count() == 0


# --- 3) authorized + attributed (the relevant denial) ---------------------------------------------


@pytest.mark.parametrize(
    "corrector", [VIEWER, OPERATOR], ids=["read_only_viewer", "platform_operator"]
)
def test_unauthorized_actor_cannot_record_a_correction_and_changes_no_state(
    session_factory, corrector
):
    """A read-only viewer (no ``field.correct``) and a platform operator (no business membership) are
    both refused BEFORE any state is read or written — the denial stages no correction row and no audit
    entry."""
    with session_factory() as session:
        src, ver = _seed_source(session)
        subject = _seed_org_subject(session, BIZ_A)
        _observe(FieldObservationStore(session, BIZ_A), subject, src, ver, "120000000", NOW)
        session.commit()

    with session_factory() as session:
        store = FieldCorrectionStore(session, BIZ_A)
        with pytest.raises(FieldCorrectionAuthorizationError) as exc:
            store.record(
                FieldCorrectionInput(
                    subject_type="organization",
                    subject_id=subject,
                    field_key=FIELD,
                    corrected_value="155000000",
                ),
                corrector=corrector,
                now=NOW,
            )
        assert exc.value.code == REASON_NOT_AUTHORIZED
        session.rollback()

    with session_factory() as session:
        assert session.query(FieldCorrection).count() == 0
        assert (
            session.query(AuditEvent)
            .filter(AuditEvent.action == AUDIT_ACTION_RECORD_CORRECTION)
            .count()
            == 0
        )


def test_authorization_is_checked_before_validation(session_factory):
    """Even with an invalid payload (empty field key / value), an unauthorized caller is refused with
    the authorization code — authority is the FIRST gate, so nothing is read or written."""
    with session_factory() as session:
        subject = _seed_org_subject(session, BIZ_A)
        session.commit()

    with session_factory() as session:
        store = FieldCorrectionStore(session, BIZ_A)
        with pytest.raises(FieldCorrectionAuthorizationError) as exc:
            store.record(
                FieldCorrectionInput(
                    subject_type="organization",
                    subject_id=subject,
                    field_key="",  # would be REASON_MISSING_FIELD_KEY if we reached validation
                    corrected_value="",
                ),
                corrector=VIEWER,
                now=NOW,
            )
        assert exc.value.code == REASON_NOT_AUTHORIZED


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (dict(field_key="   "), REASON_MISSING_FIELD_KEY),
        (dict(corrected_value="  "), REASON_MISSING_CORRECTED_VALUE),
        (dict(subject_id="does-not-exist"), REASON_SUBJECT_NOT_FOUND),
    ],
)
def test_authorized_but_invalid_payloads_are_rejected_and_stage_nothing(
    session_factory, mutation, expected_code
):
    with session_factory() as session:
        subject = _seed_org_subject(session, BIZ_A)
        store = FieldCorrectionStore(session, BIZ_A)
        base = dict(
            subject_type="organization",
            subject_id=subject,
            field_key=FIELD,
            corrected_value="155000000",
        )
        base.update(mutation)
        with pytest.raises(FieldCorrectionError) as exc:
            store.record(FieldCorrectionInput(**base), corrector=USER, now=NOW)  # type: ignore[arg-type]
        assert exc.value.code == expected_code
        session.rollback()
    with session_factory() as session:
        assert session.query(FieldCorrection).count() == 0


def test_correction_is_atomically_audited_and_never_carries_the_raw_value(session_factory):
    secret_value = "₹15,50,00,000 confidential corrected revenue"
    with session_factory() as session:
        subject = _seed_org_subject(session, BIZ_A)
        FieldCorrectionStore(session, BIZ_A).record(
            FieldCorrectionInput(
                subject_type="organization",
                subject_id=subject,
                field_key=FIELD,
                corrected_value=secret_value,
            ),
            corrector=USER,
            now=NOW,
        )
        session.commit()

    with session_factory() as session:
        audits = (
            session.query(AuditEvent)
            .filter(AuditEvent.action == AUDIT_ACTION_RECORD_CORRECTION)
            .all()
        )
        assert len(audits) == 1
        blob = repr(audits[0].references) + audits[0].reason
        assert secret_value not in blob  # the raw corrected value is never audited
        assert audits[0].references["fieldKey"] == FIELD
        assert audits[0].references["version"] == 1
        assert audits[0].references["protectedFromRefresh"] is True
        # The acting user is attributed on the audit entry.
        assert audits[0].actor_id == USER.subject


# --- 4) versioned, insert-only history ------------------------------------------------------------


def test_each_correction_inserts_a_new_version_and_prior_versions_are_preserved(session_factory):
    """Correcting the same field twice yields versions 1 then 2; the active correction is the highest
    version, and the prior version remains in history unchanged."""
    with session_factory() as session:
        subject = _seed_org_subject(session, BIZ_A)
        store = FieldCorrectionStore(session, BIZ_A)
        v1 = store.record(
            FieldCorrectionInput(
                subject_type="organization",
                subject_id=subject,
                field_key=FIELD,
                corrected_value="155000000",
            ),
            corrector=USER,
            now=NOW - timedelta(days=2),
        )
        session.commit()
        v1_id, v1_version = v1.id, v1.version

    with session_factory() as session:
        store = FieldCorrectionStore(session, BIZ_A)
        # A later correction re-records the field as UNprotected (a new version, nothing edited).
        v2 = store.record(
            FieldCorrectionInput(
                subject_type="organization",
                subject_id=subject,
                field_key=FIELD,
                corrected_value="160000000",
                protected_from_refresh=False,
            ),
            corrector=USER,
            now=NOW,
        )
        session.commit()
        assert v1_version == 1
        assert v2.version == 2

    with session_factory() as session:
        store = FieldCorrectionStore(session, BIZ_A)
        history = store.history_for_field("organization", subject, FIELD)
        assert [h.version for h in history] == [1, 2]  # oldest first, both preserved
        active = store.active_for_field("organization", subject, FIELD)
        assert active is not None
        assert active.version == 2
        assert active.corrected_value == "160000000"
        assert active.protected_from_refresh is False
        # The original version is untouched: still value 155000000 and still protected.
        original = store.get(v1_id)
        assert original is not None
        assert original.corrected_value == "155000000"
        assert original.protected_from_refresh is True


def test_active_for_subject_returns_the_highest_version_per_field(session_factory):
    with session_factory() as session:
        subject = _seed_org_subject(session, BIZ_A)
        store = FieldCorrectionStore(session, BIZ_A)
        for value in ("1", "2", "3"):
            store.record(
                FieldCorrectionInput(
                    subject_type="organization",
                    subject_id=subject,
                    field_key=FIELD,
                    corrected_value=value,
                ),
                corrector=USER,
                now=NOW,
            )
        store.record(
            FieldCorrectionInput(
                subject_type="organization",
                subject_id=subject,
                field_key="segment",
                corrected_value="mid_market",
            ),
            corrector=USER,
            now=NOW,
        )
        session.commit()
        active = store.active_for_subject("organization", subject)
        assert active[FIELD].version == 3
        assert active[FIELD].corrected_value == "3"
        assert active["segment"].version == 1
        assert session.query(FieldCorrection).count() == 4  # every version preserved


# --- Postgres-backed: the real transaction / RLS path ---------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")


@pytest.mark.skipif(not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres")
def test_record_and_denial_over_postgres():
    """Exercise the real record → read → versioning path AND the authorization denial on a disposable
    PostgreSQL with real transactions — never SQLite for the durable path. A protected correction
    persists and survives a provider refresh; an unauthorized caller is refused and stages nothing."""
    engine = create_engine(PG_URL, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        factory = make_session_factory(engine)
        with factory() as session:
            src, ver = _seed_source(session)
            subject = _seed_org_subject(session, BIZ_A)
            _observe(FieldObservationStore(session, BIZ_A), subject, src, ver, "120000000", NOW)
            FieldCorrectionStore(session, BIZ_A).record(
                FieldCorrectionInput(
                    subject_type="organization",
                    subject_id=subject,
                    field_key=FIELD,
                    corrected_value="155000000",
                ),
                corrector=USER,
                now=NOW,
            )
            session.commit()

        # A refresh lands; the protected correction still governs. (Reuse the shared source.)
        with factory() as session:
            _observe(
                FieldObservationStore(session, BIZ_A),
                subject,
                src,
                ver,
                "999000000",
                NOW + timedelta(days=1),
            )
            session.commit()

        with factory() as session:
            provider = _provider_selection(session, BIZ_A, subject)
            active = FieldCorrectionStore(session, BIZ_A).active_for_field(
                "organization", subject, FIELD
            )
            outcome = apply_user_correction(provider, correction_from_row(active))
            assert outcome.value == "155000000"
            assert outcome.origin == ORIGIN_USER_CORRECTION
            assert REASON_REFRESH_DID_NOT_OVERWRITE in outcome.reasons

        with factory() as session:
            store = FieldCorrectionStore(session, BIZ_A)
            with pytest.raises(FieldCorrectionAuthorizationError):
                store.record(
                    FieldCorrectionInput(
                        subject_type="organization",
                        subject_id=subject,
                        field_key=FIELD,
                        corrected_value="1",
                    ),
                    corrector=VIEWER,
                    now=NOW,
                )
            session.rollback()

        with factory() as session:
            # The refusal left exactly the one committed correction.
            assert session.query(FieldCorrection).count() == 1
    finally:
        Base.metadata.drop_all(engine)
