"""DCA-010 — developer-controlled manual-check runner.

These exercise the harness behaviour (scenario dispatch, safe failure on unknown ids, production
refusal, local-synthetic defaults, scoped cleanup). The runner's default datastore is local
synthetic SQLite — appropriate here because the behaviour under test is CLI/dispatch logic, not
locking/RLS/transaction semantics. A scenario can still be pointed at a disposable Postgres via
``--database-url`` for manual runs.
"""

from __future__ import annotations

from pathlib import Path

from scripts.manual_check import SCENARIOS, execute, run


class _Namespace:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _args(**overrides) -> _Namespace:
    base = {
        "story_id": "DCA-010",
        "list": False,
        "environment": "development",
        "database_url": None,
        "keep": False,
        "allow_production": False,
    }
    base.update(overrides)
    return _Namespace(**base)


def test_dca010_is_registered():
    assert "DCA-010" in SCENARIOS


def test_unknown_story_id_fails_safely(capsys):
    result = execute(_args(story_id="DCA-999"))
    assert result.exit_code == 2
    assert "unknown story id" in (result.message or "")
    # No datastore allocated, no summary — a safe no-op.
    assert result.db_url is None and result.summary == {}


def test_dca010_runs_on_local_synthetic_and_cleans_up(capsys):
    result = execute(_args())
    assert result.exit_code == 0
    assert result.environment == "development"
    assert result.db_url.startswith("sqlite")
    # Seeded the two synthetic businesses and showed real rows.
    assert len(result.summary["tenant_rows"]) == 2
    # Ephemeral DB was deleted (cleanup scoped to the run).
    assert result.cleaned_up is True
    db_file = Path(result.db_url.split("///", 1)[1])
    assert not db_file.exists()
    out = capsys.readouterr().out
    assert "does NOT mark human approval" in out


def test_keep_retains_the_synthetic_db(capsys):
    result = execute(_args(keep=True))
    assert result.exit_code == 0 and result.cleaned_up is False
    db_file = Path(result.db_url.split("///", 1)[1])
    assert db_file.exists()
    db_file.unlink()  # test-local cleanup of the retained file


def test_production_is_refused_without_explicit_opt_in(capsys):
    result = execute(_args(environment="production"))
    assert result.exit_code == 3
    assert "production" in (result.message or "")
    # Refused before any datastore was allocated — no state, no file.
    assert result.db_url is None


def test_production_allowed_with_explicit_flag(capsys):
    # --allow-production lifts the HARNESS guard, but the DCA-010 scenario still refuses to seed in
    # production (layered protection). Harness proceeds; scenario fails cleanly; synthetic db cleaned.
    result = execute(_args(environment="production", allow_production=True))
    assert result.exit_code == 1
    assert "development-only" in (result.message or "")
    assert result.cleaned_up is True


def test_dca038_scenario_runs_and_leaves_state_unchanged(capsys):
    # The DCA-038 targeting validator is stateless; the scenario runs on local synthetic SQLite and
    # proves the headline denials while touching no rows.
    result = execute(_args(story_id="DCA-038"))
    assert result.exit_code == 0
    assert result.summary["_state_unchanged"] is True
    assert result.summary["1 Indian recipient (PIN 560001)"]["allowed"] is True
    assert result.summary["2 +91 contact, NO location evidence"]["allowed"] is False
    assert result.cleaned_up is True


def test_dca039_scenario_runs_and_proves_versioning_and_denials(capsys):
    # The DCA-039 ICP-draft scenario runs on local synthetic SQLite: it proves per-business versioning,
    # the invalid-band/unsupported-field/geography/profile-link denials (each 422, no state change),
    # and tenant isolation (B cannot read A's version).
    result = execute(_args(story_id="DCA-039"))
    assert result.exit_code == 0
    assert result.summary["a_versions"] == [1, 2]
    assert result.summary["denials"]["invalid_band"]["status"] == 422
    assert result.summary["denials"]["versions_unchanged"] is True
    assert result.summary["cross_tenant"]["b_get_a_status"] == 404
    assert result.summary["cross_tenant"]["b_list"] == []
    assert result.cleaned_up is True


def test_list_and_bare_invocation_exit_codes(capsys):
    assert run(["--list"]) == 0  # listing is a success
    out = capsys.readouterr().out
    assert "DCA-010" in out
    assert run([]) == 2  # no story id is a usage error
