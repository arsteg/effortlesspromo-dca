"""DCA-031 — revision-aware projection processing.

Two layers, matching the DCA-027/028 sink suites:

- **Always-on unit tests** (in-memory SQLite) pin the decision logic: the first revision applies; an
  OLDER/duplicate revision is rejected STALE with no overwrite; a revision ahead of a missing
  predecessor is buffered as a visible GAP (never applied over the gap); the gap reconciles in order
  once the missing revision arrives; non-opaque identity/state is refused with no row staged; and the
  projector stages (never commits) into the caller's session.
- **Locking / transactional behaviour on a disposable PostgreSQL** (``DCA_TEST_DATABASE_URL``) proves
  the headline under concurrency: two interleaved deliveries for the same aggregate serialize on a
  ``FOR UPDATE`` row lock, so a delayed revision cannot overwrite the newer projection or lose an
  update — the final state is the highest contiguous revision regardless of arrival order. SQLite
  cannot prove row-level locking, so that layer skips without a Postgres URL.

Spin up a throwaway Postgres, e.g.:
    docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55476:5432 postgres:16-alpine
    export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55476/dca"
"""

from __future__ import annotations

import os
import threading

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker

from app.db.base import Base, make_engine, make_session_factory
from app.db.models import RevisionPendingEvent, RevisionProjection, RevisionStatus
from app.identity.tenant_context import TenantContext
from app.projection import RevisionDecision, RevisionPayloadError, RevisionProjector

A = "00000000-0000-4000-8000-00000000000a"
B = "00000000-0000-4000-8000-00000000000b"
SUBJECT = "00000000-0000-4000-9000-0000000a11ce"
TYPE = "consent"
AID = "aggregate-1"


def _ctx(business_id: str) -> TenantContext:
    return TenantContext(
        subject=SUBJECT, business_id=business_id, roles=("owner",), membership_version="v1"
    )


# --------------------------------------------------------------------------------------------------
# Always-on unit tests (in-memory SQLite) — the decision logic
# --------------------------------------------------------------------------------------------------


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def _apply(factory, business_id, version, state=None, *, event_id="", atype=TYPE, aid=AID):
    """One apply in its own committed unit of work; returns the outcome."""
    with factory() as session:
        outcome = RevisionProjector.for_context(session, _ctx(business_id)).apply(
            aggregate_type=atype,
            aggregate_id=aid,
            version=version,
            state=state,
            event_id=event_id,
        )
        session.commit()
    return outcome


def _projection(factory, business_id, *, atype=TYPE, aid=AID):
    with factory() as session:
        return RevisionProjector.for_context(session, _ctx(business_id)).projection(
            aggregate_type=atype, aggregate_id=aid
        )


def test_first_revision_applies_and_projects_state(session_factory):
    f = session_factory
    out = _apply(f, A, 1, {"status": "granted"}, event_id="evt-1")
    assert out.decision is RevisionDecision.APPLIED
    assert out.applied and out.applied_version == 1
    proj = _projection(f, A)
    assert proj is not None
    assert proj.applied_version == 1
    assert proj.state == {"status": "granted"}
    assert proj.status == RevisionStatus.CURRENT.value
    assert proj.last_event_id == "evt-1"


def test_contiguous_revisions_advance_in_order(session_factory):
    f = session_factory
    _apply(f, A, 1, {"v": 1})
    out = _apply(f, A, 2, {"v": 2})
    assert out.applied and out.applied_version == 2
    out = _apply(f, A, 3, {"v": 3})
    assert out.applied and out.applied_version == 3
    proj = _projection(f, A)
    assert proj.applied_version == 3
    assert proj.state == {"v": 3}


