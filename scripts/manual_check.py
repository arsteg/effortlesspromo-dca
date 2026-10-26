"""DCA-010 — developer-controlled manual-check runner.

Run one story's narrow, inspectable scenario against **local synthetic resources only** so a human
can eyeball real state (rows, decisions) rather than a bare PASS/FAIL. It is NOT a test runner and
it NEVER marks human approval.

Guarantees (DCA-010 acceptance):
- **Local synthetic by default**: an ephemeral SQLite file per run (or an explicit ``--database-url``
  you supply). AI is always left disabled, so no model/**paid** action can occur.
- **Refuses production**: a production environment is rejected unless you pass ``--allow-production``
  (an explicit, separate opt-in); even then nothing paid is enabled.
- **Scoped cleanup**: the auto-created synthetic DB is deleted at the end (``--keep`` to retain it).
  A ``--database-url`` you supply is treated as yours and never deleted.
- **Unknown story ids fail safely** with a nonzero exit and the list of known scenarios.

Each future story registers its own scenario via ``@register(...)`` — keep each one narrow and
self-cleaning. Usage:  ``python scripts/manual_check.py DCA-010``  (``--list`` to see scenarios).
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

# Allow ``python scripts/manual_check.py`` from the repo root to import the ``app`` package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.db.models  # noqa: F401 — register tables on Base.metadata so create_all builds the schema
from app.core.config import DeploymentMode, Settings
from app.db.base import Base, make_engine, make_session_factory


@dataclass
class CheckContext:
    """What a scenario is handed: synthetic settings + a session factory over the ephemeral DB."""

    settings: Settings
    engine: object
    session_factory: Callable
    args: argparse.Namespace


@dataclass
class Scenario:
    story_id: str
    summary: str
    run: Callable[[CheckContext], dict]


@dataclass
class RunResult:
    exit_code: int
    message: str | None = None
    scenario_id: str | None = None
    environment: str | None = None
    db_url: str | None = None
    cleaned_up: bool = False
    summary: dict = field(default_factory=dict)


SCENARIOS: dict[str, Scenario] = {}


def register(story_id: str, summary: str) -> Callable:
    """Register a story's manual-check scenario. Later stories append their own; names are unique."""

    def decorator(fn: Callable[[CheckContext], dict]) -> Callable[[CheckContext], dict]:
        SCENARIOS[story_id] = Scenario(story_id=story_id, summary=summary, run=fn)
        return fn

    return decorator


# --------------------------------------------------------------------------------------------------
# Registered scenarios
# --------------------------------------------------------------------------------------------------


@register("DCA-010", "self-check: seed synthetic Businesses A/B into a disposable DB and list them")
def _dca_010(ctx: CheckContext) -> dict:
    """Prove the harness end-to-end: seed the two synthetic tenants, then show the actual rows."""
    from sqlalchemy import select

    from app.db.models import IcpProfile, TenantAgent
    from app.db.seed import seed_test_businesses

    with ctx.session_factory() as session:
        seeded = seed_test_businesses(session, ctx.settings)

    with ctx.session_factory() as session:
        tenants = session.scalars(select(TenantAgent)).all()
        icps = session.scalars(select(IcpProfile)).all()
        rows = [
            {"businessId": t.business_id, "country": t.country, "enabled": t.enabled}
            for t in tenants
        ]

    print("\nseeded businesses:")
    for row in rows:
        print(f"  - {row['businessId']}  country={row['country']}  enabled={row['enabled']}")
    print(f"\nrow counts: tenant_agents={len(tenants)}  icp_profiles={len(icps)}")
    return {"seeded": seeded, "tenant_rows": rows, "icp_count": len(icps)}


# --------------------------------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------------------------------


def _build_settings(environment: str, database_url: str) -> Settings:
    # Synthetic, local-only profile. AI base URL is deliberately NEVER set => no paid/model calls.
    # ``_env_file=None`` ignores any ambient .env so the profile is fully explicit (pydantic-settings
    # accepts it at runtime; the pydantic mypy plugin only knows model fields, hence the ignore).
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment=environment,
        deployment_mode=DeploymentMode.FAKE,
        database_url=database_url,
        service_auth_token="manual-check-local",
        synthetic_sources_enabled=True,
        worker_enabled=False,
        auto_create_schema=True,
    )


