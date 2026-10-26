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


@register("DCA-011", "standardized error envelope: validation, deliberate 404/409, and a safe 500")
def _dca_011(ctx: CheckContext) -> dict:
    """Show the actual error bodies for each class of failure, side by side.

    Spins up an HTTP client over this run's synthetic settings, mounts a throwaway route that raises
    an unexpected error (dev-only, never shipped), and prints the real envelopes so a human can see:
    same shape, stable code, correlation id, and — on the 500 — no leaked exception/stack text.
    """
    from fastapi import APIRouter
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(ctx.settings)
    boom = APIRouter()

    @boom.get("/_debug/boom")
    def _boom() -> dict:
        raise RuntimeError("secret=TOPSECRET email=victim@example.com")

    app.include_router(boom)
    auth = {"X-Service-Auth": ctx.settings.service_auth_token or ""}
    client = TestClient(app, raise_server_exceptions=False)

    try:
        cases = {
            "validation_422": client.post("/v1/tenants", json={"country": "leak-me"}, headers=auth),
            "not_found_404": client.post("/v1/tenants/biz-x/enable", headers=auth),
            "unauthorized_401": client.post("/v1/tenants", json={"businessId": "b"}),
            "unexpected_500": client.get("/_debug/boom", headers={"X-Correlation-Id": "trace-500"}),
        }
    finally:
        # Release this app's own DB connection so the harness can unlink the ephemeral file (Windows).
        app.state.engine.dispose()

    out: dict = {}
    for name, resp in cases.items():
        body = resp.json()
        print(f"\n{name}  (HTTP {resp.status_code})")
        print(f"  body            : {body}")
        print(f"  X-Correlation-Id: {resp.headers.get('X-Correlation-Id')}")
        out[name] = {"status": resp.status_code, "body": body}

    leaked = [
        t
        for t in ("TOPSECRET", "victim@example.com", "Traceback")
        if t in cases["unexpected_500"].text
    ]
    print(
        f"\n500 leak check    : {'LEAKED ' + str(leaked) if leaked else 'clean (no stack/PII in body)'}"
    )
    print(
        f"422 value leak    : {'LEAKED' if 'leak-me' in cases['validation_422'].text else 'clean (submitted value not echoed)'}"
    )
    out["leak_check"] = {
        "server_error_leaks": leaked,
        "validation_echoes_value": "leak-me" in cases["validation_422"].text,
    }
    return out


@register("DCA-012", "structured correlation logging redacts sentinel token/email/phone")
def _dca_012(ctx: CheckContext) -> dict:
    """Emit logs carrying sentinel secret/contact values (in the message, in structured extra, and
    inside a request so a correlation id is attached), then show the actual JSON log lines and
    grep them for the exact sentinels — proving none survive the central redaction policy.
    """
    import io
    import json
    import logging

    from fastapi import APIRouter
    from fastapi.testclient import TestClient

    from app.core.logging import configure_logging
    from app.main import create_app

    token = "svc-tok-SENTINEL-abc123"
    email = "a.person@synthetic-lead.example"
    phone = "+91-98765-43210"

    app = create_app(ctx.settings)
    probe = APIRouter()

    @probe.get("/_debug/log")
    def _emit() -> dict:
        logging.getLogger("dca.manualcheck").info(
            "reaching lead at %s / %s", email, phone, extra={"service_auth_token": token}
        )
        return {"ok": True}

    app.include_router(probe)

    # Capture the dca log stream (registering the sentinel token as a secret) into a buffer.
    buffer = io.StringIO()
    configure_logging(
        type(ctx.settings)(
            _env_file=None,  # type: ignore[call-arg]
            environment="development",
            database_url="sqlite+pysqlite:///:memory:",
            service_auth_token=token,
        ),
        stream=buffer,
    )

    try:
        client = TestClient(app)
        client.get("/_debug/log", headers={"X-Correlation-Id": "trace-dca-012"})
        logging.getLogger("dca.manualcheck").warning(
            "out-of-request log also scrubs token=%s", token
        )
    finally:
        app.state.engine.dispose()

    raw = buffer.getvalue()
    lines = [json.loads(line) for line in raw.splitlines() if line.strip()]
    print("\nactual structured log lines:")
    for line in lines:
        print(f"  {json.dumps(line)}")

    survivors = {s: (s in raw) for s in (token, email, phone)}
    print("\nsentinel grep over the generated logs (expect all False):")
    for sentinel, present in survivors.items():
        print(f"  {sentinel!r:42} present={present}")
    correlations = [line.get("correlationId") for line in lines]
    print(f"\ncorrelation ids on the records: {correlations}")

    return {"lines": lines, "sentinels_present": survivors, "correlation_ids": correlations}


