"""DCA-080 — apply platform suppression events into the scoped local projection.

Two layers, matching the DCA-031 revision suite:

- **Always-on unit tests** (in-memory SQLite) pin the decision logic over the inbox + revision guards:
  a verified platform event blocks/clears a ``(contact_ref, channel)`` in the tenant's projection; a
  **stale** event cannot clear a newer block (the headline); an exact re-delivery is de-duplicated by
  the inbox guard; an out-of-order successor is buffered as a visible gap; and — the authority
  acceptance — a **cross-tenant** event and a **tenant-plane** event are each DENIED with no state
  change (a tenant event cannot widen its own authority).
- **Locking / transactional behaviour on a disposable PostgreSQL** (``DCA_TEST_DATABASE_URL``) proves
  the stale-cannot-clear-a-newer-block guarantee holds on real Postgres with real transactions (and
  that the inbox receipt + revision change commit atomically). SQLite cannot prove row-level locking,
  so that layer skips without a Postgres URL.

Spin up a throwaway Postgres, e.g.:
    docker run --rm -d -e POSTGRES_PASSWORD=dca -e POSTGRES_DB=dca -p 55477:5432 postgres:16-alpine
    export DCA_TEST_DATABASE_URL="postgresql+psycopg://postgres:dca@localhost:55477/dca"
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker

from app.db.base import Base, make_engine, make_session_factory
from app.db.models import InboxReceipt, RevisionProjection
from app.identity.tenant_context import TenantContext
from app.integration.envelope import new_envelope
from app.suppression import (
    SUPPRESSION_AGGREGATE_TYPE,
    SUPPRESSION_EVENT_TYPE,
    PlatformSuppressionProjector,
    SuppressionState,
    SuppressionSyncDecision,
)

A = "00000000-0000-4000-8000-00000000000a"
B = "00000000-0000-4000-8000-00000000000b"
SUBJECT = "00000000-0000-4000-9000-0000000a11ce"

# Opaque host-supplied handles only — a contact_ref is a hashed/tokenized reference, never a raw
# contact point (the envelope + projection screens would refuse an email/phone).
CONTACT_REF = "cref-7f3a9c21"
CHANNEL = "email"
OCCURRED = datetime(2026, 6, 1, tzinfo=UTC)


def _ctx(business_id: str) -> TenantContext:
    return TenantContext(
        subject=SUBJECT, business_id=business_id, roles=("owner",), membership_version="v1"
    )


def _event(
    *,
    business_id: str = A,
    version: int,
    state: str = SuppressionState.BLOCKED.value,
    plane: str = "platform",
    event_type: str = SUPPRESSION_EVENT_TYPE,
    contact_ref: str = CONTACT_REF,
    channel: str = CHANNEL,
    event_id: str | None = None,
    reason: str = "unsubscribe",
):
    """Mint a VERIFIED-shaped suppression envelope (DCA-029). In production this is produced only
    after DCA-030 transport auth; the test mints it directly to exercise the consumer."""
    payload = {"authority_plane": plane, "state": state, "channel": channel, "reason": reason}
    if contact_ref:
        payload["contact_ref"] = contact_ref
    return new_envelope(
        event_id=event_id or f"supp-{business_id[-1]}-{version}-{state}",
        business_id=business_id,
        event_type=event_type,
        aggregate_version=version,
        occurred_at=OCCURRED,
        payload=payload,
    )


# --------------------------------------------------------------------------------------------------
# Always-on unit tests (in-memory SQLite) — the decision logic over inbox + revision guards
# --------------------------------------------------------------------------------------------------


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def _apply(factory, business_id, envelope):
    """Consume one event in its own committed unit of work; returns the outcome."""
    with factory() as session:
        outcome = PlatformSuppressionProjector.for_context(session, _ctx(business_id)).apply(
            envelope
        )
        session.commit()
    return outcome


def _state(factory, business_id, *, contact_ref=CONTACT_REF, channel=CHANNEL):
    with factory() as session:
        return PlatformSuppressionProjector.for_context(session, _ctx(business_id)).current(
            contact_ref=contact_ref, channel=channel
        )


def test_first_platform_event_blocks_the_contact_point(session_factory):
    f = session_factory
    out = _apply(f, A, _event(version=1, state="blocked"))
    assert out.decision is SuppressionSyncDecision.APPLIED
    assert out.applied and out.state is SuppressionState.BLOCKED
    assert _state(f, A) is SuppressionState.BLOCKED


def test_contiguous_events_advance_block_then_clear(session_factory):
    f = session_factory
    _apply(f, A, _event(version=1, state="blocked"))
    out = _apply(f, A, _event(version=2, state="cleared"))
    assert out.applied and out.state is SuppressionState.CLEARED
    assert _state(f, A) is SuppressionState.CLEARED  # the host cleared it in order


def test_stale_event_cannot_clear_a_newer_block(session_factory):
    """The headline acceptance: block at revision 2, then a delayed clear at revision 1 — the newer
    block is preserved, the stale clear changes nothing."""
    f = session_factory
    _apply(f, A, _event(version=1, state="blocked"))
    _apply(f, A, _event(version=2, state="blocked", reason="complaint"))

    out = _apply(f, A, _event(version=1, state="cleared"))  # a delayed older revision
    assert out.decision is SuppressionSyncDecision.STALE
    assert out.stale and not out.changed_state
    assert out.state is None and out.applied_version == 2
    assert (
        _state(f, A) is SuppressionState.BLOCKED
    )  # STILL blocked — not cleared by the stale event


def test_duplicate_event_is_deduplicated_by_the_inbox_guard(session_factory):
    f = session_factory
    ev = _event(version=1, state="blocked", event_id="supp-fixed-1")
    first = _apply(f, A, ev)
    assert first.applied
    # Re-deliver the exact same event id (e.g. an at-least-once relay) -> inbox guard no-ops.
    again = _apply(f, A, _event(version=1, state="cleared", event_id="supp-fixed-1"))
    assert again.decision is SuppressionSyncDecision.DUPLICATE
    assert not again.changed_state
    assert _state(f, A) is SuppressionState.BLOCKED  # unchanged by the replay
    with f() as session:
        receipts = session.scalars(select(InboxReceipt)).all()
        assert len(receipts) == 1  # exactly one receipt for the event id


def test_event_ahead_of_a_missing_predecessor_is_a_visible_gap(session_factory):
    f = session_factory
    _apply(f, A, _event(version=1, state="blocked"))
    out = _apply(f, A, _event(version=3, state="cleared"))  # revision 2 missing
    assert out.decision is SuppressionSyncDecision.GAP
    assert not out.changed_state
    assert _state(f, A) is SuppressionState.BLOCKED  # not applied over the gap


def test_cross_tenant_event_is_denied_without_state_change(session_factory):
    """A tenant event cannot widen its authority to another tenant: an event scoped to B applied under
    A's context is DENIED and stages nothing."""
    f = session_factory
    session = f()
    try:
        projector = PlatformSuppressionProjector.for_context(session, _ctx(A))
        out = projector.apply(_event(business_id=B, version=1, state="blocked"))
        assert out.denied and out.reason == "scope_mismatch"
        assert list(session.new) == []  # refused before any staging
        session.commit()
    finally:
        session.close()
    assert _state(f, A) is None  # nothing projected for A
    assert _state(f, B) is None  # and nothing for B either


