"""DCA-045 — version field and operation rights (source terms).

Service unit tests (in-memory SQLite) pin the store logic and the acceptance behaviour:

- rights can differ per field and per use: within ONE terms version, a display-only field permits
  ``display`` but denies ``export``, while an exportable field permits both — their rights values are
  independent, driven by per-field overrides over source-wide defaults (deny-by-default);
- version and expiry are EXPLICIT: versions are insert-only (a prior version is preserved, never
  overwritten) and an expired / not-yet-effective version grants nothing, with a stable denial code —
  not a single legal boolean;
- invalid terms (an uncatalogued source, an unknown operation, missing contract evidence / purpose,
  bad retention, an incoherent validity window) are refused before anything is staged, so a rejection
  leaves no row;
- a recorded terms version is never active on its own (activation is a disabled future capability).

``source_terms`` is platform control-plane data (not tenant-scoped), so the behaviour is app-level; the
transaction/migration paths for the table are covered by ``tests/test_migrations.py`` (disposable
Postgres) and its exclusion from RLS by ``tests/test_rls.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.db.base import Base, make_engine, make_session_factory
from app.db.models import SourceTerms, SourceTermsStatus
from app.services.source_catalog import SourceCatalogStore, SourceDraftInput
from app.services.source_terms import (
    REASON_INVALID_EXPIRY,
    REASON_INVALID_RETENTION,
    REASON_MISSING_CONTRACT_EVIDENCE,
    REASON_MISSING_PURPOSE,
    REASON_OPERATION_NOT_PERMITTED,
    REASON_SOURCE_NOT_FOUND,
    REASON_TERMS_EXPIRED,
    REASON_TERMS_NOT_YET_EFFECTIVE,
    REASON_UNKNOWN_OPERATION,
    SourceTermsInput,
    SourceTermsStore,
    SourceTermsValidationError,
)

SOURCE_KEY = "mca_master_data"


@pytest.fixture
def session_factory():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        engine.dispose()


def _catalogue_source(session, key: str = SOURCE_KEY) -> None:
    """Stage a DCA-044 source draft so a terms version has a catalogued source to attach to."""
    SourceCatalogStore(session).create_draft(
        SourceDraftInput(
            key=key,
            kind="official",
            access_method="bulk_download",
            india_coverage={"nationwide": True},
        )
    )


def _terms(**overrides) -> SourceTermsInput:
    """A valid terms version: display granted source-wide, export source-wide too, but 'email' is
    overridden to display-only; 'share' is denied source-wide."""
    data = {
        "source_key": SOURCE_KEY,
        "operations": {
            "collect": True,
            "store": True,
            "derive": True,
            "display": True,
            "export": True,
            "share": False,
            "model_processing": True,
        },
        "field_rights": {
            "email": {"display": True, "export": False},  # display-only field
            "company_name": {"display": True, "export": True},  # exportable field
        },
        "purposes": ["prospecting"],
        "attribution": {"required": True, "notice": "Source: MCA"},
        "retention": {"max_days": 365, "policy": "delete after 1 year"},
        "contract_evidence": {"reference": "MCA-TOS-2026", "kind": "terms_of_use"},
    }
    data.update(overrides)
    return SourceTermsInput(**data)


# -- rights can differ per field and per use ------------------------------------------------------


def test_rights_differ_per_field_and_per_use(session_factory):
    with session_factory() as session:
        _catalogue_source(session)
        terms = SourceTermsStore(session).add_version(_terms())
        session.commit()
        terms_id = terms.id

    with session_factory() as session:
        terms = session.get(SourceTerms, terms_id)
        assert terms is not None
        store = SourceTermsStore(session)

        # Display-only field: display allowed, export denied — the two uses differ for one field.
        email_display = store.permits(terms, "email", "display")
        email_export = store.permits(terms, "email", "export")
        assert email_display.allowed is True
        assert email_export.allowed is False
        assert email_export.reason == REASON_OPERATION_NOT_PERMITTED
        assert email_export.source == "field_override"

        # Exportable field: both allowed — rights differ per FIELD for the same 'export' use.
        assert store.permits(terms, "company_name", "export").allowed is True
        assert store.permits(terms, "company_name", "display").allowed is True

        # A field with no override inherits the source-wide default (deny-by-default for 'share').
        inherited = store.permits(terms, "phone", "display")
        assert inherited.allowed is True
        assert inherited.source == "operation_default"
        assert store.permits(terms, "phone", "share").allowed is False


def test_operation_rights_are_deny_by_default(session_factory):
    # An operation the caller never mentions is stored explicitly as not-granted (no silent gap).
    with session_factory() as session:
        _catalogue_source(session)
        terms = SourceTermsStore(session).add_version(
            _terms(operations={"display": True})  # only 'display' mentioned
        )
        assert terms.operation_rights["display"] is True
        assert terms.operation_rights["export"] is False
        assert terms.operation_rights["model_processing"] is False
        # All seven operations are represented explicitly.
        assert set(terms.operation_rights) == {
            "collect",
            "store",
            "derive",
            "display",
            "export",
            "share",
            "model_processing",
        }


# -- version and expiry are explicit --------------------------------------------------------------


def test_versions_are_insert_only_and_preserved(session_factory):
    with session_factory() as session:
        _catalogue_source(session)
        store = SourceTermsStore(session)
        v1 = store.add_version(_terms())
        v2 = store.add_version(_terms(field_rights={"email": {"display": True, "export": True}}))
        session.commit()
        assert v1.version == 1
        assert v2.version == 2

    with session_factory() as session:
        store = SourceTermsStore(session)
        versions = store.versions(SOURCE_KEY)
        assert [t.version for t in versions] == [1, 2]
        # v1 is preserved unchanged: email still display-only there.
        assert store.permits(versions[0], "email", "export").allowed is False
        # v2 widened email to exportable — a new version, not an overwrite of v1.
        assert store.permits(versions[1], "email", "export").allowed is True
        assert store.latest(SOURCE_KEY).version == 2


def test_expired_version_grants_nothing(session_factory):
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        _catalogue_source(session)
        store = SourceTermsStore(session)
        terms = store.add_version(
            _terms(
                effective_from=now - timedelta(days=30),
                expires_at=now - timedelta(days=1),  # already lapsed at `now`
            )
        )
        session.commit()
        terms_id = terms.id

    with session_factory() as session:
        terms = session.get(SourceTerms, terms_id)
        store = SourceTermsStore(session)
        # A right that IS granted in the terms is still denied because the version has expired —
        # expiry is explicit, not folded into one never-lapsing boolean.
        decision = store.permits(terms, "company_name", "export", now=now)
        assert decision.allowed is False
        assert decision.reason == REASON_TERMS_EXPIRED
        # Before expiry the same right is allowed.
        earlier = now - timedelta(days=10)
        assert store.permits(terms, "company_name", "export", now=earlier).allowed is True


def test_not_yet_effective_version_grants_nothing(session_factory):
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        _catalogue_source(session)
        store = SourceTermsStore(session)
        terms = store.add_version(_terms(effective_from=now + timedelta(days=1)))
        decision = store.permits(terms, "company_name", "export", now=now)
        assert decision.allowed is False
        assert decision.reason == REASON_TERMS_NOT_YET_EFFECTIVE


def test_recorded_version_is_never_active_on_its_own(session_factory):
    with session_factory() as session:
        _catalogue_source(session)
        terms = SourceTermsStore(session).add_version(_terms())
        # A terms version is reviewable but never in force on its own — activation is disabled.
        assert terms.status == SourceTermsStatus.DRAFT.value
        assert SourceTermsStore.is_active(terms) is False


# -- invalid terms are refused before anything is staged ------------------------------------------


def test_terms_for_uncatalogued_source_are_refused(session_factory):
    with session_factory() as session:
        store = SourceTermsStore(session)  # no source catalogued
        with pytest.raises(SourceTermsValidationError) as exc:
            store.add_version(_terms(source_key="does_not_exist"))
        assert exc.value.code == REASON_SOURCE_NOT_FOUND
        session.commit()

    with session_factory() as session:
        assert SourceTermsStore(session).versions("does_not_exist") == []


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"operations": {"teleport": True}}, REASON_UNKNOWN_OPERATION),
        ({"field_rights": {"email": {"telepathy": True}}}, REASON_UNKNOWN_OPERATION),
        ({"contract_evidence": {}}, REASON_MISSING_CONTRACT_EVIDENCE),
        ({"contract_evidence": {"kind": "x"}}, REASON_MISSING_CONTRACT_EVIDENCE),  # no reference
        ({"purposes": []}, REASON_MISSING_PURPOSE),
        ({"purposes": ["   "]}, REASON_MISSING_PURPOSE),
        ({"retention": {"max_days": -1}}, REASON_INVALID_RETENTION),
        ({"retention": {"max_days": "soon"}}, REASON_INVALID_RETENTION),
    ],
)
def test_invalid_terms_are_refused_and_stage_nothing(session_factory, overrides, code):
    with session_factory() as session:
        _catalogue_source(session)
        session.commit()

    with session_factory() as session:
        store = SourceTermsStore(session)
        with pytest.raises(SourceTermsValidationError) as exc:
            store.add_version(_terms(**overrides))
        assert exc.value.code == code
        session.commit()  # commit whatever (nothing) the refusal staged

    with session_factory() as session:
        # The rejection left no terms row — validation ran before anything was staged.
        assert SourceTermsStore(session).versions(SOURCE_KEY) == []


def test_incoherent_validity_window_is_refused(session_factory):
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        _catalogue_source(session)
        store = SourceTermsStore(session)
        with pytest.raises(SourceTermsValidationError) as exc:
            store.add_version(
                _terms(effective_from=now, expires_at=now)  # expiry not after effective
            )
        assert exc.value.code == REASON_INVALID_EXPIRY
        session.commit()

    with session_factory() as session:
        assert SourceTermsStore(session).versions(SOURCE_KEY) == []


def test_permits_rejects_an_unknown_operation_string(session_factory):
    with session_factory() as session:
        _catalogue_source(session)
        terms = SourceTermsStore(session).add_version(_terms())
        with pytest.raises(SourceTermsValidationError) as exc:
            SourceTermsStore.permits(terms, "email", "teleport")
        assert exc.value.code == REASON_UNKNOWN_OPERATION
