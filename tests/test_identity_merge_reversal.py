"""DCA-072 — reverse a reviewed identity merge.

Pins the load-bearing behaviours of :meth:`app.services.identity_merge.IdentityMergeStore.reverse`, the
human ACTION that UNDOES a prior DCA-071 merge decision by flipping an ``ACTIVE`` row's ``status`` to
``REVERSED`` — restoring the two identities to their SEPARATE / unmerged state — while preserving all
history (the merge row and its original audit entry are never deleted; a separate reverse audit entry is
appended) and never resurrecting revoked consent:

1. **Undo restores separate identity links + preserves history.** Reversing an active merge flips its
   status to ``reversed`` (so the active link no longer stands), keeps the row and its DCA-071
   ``identity.merge.record`` audit entry intact, and appends one ``identity.merge.reverse`` audit entry
   (opaque references only).
2. **Same authority as the merge; AI / unauthorized refused (the relevant denial).** Reversal requires
   the SAME ``identity.merge`` scope AND a human user actor — a read-only viewer, a platform operator and
   an AI/automation (``service``) actor are each refused BEFORE any state is changed, so a denied reversal
   leaves the merge active and stages no audit entry.
3. **Not-reversible is refused.** An unknown / cross-tenant merge id reads as not-found, and an
   already-reversed merge is refused (nothing left to undo) — a double-reversal changes nothing.
4. **Consent is NEVER resurrected.** When a consent that existed at merge time is WITHDRAWN in the
   meantime, reversing the merge does not touch any ``ConsentReceipt`` / ``ProcessingRestriction``, so the
   withdrawn consent stays withdrawn — reversal moves identity state only.

Store logic is unit-tested on in-memory SQLite; the real transaction / RLS path is exercised on a
disposable PostgreSQL (``DCA_TEST_DATABASE_URL``) by the test at the end.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine

from app.audit.sink import ActorType, AuditActor
from app.db.base import Base, make_engine, make_session_factory
from app.db.models import (
    AuditEvent,
    ConsentReceipt,
    IdentityMerge,
    IdentityMergeStatus,
)
from app.identity.permissions import PLATFORM_OPERATOR_ROLE
from app.services.contact_point import ContactPointStore, PersonInput
from app.services.identity_merge import (
    AUDIT_ACTION_RECORD_MERGE,
    AUDIT_ACTION_REVERSE_MERGE,
    REASON_AI_ACTOR_CANNOT_MERGE,
    REASON_ALREADY_REVERSED,
    REASON_MERGE_NOT_FOUND,
    REASON_NOT_AUTHORIZED,
    IdentityMergeAuthorizationError,
    IdentityMergeError,
    IdentityMergeInput,
    IdentityMergeStore,
    IdentityReviewer,
)
from app.services.org_observation import ObservationInput, OrgObservationStore

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"
ACTOR = AuditActor(id="00000000-0000-4000-9000-0000000a0072", type=ActorType.USER)
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 10, 31, 13, 0, tzinfo=UTC)

# An AUTHORIZED human reviewer, a read-only viewer, a platform operator (no business scope), and an
# AI/automation actor (a non-human ``service`` actor — a suggestion can never execute a reversal).
REVIEWER = IdentityReviewer(subject="00000000-0000-4000-9000-0000000c0071", roles=("user",))
VIEWER = IdentityReviewer(subject="00000000-0000-4000-9000-0000000c0072", roles=("viewer",))
OPERATOR = IdentityReviewer(
    subject="00000000-0000-4000-9000-0000000c0073", roles=(PLATFORM_OPERATOR_ROLE,)
)
AI_ACTOR = IdentityReviewer(
    subject="00000000-0000-4000-9000-0000000c0074", roles=("user",), actor_type="service"
)


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _org(session, business_id: str, name: str) -> str:
    """Create one legal-entity organization observation (no identifiers), return its id."""
    org = OrgObservationStore(session, business_id).create(
        ObservationInput(entity_type="legal_entity", display_name=name), actor=ACTOR
    )
    return org.id


def _person(session, business_id: str, name: str) -> str:
    person = ContactPointStore(session, business_id).create_person(
        PersonInput(display_name=name), actor=ACTOR
    )
    return person.id


def _record_merge(
    session, business_id: str, primary: str, duplicate: str, subject_type="organization"
):
    """Record one reviewed-compatible merge (an ambiguous same-name REVIEW pair)."""
    return IdentityMergeStore(session, business_id).merge(
        IdentityMergeInput(
            subject_type=subject_type,
            primary_subject_id=primary,
            duplicate_subject_id=duplicate,
            reason="same_name_reviewed",
        ),
        reviewer=REVIEWER,
        now=NOW,
    )


def _active_pair_links(session, business_id: str, primary: str, duplicate: str) -> int:
    """How many ACTIVE merges link this directed pair in this business."""
    store = IdentityMergeStore(session, business_id)
    row = store.existing_for_pair("organization", primary, duplicate)
    return 1 if row is not None and row.status == IdentityMergeStatus.ACTIVE.value else 0


# --- 1) undo restores separate identity links + preserves history ---------------------------------


def test_reverse_flips_to_reversed_restores_separation_and_preserves_history(session_factory):
    """The acceptance case: an active merge is reversed — its status flips to ``reversed`` (the active
    link no longer stands, identities are separate again), the row and the original record audit entry
    are preserved, and a SEPARATE reverse audit entry is appended."""
    with session_factory() as session:
        primary = _org(session, BIZ_A, "Synthetic Textiles Pvt Ltd")
        duplicate = _org(session, BIZ_A, "Synthetic Textiles Pvt Ltd")
        merge = _record_merge(session, BIZ_A, primary, duplicate)
        session.commit()
        merge_id = merge.id

    # Before: exactly one ACTIVE link between the two identities.
    with session_factory() as session:
        assert _active_pair_links(session, BIZ_A, primary, duplicate) == 1

    with session_factory() as session:
        store = IdentityMergeStore(session, BIZ_A)
        reversed_row = store.reverse(merge_id, reviewer=REVIEWER, now=LATER)
        assert reversed_row.status == IdentityMergeStatus.REVERSED.value
        session.commit()

    with session_factory() as session:
        store = IdentityMergeStore(session, BIZ_A)
        row = store.get(merge_id)
        assert row is not None  # row preserved, NOT deleted
        assert row.status == IdentityMergeStatus.REVERSED.value
        # lineage intact: both subject refs, direction and the original reviewer survive the reversal
        assert row.primary_subject_id == primary
        assert row.duplicate_subject_id == duplicate
        assert row.reviewer_id == REVIEWER.subject
        # the identities are separate again — no ACTIVE link remains
        assert _active_pair_links(session, BIZ_A, primary, duplicate) == 0
        assert session.query(IdentityMerge).count() == 1  # still exactly one row

        # history preserved: the original record audit entry survives AND a reverse entry is appended.
        record_audits = (
            session.query(AuditEvent).filter(AuditEvent.action == AUDIT_ACTION_RECORD_MERGE).all()
        )
        reverse_audits = (
            session.query(AuditEvent).filter(AuditEvent.action == AUDIT_ACTION_REVERSE_MERGE).all()
        )
        assert len(record_audits) == 1
        assert len(reverse_audits) == 1
        refs = reverse_audits[0].references
        assert refs["identityMergeId"] == merge_id
        assert refs["previousStatus"] == IdentityMergeStatus.ACTIVE.value
        assert refs["newStatus"] == IdentityMergeStatus.REVERSED.value
        assert reverse_audits[0].actor_id == REVIEWER.subject


# --- 2) same authority; AI / unauthorized refused (the relevant denial) ---------------------------


@pytest.mark.parametrize(
    ("reviewer", "expected_code"),
    [
        (VIEWER, REASON_NOT_AUTHORIZED),
        (OPERATOR, REASON_NOT_AUTHORIZED),
        (AI_ACTOR, REASON_AI_ACTOR_CANNOT_MERGE),
    ],
    ids=["read_only_viewer", "platform_operator", "ai_automation_actor"],
)
def test_unauthorized_or_ai_actor_cannot_reverse_and_changes_no_state(
    session_factory, reviewer, expected_code
):
    """A read-only viewer, a platform operator (no business scope) and — explicitly — an AI/automation
    actor are each refused BEFORE any state is changed: the merge stays active and no reverse audit entry
    is staged."""
    with session_factory() as session:
        primary = _org(session, BIZ_A, "Synthetic Textiles Pvt Ltd")
        duplicate = _org(session, BIZ_A, "Synthetic Textiles Pvt Ltd")
        merge = _record_merge(session, BIZ_A, primary, duplicate)
        session.commit()
        merge_id = merge.id

    with session_factory() as session:
        store = IdentityMergeStore(session, BIZ_A)
        with pytest.raises(IdentityMergeAuthorizationError) as exc:
            store.reverse(merge_id, reviewer=reviewer, now=LATER)
        assert exc.value.code == expected_code
        session.rollback()

    with session_factory() as session:
        # Merge still ACTIVE; no reverse audit entry staged.
        assert IdentityMergeStore(session, BIZ_A).get(merge_id).status == (
            IdentityMergeStatus.ACTIVE.value
        )
        assert (
            session.query(AuditEvent)
            .filter(AuditEvent.action == AUDIT_ACTION_REVERSE_MERGE)
            .count()
            == 0
        )


# --- 3) not-reversible is refused -----------------------------------------------------------------


def test_unknown_or_cross_tenant_merge_id_reads_as_not_found(session_factory):
    """An unknown id — and another tenant's merge id — each read as not-found, so the reversal is refused
    and nothing changes."""
    with session_factory() as session:
        primary = _org(session, BIZ_A, "Synthetic Textiles Pvt Ltd")
        duplicate = _org(session, BIZ_A, "Synthetic Textiles Pvt Ltd")
        merge = _record_merge(session, BIZ_A, primary, duplicate)
        session.commit()
        merge_id = merge.id

    with session_factory() as session:
        store_a = IdentityMergeStore(session, BIZ_A)
        with pytest.raises(IdentityMergeError) as exc:
            store_a.reverse("00000000-0000-4000-9000-000000000bad", reviewer=REVIEWER, now=LATER)
        assert exc.value.code == REASON_MERGE_NOT_FOUND
        session.rollback()

    with session_factory() as session:
        # Business B cannot reverse A's merge: A's merge id resolves as not-found in B.
        store_b = IdentityMergeStore(session, BIZ_B)
        with pytest.raises(IdentityMergeError) as exc:
            store_b.reverse(merge_id, reviewer=REVIEWER, now=LATER)
        assert exc.value.code == REASON_MERGE_NOT_FOUND
        session.rollback()

    with session_factory() as session:
        assert IdentityMergeStore(session, BIZ_A).get(merge_id).status == (
            IdentityMergeStatus.ACTIVE.value
        )


def test_reversing_an_already_reversed_merge_is_refused_and_changes_nothing(session_factory):
    """A second reversal of the same merge is refused (there is nothing left to undo); the row stays
    reversed and exactly one reverse audit entry exists (the double-reversal staged none)."""
    with session_factory() as session:
        primary = _org(session, BIZ_A, "Synthetic Textiles Pvt Ltd")
        duplicate = _org(session, BIZ_A, "Synthetic Textiles Pvt Ltd")
        merge = _record_merge(session, BIZ_A, primary, duplicate)
        session.commit()
        merge_id = merge.id

    with session_factory() as session:
        IdentityMergeStore(session, BIZ_A).reverse(merge_id, reviewer=REVIEWER, now=LATER)
        session.commit()

    with session_factory() as session:
        store = IdentityMergeStore(session, BIZ_A)
        with pytest.raises(IdentityMergeError) as exc:
            store.reverse(merge_id, reviewer=REVIEWER, now=LATER)
        assert exc.value.code == REASON_ALREADY_REVERSED
        session.rollback()

    with session_factory() as session:
        assert IdentityMergeStore(session, BIZ_A).get(merge_id).status == (
            IdentityMergeStatus.REVERSED.value
        )
        assert (
            session.query(AuditEvent)
            .filter(AuditEvent.action == AUDIT_ACTION_REVERSE_MERGE)
            .count()
            == 1  # only the first reversal; the refused double-reversal staged nothing
        )


# --- 4) reversal never resurrects revoked consent -------------------------------------------------


def _seed_consent(
    session, subject_id: str, contact_point_id: str, action: str, at: datetime
) -> str:
    """Seed one consent receipt (grant or withdraw) for a subject directly (a fixture)."""
    receipt = ConsentReceipt(
        business_id=BIZ_A,
        brand_key="synthetic_brand",
        subject_type="person",
        subject_id=subject_id,
        contact_point_id=contact_point_id,
        channel="email",
        purpose="marketing_email",
        action=action,
        notice_version="notice-v1",
        form_version="form-v1",
        language="en",
        evidence_id=f"evidence-{action}-{subject_id}",
        collected_at=at,
    )
    session.add(receipt)
    return receipt.id


def test_reversal_does_not_resurrect_a_consent_withdrawn_after_the_merge(session_factory):
    """The CRITICAL consent guarantee. A person identity carries a GRANT at merge time; after the merge
    the subject WITHDRAWS consent (a later withdraw receipt). Reversing the merge must not touch consent
    state at all, so the withdrawal stays in place — no permission is re-granted or recombined."""
    with session_factory() as session:
        primary = _person(session, BIZ_A, "Anil Kumar Sharma")
        duplicate = _person(session, BIZ_A, "Anil Kumar Sharma")
        _seed_consent(session, primary, "cp-primary", action="grant", at=NOW)
        _seed_consent(session, duplicate, "cp-duplicate", action="grant", at=NOW)
        merge = _record_merge(session, BIZ_A, primary, duplicate, subject_type="person")
        session.commit()
        merge_id = merge.id

    # Between the merge and the reversal, the primary subject WITHDRAWS consent.
    with session_factory() as session:
        _seed_consent(session, primary, "cp-primary", action="withdraw", at=LATER)
        session.commit()

    with session_factory() as session:
        consent_before = session.query(ConsentReceipt).count()
        assert consent_before == 3  # two grants + one withdraw

    with session_factory() as session:
        IdentityMergeStore(session, BIZ_A).reverse(merge_id, reviewer=REVIEWER, now=LATER)
        session.commit()

    with session_factory() as session:
        # The reversal touched NO consent row: the count is unchanged and the withdraw receipt survives
        # intact — the revoked consent was NOT resurrected and nothing was re-granted.
        assert session.query(ConsentReceipt).count() == 3
        actions_for_primary = sorted(
            r.action
            for r in session.query(ConsentReceipt).filter(ConsentReceipt.subject_id == primary)
        )
        assert actions_for_primary == ["grant", "withdraw"]  # withdrawal still stands
        # the merge itself is reversed
        assert IdentityMergeStore(session, BIZ_A).get(merge_id).status == (
            IdentityMergeStatus.REVERSED.value
        )


# --- Postgres-backed: the real transaction / RLS path ---------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")


@pytest.mark.skipif(not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres")
def test_reverse_and_denial_over_postgres():
    """Exercise the real record → reverse → double-reverse path AND the relevant denials on a disposable
    PostgreSQL with real transactions — never SQLite for the durable path. An active merge is reversed
    (preserved as ``reversed``); an unauthorized caller and a second reversal are each refused and change
    nothing."""
    engine = create_engine(PG_URL, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        factory = make_session_factory(engine)
        with factory() as session:
            primary = _org(session, BIZ_A, "Synthetic Textiles Pvt Ltd")
            duplicate = _org(session, BIZ_A, "Synthetic Textiles Pvt Ltd")
            merge = _record_merge(session, BIZ_A, primary, duplicate)
            session.commit()
            merge_id = merge.id

        with factory() as session:
            # A viewer is refused — the merge stays active.
            store = IdentityMergeStore(session, BIZ_A)
            with pytest.raises(IdentityMergeAuthorizationError):
                store.reverse(merge_id, reviewer=VIEWER, now=LATER)
            session.rollback()

        with factory() as session:
            IdentityMergeStore(session, BIZ_A).reverse(merge_id, reviewer=REVIEWER, now=LATER)
            session.commit()

        with factory() as session:
            store = IdentityMergeStore(session, BIZ_A)
            assert store.get(merge_id).status == IdentityMergeStatus.REVERSED.value
            with pytest.raises(IdentityMergeError) as exc:
                store.reverse(merge_id, reviewer=REVIEWER, now=LATER)
            assert exc.value.code == REASON_ALREADY_REVERSED
            session.rollback()

        with factory() as session:
            assert session.query(IdentityMerge).count() == 1
    finally:
        Base.metadata.drop_all(engine)