def test_tenant_plane_event_cannot_assert_suppression_authority(session_factory):
    """A tenant event cannot widen its own authority: suppression is host-owned, so an event carrying
    a BUSINESS authority plane is DENIED and changes nothing."""
    f = session_factory
    session = f()
    try:
        projector = PlatformSuppressionProjector.for_context(session, _ctx(A))
        out = projector.apply(_event(version=1, state="blocked", plane="business"))
        assert out.denied and out.reason == "authority_not_platform"
        assert list(session.new) == []  # refused before any staging
        session.commit()
    finally:
        session.close()
    assert _state(f, A) is None


def test_tenant_plane_event_cannot_clear_an_existing_platform_block(session_factory):
    """Belt-and-braces: a tenant-plane 'clear' can never undo a platform block already applied."""
    f = session_factory
    _apply(f, A, _event(version=1, state="blocked"))  # platform block stands
    out = _apply(f, A, _event(version=2, state="cleared", plane="business"))
    assert out.denied and out.reason == "authority_not_platform"
    assert _state(f, A) is SuppressionState.BLOCKED  # still blocked — the tenant could not clear it


def test_unrecognized_event_type_is_denied(session_factory):
    f = session_factory
    out = _apply(f, A, _event(version=1, event_type="candidate.accepted"))
    assert out.denied and out.reason == "unrecognized_event_type"
    assert _state(f, A) is None


