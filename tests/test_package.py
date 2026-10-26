"""DCA-003 — the installable package: imports stand alone, no provider SDKs.

The import package is ``app`` (same layout convention as effortlesspromo-ai); the DISTRIBUTION is
``effortlesspromo-dca`` (pyproject [project].name). Tests avoid importing ``app.asgi`` — that
module builds from ambient env/.env as a real server entrypoint would (creates the dev SQLite
file); tests construct apps from explicit Settings instead (conftest.make_settings).
"""

import sys

import pytest

from tests.conftest import make_settings


def test_package_imports_and_builds_independently():
    from app.main import create_app

    application = create_app(make_settings())
    assert application.title == "EffortlessPromo Data Concierge Agent"


def test_denial_no_provider_packages_loaded():
    # Importing the whole DCA surface must not pull in any model-provider SDK — the DCA boundary
    # forbids them; all model execution goes through the AI service over HTTP.
    import app.cli
    import app.services.discovery
    import app.workers.scheduler

    assert all(m is not None for m in (app.cli, app.services.discovery, app.workers.scheduler))
    for forbidden in ("anthropic", "openai"):
        assert forbidden not in sys.modules, f"forbidden package imported: {forbidden}"


def test_denial_provider_sdks_not_installed():
    with pytest.raises(ImportError):
        import anthropic  # noqa: F401
    with pytest.raises(ImportError):
        import openai  # noqa: F401


def test_denial_ai_service_application_package_not_importable():
    # Acceptance: DCA imports standalone WITHOUT importing the AI service application package.
    # Both services use the top-level import name ``app`` (shared convention), so the guarantee is
    # that *this* ``app`` is DCA's own and none of effortlesspromo-ai's distinct gateway modules
    # resolve in a DCA-only environment. find_spec raises ModuleNotFoundError for a missing
    # submodule of an existing package — treat that, or a None spec, as "absent".
    import importlib.util

    def absent(module: str) -> bool:
        try:
            return importlib.util.find_spec(module) is None
        except ModuleNotFoundError:
            return True

    for ai_module in (
        "app.gateway",
        "app.gateway.gateway",
        "app.gateway.registry",
        "app.gateway.envelope",
    ):
        assert absent(ai_module), f"AI-service module leaked into DCA env: {ai_module}"


def test_console_entry_point_is_declared_and_importable():
    from app.cli import main

    assert callable(main)  # actually running it would bind a port; declaration test only