def test_stale_revision_cannot_overwrite_the_current_one(session_factory):
    """The headline: apply revision 3, then a delayed revision 2 — state is unchanged."""
    f = session_factory
    _apply(f, A, 1, {"v": 1})
    _apply(f, A, 2, {"v": 2})
    _apply(f, A, 3, {"v": 3, "consent": "granted"})

    out = _apply(f, A, 2, {"v": 2, "consent": "withdrawn"})  # a delayed older revision
    assert out.decision is RevisionDecision.STALE
    assert out.stale and out.applied_version == 3

    proj = _projection(f, A)
    assert proj.applied_version == 3
    assert proj.state == {"v": 3, "consent": "granted"}  # NOT overwritten by the stale revision
    assert proj.status == RevisionStatus.CURRENT.value


def test_duplicate_of_the_current_revision_is_stale_noop(session_factory):
    f = session_factory
    _apply(f, A, 1, {"v": 1})
    out = _apply(f, A, 1, {"v": "tampered"})  # same revision re-delivered
    assert out.stale
    assert _projection(f, A).state == {"v": 1}  # unchanged


def test_gap_is_buffered_visible_and_not_applied(session_factory):
    """A revision ahead of a missing predecessor is buffered, not applied over the gap."""
    f = session_factory
    _apply(f, A, 1, {"v": 1})
    out = _apply(f, A, 3, {"v": 3})  # revision 2 is missing
    assert out.decision is RevisionDecision.GAP
    assert out.gap and out.missing == (2,)

    proj = _projection(f, A)
    assert proj.applied_version == 1  # NOT advanced — the gap is not guessed
    assert proj.state == {"v": 1}
    assert proj.status == RevisionStatus.GAPPED.value

    with f() as session:
        projector = RevisionProjector.for_context(session, _ctx(A))
        pend = projector.pending(aggregate_type=TYPE, aggregate_id=AID)
        assert [p.aggregate_version for p in pend] == [3]  # buffered, visible
        assert projector.gaps(aggregate_type=TYPE, aggregate_id=AID) == [2]  # computed, not guessed


def test_gap_reconciles_in_order_when_the_missing_revision_arrives(session_factory):
    f = session_factory
    _apply(f, A, 1, {"v": 1})
    _apply(f, A, 4, {"v": 4})  # gap: 2,3 missing -> buffered
    _apply(f, A, 3, {"v": 3})  # still a gap: 2 missing -> buffered (3 and 4 now pending)

    with f() as session:
        projector = RevisionProjector.for_context(session, _ctx(A))
        assert projector.gaps(aggregate_type=TYPE, aggregate_id=AID) == [2]
        assert [
            p.aggregate_version for p in projector.pending(aggregate_type=TYPE, aggregate_id=AID)
        ] == [
            3,
            4,
        ]

    out = _apply(f, A, 2, {"v": 2})  # fills the gap -> drains 3 then 4
    assert out.applied
    assert out.applied_version == 4
    assert out.reconciled == (3, 4)

    proj = _projection(f, A)
    assert proj.applied_version == 4
    assert proj.state == {"v": 4}  # the latest reconciled revision's state
    assert proj.status == RevisionStatus.CURRENT.value
    with f() as session:
        assert (
            RevisionProjector.for_context(session, _ctx(A)).pending(
                aggregate_type=TYPE, aggregate_id=AID
            )
            == []
        )  # buffer drained


def test_first_event_ahead_of_revision_one_is_a_gap_from_the_start(session_factory):
    f = session_factory
    out = _apply(f, A, 3, {"v": 3})  # never saw 1 or 2
    assert out.gap and out.missing == (1, 2)
    proj = _projection(f, A)
    assert proj.applied_version == 0  # nothing applied
    assert proj.state == {}
    assert proj.status == RevisionStatus.GAPPED.value

    _apply(f, A, 1, {"v": 1})  # fills 1 -> can it reconcile 3? no, 2 still missing
    proj = _projection(f, A)
    assert proj.applied_version == 1
    assert proj.state == {"v": 1}
    assert proj.status == RevisionStatus.GAPPED.value  # 3 still buffered, 2 still missing

    _apply(f, A, 2, {"v": 2})  # fills 2 -> drains 3
    proj = _projection(f, A)
    assert proj.applied_version == 3
    assert proj.state == {"v": 3}
    assert proj.status == RevisionStatus.CURRENT.value


