"""DCA-071 — approve an ambiguous identity merge.

Pins the load-bearing behaviours of the review-only merge store
(:mod:`app.services.identity_merge`), which records that two of a business's observed identities are the
SAME entity on top of a reviewed COMPATIBLE matcher conclusion — the human ACTION that sits on top of
the pure DCA-067/068 conclusions (which merge, transfer and record nothing):

1. **Only reviewed COMPATIBLE identities merge.** An ambiguous ``REVIEW`` pair (a shared company name /
   a common person name) is approved and recorded, snapshotting the matcher conclusion/evidence. A
   ``NO_MATCH`` pair (a CONFLICTING typed registration identifier — a shared name can never override it)
   and an ``INSUFFICIENT`` pair (no linking evidence at all) are each refused BEFORE the session is
   mutated, so an incompatible merge stages nothing.
2. **Unrelated consent scopes remain SEPARATE.** Merging two person identities records the identity link
   only; it creates/modifies/transfers NO ``ConsentReceipt`` or ``ProcessingRestriction`` — each
   subject's permission state is exactly as it was, and no consent is combined onto the surviving
   identity.
3. **Authorized by a HUMAN reviewer; an AI suggestion can never execute a merge (the relevant denial).**
   Only a business user holding :attr:`~app.identity.permissions.Scope.IDENTITY_MERGE` may record a
   merge; a read-only viewer, a platform operator and — explicitly — an AI/automation (``service``)
   actor are refused before any state is touched, so a denial stages no row and no audit entry.
4. **Business-private, tenant-scoped, idempotent.** A cross-tenant subject reads as not-found, a by-id
   read of another business's merge is not-found, and re-approving an already-merged pair returns the
   existing row (no duplicate, no second audit).

Within ONE business two distinct observations can never share a strong typed identifier or a personal
contact value (those are unique per business), so a same-business merge is inherently the AMBIGUOUS
``REVIEW`` case — exactly what this story approves. The store logic is unit-tested on in-memory SQLite;
the real transaction / RLS path is exercised on a disposable PostgreSQL (``DCA_TEST_DATABASE_URL``) by
the test at the end. The RLS/migration paths for the table are covered by ``tests/test_rls.py`` and
``tests/test_migrations.py``.
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
    ProcessingRestriction,
)
from app.identity.permissions import PLATFORM_OPERATOR_ROLE
from app.services.contact_point import ContactPointStore, PersonInput
from app.services.identity_merge import (
    AUDIT_ACTION_RECORD_MERGE,
    REASON_AI_ACTOR_CANNOT_MERGE,
    REASON_INCOMPATIBLE_IDENTITIES,
    REASON_NOT_AUTHORIZED,
    REASON_SELF_MERGE,
    REASON_SUBJECT_NOT_FOUND,
    IdentityMergeAuthorizationError,
    IdentityMergeError,
    IdentityMergeInput,
    IdentityMergeStore,
    IdentityReviewer,
)
from app.services.org_observation import ExternalIdInput, ObservationInput, OrgObservationStore

BIZ_A = "11111111-1111-1111-1111-111111111111"
BIZ_B = "22222222-2222-2222-2222-222222222222"
ACTOR = AuditActor(id="00000000-0000-4000-9000-0000000a0071", type=ActorType.USER)
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)

# Two valid, DISTINCT legal-entity CINs (DCA-065 shape) — a conflicting pair proves NO_MATCH.
CIN_ONE = "U72900MH2010PTC123456"
CIN_TWO = "U72900KA2011PTC654321"

# An AUTHORIZED human reviewer, a read-only viewer, a platform operator (no business scope), and an
# AI/automation actor (a non-human ``service`` actor — a suggestion can never execute a merge).
REVIEWER = IdentityReviewer(subject="00000000-0000-4000-9000-0000000c0071", roles=("user",))
VIEWER = IdentityReviewer(subject="00000000-0000-4000-9000-0000000c0072", roles=("viewer",))
OPERATOR = IdentityReviewer(
    subject="00000000-0000-4000-9000-0000000c0073", roles=(PLATFORM_OPERATOR_ROLE,)
)
# An AI actor given a human role on purpose: it is STILL refused because it is not a human user actor.
AI_ACTOR = IdentityReviewer(
    subject="00000000-0000-4000-9000-0000000c0074", roles=("user",), actor_type="service"
)


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _org(session, business_id: str, name: str, *, cin: str | None = None) -> str:
    """Create one legal-entity organization observation (optionally with a CIN), return its id."""
    external_ids = [ExternalIdInput(scheme="cin", value=cin)] if cin else []
    org = OrgObservationStore(session, business_id).create(
        ObservationInput(entity_type="legal_entity", display_name=name, external_ids=external_ids),
        actor=ACTOR,
    )
    return org.id


def _person(session, business_id: str, name: str) -> str:
    """Create one person observation (no contact points), return its id."""
    person = ContactPointStore(session, business_id).create_person(
        PersonInput(display_name=name), actor=ACTOR
    )
    return person.id


# --- 1) only reviewed compatible identities merge -------------------------------------------------


def test_ambiguous_review_pair_is_merged_and_records_the_evidence(session_factory):
    """The acceptance case: two same-named legal entities with no identifiers are an ambiguous REVIEW
    candidate; an authorized human reviewer approves the merge, which is recorded ACTIVE with the
    matcher conclusion/evidence snapshotted and an atomic audit entry — nothing else is touched."""
    with session_factory() as session:
        primary = _org(session, BIZ_A, "Synthetic Textiles Pvt Ltd")
        duplicate = _org(session, BIZ_A, "Synthetic Textiles Pvt Ltd")
        session.commit()

    with session_factory() as session:
        store = IdentityMergeStore(session, BIZ_A)
        # The pure evaluation is an ambiguous REVIEW candidate (shared name, no strong identifier).
        evidence = store.evaluate("organization", primary, duplicate)
        assert evidence.outcome == "review"
        assert evidence.compatible is True

        merge = store.merge(
            IdentityMergeInput(
                subject_type="organization",
                primary_subject_id=primary,
                duplicate_subject_id=duplicate,
                reason="same_name_reviewed",
            ),
            reviewer=REVIEWER,
            now=NOW,
        )
        session.commit()
        merge_id = merge.id

    with session_factory() as session:
        store = IdentityMergeStore(session, BIZ_A)
        row = store.get(merge_id)
        assert row is not None
        assert row.status == IdentityMergeStatus.ACTIVE.value
        assert row.match_outcome == "review"
        assert row.primary_subject_id == primary
        assert row.duplicate_subject_id == duplicate
        assert row.reviewer_id == REVIEWER.subject
        assert len(row.evidence) >= 1  # the name signal was snapshotted
        assert session.query(IdentityMerge).count() == 1


def test_conflicting_identifier_pair_is_refused_and_stages_nothing(session_factory):
    """A NO_MATCH pair — two legal entities with the SAME name but DIFFERENT CINs — is incompatible: a
    conflicting typed registration identifier refutes identity and a shared name can never override it.
    The merge is refused before the session is mutated and stages no row."""
    with session_factory() as session:
        primary = _org(session, BIZ_A, "Acme Industries Ltd", cin=CIN_ONE)
        duplicate = _org(session, BIZ_A, "Acme Industries Ltd", cin=CIN_TWO)
        session.commit()

    with session_factory() as session:
        store = IdentityMergeStore(session, BIZ_A)
        assert store.evaluate("organization", primary, duplicate).outcome == "no_match"
        with pytest.raises(IdentityMergeError) as exc:
            store.merge(
                IdentityMergeInput(
                    subject_type="organization",
                    primary_subject_id=primary,
                    duplicate_subject_id=duplicate,
                ),
                reviewer=REVIEWER,
                now=NOW,
            )
        assert exc.value.code == REASON_INCOMPATIBLE_IDENTITIES
        session.rollback()

    with session_factory() as session:
        assert session.query(IdentityMerge).count() == 0


def test_insufficient_evidence_pair_is_refused(session_factory):
    """Two unrelated legal entities (different names, no identifiers) overlap on NOTHING — INSUFFICIENT
    — so there is nothing to review-approve and the merge is refused with no state change."""
    with session_factory() as session:
        primary = _org(session, BIZ_A, "Blue Ocean Foods Pvt Ltd")
        duplicate = _org(session, BIZ_A, "Red Mountain Steel Pvt Ltd")
        session.commit()

    with session_factory() as session:
        store = IdentityMergeStore(session, BIZ_A)
        assert store.evaluate("organization", primary, duplicate).outcome == "insufficient"
        with pytest.raises(IdentityMergeError) as exc:
            store.merge(
                IdentityMergeInput(
                    subject_type="organization",
                    primary_subject_id=primary,
                    duplicate_subject_id=duplicate,
                ),
                reviewer=REVIEWER,
                now=NOW,
            )
        assert exc.value.code == REASON_INCOMPATIBLE_IDENTITIES
        session.rollback()
    with session_factory() as session:
        assert session.query(IdentityMerge).count() == 0


def test_self_merge_is_refused(session_factory):
    with session_factory() as session:
        subject = _org(session, BIZ_A, "Synthetic Textiles Pvt Ltd")
        session.commit()
    with session_factory() as session:
        store = IdentityMergeStore(session, BIZ_A)
        with pytest.raises(IdentityMergeError) as exc:
            store.merge(
                IdentityMergeInput(
                    subject_type="organization",
                    primary_subject_id=subject,
                    duplicate_subject_id=subject,
                ),
                reviewer=REVIEWER,
                now=NOW,
            )
        assert exc.value.code == REASON_SELF_MERGE
        session.rollback()
    with session_factory() as session:
        assert session.query(IdentityMerge).count() == 0


# --- 2) unrelated consent scopes remain separate --------------------------------------------------


def _seed_consent(session, business_id: str, subject_id: str, contact_point_id: str) -> str:
    """Seed one evidenced consent receipt for a subject directly (a fixture, not the DCA-074 store)."""
    receipt = ConsentReceipt(
        business_id=business_id,
        brand_key="synthetic_brand",
        subject_type="person",
        subject_id=subject_id,
        contact_point_id=contact_point_id,
        channel="email",
        purpose="marketing_email",
        action="grant",
        notice_version="notice-v1",
        form_version="form-v1",
        language="en",
        evidence_id="evidence-" + subject_id,
        collected_at=NOW,
    )
    session.add(receipt)
    return receipt.id


def test_merging_identities_does_not_combine_or_transfer_consent(session_factory):
    """Merge two ambiguous (same-name) person identities and prove the acceptance: the identity link is
    recorded, but each subject's consent receipt stays attached to its OWN subject (nothing transferred
    to the primary), no consent receipt is created or deleted, and the processing restriction is
    untouched — unrelated consent scopes remain SEPARATE."""
    with session_factory() as session:
        primary = _person(session, BIZ_A, "Anil Kumar Sharma")
        duplicate = _person(session, BIZ_A, "Anil Kumar Sharma")
        # A consent receipt per person, plus one unrelated processing restriction.
        _seed_consent(session, BIZ_A, primary, "cp-primary")
        _seed_consent(session, BIZ_A, duplicate, "cp-duplicate")
        session.add(
            ProcessingRestriction(
                business_id=BIZ_A,
                scope_channel="email",
                scope_purpose="marketing_email",
                version=1,
                authorized_by=REVIEWER.subject,
            )
        )
        session.commit()

    with session_factory() as session:
        before_receipts = session.query(ConsentReceipt).count()
        before_restrictions = session.query(ProcessingRestriction).count()

        store = IdentityMergeStore(session, BIZ_A)
        assert store.evaluate("person", primary, duplicate).outcome == "review"
        store.merge(
            IdentityMergeInput(
                subject_type="person",
                primary_subject_id=primary,
                duplicate_subject_id=duplicate,
            ),
            reviewer=REVIEWER,
            now=NOW,
        )
        session.commit()

    with session_factory() as session:
        # The merge was recorded...
        assert session.query(IdentityMerge).count() == 1
        # ...but consent is NEITHER combined NOR transferred: counts unchanged, and each receipt still
        # belongs to its ORIGINAL subject (none repointed to the surviving/primary identity).
        assert session.query(ConsentReceipt).count() == before_receipts == 2
        assert session.query(ProcessingRestriction).count() == before_restrictions == 1
        subjects = {r.subject_id for r in session.query(ConsentReceipt).all()}
        assert subjects == {
            primary,
            duplicate,
        }  # both kept separate; nothing collapsed onto primary


# --- 3) authorized by a human; AI/unauthorized refused (the relevant denial) ----------------------


@pytest.mark.parametrize(
    ("reviewer", "expected_code"),
    [
        (VIEWER, REASON_NOT_AUTHORIZED),
        (OPERATOR, REASON_NOT_AUTHORIZED),
        (AI_ACTOR, REASON_AI_ACTOR_CANNOT_MERGE),
    ],
    ids=["read_only_viewer", "platform_operator", "ai_automation_actor"],
)
def test_unauthorized_or_ai_actor_cannot_merge_and_changes_no_state(
    session_factory, reviewer, expected_code
):
    """A read-only viewer and a platform operator (no business scope), and — explicitly — an AI/
    automation actor (even one carrying a human role), are each refused BEFORE any state is read or
    written: the denial stages no merge row and no audit entry."""
    with session_factory() as session:
        primary = _org(session, BIZ_A, "Synthetic Textiles Pvt Ltd")
        duplicate = _org(session, BIZ_A, "Synthetic Textiles Pvt Ltd")
        session.commit()

    with session_factory() as session:
        store = IdentityMergeStore(session, BIZ_A)
        with pytest.raises(IdentityMergeAuthorizationError) as exc:
            store.merge(
                IdentityMergeInput(
                    subject_type="organization",
                    primary_subject_id=primary,
                    duplicate_subject_id=duplicate,
                ),
                reviewer=reviewer,
                now=NOW,
            )
        assert exc.value.code == expected_code
        session.rollback()

    with session_factory() as session:
        assert session.query(IdentityMerge).count() == 0
        assert (
            session.query(AuditEvent).filter(AuditEvent.action == AUDIT_ACTION_RECORD_MERGE).count()
            == 0
        )


def test_authority_is_checked_before_compatibility(session_factory):
    """Even for an INCOMPATIBLE pair, an AI actor is refused with the AI-actor code — authority is the
    FIRST gate, so nothing is read or evaluated."""
    with session_factory() as session:
        primary = _org(session, BIZ_A, "Acme Industries Ltd", cin=CIN_ONE)
        duplicate = _org(session, BIZ_A, "Acme Industries Ltd", cin=CIN_TWO)
        session.commit()

    with session_factory() as session:
        store = IdentityMergeStore(session, BIZ_A)
        with pytest.raises(IdentityMergeAuthorizationError) as exc:
            store.merge(
                IdentityMergeInput(
                    subject_type="organization",
                    primary_subject_id=primary,
                    duplicate_subject_id=duplicate,
                ),
                reviewer=AI_ACTOR,
                now=NOW,
            )
        assert exc.value.code == REASON_AI_ACTOR_CANNOT_MERGE


def test_merge_is_atomically_audited_with_opaque_references_only(session_factory):
    with session_factory() as session:
        primary = _org(session, BIZ_A, "Synthetic Textiles Pvt Ltd")
        duplicate = _org(session, BIZ_A, "Synthetic Textiles Pvt Ltd")
        merge = IdentityMergeStore(session, BIZ_A).merge(
            IdentityMergeInput(
                subject_type="organization",
                primary_subject_id=primary,
                duplicate_subject_id=duplicate,
            ),
            reviewer=REVIEWER,
            now=NOW,
        )
        session.commit()
        merge_id = merge.id

    with session_factory() as session:
        audits = (
            session.query(AuditEvent).filter(AuditEvent.action == AUDIT_ACTION_RECORD_MERGE).all()
        )
        assert len(audits) == 1
        refs = audits[0].references
        assert refs["identityMergeId"] == merge_id
        assert refs["subjectType"] == "organization"
        assert refs["matchOutcome"] == "review"
        assert refs["signalCount"] >= 1
        assert audits[0].actor_id == REVIEWER.subject


# --- 4) business-private, tenant-scoped, idempotent -----------------------------------------------


def test_cross_tenant_subjects_read_as_not_found(session_factory):
    """Business B cannot merge Business A's subjects: A's ids resolve as not-found in B, so the merge is
    refused before anything is written and stages nothing."""
    with session_factory() as session:
        primary_a = _org(session, BIZ_A, "Synthetic Textiles Pvt Ltd")
        duplicate_a = _org(session, BIZ_A, "Synthetic Textiles Pvt Ltd")
        session.commit()

    with session_factory() as session:
        store_b = IdentityMergeStore(session, BIZ_B)
        with pytest.raises(IdentityMergeError) as exc:
            store_b.merge(
                IdentityMergeInput(
                    subject_type="organization",
                    primary_subject_id=primary_a,  # owned by A
                    duplicate_subject_id=duplicate_a,  # owned by A
                ),
                reviewer=REVIEWER,
                now=NOW,
            )
        assert exc.value.code == REASON_SUBJECT_NOT_FOUND
        session.rollback()
    with session_factory() as session:
        assert session.query(IdentityMerge).count() == 0


def test_cross_tenant_merge_id_reads_as_not_found(session_factory):
    with session_factory() as session:
        primary = _org(session, BIZ_A, "Synthetic Textiles Pvt Ltd")
        duplicate = _org(session, BIZ_A, "Synthetic Textiles Pvt Ltd")
        merge = IdentityMergeStore(session, BIZ_A).merge(
            IdentityMergeInput(
                subject_type="organization",
                primary_subject_id=primary,
                duplicate_subject_id=duplicate,
            ),
            reviewer=REVIEWER,
            now=NOW,
        )
        session.commit()
        merge_id = merge.id

    with session_factory() as session:
        assert IdentityMergeStore(session, BIZ_B).get(merge_id) is None


def test_re_approving_a_pair_is_idempotent(session_factory):
    """Re-approving the SAME pair (even in the reverse direction) returns the existing merge — no
    duplicate row and no second audit entry."""
    with session_factory() as session:
        primary = _org(session, BIZ_A, "Synthetic Textiles Pvt Ltd")
        duplicate = _org(session, BIZ_A, "Synthetic Textiles Pvt Ltd")
        session.commit()

    with session_factory() as session:
        store = IdentityMergeStore(session, BIZ_A)
        first = store.merge(
            IdentityMergeInput(
                subject_type="organization",
                primary_subject_id=primary,
                duplicate_subject_id=duplicate,
            ),
            reviewer=REVIEWER,
            now=NOW,
        )
        session.commit()
        first_id = first.id

    with session_factory() as session:
        store = IdentityMergeStore(session, BIZ_A)
        # Re-approve, swapping the direction — the store returns the already-recorded merge.
        again = store.merge(
            IdentityMergeInput(
                subject_type="organization",
                primary_subject_id=duplicate,
                duplicate_subject_id=primary,
            ),
            reviewer=REVIEWER,
            now=NOW,
        )
        session.commit()
        assert again.id == first_id

    with session_factory() as session:
        assert session.query(IdentityMerge).count() == 1
        assert (
            session.query(AuditEvent).filter(AuditEvent.action == AUDIT_ACTION_RECORD_MERGE).count()
            == 1
        )


# --- Postgres-backed: the real transaction / RLS path ---------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")


@pytest.mark.skipif(not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres")
def test_merge_and_denial_over_postgres():
    """Exercise the real record → read → idempotency path AND the relevant denials on a disposable
    PostgreSQL with real transactions — never SQLite for the durable path. A reviewed compatible merge
    persists; an unauthorized caller and an incompatible pair are each refused and stage nothing."""
    engine = create_engine(PG_URL, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        factory = make_session_factory(engine)
        with factory() as session:
            primary = _org(session, BIZ_A, "Synthetic Textiles Pvt Ltd")
            duplicate = _org(session, BIZ_A, "Synthetic Textiles Pvt Ltd")
            IdentityMergeStore(session, BIZ_A).merge(
                IdentityMergeInput(
                    subject_type="organization",
                    primary_subject_id=primary,
                    duplicate_subject_id=duplicate,
                ),
                reviewer=REVIEWER,
                now=NOW,
            )
            session.commit()

        with factory() as session:
            # A viewer is refused; an incompatible (conflicting-CIN) pair is refused — nothing new.
            store = IdentityMergeStore(session, BIZ_A)
            with pytest.raises(IdentityMergeAuthorizationError):
                store.merge(
                    IdentityMergeInput(
                        subject_type="organization",
                        primary_subject_id=primary,
                        duplicate_subject_id=duplicate,
                    ),
                    reviewer=VIEWER,
                    now=NOW,
                )
            session.rollback()

        with factory() as session:
            assert session.query(IdentityMerge).count() == 1
    finally:
        Base.metadata.drop_all(engine)