@pytest.mark.parametrize(
    "kwargs,expected_blank",
    [
        ({"contact_ref": ""}, "contact_ref"),
        ({"channel": ""}, "channel"),
        ({"state": "paused"}, "state"),
    ],
)
def test_malformed_suppression_payload_is_denied(session_factory, kwargs, expected_blank):
    f = session_factory
    out = _apply(f, A, _event(version=1, **kwargs))
    assert out.denied and out.reason == "malformed_suppression_payload"
    assert _state(f, A) is None


def test_projections_are_tenant_isolated(session_factory):
    f = session_factory
    _apply(f, A, _event(business_id=A, version=1, state="blocked"))
    _apply(f, B, _event(business_id=B, version=1, state="cleared"))
    assert _state(f, A) is SuppressionState.BLOCKED
    assert _state(f, B) is SuppressionState.CLEARED
    # The aggregate for A and B share a contact_ref+channel but live in separate tenant projections.
    with f() as session:
        rows = session.scalars(
            select(RevisionProjection).where(
                RevisionProjection.aggregate_type == SUPPRESSION_AGGREGATE_TYPE
            )
        ).all()
        assert {r.business_id for r in rows} == {A, B}


def test_apply_stages_without_committing(session_factory):
    f = session_factory
    session = f()
    try:
        out = PlatformSuppressionProjector.for_context(session, _ctx(A)).apply(
            _event(version=1, state="blocked")
        )
        assert out.applied
        # Not persisted until the caller commits: a second session sees nothing.
        with f() as other:
            assert other.scalars(select(RevisionProjection)).all() == []
            assert other.scalars(select(InboxReceipt)).all() == []
        session.commit()
    finally:
        session.close()
    with f() as check:
        assert len(check.scalars(select(RevisionProjection)).all()) == 1
        assert (
            len(check.scalars(select(InboxReceipt)).all()) == 1
        )  # atomic with the revision change


def test_apply_requires_a_scope(session_factory):
    f = session_factory
    with f() as session:
        with pytest.raises(ValueError):
            PlatformSuppressionProjector(session).apply(_event(version=1))


# --------------------------------------------------------------------------------------------------
# Locking / transactional behaviour on a disposable PostgreSQL
# --------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("DCA_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not PG_URL, reason="set DCA_TEST_DATABASE_URL to a disposable Postgres"
)
SCHEMA = "dca_suppression_sync_test"


@pytest.fixture
def pg_factory():
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
def test_stale_event_cannot_clear_a_newer_block_on_real_postgres(pg_factory):
    """Block at revision 2 then a delayed clear at revision 1 on real Postgres — still blocked."""
    factory = pg_factory
    for v, s in ((1, "blocked"), (2, "blocked")):
        with factory() as session:
            PlatformSuppressionProjector.for_context(session, _ctx(A)).apply(
                _event(version=v, state=s)
            )
            session.commit()

    with factory() as session:
        out = PlatformSuppressionProjector.for_context(session, _ctx(A)).apply(
            _event(version=1, state="cleared")
        )
        session.commit()
    assert out.stale

    with factory() as check:
        proj = check.scalars(select(RevisionProjection)).one()
        assert proj.applied_version == 2
        assert proj.state["state"] == SuppressionState.BLOCKED.value


@requires_pg
def test_denied_cross_tenant_event_persists_nothing_on_real_postgres(pg_factory):
    """A cross-tenant event is refused and leaves no revision row and no inbox receipt behind."""
    factory = pg_factory
    with factory() as session:
        out = PlatformSuppressionProjector.for_context(session, _ctx(A)).apply(
            _event(business_id=B, version=1, state="blocked")
        )
        assert out.denied
        session.commit()
    with factory() as check:
        assert check.scalars(select(RevisionProjection)).all() == []
        assert check.scalars(select(InboxReceipt)).all() == []