def test_re_delivery_of_a_buffered_revision_is_not_double_buffered(session_factory):
    f = session_factory
    _apply(f, A, 1, {"v": 1})
    _apply(f, A, 3, {"v": 3})
    _apply(f, A, 3, {"v": 3})  # same out-of-order revision again
    with f() as session:
        pend = RevisionProjector.for_context(session, _ctx(A)).pending(
            aggregate_type=TYPE, aggregate_id=AID
        )
        assert [p.aggregate_version for p in pend] == [3]  # still exactly one buffered row


def test_projections_are_tenant_isolated(session_factory):
    f = session_factory
    _apply(f, A, 1, {"owner": "A"})
    _apply(f, B, 1, {"owner": "B"})
    assert _projection(f, A).state == {"owner": "A"}
    assert _projection(f, B).state == {"owner": "B"}
    # B's projector cannot see A's aggregate even by the same key.
    with f() as session:
        projector_b = RevisionProjector.for_context(session, _ctx(B))
        assert projector_b.projection(aggregate_type=TYPE, aggregate_id=AID).state == {"owner": "B"}


def test_apply_stages_without_committing(session_factory):
    f = session_factory
    session = f()
    try:
        out = RevisionProjector.for_context(session, _ctx(A)).apply(
            aggregate_type=TYPE, aggregate_id=AID, version=1, state={"v": 1}
        )
        assert out.applied
        # Not yet persisted: a second session sees nothing until the caller commits.
        with f() as other:
            assert other.scalars(select(RevisionProjection)).all() == []
        session.commit()
    finally:
        session.close()
    with f() as check:
        assert len(check.scalars(select(RevisionProjection)).all()) == 1


@pytest.mark.parametrize(
    "state",
    [
        {"owner": "founder@acme.co.in"},  # email by content
        {"note": "call +91 98765 43210"},  # phone by content
        {"email": "x"},  # sensitive key
        {"contact": {"phone": "x"}},  # nested sensitive key
    ],
)
def test_apply_refuses_raw_contact_or_secret_state(session_factory, state):
    f = session_factory
    session = f()
    try:
        projector = RevisionProjector.for_context(session, _ctx(A))
        with pytest.raises(RevisionPayloadError):
            projector.apply(aggregate_type=TYPE, aggregate_id=AID, version=1, state=state)
        assert list(session.new) == []  # refused before staging
        session.commit()
    finally:
        session.close()
    with f() as check:
        assert check.scalars(select(RevisionProjection)).all() == []


def test_apply_refuses_contact_data_in_identity(session_factory):
    f = session_factory
    session = f()
    try:
        projector = RevisionProjector.for_context(session, _ctx(A))
        with pytest.raises(RevisionPayloadError):
            projector.apply(aggregate_type="notify founder@acme.co.in", aggregate_id=AID, version=1)
        with pytest.raises(RevisionPayloadError):
            projector.apply(aggregate_type=TYPE, aggregate_id="founder@acme.co.in", version=1)
        assert list(session.new) == []
    finally:
        session.close()


def test_apply_requires_scope_type_id_and_positive_version(session_factory):
    f = session_factory
    session = f()
    try:
        with pytest.raises(ValueError):
            RevisionProjector(session).apply(aggregate_type=TYPE, aggregate_id=AID, version=1)
        with pytest.raises(ValueError):
            RevisionProjector(session, business_id=A).apply(
                aggregate_type="  ", aggregate_id=AID, version=1
            )
        with pytest.raises(ValueError):
            RevisionProjector(session, business_id=A).apply(
                aggregate_type=TYPE, aggregate_id="  ", version=1
            )
        for bad_version in (0, -1, True):
            with pytest.raises(ValueError):
                RevisionProjector(session, business_id=A).apply(
                    aggregate_type=TYPE, aggregate_id=AID, version=bad_version
                )
        assert list(session.new) == []
    finally:
        session.close()