def _print_header(scenario: Scenario, settings: Settings, db_url: str, ephemeral: bool) -> None:
    kind = "postgresql" if db_url.startswith("postgresql") else "sqlite"
    print(f"manual-check :: {scenario.story_id}")
    print(f"  scenario     : {scenario.summary}")
    print(f"  environment  : {settings.environment}")
    print(f"  deployment   : {settings.deployment_mode.value}")
    print(f"  datastore    : {kind} ({'ephemeral synthetic' if ephemeral else 'caller-supplied'})")
    print(f"  ai / paid    : disabled (ai_enabled={settings.ai_enabled})")
    print(f"  synthetic src: {settings.synthetic_sources_enabled}")


def execute(args: argparse.Namespace) -> RunResult:
    """Run one scenario end-to-end. Pure enough to unit-test: returns a RunResult, prints state."""
    story_id = args.story_id
    if story_id not in SCENARIOS:
        known = ", ".join(sorted(SCENARIOS)) or "(none registered)"
        return RunResult(
            exit_code=2,
            message=f"unknown story id '{story_id}'. known scenarios: {known}",
        )
    scenario = SCENARIOS[story_id]

    environment = args.environment
    is_production = (environment or "").strip().lower() == "production"
    if is_production and not args.allow_production:
        return RunResult(
            exit_code=3,
            scenario_id=story_id,
            environment=environment,
            message=(
                "refused: manual-check will not run against a production environment; pass "
                "--allow-production to override (no paid/model action is ever enabled regardless)"
            ),
        )

    # Allocate the synthetic datastore only after the guards pass, so a refusal creates nothing.
    ephemeral = not args.database_url
    tmp_path: Path | None = None
    if ephemeral:
        tmp_path = Path(tempfile.gettempdir()) / f"dca-manual-{story_id}-{uuid.uuid4().hex[:8]}.db"
        db_url = f"sqlite+pysqlite:///{tmp_path.as_posix()}"
    else:
        db_url = args.database_url

    settings = _build_settings(environment, db_url)
    # Defence in depth: the synthetic profile never sets an AI base URL, so this holds — but make the
    # "no paid action" guarantee explicit rather than implicit.
    if settings.ai_enabled:
        return RunResult(
            exit_code=3,
            scenario_id=story_id,
            environment=environment,
            db_url=db_url,
            message="refused: manual-check performs no paid/model actions (AI must stay disabled)",
        )

    _print_header(scenario, settings, db_url, ephemeral)

    engine = make_engine(db_url)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    ctx = CheckContext(settings=settings, engine=engine, session_factory=factory, args=args)

    cleaned_up = False
    summary: dict = {}
    error: str | None = None
    try:
        summary = scenario.run(ctx)
    except Exception as exc:  # noqa: BLE001 — harness deliberately reports ANY scenario failure cleanly
        # A scenario may refuse on its own (e.g. the seed is dev-only even if the harness guard was
        # overridden with --allow-production). Report it cleanly, no traceback, still clean up.
        error = f"scenario '{story_id}' failed: {exc}"
    finally:
        engine.dispose()
        if ephemeral and tmp_path is not None and tmp_path.exists():
            if args.keep:
                print(f"\nkept synthetic db for inspection: {tmp_path}")
            else:
                tmp_path.unlink()
                cleaned_up = True
                print(f"\ncleaned up synthetic db: {tmp_path}")

    if error is not None:
        return RunResult(
            exit_code=1,
            scenario_id=story_id,
            environment=environment,
            db_url=db_url,
            cleaned_up=cleaned_up,
            message=error,
        )

    print("\n(manual-check only - this does NOT mark human approval)")
    return RunResult(
        exit_code=0,
        scenario_id=story_id,
        environment=environment,
        db_url=db_url,
        cleaned_up=cleaned_up,
        summary=summary,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manual_check.py",
        description="DCA developer-controlled manual-check runner (local synthetic only).",
    )
    parser.add_argument("story_id", nargs="?", help="story id to run, e.g. DCA-010")
    parser.add_argument("--list", action="store_true", help="list registered scenarios and exit")
    parser.add_argument(
        "--environment",
        default="development",
        help="synthetic environment label (default: development)",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="use this datastore instead of an ephemeral synthetic SQLite file (never deleted)",
    )
    parser.add_argument(
        "--keep", action="store_true", help="keep the ephemeral synthetic DB instead of deleting it"
    )
    parser.add_argument(
        "--allow-production",
        action="store_true",
        help="explicit opt-in to run against a production environment (still no paid actions)",
    )
    return parser


def run(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.list or not args.story_id:
        print("registered manual-check scenarios:")
        for sid in sorted(SCENARIOS):
            print(f"  {sid}  - {SCENARIOS[sid].summary}")
        # Listing is a success; a bare invocation with no story id is a usage error.
        return 0 if args.list else 2

    result = execute(args)
    if result.message:
        print(result.message, file=sys.stderr)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(run())