@register("DCA-013", "readiness fails on DB loss while liveness stays up; no credentials leaked")
def _dca_013(ctx: CheckContext) -> dict:
    """Show liveness vs readiness side by side, healthy then with the DB swapped out, and confirm a
    password-bearing DSN never appears in the readiness body.
    """
    import os
    import tempfile

    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine

    from app.core.readiness import build_readiness
    from app.main import create_app

    app = create_app(ctx.settings)  # the real ephemeral synthetic DB (reachable)
    original_engine = app.state.engine
    client = TestClient(app)

    def snapshot(label: str) -> dict:
        live = client.get("/health/live")
        ready = client.get("/health/ready")
        print(f"\n[{label}]")
        print(f"  GET /health/live  -> {live.status_code} {live.json()}")
        print(f"  GET /health/ready -> {ready.status_code} {ready.json()}")
        return {
            "live": {"status": live.status_code, "body": live.json()},
            "ready": {"status": ready.status_code, "body": ready.json()},
        }

    try:
        healthy = snapshot("database reachable")

        # Simulate the disposable PostgreSQL being stopped: point the engine at an unopenable path.
        missing = os.path.join(tempfile.gettempdir(), "dca-no-such-dir-xyz", "readiness.db")
        broken = create_engine(f"sqlite:///{missing}")
        app.state.engine = broken
        down = snapshot("database stopped")
    finally:
        # Release every engine (the original holds the ephemeral file open on Windows otherwise).
        original_engine.dispose()
        app.state.engine.dispose()

    # Prove a password-bearing DSN is never echoed: build the report straight from a secret DSN.
    secret_pw = "SuperSecretPw"  # synthetic sentinel, not a real credential
    secret_url = f"postgresql+psycopg://dca_user:{secret_pw}@db.internal:5432/dca"
    secret_settings = type(ctx.settings)(
        _env_file=None,  # type: ignore[call-arg]
        environment="development",
        database_url=secret_url,
    )
    report, _ = build_readiness(secret_settings, create_engine(f"sqlite:///{missing}"))
    blob = str(report)
    leaked = [v for v in (secret_pw, "dca_user", "db.internal", secret_url) if v in blob]
    print(f"\nreadiness report from a secret DSN: {report}")
    print(
        f"credential leak check: "
        f"{'LEAKED ' + str(leaked) if leaked else 'clean (no DSN/user/password in readiness body)'}"
    )

    return {
        "healthy": healthy,
        "db_down": down,
        "liveness_stayed_up": down["live"]["status"] == 200,
        "readiness_failed_on_db_down": down["ready"]["status"] == 503,
        "credential_leaks": leaked,
    }


@register("DCA-014", "identity contract fake: two users, expired & revoked, production refuses it")
def _dca_014(ctx: CheckContext) -> dict:
    """Resolve valid / expired / revoked / unknown identities through the deterministic fake and show
    the distinct responses, then prove production refuses the fake (contract disabled, B1).
    """
    from app.identity import (
        FakeIdentityClient,
        IdentityContractDisabled,
        IdentityRequest,
        build_identity_client,
    )
    from app.identity.fake import BUSINESS_A, BUSINESS_B, USER_ALICE, USER_BOB

    fake = FakeIdentityClient()
    scenarios = {
        "alice@A (active owner)": (USER_ALICE, BUSINESS_A),
        "alice@B (expired)": (USER_ALICE, BUSINESS_B),
        "bob@B (active admin)": (USER_BOB, BUSINESS_B),
        "bob@A (revoked)": (USER_BOB, BUSINESS_A),
        "stranger@A (no membership)": ("stranger", BUSINESS_A),
    }

    rows: dict = {}
    print("\nidentity resolutions through the fake:")
    for label, (subject, business_id) in scenarios.items():
        resp = fake.resolve(IdentityRequest(subject=subject, business_id=business_id))
        print(
            f"  {label:30} status={resp.status.value:8} roles={list(resp.roles)} "
            f"allowed={list(resp.allowed_businesses)} version={resp.version}"
        )
        rows[label] = resp.model_dump(mode="json")

    # Production refusal: the contract is disabled until the live adapter ships.
    prod_settings = type(ctx.settings)(
        _env_file=None,  # type: ignore[call-arg]
        environment="production",
        database_url="postgresql+psycopg://user@db/dca",
        service_auth_token="prod-token",
        synthetic_sources_enabled=False,
        auto_create_schema=False,
    )
    refused = False
    try:
        build_identity_client(prod_settings)
    except IdentityContractDisabled as exc:
        refused = True
        print(f"\nproduction build_identity_client -> refused: {exc}")
    dev_client = build_identity_client(ctx.settings)
    print(f"development build_identity_client -> {type(dev_client).__name__}")

    return {
        "resolutions": rows,
        "production_refused_fake": refused,
        "development_client": type(dev_client).__name__,
    }


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