def test_stale_apply_makes_no_state_change(session_factory):
    """Belt-and-braces on the acceptance 'relevant denial without unintended state changes'."""
    f = session_factory
    _apply(f, A, 1, {"v": 1})
    _apply(f, A, 2, {"v": 2})
    session = f()
    try:
        before = session.scalars(select(RevisionPendingEvent)).all()
        out = RevisionProjector.for_context(session, _ctx(A)).apply(
            aggregate_type=TYPE, aggregate_id=AID, version=1, state={"v": "x"}
        )
        assert out.stale
        assert list(session.new) == [] and list(session.dirty) == []  # nothing staged or mutated
        assert session.scalars(select(RevisionPendingEvent)).all() == before
    finally:
        session.close()


# --------------------------------------------------------------------------------------------------
# Locking / concurrency on a disposable PostgreSQL — the headline under interleaving
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)
SCHEMA = "dca_revision_test"


@pytest.fixture
def pg_factory():
    """A threaded sync session factory over a fresh schema on the disposable Postgres (superuser —
    this layer exercises FOR UPDATE row-lock serialization, which the RLS tests cover separately)."""
    admin = create_engine(PG_URL)
    with admin.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE'))
        conn.execute(text(f'CREATE SCHEMA "{SCHEMA}"'))
    admin.dispose()

    engine = create_engine(PG_URL)

    @event.listens_for(engine, "connect")
    def _pin_search_path(dbapi_conn, _record):  # pragma: no cover - trivial connect hook
        cur = dbapi_conn.cursor()
        cur.execute(f'SET search_path TO "{SCHEMA}"')
        cur.close()

    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        engine.dispose()
        admin = create_engine(PG_URL)
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE'))
        admin.dispose()


@requires_pg
def test_stale_revision_rejected_on_real_postgres(pg_factory):
    """Apply revision 3 then revision 2 sequentially on real Postgres — state is unchanged."""
    factory = pg_factory
    for v in (1, 2, 3):
        with factory() as session:
            RevisionProjector.for_context(session, _ctx(A)).apply(
                aggregate_type=TYPE, aggregate_id=AID, version=v, state={"v": v}
            )
            session.commit()

    with factory() as session:
        out = RevisionProjector.for_context(session, _ctx(A)).apply(
            aggregate_type=TYPE, aggregate_id=AID, version=2, state={"v": "stale"}
        )
        session.commit()
    assert out.stale

    with factory() as check:
        proj = check.scalars(select(RevisionProjection)).one()
        assert proj.applied_version == 3
        assert proj.state == {"v": 3}


@requires_pg
def test_concurrent_deliveries_serialize_on_the_row_lock(pg_factory):
    """Two interleaved deliveries (revision 2 and revision 3) for one aggregate race under a
    ``FOR UPDATE`` lock. Whatever the interleaving, no update is lost and no stale revision wins:
    the final projection is the highest CONTIGUOUS revision (3) with revision 3's state."""
    factory = pg_factory

    # Seed revision 1 so both racers lock the SAME existing projection row.
    with factory() as session:
        RevisionProjector.for_context(session, _ctx(A)).apply(
            aggregate_type=TYPE, aggregate_id=AID, version=1, state={"v": 1}
        )
        session.commit()

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def deliver(version: int) -> None:
        try:
            barrier.wait(timeout=5)
            with factory() as session:
                RevisionProjector.for_context(session, _ctx(A)).apply(
                    aggregate_type=TYPE, aggregate_id=AID, version=version, state={"v": version}
                )
                session.commit()
        except BaseException as exc:  # noqa: BLE001 — surface any thread failure to the test
            errors.append(exc)

    threads = [threading.Thread(target=deliver, args=(v,)) for v in (2, 3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    with factory() as check:
        proj = check.scalars(select(RevisionProjection)).one()
        assert proj.applied_version == 3  # no lost update — both revisions landed, in order
        assert proj.state == {"v": 3}  # the newer revision won; neither was overwritten by an older
        assert proj.status == RevisionStatus.CURRENT.value
        assert check.scalars(select(RevisionPendingEvent)).all() == []  # fully reconciled
