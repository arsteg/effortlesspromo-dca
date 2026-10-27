"""DCA-044 — store source catalog drafts.

Service unit tests (in-memory SQLite) pin the store logic and the acceptance behaviour:

- a newly created official OR commercial source is a DISABLED draft with no implicit permissions;
- a draft source cannot be used (the headline denial), with a stable reason code and no silent
  acquisition — the ``usable`` list stays empty while every source is a draft;
- invalid drafts (unknown kind / access method, missing or duplicate key, non-India coverage) are
  refused before anything is staged, so a rejection leaves no row.

``sources`` is platform control-plane data (not tenant-scoped), so the behaviour is app-level; the
transaction/migration paths for the table are covered by ``tests/test_migrations.py`` (disposable
Postgres) and its exclusion from RLS by ``tests/test_rls.py``.
"""

from __future__ import annotations

import pytest

from app.db.base import Base, make_engine, make_session_factory
from app.db.models import Source, SourceStatus
from app.services.source_catalog import (
    REASON_DUPLICATE_KEY,
    REASON_INVALID_COVERAGE,
    REASON_MISSING_KEY,
    REASON_SOURCE_NOT_ENABLED,
    REASON_UNKNOWN_ACCESS_METHOD,
    REASON_UNKNOWN_KIND,
    SourceCatalogStore,
    SourceDraftInput,
    SourceValidationError,
)


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def _official() -> SourceDraftInput:
    return SourceDraftInput(
        key="mca_master_data",
        kind="official",
        access_method="bulk_download",
        name="MCA Company Master Data",
        trust_tier="official_registry",
        capabilities=["org_identifier", "org_name"],
        fields=["cin", "company_name", "state"],
        india_coverage={"nationwide": True},
    )


def _commercial() -> SourceDraftInput:
    return SourceDraftInput(
        key="acme_leads",
        kind="commercial",
        access_method="api",
        name="Acme Leads API",
        trust_tier="licensed_commercial",
        capabilities=["org_name", "role_email"],
        fields=["company", "email"],
        india_coverage={"states": ["MH", "Karnataka"]},
    )


# -- a new source is a disabled draft with no implicit permissions --------------------------------


@pytest.mark.parametrize("make_input", [_official, _commercial], ids=["official", "commercial"])
def test_new_source_is_disabled_draft_with_no_permissions(session_factory, make_input):
    with session_factory() as session:
        store = SourceCatalogStore(session)
        source = store.create_draft(make_input())
        session.commit()
        source_id = source.id

    with session_factory() as session:
        row = session.get(Source, source_id)
        assert row is not None
        # Disabled draft by default...
        assert row.status == SourceStatus.DRAFT.value
        assert row.enabled is False
        # ...and no implicit permissions.
        assert row.permissions == []
        # The declared shape was retained.
        assert row.kind == make_input().kind
        assert row.access_method == make_input().access_method
        assert row.capabilities == make_input().capabilities


def test_caller_cannot_create_an_enabled_source(session_factory):
    """Even if a future caller sets fields it shouldn't, the store forces a disabled, permission-less
    draft — the disabled-by-default guarantee is not negotiable through the input."""
    with session_factory() as session:
        store = SourceCatalogStore(session)
        # SourceDraftInput intentionally has no status/enabled/permissions fields, so the only way to
        # create a source is a draft; prove the stored row reflects that regardless of input.
        source = store.create_draft(_official())
        assert source.status == SourceStatus.DRAFT.value
        assert source.enabled is False
        assert source.permissions == []


# -- a draft source cannot be used (the denial) ---------------------------------------------------


def test_draft_source_cannot_be_used(session_factory):
    with session_factory() as session:
        store = SourceCatalogStore(session)
        store.create_draft(_official())
        store.create_draft(_commercial())
        session.commit()

    with session_factory() as session:
        store = SourceCatalogStore(session)
        sources = store.all()
        assert len(sources) == 2
        # Neither draft is usable, and each refusal carries the stable denial code.
        for source in sources:
            assert store.is_usable(source) is False
            assert store.usability_reason(source) == REASON_SOURCE_NOT_ENABLED
        # The usable set is empty while every catalogued source is a draft — nothing can be acquired.
        assert store.usable() == []


def test_coverage_states_are_normalized_to_codes(session_factory):
    with session_factory() as session:
        store = SourceCatalogStore(session)
        source = store.create_draft(_commercial())
        # "MH" and the full name "Karnataka" both normalize to ISO 3166-2:IN codes.
        assert source.india_coverage == {"nationwide": False, "states": ["MH", "KA"]}


# -- invalid drafts are rejected before anything is staged ----------------------------------------


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda i: setattr(i, "key", "  "), REASON_MISSING_KEY),
        (lambda i: setattr(i, "kind", "government"), REASON_UNKNOWN_KIND),
        (lambda i: setattr(i, "access_method", "telepathy"), REASON_UNKNOWN_ACCESS_METHOD),
        (lambda i: setattr(i, "india_coverage", {"states": ["XX"]}), REASON_INVALID_COVERAGE),
        (lambda i: setattr(i, "india_coverage", {}), REASON_INVALID_COVERAGE),
    ],
)
def test_invalid_draft_is_refused_and_stages_nothing(session_factory, mutate, code):
    with session_factory() as session:
        store = SourceCatalogStore(session)
        bad = _official()
        mutate(bad)
        with pytest.raises(SourceValidationError) as exc:
            store.create_draft(bad)
        assert exc.value.code == code
        session.commit()  # commit whatever (nothing) the refusal staged

    with session_factory() as session:
        # The rejection left no row — validation ran before anything was staged.
        assert SourceCatalogStore(session).all() == []


def test_duplicate_key_is_refused(session_factory):
    with session_factory() as session:
        store = SourceCatalogStore(session)
        store.create_draft(_official())
        session.commit()

    with session_factory() as session:
        store = SourceCatalogStore(session)
        with pytest.raises(SourceValidationError) as exc:
            store.create_draft(_official())  # same key
        assert exc.value.code == REASON_DUPLICATE_KEY
        session.commit()

    with session_factory() as session:
        # Only the original row exists; the duplicate staged nothing.
        assert len(SourceCatalogStore(session).all()) == 1
